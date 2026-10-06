# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for inventory replacement and interrupted discovery."""

from __future__ import annotations

import asyncio
import gc
import logging
import weakref
from functools import partial
from unittest.mock import AsyncMock

import pytest
from kasa import Credentials

import pyprom_exporters.exporters.tapo as tapo_module
from pyprom_exporters.exporters.tapo import (
    TapoDiscoveryOptions,
    TapoExporterOptions,
    TapoPowerPlugPrometheusExporter,
)
from tests.conftest import FakeDevice, make_features


def make_exporter(*, devices: list[str] | None = None) -> TapoPowerPlugPrometheusExporter:
    """Create an exporter whose discovery calls are replaced by each test.

    Returns
    -------
    TapoPowerPlugPrometheusExporter
        An exporter with explicit empty credentials and default live refreshes.
    """
    return TapoPowerPlugPrometheusExporter(
        asyncio.get_running_loop(),
        TapoExporterOptions(
            devices=devices or [],
            discovery_options=TapoDiscoveryOptions(credentials=Credentials()),
        ),
    )


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
def test_failed_rediscovery_retires_metrics_and_cannot_reopen_old_devices(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    """An interrupted broadcast cannot leave retired device factories or stale samples."""
    retired = FakeDevice("simulated-0", "old plug", make_features())
    monkeypatch.setattr(
        tapo_module.Discover,
        "discover",
        AsyncMock(side_effect=[{retired.host: retired}, failure("simulated broadcast interruption")]),
    )

    async def exercise() -> None:
        exporter = make_exporter()
        try:
            await exporter.discover()
            assert list(exporter.collect())
            assert retired.update_calls == 1
            with pytest.raises(failure):
                await exporter.discover()
            assert exporter.discovered_devices == {}
            assert not any(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
            await exporter.update_and_collect()
            assert retired.update_calls == 1
        finally:
            await exporter.cleanup()
        assert retired.disconnect_calls == 1

    asyncio.run(exercise())


def test_configured_inventory_can_recover_after_broadcast_rediscovery_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next update tracks replacement sessions for configured hosts and closes them."""
    retired = FakeDevice("simulated-0", "old plug", make_features())
    replacement = FakeDevice(retired.host, "replacement plug", make_features())
    monkeypatch.setattr(
        tapo_module.Discover,
        "discover",
        AsyncMock(side_effect=[{retired.host: retired}, OSError("simulated broadcast failure")]),
    )
    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(return_value=replacement))

    async def exercise() -> None:
        exporter = make_exporter(devices=[retired.host])
        try:
            await exporter.discover()
            with pytest.raises(OSError, match="broadcast failure"):
                await exporter.discover()
            assert not any(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
            await exporter.update_and_collect()
            assert exporter.discovered_devices == {replacement.host: replacement}
            assert retired.update_calls == 1
            assert replacement.update_calls == 1
            power = next(metric for metric in exporter.collect() if metric.name == "current_consumption")
            assert power.samples[0].labels["alias"] == replacement.alias
        finally:
            await exporter.cleanup()
        assert retired.disconnect_calls == 1
        assert replacement.disconnect_calls == 1

    asyncio.run(exercise())


def test_cancelling_rediscovery_during_disconnect_preserves_cleanup_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled close clears exported readings while retaining sessions for retry."""
    device = FakeDevice("simulated-0", "plug", make_features())
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={device.host: device}))

    async def exercise() -> None:
        exporter = make_exporter()
        started = asyncio.Event()
        release = asyncio.Event()

        async def disconnect() -> None:
            device.disconnect_calls += 1
            started.set()
            await release.wait()

        monkeypatch.setattr(device, "disconnect", disconnect)
        await exporter.discover()
        rediscovery = asyncio.create_task(exporter.discover())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            rediscovery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await rediscovery
            assert exporter.discovered_devices == {device.host: device}
            assert not any(metric.samples for metric in exporter.collect() if metric.name == "current_consumption")
            release.set()
        finally:
            release.set()
            await exporter.cleanup()
        assert device.disconnect_calls == 2

    asyncio.run(exercise())


