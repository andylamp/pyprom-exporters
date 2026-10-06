# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for isolated device failures and valid Prometheus snapshots."""

from __future__ import annotations

import asyncio
import threading
import time
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypedDict, Unpack, cast
from unittest.mock import AsyncMock, Mock

import pytest
from kasa.exceptions import AuthenticationError
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
    from kasa import Device

    from pyprom_exporters.exporters.tapo import TapoDeviceFamily


class ExporterTestOptions(TypedDict, total=False):
    """Optional exporter settings exercised by the regression fixtures."""

    supported_device_families: dict[TapoDeviceFamily, bool]
    per_device_family_metrics: TapoDeviceFamilyMetrics
    max_concurrent_devices: int
    prometheus_options: TapoPrometheusOptions
    discovery_options: TapoDiscoveryOptions


def make_exporter(
    loop: asyncio.AbstractEventLoop, devices: list[FakeDevice], **options: Unpack[ExporterTestOptions]
) -> TapoPowerPlugPrometheusExporter:
    """Build an exporter with already discovered fake devices.

    Returns
    -------
    TapoPowerPlugPrometheusExporter
        Exporter initialized with the current fake-device inventory.
    """
    exporter = TapoPowerPlugPrometheusExporter(loop, TapoExporterOptions(**options))
    exporter.discovered_devices = {device.host: cast("Device", device) for device in devices}
    return exporter


