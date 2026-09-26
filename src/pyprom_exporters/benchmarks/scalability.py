# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Measure exporter scaling with deterministic simulated device I/O.

Run ``uv run --locked benchmark --help`` for available settings.
Each run writes an HTML report and metrics JSON into its own directory under ``report/``.
No discovery packet, device request, or real credential is used.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import math
import platform
import statistics
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from kasa import Credentials, Device, DeviceType
from kasa.exceptions import AuthenticationError
from prometheus_client import CollectorRegistry, generate_latest

from pyprom_exporters.benchmarks.report import render_html
from pyprom_exporters.exporters.tapo import (
    TapoDiscoveryOptions,
    TapoExporterOptions,
    TapoPowerPlugPrometheusExporter,
    TapoPrometheusOptions,
)
from pyprom_exporters.task_collector import run_tasks_with_retry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping


@dataclass
class Activity:
    """Track simulated I/O and asyncio task counts without a sampler task."""

    latency: float
    active: int = 0
    peak_active: int = 0
    peak_tasks: int = 0
    calls: int = 0

    async def operation(self) -> None:
        """Yield for simulated network latency and track live concurrency."""
        self.active += 1
        self.calls += 1
        if self.active > self.peak_active:
            self.peak_active = self.active
            self.peak_tasks = max(self.peak_tasks, len(asyncio.all_tasks()))
        try:
            await asyncio.sleep(self.latency)
        finally:
            self.active -= 1


@dataclass
class Feature:
    """Provide the numeric feature interface read by the exporter."""

    value: float


@dataclass
class DeviceInfo:
    """Provide firmware and hardware labels without constructing kasa devices."""

    firmware_version: str = "simulated"
    hardware_version: str = "simulated"


@dataclass
class SimulatedDevice:
    """Implement the small kasa device interface consumed by the exporter."""

    host: str
    activity: Activity
    auth_failure: bool = False
    alias: str = "simulated-plug"
    model: str = "simulated"
    device_type: DeviceType = DeviceType.Plug
    device_info: DeviceInfo = field(default_factory=DeviceInfo)
    features: dict[str, Feature] = field(
        default_factory=lambda: {
            "current_consumption": Feature(5.0),
            "voltage": Feature(230.0),
            "current": Feature(0.2),
            "consumption_today": Feature(0.12),
            "consumption_this_month": Feature(4.5),
            "rssi": Feature(-50.0),
        }
    )

    async def update(self) -> None:
        """Simulate an update, optionally failing permanently at authentication.

        Raises
        ------
        AuthenticationError
            If this simulated device is configured to reject authentication.

        """
        await self.activity.operation()
        if self.auth_failure:
            message = "Simulated authentication failure"
            raise AuthenticationError(message)

    async def disconnect(self) -> None:
        """Simulate session teardown without network access."""
        await self.activity.operation()


@dataclass
class Fleet:
    """Create fake devices lazily through the actual configured-host discovery path."""

    activity: Activity
    fail_every: int = 0
    missing_count: int = 0

    async def discover(self, host: str, **_kwargs: object) -> Device:
        """Return a simulated device or time out a configured missing host.

        Returns
        -------
        Device
            A simulated device with the configured latency and failure behavior.

        Raises
        ------
        TimeoutError
            If the requested host belongs to the simulated missing-device set.

        """
        await self.activity.operation()
        index = int(host.removeprefix("device-"))
        if index < self.missing_count:
            message = "Simulated missing host"
            raise TimeoutError(message)
        failing = self.fail_every > 0 and index % self.fail_every == 0
        return cast("Device", SimulatedDevice(host=host, activity=self.activity, auth_failure=failing))


@dataclass
class Timing:
    """Wall and process CPU times in milliseconds."""

    wall_ms: float
    cpu_ms: float


async def timed(operation: Callable[[], Awaitable[object]]) -> Timing:
    """Measure an awaited operation using monotonic wall and process CPU clocks.

    Returns
    -------
    Timing
        Elapsed wall-clock and process CPU times in milliseconds.

    """
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    await operation()
    return Timing((time.perf_counter() - wall_start) * 1000, (time.process_time() - cpu_start) * 1000)


def exporter_for(count: int, concurrency: int) -> TapoPowerPlugPrometheusExporter:
    """Construct an exporter with broadcast discovery and environment credentials disabled.

    Returns
    -------
    TapoPowerPlugPrometheusExporter
        An exporter configured for the requested simulated fleet and I/O limit.

    """
    return TapoPowerPlugPrometheusExporter(
        asyncio.get_running_loop(),
        TapoExporterOptions(
            devices=[f"device-{index}" for index in range(count)],
            max_concurrent_devices=concurrency,
            discovery_options=TapoDiscoveryOptions(
                perform_discovery=False, with_update=False, credentials=Credentials()
            ),
            prometheus_options=TapoPrometheusOptions(refresh_interval=None),
        ),
    )