def test_cancelled_discovery_keeps_successful_hosts_available_for_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successfully discovered devices remain usable when another host is cancelled."""
    recovered = FakeDevice("simulated-0", "plug", make_features())

    async def exercise() -> None:
        started = asyncio.Event()

        async def discover_single(host: str, **_kwargs: object) -> FakeDevice:
            if host == recovered.host:
                return recovered
            started.set()
            await asyncio.Event().wait()
            message = "unreachable discovery result"
            raise AssertionError(message)

        monkeypatch.setattr(tapo_module.Discover, "discover_single", discover_single)
        exporter = make_exporter(devices=[recovered.host, "simulated-1"])
        assert exporter.options.discovery_options is not None
        exporter.options.discovery_options.perform_discovery = False
        exporter.options.max_concurrent_devices = 1
        discovery = asyncio.create_task(exporter.discover())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            discovery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await discovery
            await exporter.update_and_collect()
            assert exporter.discovered_devices == {recovered.host: recovered}
            assert recovered.update_calls == 1
            power = next(metric for metric in exporter.collect() if metric.name == "current_consumption")
            assert power.samples[0].labels["host"] == recovered.host
        finally:
            await exporter.cleanup()
        assert recovered.disconnect_calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("duplicate_mac", ["00:11:22:AA:BB:CC", "00-11-22-aa-bb-cc", "001122aabbcc"])
def test_hostname_and_broadcast_discovery_share_one_physical_device(
    monkeypatch: pytest.MonkeyPatch, duplicate_mac: str
) -> None:
    """Kasa preserves the configured hostname, so matching MACs must deduplicate aliases."""
    broadcast = FakeDevice("192.0.2.1", "plug", make_features())
    duplicate = FakeDevice("kitchen-plug.local", "plug", make_features())
    monkeypatch.setattr(broadcast, "mac", "00:11:22:aa:bb:cc", raising=False)
    monkeypatch.setattr(duplicate, "mac", duplicate_mac, raising=False)
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={broadcast.host: broadcast}))
    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(return_value=duplicate))

    async def exercise() -> None:
        exporter = make_exporter(devices=[duplicate.host])
        try:
            await exporter.discover()
            assert exporter.discovered_devices == {broadcast.host: broadcast}
            assert broadcast.update_calls == 1
            assert duplicate.update_calls == 0
            assert duplicate.disconnect_calls == 1
            assert not exporter._pending_hosts
            assert not exporter._pending_disconnects
            power = next(metric for metric in exporter.collect() if metric.name == "current_consumption")
            assert [(sample.labels["host"], sample.value) for sample in power.samples] == [(broadcast.host, 5.0)]
        finally:
            await exporter.cleanup()
        assert broadcast.disconnect_calls == 1
        assert duplicate.disconnect_calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "unknown_mac", [None, "None", "", "invalid", "00:11:22", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"]
)
def test_unknown_device_identity_does_not_merge_different_hosts(
    monkeypatch: pytest.MonkeyPatch, unknown_mac: str | None
) -> None:
    """Missing or incomplete MAC addresses cannot act as a shared device identifier."""
    first = FakeDevice("192.0.2.1", "first plug", make_features())
    second = FakeDevice("192.0.2.2", "second plug", make_features())
    monkeypatch.setattr(first, "mac", unknown_mac, raising=False)
    monkeypatch.setattr(second, "mac", unknown_mac, raising=False)
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={first.host: first}))
    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(return_value=second))

    async def exercise() -> None:
        exporter = make_exporter(devices=[second.host])
        try:
            await exporter.discover()
            assert exporter.discovered_devices == {first.host: first, second.host: second}
            assert first.update_calls == second.update_calls == 1
            assert second.disconnect_calls == 0
        finally:
            await exporter.cleanup()

    asyncio.run(exercise())


def test_cancelled_duplicate_close_remains_owned_until_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupted alias-session close cannot leak a session or create duplicate samples."""
    canonical = FakeDevice("192.0.2.1", "plug", make_features())
    duplicate = FakeDevice("kitchen-plug.local", "plug", make_features())
    for device in (canonical, duplicate):
        monkeypatch.setattr(device, "mac", "00:11:22:33:44:55", raising=False)
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={canonical.host: canonical}))
    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(return_value=duplicate))

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def disconnect() -> None:
            duplicate.disconnect_calls += 1
            started.set()
            await release.wait()

        monkeypatch.setattr(duplicate, "disconnect", disconnect)
        exporter = make_exporter(devices=[duplicate.host])
        discovery = asyncio.create_task(exporter.discover())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            discovery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await discovery
            assert exporter.discovered_devices == {canonical.host: canonical}
            assert exporter._pending_disconnects == {id(duplicate): duplicate}
            assert not exporter._pending_hosts
            await exporter.update_and_collect()
            assert canonical.update_calls == 1
            assert duplicate.update_calls == 0
            release.set()
        finally:
            release.set()
            await exporter.cleanup()
        assert duplicate.disconnect_calls == 2
        assert canonical.disconnect_calls == 1
        assert not exporter._pending_disconnects

    asyncio.run(exercise())