def test_multi_device_exposition_has_one_family_per_metric() -> None:
    """Prometheus metadata must not be repeated for every device."""
    loop = asyncio.new_event_loop()
    try:
        devices = [FakeDevice(f"10.0.0.{i}", f"plug-{i}", make_features()) for i in range(1, 3)]
        exporter = make_exporter(loop, devices)
        loop.run_until_complete(exporter.update_and_collect())
        registry = CollectorRegistry()
        registry.register(exporter)
        exposition = generate_latest(registry).decode()
        assert exposition.count("# TYPE current_consumption gauge\n") == 1
        samples = next(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        assert len(samples) == 2
        assert {sample.labels["host"] for sample in samples} == {device.host for device in devices}
    finally:
        loop.close()


def test_energy_features_are_converted_from_kwh_to_documented_wh() -> None:
    """Kasa exposes energy in kWh whereas these metric names document Wh."""
    features = make_features()
    features["consumption_today"].value = 0.125
    features["consumption_this_month"].value = 4.5
    device = FakeDevice("10.0.0.1", "plug", features)
    dump = TapoPowerPlugPrometheusExporter.collect_from_plug_device(cast("Device", device))
    assert dump is not None
    assert dump.current_consumption_today == pytest.approx(125.0)
    assert dump.current_month_consumption == pytest.approx(4500.0)


def test_unavailable_features_do_not_emit_zero_measurements() -> None:
    """Missing, invalid and unavailable features must not look like real zeroes."""
    loop = asyncio.new_event_loop()
    try:
        features = make_features()
        del features["voltage"]
        features["current"].value = None
        features["rssi"].value = float("nan")
        features["consumption_today"].value = 1e308
        features["consumption_this_month"].value = float("inf")
        exporter = make_exporter(loop, [FakeDevice("10.0.0.1", "plug", features)])
        families = {metric.name: metric for metric in exporter._build_metrics()}
        assert families["current_voltage"].samples == []
        assert families["current_current"].samples == []
        assert families["current_rssi"].samples == []
        assert families["current_consumption_today"].samples == []
        assert families["current_month_consumption"].samples == []
        assert families["current_consumption"].samples[0].value == pytest.approx(5.0)
    finally:
        loop.close()


def test_zero_discovered_devices_is_visible() -> None:
    """An empty inventory still publishes a useful device count."""
    loop = asyncio.new_event_loop()
    try:
        exporter = make_exporter(loop, [])
        count = next(metric for metric in exporter._build_metrics() if metric.name == "tapo_discovered_devices")
        assert count.samples[0].value == 0
    finally:
        loop.close()


def test_empty_enabled_families_disables_plug_metrics() -> None:
    """An explicit empty family map disables every device family."""
    loop = asyncio.new_event_loop()
    try:
        exporter = make_exporter(loop, [FakeDevice("10.0.0.1", "plug", make_features())], supported_device_families={})
        assert all(metric.name.startswith("tapo_") for metric in exporter._build_metrics())
        assert any(metric.name == "tapo_device_update_success" for metric in exporter.collect())
    finally:
        loop.close()


def test_custom_names_and_label_order_preserve_feature_mapping() -> None:
    """Renaming a metric must not rename the source feature or reorder label values."""
    loop = asyncio.new_event_loop()
    try:
        definitions = TapoDeviceFamilyMetrics(
            plug={
                TapoPerPlugMetricType.CURRENT_CONSUMPTION: TapoPlugGaugeMetric(
                    "custom_power_watts", labels=["model", "host", "alias"]
                )
            }
        )
        exporter = make_exporter(
            loop, [FakeDevice("10.0.0.1", "plug", make_features())], per_device_family_metrics=definitions
        )
        metric = next(metric for metric in exporter._build_metrics() if metric.name == "custom_power_watts")
        assert metric.samples[0].value == pytest.approx(5.0)
        assert metric.samples[0].labels == {"model": "P100", "host": "10.0.0.1", "alias": "plug"}
    finally:
        loop.close()


@pytest.mark.parametrize("labels", [["not_a_label"], ["host", "host"]])
def test_invalid_metric_labels_are_rejected(labels: list[str]) -> None:
    """Reject labels that cannot be mapped unambiguously to device metadata."""
    with pytest.raises(ValueError, match="labels"):
        TapoExporterOptions(
            per_device_family_metrics=TapoDeviceFamilyMetrics(
                plug={TapoPerPlugMetricType.CURRENT_CONSUMPTION: TapoPlugGaugeMetric("power", labels=labels)}
            )
        )


def test_failed_device_does_not_cancel_healthy_updates_or_emit_stale_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """One failed SDK update cannot prevent fresh healthy samples or add exporter retries."""
    monkeypatch.setattr(
        tapo_module, "run_tasks_with_retry", partial(tapo_module.run_tasks_with_retry, delay=0, jitter=0)
    )

    async def exercise() -> None:
        good = FakeDevice("10.0.0.1", "healthy", make_features())
        bad = FakeDevice("10.0.0.2", "offline", make_features())
        bad.update = AsyncMock(side_effect=OSError("offline"))
        exporter = make_exporter(asyncio.get_running_loop(), [bad, good])
        await exporter.update_and_collect()
        samples = next(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        assert len(samples) == 1
        assert samples[0].labels["host"] == good.host
        assert bad.update.await_count == 1
        bad.update = AsyncMock()
        exporter._failed_retry_hosts[bad.host] = 0
        await exporter.update_and_collect()
        samples = next(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        assert len(samples) == 2

    asyncio.run(exercise())


def test_authentication_failures_are_not_retried_and_recover() -> None:
    """Wrong credentials omit the device until a subsequent successful update."""

    async def exercise() -> None:
        device = FakeDevice("10.0.0.1", "plug", make_features())
        device.update = AsyncMock(side_effect=AuthenticationError("invalid credentials"))
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        await exporter.update_and_collect()
        samples = next(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        assert not samples
        assert device.update.await_count == 1
        device.update = AsyncMock()
        exporter._failed_retry_hosts[device.host] = 0
        await exporter.update_and_collect()
        samples = next(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        assert len(samples) == 1

    asyncio.run(exercise())


def test_simultaneous_update_calls_do_not_overlap_device_io() -> None:
    """Background/manual refreshes cannot share a device transport concurrently."""

    async def exercise() -> None:
        device = FakeDevice("10.0.0.1", "plug", make_features())
        active = 0
        peak = 0

        async def update() -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

        device.update = AsyncMock(side_effect=update)
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        await asyncio.gather(*(exporter.update_and_collect() for _ in range(5)))
        assert peak == 1

    asyncio.run(exercise())


def test_device_update_concurrency_is_bounded() -> None:
    """Large inventories use the configured maximum number of simultaneous requests."""

    async def exercise() -> None:
        active = 0
        peak = 0

        async def update() -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

        devices = [FakeDevice(f"10.0.0.{i}", str(i), make_features()) for i in range(20)]
        for device in devices:
            device.update = AsyncMock(side_effect=update)
        exporter = make_exporter(asyncio.get_running_loop(), devices, max_concurrent_devices=3)
        await exporter.update_and_collect()
        assert peak == 3

    asyncio.run(exercise())


def test_single_host_discovery_failures_do_not_abort_other_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit unavailable hosts must not prevent the exporter from starting."""
    device = FakeDevice("10.0.0.2", "plug", make_features())

    def discover_single(host: str, **_kwargs: object) -> FakeDevice:
        if host == "10.0.0.1":
            message = "offline"
            raise OSError(message)
        return device

    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(side_effect=discover_single))

    async def exercise() -> None:
        options = TapoExporterOptions(
            devices=["10.0.0.1", "10.0.0.2", "10.0.0.2"],
            discovery_options=TapoDiscoveryOptions(perform_discovery=False, with_update=False),
        )
        exporter = TapoPowerPlugPrometheusExporter(asyncio.get_running_loop(), options)
        await exporter.discover()
        assert exporter.discovered_devices == {device.host: device}
        assert device.update_calls == 0
        await exporter.cleanup()

    asyncio.run(exercise())


def test_discovery_deduplicates_hostname_and_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only one session and series set should remain for multiple names of one host."""
    device = FakeDevice("10.0.0.1", "plug", make_features())
    duplicate = FakeDevice("10.0.0.1", "plug", make_features())
    close_duplicate = AsyncMock()
    duplicate.disconnect = close_duplicate
    discover_single = AsyncMock(side_effect=[device, duplicate])
    monkeypatch.setattr(tapo_module.Discover, "discover_single", discover_single)

    async def exercise() -> None:
        options = TapoExporterOptions(
            devices=["10.0.0.1", "plug.local"],
            discovery_options=TapoDiscoveryOptions(perform_discovery=False, with_update=False),
        )
        exporter = TapoPowerPlugPrometheusExporter(asyncio.get_running_loop(), options)
        await exporter.discover()
        assert exporter.discovered_devices == {device.host: device}
        close_duplicate.assert_awaited_once()
        await exporter.cleanup()

    asyncio.run(exercise())


def test_one_disconnect_failure_does_not_skip_other_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup attempts every session and may safely be called again."""
    monkeypatch.setattr(
        tapo_module, "run_tasks_with_retry", partial(tapo_module.run_tasks_with_retry, delay=0, jitter=0)
    )

    async def exercise() -> None:
        good = FakeDevice("10.0.0.1", "good", make_features())
        bad = FakeDevice("10.0.0.2", "bad", make_features())
        good.disconnect = AsyncMock()
        bad.disconnect = AsyncMock(side_effect=[OSError("disconnect failure")] * 3 + [None])
        exporter = make_exporter(asyncio.get_running_loop(), [bad, good])
        await exporter.cleanup()
        assert exporter._pending_disconnects == {id(bad): bad}
        assert not exporter._cleanup_complete
        await exporter.cleanup()
        assert not exporter._pending_disconnects
        assert exporter._cleanup_complete
        await exporter.cleanup()
        good.disconnect.assert_awaited_once()
        assert bad.disconnect.await_count == 4

    asyncio.run(exercise())


def test_scrape_on_exporter_loop_and_stopped_loop_returns_cache() -> None:
    """A synchronous scrape must never wait on its own or an inactive event loop."""

    async def exercise() -> None:
        exporter = make_exporter(
            asyncio.get_running_loop(), [], prometheus_options=TapoPrometheusOptions(refresh_interval=None)
        )
        assert not any(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        await exporter.cleanup()

    asyncio.run(exercise())
    loop = asyncio.new_event_loop()
    try:
        exporter = make_exporter(loop, [], prometheus_options=TapoPrometheusOptions(refresh_interval=None))
        assert not any(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
    finally:
        loop.close()


def test_registry_registration_does_not_trigger_device_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry auto-description uses describe rather than a blocking scrape."""
    loop = asyncio.new_event_loop()
    try:
        exporter = make_exporter(loop, [], prometheus_options=TapoPrometheusOptions(refresh_interval=None))
        monkeypatch.setattr(exporter, "collect", lambda: pytest.fail("registration must not collect"))
        CollectorRegistry(auto_describe=True).register(exporter)
    finally:
        loop.close()


def test_scrape_timeout_returns_cache_until_cleanup_cancels_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresponsive device cannot tie up the HTTP worker indefinitely."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    cancelled = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run_loop)
    thread.start()
    assert ready.wait(5)
    exporter = make_exporter(
        loop, [], prometheus_options=TapoPrometheusOptions(refresh_interval=None, scrape_timeout=0.02)
    )

    async def hang() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(exporter, "update_and_collect", hang)
    try:
        start = time.monotonic()
        assert not any(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        assert time.monotonic() - start < 1
        assert not cancelled.is_set()
    finally:
        asyncio.run_coroutine_threadsafe(exporter.cleanup(), loop).result(timeout=5)
        assert cancelled.wait(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


@pytest.mark.parametrize("interval", [0, -1])
def test_nonpositive_refresh_interval_is_rejected(interval: int) -> None:
    """Zero and negative refresh delays must not cause busy polling loops."""
    with pytest.raises(ValueError, match="refresh_interval"):
        TapoPrometheusOptions(refresh_interval=interval)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_scrape_timeout_is_rejected(timeout: float) -> None:
    """Every scrape needs a finite positive upper bound."""
    with pytest.raises(ValueError, match="scrape_timeout"):
        TapoPrometheusOptions(scrape_timeout=timeout)


def test_offline_configured_device_is_rediscovered_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Devices unavailable at startup can recover without restarting the exporter."""
    device = FakeDevice("10.0.0.1", "recovered", make_features())
    discover = AsyncMock(side_effect=[OSError("offline"), device])
    monkeypatch.setattr(tapo_module.Discover, "discover_single", discover)

    async def exercise() -> None:
        exporter = TapoPowerPlugPrometheusExporter(
            asyncio.get_running_loop(),
            TapoExporterOptions(
                devices=[device.host], discovery_options=TapoDiscoveryOptions(perform_discovery=False)
            ),
        )
        await exporter.discover()
        await exporter.update_and_collect()
        assert discover.await_count == 1
        exporter._pending_hosts[device.host] = 0
        await exporter.update_and_collect()
        assert discover.await_count == 2
        assert exporter.discovered_devices == {device.host: device}
        assert not exporter._pending_hosts
        samples = next(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
        assert samples[0].labels["host"] == device.host
        await exporter.cleanup()

    asyncio.run(exercise())


def test_exporter_does_not_use_private_kasa_timestamps() -> None:
    """Different kasa device types may use different clocks for private fields."""

    async def exercise() -> None:
        device = FakeDevice("10.0.0.1", "plug", make_features())
        device._last_update_time = time.time() + 1000000
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        await exporter._update_device(cast("Device", device), 10)
        assert device.update_calls == 1
        await exporter._update_device(cast("Device", device), 10)
        assert device.update_calls == 1

    asyncio.run(exercise())


def test_custom_consumption_feature_key_is_honored() -> None:
    """The configured consumption key selects a feature without renaming metrics."""
    loop = asyncio.new_event_loop()
    try:
        features = make_features()
        features["power"] = features.pop("current_consumption")
        exporter = make_exporter(
            loop,
            [FakeDevice("10.0.0.1", "plug", features)],
            discovery_options=TapoDiscoveryOptions(current_consumption_key="power"),
        )
        metric = next(metric for metric in exporter._build_metrics() if metric.name == "current_consumption")
        assert metric.samples[0].value == 5
    finally:
        loop.close()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, float("inf")])
def test_invalid_device_concurrency_is_rejected(limit: object) -> None:
    """Reject invalid concurrency settings before discovery starts any network work."""
    with pytest.raises(ValueError, match="max_concurrent_devices"):
        TapoExporterOptions(max_concurrent_devices=cast("int", limit))


def test_recovery_discovery_is_bounded_fair_and_removes_recovered_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each refresh tries one bounded wave while every missing host gets a turn."""
    monotonic = Mock(return_value=100.0)
    monkeypatch.setattr(tapo_module, "time", SimpleNamespace(monotonic=monotonic))
    attempts: list[str] = []
    healthy = FakeDevice("192.0.2.10", "healthy", make_features())
    missing_hosts = [f"192.0.2.{index}" for index in range(1, 6)]
    available_devices = {healthy.host: healthy}

    def discover_single(host: str, **_kwargs: object) -> FakeDevice:
        attempts.append(host)
        if host in available_devices:
            return available_devices[host]
        message = "offline"
        raise OSError(message)

    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(side_effect=discover_single))

    async def exercise() -> None:
        exporter = TapoPowerPlugPrometheusExporter(
            asyncio.get_running_loop(),
            TapoExporterOptions(
                devices=[healthy.host, *missing_hosts],
                max_concurrent_devices=2,
                discovery_options=TapoDiscoveryOptions(perform_discovery=False, with_update=False),
            ),
        )
        try:
            await exporter.discover()
            assert sorted(attempts) == sorted([healthy.host, *missing_hosts])
            attempts.clear()
            monotonic.return_value = 1000.0
            batch_sizes = []
            for _ in range(3):
                previous_attempts = len(attempts)
                await exporter.update_and_collect()
                batch_sizes.append(len(attempts) - previous_attempts)
            assert batch_sizes == [2, 2, 1]
            assert sorted(attempts) == missing_hosts
            assert healthy.update_calls == 3
            await exporter.update_and_collect()
            assert len(attempts) == len(missing_hosts)
            recovered_host = attempts[0]
            recovered = FakeDevice(recovered_host, "recovered", make_features())
            available_devices[recovered_host] = recovered
            monotonic.return_value = 2000.0
            await exporter.update_and_collect()
            assert exporter.discovered_devices is not None
            assert exporter.discovered_devices[recovered_host] is recovered
            assert recovered_host not in exporter._pending_hosts
            assert recovered.update_calls == 1
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


def test_discovery_cooldown_starts_when_slow_discovery_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow startup discovery must not immediately retry its unavailable hosts."""
    monotonic = Mock(return_value=100.0)
    monkeypatch.setattr(tapo_module, "time", SimpleNamespace(monotonic=monotonic))
    device = FakeDevice("192.0.2.1", "recovered", make_features())
    attempts = 0

    def discover_single(host: str, **_kwargs: object) -> FakeDevice:
        nonlocal attempts
        assert host == device.host
        attempts += 1
        if attempts == 1:
            monotonic.return_value = 200.0
            message = "offline"
            raise OSError(message)
        return device

    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(side_effect=discover_single))

    async def exercise() -> None:
        exporter = TapoPowerPlugPrometheusExporter(
            asyncio.get_running_loop(),
            TapoExporterOptions(
                devices=[device.host], discovery_options=TapoDiscoveryOptions(perform_discovery=False)
            ),
        )
        try:
            await exporter.discover()
            assert attempts == 1
            monotonic.return_value = 229.0
            await exporter.update_and_collect()
            assert attempts == 1
            monotonic.return_value = 230.0
            await exporter.update_and_collect()
            assert attempts == 2
            assert exporter.discovered_devices == {device.host: device}
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


def test_cleanup_releases_device_metrics_and_recovery_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed exporters release inventory-sized caches and cannot expose old readings."""
    healthy = FakeDevice("192.0.2.1", "healthy", make_features())
    unauthorized = FakeDevice("192.0.2.2", "unauthorized", make_features())
    unavailable = FakeDevice("192.0.2.3", "unavailable", make_features())
    unauthorized.update = AsyncMock(side_effect=AuthenticationError("invalid credentials"))
    unavailable.update = AsyncMock(side_effect=OSError("offline"))
    devices = {device.host: device for device in (healthy, unauthorized, unavailable)}
    missing_host = "192.0.2.4"

    def discover_single(host: str, **_kwargs: object) -> FakeDevice:
        if host in devices:
            return devices[host]
        message = "offline"
        raise OSError(message)

    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(side_effect=discover_single))
    monkeypatch.setattr(
        tapo_module, "run_tasks_with_retry", partial(tapo_module.run_tasks_with_retry, delay=0, jitter=0)
    )

    async def exercise() -> None:
        exporter = TapoPowerPlugPrometheusExporter(
            asyncio.get_running_loop(),
            TapoExporterOptions(
                devices=[*devices, missing_host], discovery_options=TapoDiscoveryOptions(perform_discovery=False)
            ),
        )
        await exporter.discover()
        await exporter.update_and_collect()
        assert list(exporter.collect())
        assert exporter._last_successful_updates
        assert exporter._failed_devices == {unauthorized.host, unavailable.host}
        assert exporter._pending_hosts
        await exporter.cleanup()
        assert not list(exporter.collect())
        assert not exporter.discovered_devices
        assert not exporter._last_successful_updates
        assert not exporter._failed_devices
        assert not exporter._pending_hosts
        assert exporter._refresh_future is None
        await exporter.cleanup()

    asyncio.run(exercise())


def test_cancelled_cleanup_can_retry_unfinished_disconnect() -> None:
    """Cancellation clears stale readings while preserving the session for a later close."""

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        device = FakeDevice("192.0.2.1", "plug", make_features())

        async def disconnect() -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        device.disconnect = AsyncMock(side_effect=disconnect)
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        await exporter.update_and_collect()
        assert list(exporter.collect())
        cleanup = asyncio.create_task(exporter.cleanup())
        await asyncio.wait_for(started.wait(), timeout=2)
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert cancelled.is_set()
        assert exporter.discovered_devices == {device.host: device}
        assert not list(exporter.collect())
        await exporter.update_and_collect()
        assert not list(exporter.collect())
        assert device.update_calls == 1
        release.set()
        await asyncio.wait_for(exporter.cleanup(), timeout=2)
        assert device.disconnect.await_count == 2
        assert not exporter.discovered_devices
        assert not exporter._last_successful_updates
        assert not list(exporter.collect())
        await exporter.cleanup()
        assert device.disconnect.await_count == 2

    asyncio.run(exercise())


def test_concurrent_cleanup_waits_for_single_disconnect() -> None:
    """Every cleanup caller waits for the same close without repeating device I/O."""

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        device = FakeDevice("192.0.2.1", "plug", make_features())

        async def disconnect() -> None:
            started.set()
            await release.wait()

        device.disconnect = AsyncMock(side_effect=disconnect)
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        first = asyncio.create_task(exporter.cleanup())
        await asyncio.wait_for(started.wait(), timeout=2)
        second = asyncio.create_task(exporter.cleanup())
        try:
            await asyncio.sleep(0)
            assert not first.done()
            assert not second.done()
            device.disconnect.assert_awaited_once()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        device.disconnect.assert_awaited_once()
        assert not exporter.discovered_devices

    asyncio.run(exercise())


@pytest.mark.parametrize("rediscover", [False, True])
def test_cancelled_cleanup_prevents_inflight_refresh_from_republishing(
    monkeypatch: pytest.MonkeyPatch, *, rediscover: bool
) -> None:
    """A refresh finishing after interrupted shutdown cannot restore stale cached readings."""

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        device = FakeDevice("192.0.2.1", "plug", make_features())
        exporter = make_exporter(asyncio.get_running_loop(), [device])
        await exporter.update_and_collect()
        assert list(exporter.collect())

        async def update() -> None:
            started.set()
            await release.wait()

        device.update = AsyncMock(side_effect=update)
        monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={device.host: device}))
        refresh = asyncio.create_task(exporter.discover() if rediscover else exporter.update_and_collect())
        await asyncio.wait_for(started.wait(), timeout=2)
        cleanup = asyncio.create_task(exporter.cleanup())
        try:
            await asyncio.sleep(0)
            assert not cleanup.done()
            cleanup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cleanup
            assert not list(exporter.collect())
        finally:
            release.set()
            await asyncio.wait_for(refresh, timeout=2)
        assert not list(exporter.collect())
        await exporter.cleanup()
        assert not exporter.discovered_devices

    asyncio.run(exercise())