def scrape(exporter: TapoPowerPlugPrometheusExporter, repeats: int) -> dict[str, float | int]:
    """Measure real Prometheus text serialization from the cached snapshot.

    Returns
    -------
    dict[str, float | int]
        Median serialization timings, encoded payload size, and sample count.

    """
    registry = CollectorRegistry()
    registry.register(exporter)
    wall_times = []
    cpu_times = []
    payload = b""
    for _ in range(repeats):
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        payload = generate_latest(registry)
        wall_times.append((time.perf_counter() - wall_start) * 1000)
        cpu_times.append((time.process_time() - cpu_start) * 1000)
    samples = sum(len(metric.samples) for metric in exporter.collect())
    return {
        "wall_ms": statistics.median(wall_times),
        "cpu_ms": statistics.median(cpu_times),
        "bytes": len(payload),
        "samples": samples,
    }


async def measure_exporter(count: int, concurrency: int, latency: float, *, failures: bool) -> dict[str, object]:
    """Measure initial discovery, refreshes, text serialization, and bounded cleanup.

    Returns
    -------
    dict[str, object]
        Lifecycle timings, serialization measurements, and observed resource bounds.

    """
    activity = Activity(latency)
    fleet = Fleet(activity, fail_every=10 if failures else 0)
    exporter = exporter_for(count, concurrency)
    with patch("pyprom_exporters.exporters.tapo.Discover.discover_single", fleet.discover):
        try:
            discovery = await timed(exporter.discover)
            updates = [await timed(exporter.update_and_collect) for _ in range(3)]
            serialization = scrape(exporter, 5)
        finally:
            cleanup = await timed(exporter.cleanup)
    return {
        "devices": count,
        "concurrency": concurrency,
        "scenario": "authentication_failures_10_percent" if failures else "healthy",
        "discovery": asdict(discovery),
        "update": {
            "wall_ms": statistics.median(item.wall_ms for item in updates),
            "cpu_ms": statistics.median(item.cpu_ms for item in updates),
        },
        "serialization": serialization,
        "cleanup": asdict(cleanup),
        "peak_active_io": activity.peak_active,
        "peak_asyncio_tasks": activity.peak_tasks,
        "remaining_asyncio_tasks": len(asyncio.all_tasks()) - 1,
    }


async def measure_retries(count: int, concurrency: int, latency: float) -> dict[str, object]:
    """Measure bounded retry scheduling when every tenth operation always times out.

    Returns
    -------
    dict[str, object]
        Retry timing, attempt count, and observed I/O and task counts.

    """
    activity = Activity(latency)

    async def operation(index: int) -> int:
        await activity.operation()
        if index % 10 == 0:
            message = "Simulated operation timeout"
            raise TimeoutError(message)
        return index

    async def run() -> list[int | Exception]:
        return await run_tasks_with_retry(
            (partial(operation, index) for index in range(count)),
            concurrency=concurrency,
            attempts=3,
            delay=0.001,
            jitter=0,
            return_exceptions=True,
        )

    timing = await timed(run)
    return {
        "operations": count,
        "concurrency": concurrency,
        "timing": asdict(timing),
        "attempts": activity.calls,
        "peak_active_io": activity.peak_active,
        "peak_asyncio_tasks": activity.peak_tasks,
        "remaining_asyncio_tasks": len(asyncio.all_tasks()) - 1,
    }


async def measure_missing(count: int, concurrency: int, latency: float) -> dict[str, object]:
    """Measure healthy-device publication during a 90-percent configured-host outage.

    Returns
    -------
    dict[str, object]
        Discovery and refresh costs with the configured majority of hosts unavailable.

    """
    activity = Activity(latency)
    fleet = Fleet(activity, missing_count=count * 9 // 10)
    exporter = exporter_for(count, concurrency)
    with (
        patch("pyprom_exporters.exporters.tapo.Discover.discover_single", fleet.discover),
        patch("pyprom_exporters.exporters.tapo.REDISCOVERY_INTERVAL", 0.0),
    ):
        try:
            discovery = await timed(exporter.discover)
            calls_before = activity.calls
            refresh = await timed(exporter.update_and_collect)
            refresh_calls = activity.calls - calls_before
            serialization = scrape(exporter, 1)
        finally:
            await exporter.cleanup()
    return {
        "configured_devices": count,
        "missing_devices": fleet.missing_count,
        "concurrency": concurrency,
        "discovery": asdict(discovery),
        "refresh": asdict(refresh),
        "refresh_io_calls": refresh_calls,
        "serialization": serialization,
        "peak_active_io": activity.peak_active,
        "peak_asyncio_tasks": activity.peak_tasks,
    }


async def measure_memory(count: int, concurrency: int) -> dict[str, object]:
    """Track Python allocations separately from timing and repeat cache replacement.

    Returns
    -------
    dict[str, object]
        Allocation snapshots before and after repeated refreshes and cleanup.

    """
    gc.collect()
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    activity = Activity(0.0)
    fleet = Fleet(activity)
    exporter = exporter_for(count, concurrency)
    with patch("pyprom_exporters.exporters.tapo.Discover.discover_single", fleet.discover):
        try:
            await exporter.discover()
            await exporter.update_and_collect()
            gc.collect()
            warm, _ = tracemalloc.get_traced_memory()
            for _ in range(20):
                await exporter.update_and_collect()
                scrape(exporter, 1)
            gc.collect()
            final, peak = tracemalloc.get_traced_memory()
        finally:
            await exporter.cleanup()
    gc.collect()
    cleaned, _ = tracemalloc.get_traced_memory()
    del exporter
    gc.collect()
    released, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "devices": count,
        "concurrency": concurrency,
        "repeated_refreshes": 20,
        "warm_bytes": warm - baseline,
        "final_bytes": final - baseline,
        "peak_bytes": peak - baseline,
        "growth_after_warmup_bytes": final - warm,
        "after_cleanup_bytes": cleaned - baseline,
        "after_release_bytes": released - baseline,
        "remaining_asyncio_tasks": len(asyncio.all_tasks()) - 1,
    }


