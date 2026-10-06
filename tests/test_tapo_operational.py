# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Deadline, independent publication and operational metric regressions."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from kasa import Credentials
from omegaconf import OmegaConf
from prometheus_client import CollectorRegistry, generate_latest

import pyprom_exporters.exporters.tapo as tapo_module
from pyprom_exporters.exporters.tapo import (
    TapoDeviceFamilyMetrics,
    TapoDiscoveryOptions,
    TapoExporterOptions,
    TapoPerPlugMetricType,
    TapoPlugGaugeMetric,
    TapoPowerPlugPrometheusExporter,
    TapoPrometheusOptions,
)
from tests.conftest import FakeDevice, make_features

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from kasa import Device
    from prometheus_client.metrics_core import Metric


def sample_values(metrics: Iterable[Metric], name: str) -> dict[str, float]:
    """Read a named sample family by host, with an empty key for global samples.

    Returns
    -------
    dict[str, float]
        Samples keyed by host.
    """
    return {
        sample.labels.get("host", ""): float(sample.value)
        for metric in metrics
        for sample in metric.samples
        if sample.name == name
    }


def make_exporter(
    loop: asyncio.AbstractEventLoop, devices: list[FakeDevice], *, update_timeout: float = 1.0
) -> TapoPowerPlugPrometheusExporter:
    """Construct an offline exporter with short caller and device budgets.

    Returns
    -------
    TapoPowerPlugPrometheusExporter
        Exporter owning the supplied simulated inventory.
    """
    exporter = TapoPowerPlugPrometheusExporter(
        loop,
        TapoExporterOptions(
            max_concurrent_devices=2,
            update_timeout=update_timeout,
            discovery_options=TapoDiscoveryOptions(perform_discovery=False, credentials=Credentials()),
            prometheus_options=TapoPrometheusOptions(scrape_timeout=0.03),
        ),
    )
    exporter.discovered_devices = {device.host: cast("Device", device) for device in devices}
    return exporter


@contextmanager
def running_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Run device operations independently of synchronous scrape callers.

    Yields
    ------
    asyncio.AbstractEventLoop
        The event loop owned by a joined background thread.
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(5)
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not thread.is_alive()
        loop.close()


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_invalid_update_timeout_is_rejected(value: float) -> None:
    """A whole-update deadline must always be finite and positive."""
    with pytest.raises(ValueError, match="update_timeout"):
        TapoExporterOptions(update_timeout=value)


def test_update_timeout_roundtrips_through_structured_configuration() -> None:
    """The default and explicit fractional deadline survive the public YAML schema."""
    assert TapoExporterOptions().update_timeout == 10
    config = OmegaConf.merge(OmegaConf.structured(TapoExporterOptions), {"update_timeout": 0.25})
    options = OmegaConf.to_object(config)
    assert isinstance(options, TapoExporterOptions)
    assert options.update_timeout == pytest.approx(0.25)


