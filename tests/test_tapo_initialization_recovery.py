# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for recovery after Kasa's initial negotiation fails."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from kasa import Credentials, DeviceConfig
from kasa.exceptions import AuthenticationError
from kasa.smart import SmartDevice

import pyprom_exporters.exporters.tapo as tapo_module
from pyprom_exporters.exporters.tapo import (
    TapoDiscoveryOptions,
    TapoExporterOptions,
    TapoPowerPlugPrometheusExporter,
)
from tests.conftest import FakeDevice, make_features

if TYPE_CHECKING:
    from collections.abc import Mapping


def make_smart_device(host: str, monkeypatch: pytest.MonkeyPatch) -> tuple[SmartDevice, AsyncMock]:
    """Create a real Kasa device whose protocol uses only deterministic local responses.

    Returns
    -------
    tuple[SmartDevice, AsyncMock]
        The uninitialized device and its simulated query operation.
    """
    device = SmartDevice(host, config=DeviceConfig(host=host, credentials=Credentials()))
    device.update_from_discover_info(
        {"device_model": "P110", "device_type": "SMART.TAPOPLUG", "mac": "00:11:22:33:44:55"}
    )
    responses = {
        "component_nego": {
            "component_list": [{"id": "device", "ver_code": 1}, {"id": "energy_monitoring", "ver_code": 1}]
        },
        "get_device_info": {
            "type": "SMART.TAPOPLUG",
            "model": "P110",
            "device_on": True,
            "nickname": "cGx1Zw==",
            "mac": "00:11:22:33:44:55",
            "fw_ver": "1.0.0 Build 1",
            "hw_ver": "1.0",
            "rssi": -50,
        },
        "get_connect_cloud_state": {"status": 0},
        "get_energy_usage": {"current_power": 5000, "today_energy": 125, "month_energy": 4500},
    }

    def respond(request: Mapping[str, object]) -> dict[str, object]:
        return {name: responses[name] for name in request}

    query = AsyncMock(side_effect=respond)
    monkeypatch.setattr(device.protocol, "query", query)
    monkeypatch.setattr(device, "disconnect", AsyncMock())
    return device, query


@pytest.mark.parametrize("source", ["configured", "broadcast", "broadcast_alias"])
@pytest.mark.parametrize("failure", [OSError, AuthenticationError])
@pytest.mark.parametrize("failed_close", [False, True])
def test_initial_update_failure_recovers_with_fresh_session(
    monkeypatch: pytest.MonkeyPatch, source: str, failure: type[Exception], *, failed_close: bool
) -> None:
    """A failed negotiation cannot poison later scrapes or lose original DNS/close ownership."""

    async def exercise() -> None:
        clock = Mock(return_value=100.0)
        monkeypatch.setattr(tapo_module, "time", SimpleNamespace(monotonic=clock))
        retired, retired_query = make_smart_device("192.0.2.1", monkeypatch)
        retired_query.side_effect = failure("Simulated first negotiation failure")
        target = retired.host if source == "broadcast" else "plug.example.test"
        replacement_host = target if source == "broadcast" else "192.0.2.2"
        replacement, replacement_query = make_smart_device(replacement_host, monkeypatch)
        close = AsyncMock(side_effect=[OSError("Simulated close failure"), None] if failed_close else None)
        monkeypatch.setattr(retired, "disconnect", close)
        healthy = FakeDevice("192.0.2.3", "healthy", make_features())
        broadcast = {device.host: device for device in (retired, healthy)}
        monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value=broadcast))
        attempts: list[str] = []

        def discover_single(host: str, **_kwargs: object) -> SmartDevice | FakeDevice:
            attempts.append(host)
            if host == healthy.host:
                return healthy
            assert host == target
            return replacement if clock.return_value >= 130.0 else retired

        monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(side_effect=discover_single))

        configured = (
            [target, healthy.host] if source == "configured" else [target] if source == "broadcast_alias" else []
        )
        exporter = TapoPowerPlugPrometheusExporter(
            asyncio.get_running_loop(),
            TapoExporterOptions(
                devices=configured,
                max_concurrent_devices=1,
                discovery_options=TapoDiscoveryOptions(
                    perform_discovery=source != "configured", credentials=Credentials()
                ),
            ),
        )
        try:
            await exporter.discover()
            assert exporter.discovered_devices == {healthy.host: healthy}
            assert exporter._pending_hosts == {target: 130.0}
            assert bool(exporter._pending_disconnects) == failed_close
            power = next(metric for metric in exporter.collect() if metric.name == "current_consumption")
            assert {sample.labels["host"] for sample in power.samples} == {healthy.host}

            attempts.clear()
            clock.return_value = 129.0
            await exporter.update_and_collect()
            assert not attempts
            retired_query.assert_awaited_once()
            clock.return_value = 130.0
            await exporter.update_and_collect()
            assert attempts == [target]
            assert replacement_query.await_args_list[0].args[0].keys() == {
                "component_nego",
                "get_device_info",
                "get_connect_cloud_state",
            }
            assert exporter.discovered_devices == {healthy.host: healthy, replacement.host: replacement}
            assert not exporter._pending_hosts
            power = next(metric for metric in exporter.collect() if metric.name == "current_consumption")
            assert {sample.labels["host"] for sample in power.samples} == {healthy.host, replacement.host}
            assert [sample.value for sample in power.samples] == pytest.approx([5.0, 5.0])
            assert healthy.update_calls == 3
        finally:
            await exporter.cleanup()
        assert close.await_count == 1 + failed_close
        assert not exporter._pending_disconnects

    asyncio.run(exercise())


