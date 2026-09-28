# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Tapo Prometheus exporter and async retry task runner.

This module exercises discovery behavior, metric collection output, and retry handling using fake Tapo devices.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pyprom_exporters.exporters.tapo as tapo_module
from pyprom_exporters.exporters.tapo import TapoExporterOptions, TapoPowerPlugPrometheusExporter
from pyprom_exporters.task_collector import run_tasks_with_retry
from tests.conftest import FakeDevice, make_features

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import pytest
    from kasa import Device

EXPECTED_RETRY_ATTEMPTS = 3


def test_collect_from_device_requires_current_consumption() -> None:
    """Ensure devices without consumption data are skipped."""
    device = FakeDevice("10.0.0.1", "plug-1", features={"rssi": SimpleNamespace(value=-40.0)})
    dump = TapoPowerPlugPrometheusExporter.collect_from_plug_device(cast("Device", device))
    assert dump is None


def test_discover_adds_missing_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify discovery adds explicit devices when missing from scan results."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device_a = FakeDevice("10.0.0.1", "plug-a", make_features())
        device_b = FakeDevice("10.0.0.2", "plug-b", make_features())

        monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={"10.0.0.1": device_a}))
        monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(return_value=device_b))

        options = TapoExporterOptions(devices=["10.0.0.1", "10.0.0.2"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)

        loop.run_until_complete(exporter.discover())

        assert exporter.discovered_devices is not None
        assert set(exporter.discovered_devices.keys()) == {"10.0.0.1", "10.0.0.2"}
        assert device_a.update_calls == 1
        assert device_b.update_calls == 1
    finally:
        loop.close()


def test_collect_emits_expected_metrics() -> None:
    """Check that cached metrics are emitted with expected labels."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device = FakeDevice("10.0.0.3", "plug-c", make_features())
        options = TapoExporterOptions(devices=["10.0.0.3"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)
        exporter.discovered_devices = {"10.0.0.3": cast("Device", device)}
        metrics_snapshot = exporter._build_metrics()
        with exporter._metrics_lock:
            exporter._latest_metrics = metrics_snapshot

        metrics = list(exporter.collect())
        names = {metric.name for metric in metrics}

        assert "tapo_discovered_devices" in names
        assert "current_consumption" in names

        consumption_metric = next(metric for metric in metrics if metric.name == "current_consumption")
        assert consumption_metric.samples
        assert consumption_metric.samples[0].labels == {"host": "10.0.0.3", "alias": "plug-c"}
    finally:
        loop.close()


def test_collect_refreshes_on_scrape_when_auto_polling_disabled() -> None:
    """Ensure collect triggers an update pass when background polling is disabled."""
    loop = asyncio.new_event_loop()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    try:
        device = FakeDevice("10.0.0.33", "plug-on-demand", make_features())
        options = TapoExporterOptions(devices=["10.0.0.33"])
        assert options.prometheus_options is not None
        options.prometheus_options.refresh_interval = None

        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)
        exporter.discovered_devices = {"10.0.0.33": cast("Device", device)}

        metrics = list(exporter.collect())

        assert device.update_calls == 1
        assert any(metric.name == "current_consumption" for metric in metrics)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        loop.close()


def test_update_uses_retry_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure update calls the retry runner and updates devices."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device = FakeDevice("10.0.0.4", "plug-d", make_features())
        options = TapoExporterOptions(devices=["10.0.0.4"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)
        exporter.discovered_devices = {"10.0.0.4": cast("Device", device)}

        called = {"count": 0}

        async def fake_run_tasks_with_retry(
            factories: Iterable[Callable[[], Awaitable[object]]], **_kwargs: object
        ) -> list[object]:
            called["count"] += 1
            return [await factory() for factory in factories]

        monkeypatch.setattr(tapo_module, "run_tasks_with_retry", fake_run_tasks_with_retry)

        loop.run_until_complete(exporter.update())

        assert called["count"] == 1
        assert device.update_calls == 1
    finally:
        loop.close()


def test_run_tasks_with_retry_retries() -> None:
    """Verify retry logic eventually succeeds for transient failures."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        attempts = {"count": 0}

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < EXPECTED_RETRY_ATTEMPTS:
                msg = "transient"
                raise ValueError(msg)
            return "ok"

        result = loop.run_until_complete(
            run_tasks_with_retry(
                [AsyncMock(side_effect=flaky)],
                attempts=EXPECTED_RETRY_ATTEMPTS,
                delay=0.0,
                backoff=1.0,
                jitter=0.0,
                retry_exceptions=(ValueError,),
            ),
        )

        assert result == ["ok"]
        assert attempts["count"] == EXPECTED_RETRY_ATTEMPTS
    finally:
        loop.close()


def test_background_update_populates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the first update pass populates the cached metrics."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device = FakeDevice("10.0.0.5", "plug-e", make_features())

        monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={"10.0.0.5": device}))

        options = TapoExporterOptions(devices=["10.0.0.5"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)

        loop.run_until_complete(exporter.discover())
        loop.run_until_complete(exporter.update_and_collect())

        with exporter._metrics_lock:
            cached = list(exporter._latest_metrics)

        assert cached
        assert any(metric.name == "tapo_discovered_devices" for metric in cached)
    finally:
        loop.close()


def test_start_background_updates_skips_by_default() -> None:
    """The default live mode must not start a background update task."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        options = TapoExporterOptions(devices=["10.0.0.6"])
        assert options.prometheus_options is not None
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)

        loop.run_until_complete(exporter.start_background_updates())

        assert exporter._update_task is None
    finally:
        loop.close()