def test_operational_metrics_track_failures_recovery_and_real_counter_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures omit readings and retain last success; successful recovery keeps cumulative counts."""
    epoch = Mock(return_value=1000.0)
    monkeypatch.setattr(tapo_module, "epoch_time", epoch)

    async def exercise() -> None:
        device = FakeDevice("simulated-0", "plug", make_features())
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        try:
            await exporter.update_and_collect()
            first = list(exporter.collect())
            assert sample_values(first, "tapo_device_update_success") == {device.host: 1}
            assert sample_values(first, "tapo_device_last_success_timestamp_seconds") == {device.host: 1000}
            failing_update = AsyncMock(side_effect=OSError("Simulated outage"))
            monkeypatch.setattr(device, "update", failing_update)
            await exporter.update_and_collect()
            failed = list(exporter.collect())
            assert not sample_values(failed, "current_consumption")
            assert sample_values(failed, "tapo_device_update_success") == {device.host: 0}
            assert sample_values(failed, "tapo_device_last_success_timestamp_seconds") == {device.host: 1000}
            assert sample_values(failed, "tapo_device_update_failures_total") == {device.host: 1}
            assert sample_values(failed, "tapo_device_update_timeouts_total") == {device.host: 0}
            failing_update.assert_awaited_once()
            epoch.return_value = 1002.0
            monkeypatch.setattr(device, "update", AsyncMock())
            exporter._failed_retry_hosts[device.host] = 0
            await exporter.update_and_collect()
            recovered = list(exporter.collect())
            assert sample_values(recovered, "tapo_device_update_success") == {device.host: 1}
            assert sample_values(recovered, "tapo_device_last_success_timestamp_seconds") == {device.host: 1002}
            assert sample_values(recovered, "tapo_device_update_failures_total") == {device.host: 1}
            assert sample_values(recovered, "tapo_refresh_in_progress") == {"": 0}
            registry = CollectorRegistry()
            registry.register(exporter)
            assert b"# TYPE tapo_device_update_failures_total counter" in generate_latest(registry)
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


def test_sdk_deadline_bounds_native_retry_and_close_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """An initialized hanging session expires once, publishes failure and remains owned until closed."""

    async def exercise() -> None:
        device = FakeDevice("simulated-0", "plug", make_features())
        exporter = make_exporter(asyncio.get_running_loop(), [device], update_timeout=0.02)
        await exporter.update_and_collect()
        timestamp = sample_values(exporter.collect(), "tapo_device_last_success_timestamp_seconds")
        cancelled = asyncio.Event()

        async def hang() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        update = AsyncMock(side_effect=hang)
        monkeypatch.setattr(device, "update", update)
        monkeypatch.setattr(device, "disconnect", AsyncMock(side_effect=hang))
        try:
            start = time.monotonic()
            await asyncio.wait_for(exporter.update_and_collect(), timeout=1)
            assert time.monotonic() - start < 0.5
            assert cancelled.is_set()
            update.assert_awaited_once()
            assert exporter._pending_disconnects == {id(device): device}
            assert not exporter.discovered_devices
            metrics = list(exporter.collect())
            assert not sample_values(metrics, "current_consumption")
            assert sample_values(metrics, "tapo_device_update_success") == {device.host: 0}
            assert sample_values(metrics, "tapo_device_update_failures_total") == {device.host: 1}
            assert sample_values(metrics, "tapo_device_update_timeouts_total") == {device.host: 1}
            assert sample_values(metrics, "tapo_device_last_success_timestamp_seconds") == timestamp
            assert sample_values(metrics, "tapo_device_update_duration_seconds")[device.host] >= 0.02
        finally:
            monkeypatch.setattr(device, "disconnect", AsyncMock())
            await exporter.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("blocked_stage", ["failed", "discovery"])
def test_healthy_scrapes_finish_while_recovery_remains_blocked(
    monkeypatch: pytest.MonkeyPatch, blocked_stage: str
) -> None:
    """Known slow peers and missing-host discovery cannot delay fresh healthy scrapes."""
    healthy = FakeDevice("simulated-good", "healthy", make_features())
    healthy.features["current_consumption"].value = 42.0
    blocked = FakeDevice("simulated-bad", "unavailable", make_features())
    release = asyncio.Event()
    started = threading.Event()

    async def hang(*_args: object, **_kwargs: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(blocked, "update", hang)
    monkeypatch.setattr(tapo_module.Discover, "discover_single", hang)
    with running_loop() as loop:
        exporter = make_exporter(loop, [blocked, healthy] if blocked_stage == "failed" else [healthy])
        exporter._failed_devices.add(blocked.host)
        if blocked_stage == "discovery":
            exporter._pending_hosts[blocked.host] = 0
        try:
            metrics = list(exporter.collect())
            assert started.wait(5)
            assert sample_values(metrics, "current_consumption") == {healthy.host: 42}
            assert sample_values(metrics, "tapo_scrape_refresh_timeouts_total") == {"": 0}
            pending = exporter._refresh_future
            assert pending is not None
            assert not pending.done()
            repeated = list(exporter.collect())
            assert sample_values(repeated, "tapo_refresh_in_progress") == {"": 1}
            assert healthy.update_calls == 1
            assert exporter._refresh_future is pending
            loop.call_soon_threadsafe(release.set)
            pending.result(timeout=5)
        finally:
            loop.call_soon_threadsafe(release.set)
            asyncio.run_coroutine_threadsafe(exporter.cleanup(), loop).result(timeout=5)


def test_scrape_deadline_reads_completed_devices_before_whole_pass_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A newly slow peer cannot hide readings already completed within this scrape."""
    healthy = FakeDevice("simulated-good", "healthy", make_features())
    healthy.features["current_consumption"].value = 42.0
    blocked = FakeDevice("simulated-bad", "unavailable", make_features())
    release = asyncio.Event()

    async def hang() -> None:
        await release.wait()

    monkeypatch.setattr(blocked, "update", hang)
    with running_loop() as loop:
        exporter = make_exporter(loop, [blocked, healthy])
        try:
            metrics = list(exporter.collect())
            assert sample_values(metrics, "current_consumption") == {healthy.host: 42}
            assert sample_values(metrics, "tapo_refresh_in_progress") == {"": 1}
            assert sample_values(metrics, "tapo_scrape_refresh_timeouts_total") == {"": 1}
            assert exporter._refresh_future is not None
            assert not exporter._refresh_future.done()
            loop.call_soon_threadsafe(release.set)
            exporter._refresh_future.result(timeout=5)
        finally:
            loop.call_soon_threadsafe(release.set)
            asyncio.run_coroutine_threadsafe(exporter.cleanup(), loop).result(timeout=5)