def test_interrupted_retirement_keeps_failed_session_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation during retirement still leaves the session available for shutdown cleanup."""

    async def exercise() -> None:
        device, query = make_smart_device("192.0.2.1", monkeypatch)
        query.side_effect = OSError("Simulated negotiation failure")
        closing = asyncio.Event()
        attempts = 0

        async def disconnect() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                closing.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(device, "disconnect", disconnect)
        monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={device.host: device}))
        exporter = TapoPowerPlugPrometheusExporter(asyncio.get_running_loop())
        discovering = asyncio.create_task(exporter.discover())
        await asyncio.wait_for(closing.wait(), timeout=5)
        discovering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await discovering
        assert not exporter.discovered_devices
        assert exporter._pending_disconnects == {id(device): device}
        await exporter.cleanup()
        assert attempts == 2
        assert not exporter._pending_disconnects
        assert not exporter._discovery_hosts
        assert exporter._cleanup_complete

    asyncio.run(exercise())


@pytest.mark.parametrize("target", ["plug.example.test", "192.0.2.1"])
def test_recovery_closes_retired_session_before_allocating_replacement(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Repeated close failures retain one session per target while healthy devices keep refreshing."""

    async def exercise() -> None:
        clock = Mock(return_value=100.0)
        monkeypatch.setattr(tapo_module, "time", SimpleNamespace(monotonic=clock))
        sessions = [make_smart_device(f"192.0.2.{index}", monkeypatch) for index in range(1, 4)]
        retired = [device for device, _query in sessions[:2]]
        closes = [AsyncMock(side_effect=OSError("Simulated persistent close failure")) for _ in retired]
        for (device, query), close in zip(sessions[:2], closes, strict=True):
            query.side_effect = OSError("Simulated negotiation failure")
            monkeypatch.setattr(device, "disconnect", close)
        healthy = FakeDevice("192.0.2.4", "healthy", make_features())
        created: list[SmartDevice] = []

        def discover_single(host: str, **_kwargs: object) -> SmartDevice | FakeDevice:
            if host == healthy.host:
                return healthy
            assert host == target
            device = sessions[len(created)][0]
            created.append(device)
            return device

        monkeypatch.setattr(tapo_module.Discover, "discover_single", AsyncMock(side_effect=discover_single))
        exporter = TapoPowerPlugPrometheusExporter(
            asyncio.get_running_loop(),
            TapoExporterOptions(
                devices=[target, healthy.host],
                max_concurrent_devices=1,
                discovery_options=TapoDiscoveryOptions(perform_discovery=False, credentials=Credentials()),
            ),
        )
        try:
            await exporter.discover()
            for generation, (device, close) in enumerate(zip(retired, closes, strict=True), start=1):
                for _ in range(4):
                    clock.return_value += 30.0
                    await exporter.update_and_collect()
                    assert len(created) == generation
                    assert exporter._pending_disconnects == {id(device): device}
                    assert exporter._retired_devices == {target: device}
                    assert exporter._pending_hosts == {target: clock.return_value + 30.0}
                close.side_effect = None
                clock.return_value += 30.0
                await exporter.update_and_collect()
            assert len(created) == 3
            assert not exporter._pending_disconnects
            assert not exporter._retired_devices
            assert not exporter._pending_hosts
            assert healthy.update_calls == 11
            power = next(metric for metric in exporter.collect() if metric.name == "current_consumption")
            assert {sample.labels["host"] for sample in power.samples} == {healthy.host, created[-1].host}
        finally:
            for close in closes:
                close.side_effect = None
            await exporter.cleanup()
        assert not exporter._retired_devices

    asyncio.run(exercise())