def test_failed_duplicate_close_can_retry_cleanup_without_polling_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close failures retain only the duplicate resource until a later cleanup succeeds."""
    canonical = FakeDevice("192.0.2.1", "plug", make_features())
    duplicate = FakeDevice("kitchen-plug.local", "plug", make_features())
    for device in (canonical, duplicate):
        monkeypatch.setattr(device, "mac", "00:11:22:33:44:55", raising=False)
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={canonical.host: canonical}))
    monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(return_value=duplicate))
    close = AsyncMock(side_effect=[OSError("simulated close failure")] * 4 + [None])
    monkeypatch.setattr(duplicate, "disconnect", close)
    monkeypatch.setattr(
        tapo_module, "run_tasks_with_retry", partial(tapo_module.run_tasks_with_retry, delay=0, jitter=0)
    )

    async def exercise() -> None:
        exporter = make_exporter(devices=[duplicate.host])
        try:
            await exporter.discover()
            await exporter.update_and_collect()
            assert canonical.update_calls == 2
            assert duplicate.update_calls == 0
            await exporter.cleanup()
            assert not exporter.discovered_devices
            assert exporter._pending_disconnects == {id(duplicate): duplicate}
            assert not exporter._cleanup_complete
        finally:
            await exporter.cleanup()
        assert close.await_count == 5
        assert not exporter._pending_disconnects
        assert exporter._cleanup_complete

    asyncio.run(exercise())


def test_failed_close_in_inventory_and_pending_ownership_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session tracked for a retry must not be closed twice in one cleanup pass."""
    device = FakeDevice("simulated-0", "plug", make_features())
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={device.host: device}))
    close = AsyncMock(side_effect=[OSError("simulated close failure")] * 3 + [None])
    monkeypatch.setattr(device, "disconnect", close)
    monkeypatch.setattr(
        tapo_module, "run_tasks_with_retry", partial(tapo_module.run_tasks_with_retry, delay=0, jitter=0)
    )

    async def exercise() -> None:
        exporter = make_exporter()
        try:
            await exporter.discover()
            await exporter.disconnect()
            assert exporter.discovered_devices == {device.host: device}
            assert exporter._pending_disconnects == {id(device): device}
        finally:
            await exporter.cleanup()
        assert close.await_count == 4
        assert not exporter._pending_disconnects
        assert exporter._cleanup_complete

    asyncio.run(exercise())


class DiscoveryPayload:
    """Track whether failed-discovery tracebacks retain their local resources."""


def test_discovery_failures_release_tracebacks_even_when_logs_are_buffered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Offline hosts must not accumulate exception graphs across a discovery wave."""
    hosts = [f"simulated-{index}" for index in range(5)]
    payloads: list[weakref.ReferenceType[DiscoveryPayload]] = []

    async def discover_single(host: str, **_kwargs: object) -> None:
        payload = DiscoveryPayload()
        payloads.append(weakref.ref(payload))
        await asyncio.sleep(0)
        message = f"simulated offline host {host}"
        raise OSError(message)

    monkeypatch.setattr(tapo_module.Discover, "discover_single", discover_single)

    async def exercise() -> None:
        exporter = make_exporter(devices=hosts)
        assert exporter.options.discovery_options is not None
        exporter.options.discovery_options.perform_discovery = False
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            await exporter.discover()
            # Reference counting must release failure resources without waiting
            # for a later cyclic-GC pause or for buffered log records to be dropped.
            assert len(payloads) == len(hosts)
            assert all(payload() is None for payload in payloads)
            assert set(exporter._pending_hosts) == set(hosts)
            assert not exporter.discovered_devices
        finally:
            if gc_was_enabled:
                gc.enable()
            await exporter.cleanup()

    with caplog.at_level(logging.WARNING, logger=tapo_module.__name__):
        asyncio.run(exercise())
    assert {record.getMessage() for record in caplog.records} == {
        f"Could not discover configured device {host}: simulated offline host {host}" for host in hosts
    }
    assert all(record.exc_info is None for record in caplog.records)
    assert all(not isinstance(arg, BaseException) for record in caplog.records for arg in (record.args or ()))