@pytest.mark.parametrize("count", [10, 1000])
def test_device_completion_does_not_rebuild_the_fleet(count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-device publication reads each device once and builds metric families once per pass."""

    async def exercise() -> None:
        devices = [FakeDevice(f"simulated-{index}", "plug", make_features()) for index in range(count)]
        exporter = make_exporter(asyncio.get_running_loop(), devices)
        render = Mock(wraps=exporter._render_metrics)
        read = Mock(wraps=exporter._read_device_dump)
        monkeypatch.setattr(exporter, "_render_metrics", render)
        monkeypatch.setattr(exporter, "_read_device_dump", read)
        try:
            await exporter.update_and_collect()
            assert len(sample_values(exporter.collect(), "current_consumption")) == count
            assert read.call_count == count
            render.assert_called_once()
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


def test_never_discovered_hosts_have_zero_health_and_unknown_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing configured hosts remain visible without fabricating SDK successes or failures."""
    monkeypatch.setattr(
        tapo_module.Discover, "discover_single", AsyncMock(side_effect=OSError("Simulated offline host"))
    )

    async def exercise() -> None:
        exporter = make_exporter(asyncio.get_running_loop(), [])
        exporter.options.devices = ["plug.example.test"]
        exporter.options.supported_device_families = {}
        try:
            await exporter.discover()
            metrics = list(exporter.collect())
            for metric in metrics:
                assert metric.name.startswith("tapo_")
                for sample in metric.samples:
                    if sample.labels.get("host") == "plug.example.test":
                        assert sample.labels["alias"] == "unknown"
                        assert sample.value == 0
            assert sample_values(metrics, "tapo_device_update_success") == {"plug.example.test": 0}
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


def test_hostname_replacements_keep_one_diagnostic_record_and_counter_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS/IP changes migrate history without accumulating obsolete host or placeholder series."""
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(tapo_module, "time", SimpleNamespace(monotonic=clock))
    devices = [FakeDevice(f"192.0.2.{index}", "plug", make_features()) for index in range(1, 4)]
    discover = AsyncMock(side_effect=devices)
    monkeypatch.setattr(tapo_module.Discover, "discover_single", discover)

    async def exercise() -> None:
        exporter = make_exporter(asyncio.get_running_loop(), [])
        exporter.options.devices = ["plug.example.test"]
        try:
            await exporter.discover()
            for failures, device in enumerate(devices):
                metrics = list(exporter.collect())
                assert sample_values(metrics, "tapo_device_update_failures_total") == {device.host: failures}
                assert len(exporter._device_snapshots) == 1
                assert exporter._health_hosts == {"plug.example.test": device.host}
                if failures < len(devices) - 1:
                    monkeypatch.setattr(device, "update", AsyncMock(side_effect=TimeoutError))
                    await exporter.update_and_collect()
                    clock.return_value += 30
                    await exporter.update_and_collect()
            assert [call.kwargs["host"] for call in discover.await_args_list] == ["plug.example.test"] * 3
        finally:
            await exporter.cleanup()
        assert not exporter._device_snapshots
        assert not exporter._health_hosts

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "name",
    [
        "tapo_device_update_success",
        "tapo_device_update_failures",
        "tapo_device_update_failures_total",
        "tapo_device_update_failures_created",
    ],
)
def test_operational_metric_names_cannot_be_reused_by_measurements(name: str) -> None:
    """Custom measurement names must not collide with fixed families or counter sample names."""
    with pytest.raises(ValueError, match="Duplicate metric name"):
        TapoExporterOptions(
            per_device_family_metrics=TapoDeviceFamilyMetrics(
                plug={TapoPerPlugMetricType.CURRENT_CONSUMPTION: TapoPlugGaugeMetric(name)}
            )
        )


def test_caller_cancellation_is_not_counted_as_a_device_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown cancellation propagates without fabricating failed or timed-out SDK attempts."""

    async def exercise() -> None:
        device = FakeDevice("simulated-0", "plug", make_features())
        started = asyncio.Event()

        async def hang() -> None:
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(device, "update", hang)
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        refreshing = asyncio.create_task(exporter.update_and_collect())
        await asyncio.wait_for(started.wait(), timeout=5)
        refreshing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refreshing
        metrics = list(exporter.collect())
        assert sample_values(metrics, "tapo_device_update_failures_total") == {device.host: 0}
        assert sample_values(metrics, "tapo_device_update_timeouts_total") == {device.host: 0}
        assert sample_values(metrics, "tapo_refresh_in_progress") == {"": 0}
        await exporter.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("broadcast", [False, True])
def test_discovery_readings_remain_available_without_an_explicit_sdk_update(
    monkeypatch: pytest.MonkeyPatch, *, broadcast: bool
) -> None:
    """Existing discovery readings remain usable without inventing a successful SDK update."""
    device = FakeDevice("192.0.2.1", "plug", make_features())
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={device.host: device}))
    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(return_value=device))

    async def exercise() -> None:
        exporter = make_exporter(asyncio.get_running_loop(), [])
        exporter.options.devices = [] if broadcast else ["plug.example.test"]
        assert exporter.options.discovery_options is not None
        exporter.options.discovery_options.perform_discovery = broadcast
        exporter.options.discovery_options.with_update = False
        try:
            await exporter.discover()
            metrics = list(exporter.collect())
            assert sample_values(metrics, "current_consumption") == {device.host: 5}
            assert sample_values(metrics, "tapo_device_update_success") == {device.host: 0}
            assert sample_values(metrics, "tapo_device_last_success_timestamp_seconds") == {device.host: 0}
            assert device.update_calls == 0
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


