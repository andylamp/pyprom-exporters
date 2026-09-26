# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for live refresh progress and cancellation ownership."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest
from kasa import Credentials

from pyprom_exporters.exporters.tapo import (
    TapoDiscoveryOptions,
    TapoExporterOptions,
    TapoPowerPlugPrometheusExporter,
    TapoPrometheusOptions,
)
from tests.conftest import FakeDevice, make_features

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kasa import Device


@pytest.fixture
def exporter_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Run the exporter loop independently of synchronous HTTP-style callers.

    Yields
    ------
    asyncio.AbstractEventLoop
        An event loop running in a separate thread for the duration of the test.
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


def make_exporter(loop: asyncio.AbstractEventLoop, devices: list[FakeDevice]) -> TapoPowerPlugPrometheusExporter:
    """Configure a serial simulated fleet with a short HTTP wait budget.

    Returns
    -------
    TapoPowerPlugPrometheusExporter
        A live exporter with discovered devices and no external network dependencies.
    """
    exporter = TapoPowerPlugPrometheusExporter(
        loop,
        TapoExporterOptions(
            max_concurrent_devices=1,
            discovery_options=TapoDiscoveryOptions(perform_discovery=False, credentials=Credentials()),
            prometheus_options=TapoPrometheusOptions(refresh_interval=None, scrape_timeout=0.02),
        ),
    )
    exporter.discovered_devices = {device.host: cast("Device", device) for device in devices}
    exporter._update_device_factories = [
        lambda device=device: exporter._update_device(cast("Device", device), None) for device in devices
    ]
    return exporter


def test_scrape_deadlines_preserve_progress_for_the_entire_fleet(
    exporter_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow first device cannot make every scrape restart and starve later devices."""
    devices = [FakeDevice(f"simulated-{index}", "plug", make_features()) for index in range(4)]
    release = asyncio.Event()
    started = threading.Event()
    first = devices[0]

    async def slow_update() -> None:
        first.update_calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(first, "update", slow_update)
    exporter = make_exporter(exporter_loop, devices)
    try:
        assert not list(exporter.collect())
        assert started.wait(5)
        pending = exporter._refresh_future
        assert pending is not None
        assert not pending.done()
        assert not list(exporter.collect())
        assert exporter._refresh_future is pending
        assert first.update_calls == 1

        exporter_loop.call_soon_threadsafe(release.set)
        pending.result(timeout=5)
        assert [device.update_calls for device in devices] == [1, 1, 1, 1]
        power = next(metric for metric in exporter._latest_metrics if metric.name == "current_consumption")
        assert {sample.labels["host"] for sample in power.samples} == {device.host for device in devices}
    finally:
        asyncio.run_coroutine_threadsafe(exporter.cleanup(), exporter_loop).result(timeout=5)


def test_overlapping_scrapes_share_refresh_after_all_waiters_time_out(
    exporter_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight HTTP callers retain one device update even after their deadlines expire."""
    device = FakeDevice("simulated-0", "plug", make_features())
    release = asyncio.Event()
    callers = 8
    barrier = threading.Barrier(callers)

    async def slow_update() -> None:
        device.update_calls += 1
        await release.wait()

    monkeypatch.setattr(device, "update", slow_update)
    exporter = make_exporter(exporter_loop, [device])

    def scrape() -> int:
        barrier.wait(timeout=5)
        return len(list(exporter.collect()))

    try:
        with ThreadPoolExecutor(max_workers=callers) as pool:
            results = [pool.submit(scrape) for _ in range(callers)]
            assert [result.result(timeout=5) for result in results] == [0] * callers
        assert device.update_calls == 1
        pending = exporter._refresh_future
        assert pending is not None
        assert not pending.done()
        exporter_loop.call_soon_threadsafe(release.set)
        pending.result(timeout=5)
        assert device.update_calls == 1
    finally:
        asyncio.run_coroutine_threadsafe(exporter.cleanup(), exporter_loop).result(timeout=5)


def test_cleanup_cancels_a_shared_refresh_after_scrape_timeout(
    exporter_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown owns cancellation of the refresh that outlived its HTTP callers."""
    device = FakeDevice("simulated-0", "plug", make_features())
    cancelled = threading.Event()

    async def slow_update() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(device, "update", slow_update)
    exporter = make_exporter(exporter_loop, [device])
    try:
        assert not list(exporter.collect())
        asyncio.run_coroutine_threadsafe(exporter.cleanup(), exporter_loop).result(timeout=5)
        assert cancelled.is_set()
        assert device.disconnect_calls == 1
        assert exporter._refresh_future is None
        assert not exporter._latest_metrics
    finally:
        asyncio.run_coroutine_threadsafe(exporter.cleanup(), exporter_loop).result(timeout=5)


def test_stopping_background_updates_preserves_caller_cancellation() -> None:
    """Cancellation during the child's finalizer must propagate to the stopping caller."""

    async def exercise() -> None:
        exporter = make_exporter(asyncio.get_running_loop(), [])
        started = asyncio.Event()
        closing = asyncio.Event()

        async def background() -> None:
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                closing.set()
                await asyncio.Event().wait()

        exporter._update_task = asyncio.create_task(background())
        await asyncio.wait_for(started.wait(), timeout=5)
        stopping = asyncio.create_task(exporter.stop_background_updates())
        await asyncio.wait_for(closing.wait(), timeout=5)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert stopping.cancelled()
        await exporter.cleanup()

    asyncio.run(exercise())


def test_cleanup_timeout_does_not_start_disconnect_after_cancellation() -> None:
    """A cleanup deadline cannot be swallowed while joining a background task."""

    async def exercise() -> None:
        device = FakeDevice("simulated-0", "plug", make_features())
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        started = asyncio.Event()

        async def background() -> None:
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.Event().wait()

        exporter._update_task = asyncio.create_task(background())
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(exporter.cleanup(), timeout=0.02)
        assert device.disconnect_calls == 0
        assert not exporter._cleanup_complete
        await exporter.cleanup()
        assert device.disconnect_calls == 1

    asyncio.run(exercise())