async def benchmark(sizes: list[int], concurrency: list[int], latency: float) -> dict[str, object]:
    """Run timing and memory scenarios serially to avoid cross-scenario contention.

    Returns
    -------
    dict[str, object]
        Environment, settings, and the measurements from every benchmark scenario.

    """
    exporter_results = []
    retry_results = []
    for count in sizes:
        for workers in concurrency:
            exporter_results.append(await measure_exporter(count, workers, latency, failures=False))
            retry_results.append(await measure_retries(count, workers, latency))
    largest = max(sizes)
    representative_concurrency = concurrency[len(concurrency) // 2]
    exporter_results.append(await measure_exporter(largest, representative_concurrency, latency, failures=True))
    return {
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "recorded_at": datetime.now(UTC).isoformat(),
        },
        "settings": {"sizes": sizes, "concurrency": concurrency, "simulated_io_latency_ms": latency * 1000},
        "exporter": exporter_results,
        "retry_collector": retry_results,
        "missing_hosts": await measure_missing(largest, representative_concurrency, latency),
        "memory": await measure_memory(largest, representative_concurrency),
    }


def write_reports(results: Mapping[str, object], output_dir: Path) -> Path:
    """Publish both reports in a unique UTC-stamped directory after writing succeeds.

    Returns
    -------
    Path
        The completed run directory containing report.html and metrics.json.

    """
    metrics = json.dumps(dict(results), indent=2, sort_keys=True, allow_nan=False) + "\n"
    report = render_html(results)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_prefix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ-")
    with TemporaryDirectory(prefix=f".{run_prefix}", dir=output_dir) as temporary:
        temporary_dir = Path(temporary)
        (temporary_dir / "metrics.json").write_text(metrics, encoding="utf-8")
        (temporary_dir / "report.html").write_text(report, encoding="utf-8")
        run_dir = output_dir / temporary_dir.name.removeprefix(".")
        temporary_dir.rename(run_dir)
    return run_dir


def positive_integer(value: str) -> int:
    """Parse positive workload sizes for argparse.

    Returns
    -------
    int
        The parsed positive integer.

    Raises
    ------
    ArgumentTypeError
        If the parsed integer is less than one.

    """
    result = int(value)
    if result < 1:
        message = "must be a positive integer"
        raise argparse.ArgumentTypeError(message)
    return result


def nonnegative_float(value: str) -> float:
    """Reject invalid or infinite simulated latencies.

    Returns
    -------
    float
        The parsed finite, non-negative latency.

    Raises
    ------
    ArgumentTypeError
        If the parsed number is negative or non-finite.

    """
    result = float(value)
    if not math.isfinite(result) or result < 0:
        message = "must be a finite non-negative number"
        raise argparse.ArgumentTypeError(message)
    return result


def main() -> None:
    """Run the offline benchmark and publish an HTML report with its metrics JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=positive_integer, default=[1, 10, 100, 1000])
    parser.add_argument("--concurrency", nargs="+", type=positive_integer, default=[1, 10, 50])
    parser.add_argument("--latency-ms", type=nonnegative_float, default=1.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("report"),
        help="parent directory for a unique run folder containing report.html and metrics.json (default: report)",
    )
    args = parser.parse_args()
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        results = asyncio.run(benchmark(args.sizes, args.concurrency, args.latency_ms / 1000))
    finally:
        logging.disable(previous_logging_level)
    run_dir = write_reports(results, args.output_dir).resolve()
    sys.stdout.write(f"HTML report: {run_dir / 'report.html'}\nMetrics JSON: {run_dir / 'metrics.json'}\n")


if __name__ == "__main__":
    main()