def test_full_broadcast_rediscovery_discards_obsolete_diagnostic_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replacing an unconfigured broadcast inventory must not accumulate old host records."""
    devices = [FakeDevice(f"192.0.2.{index}", "plug", make_features()) for index in range(1, 6)]
    monkeypatch.setattr(
        tapo_module.Discover, "discover", AsyncMock(side_effect=[{device.host: device} for device in devices])
    )

    async def exercise() -> None:
        exporter = make_exporter(asyncio.get_running_loop(), [])
        assert exporter.options.discovery_options is not None
        exporter.options.discovery_options.perform_discovery = True
        try:
            for device in devices:
                await exporter.discover()
                assert sample_values(exporter.collect(), "tapo_device_update_success") == {device.host: 1}
                assert len(exporter._device_snapshots) == 1
        finally:
            await exporter.cleanup()
        assert all(device.disconnect_calls == 1 for device in devices)

    asyncio.run(exercise())


def test_failed_devices_retry_one_fair_wave_with_cooldowns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Large failed inventories cannot hold one shared refresh across repeated healthy scrapes."""
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(tapo_module, "time", SimpleNamespace(monotonic=clock))

    async def exercise() -> None:
        failed = [FakeDevice(f"simulated-{index}", "failed", make_features()) for index in range(9)]
        healthy = FakeDevice("simulated-healthy", "healthy", make_features())
        for device in failed:
            monkeypatch.setattr(device, "update", AsyncMock(side_effect=OSError("Simulated outage")))
        exporter = make_exporter(asyncio.get_running_loop(), [*failed, healthy])
        exporter._failed_devices = {device.host for device in failed}
        try:
            for expected_failures in (2, 4, 6, 8, 9):
                await exporter.update_and_collect()
                assert (
                    sum(sample_values(exporter.collect(), "tapo_device_update_failures_total").values())
                    == expected_failures
                )
            assert healthy.update_calls == 5
            assert list(exporter._failed_retry_hosts) == [device.host for device in failed]
            await exporter.update_and_collect()
            assert sum(sample_values(exporter.collect(), "tapo_device_update_failures_total").values()) == 9
            clock.return_value = 130.0
            await exporter.update_and_collect()
            assert sum(sample_values(exporter.collect(), "tapo_device_update_failures_total").values()) == 11
            assert list(exporter._failed_retry_hosts) == [device.host for device in [*failed[2:], *failed[:2]]]
            assert healthy.update_calls == 7
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("split_aliases", [False, True])
def test_alias_recovery_and_full_rediscovery_preserve_shared_failure_history(
    monkeypatch: pytest.MonkeyPatch, *, split_aliases: bool
) -> None:
    """Changing which configured alias owns a session must not reset its counters."""
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(tapo_module, "time", SimpleNamespace(monotonic=clock))
    targets = ["a.example.test", "b.example.test"]
    split = Mock(return_value=False)

    def discover_single(host: str, **_kwargs: object) -> FakeDevice:
        device = FakeDevice(host, "plug", make_features())
        mac = "00:11:22:33:44:66" if split.return_value and host == targets[0] else "00:11:22:33:44:55"
        monkeypatch.setattr(device, "mac", mac, raising=False)
        return device

    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(side_effect=discover_single))

    async def exercise() -> None:
        exporter = make_exporter(asyncio.get_running_loop(), [])
        exporter.options.devices = targets
        try:
            await exporter.discover()
            assert exporter.discovered_devices is not None
            initial = exporter.discovered_devices[targets[0]]
            monkeypatch.setattr(initial, "update", AsyncMock(side_effect=TimeoutError))
            await exporter.update_and_collect()
            clock.return_value = 130.0
            await exporter.update_and_collect()
            assert sample_values(exporter.collect(), "tapo_device_update_failures_total") == {targets[1]: 1}
            assert exporter._health_hosts == dict.fromkeys(targets, targets[1])
            split.return_value = split_aliases
            await exporter.discover()
            expected = {targets[0]: 0, targets[1]: 1} if split_aliases else {targets[0]: 1}
            assert sample_values(exporter.collect(), "tapo_device_update_failures_total") == expected
            expected_hosts = (
                {target: target for target in targets} if split_aliases else dict.fromkeys(targets, targets[0])
            )
            assert exporter._health_hosts == expected_hosts
            assert len(exporter._device_snapshots) == len(expected)
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())
