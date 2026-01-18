"""Unit tests for the Tapo Prometheus exporter and async retry task runner.

This module exercises discovery behavior, metric collection output, and retry handling using fake Tapo devices.
"""

import asyncio
from types import SimpleNamespace

import pyprom_exporters.exporters.tapo as tapo_module
from pyprom_exporters.exporters.tapo import TapoExporterOptions, TapoPowerPlugPrometheusExporter
from pyprom_exporters.task_collector import run_tasks_with_retry
from tests.conftest import FakeDevice, make_features

EXPECTED_UPDATE_DEVICE_FACTORY_COUNT = 2
EXPECTED_RETRY_ATTEMPTS = 3


def test_collect_from_device_requires_current_consumption() -> None:
    device = FakeDevice("10.0.0.1", "plug-1", features={"rssi": SimpleNamespace(value=-40.0)})
    dump = TapoPowerPlugPrometheusExporter.collect_from_plug_device(device)
    assert dump is None  # noqa: S101


def test_discover_adds_missing_devices(monkeypatch) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device_a = FakeDevice("10.0.0.1", "plug-a", make_features())
        device_b = FakeDevice("10.0.0.2", "plug-b", make_features())

        async def fake_discover(*_args, **_kwargs):
            return {"10.0.0.1": device_a}

        async def fake_discover_single(*_args, **_kwargs):
            return device_b

        monkeypatch.setattr(tapo_module.Discover, "discover", fake_discover)
        monkeypatch.setattr(tapo_module.Discover, "discover_single", fake_discover_single)

        options = TapoExporterOptions(devices=["10.0.0.1", "10.0.0.2"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)

        loop.run_until_complete(exporter.discover())

        assert exporter.discovered_devices is not None  # noqa: S101
        assert set(exporter.discovered_devices.keys()) == {"10.0.0.1", "10.0.0.2"}  # noqa: S101
        assert exporter._update_device_factories is not None  # noqa: S101, SLF001
        assert len(exporter._update_device_factories) == EXPECTED_UPDATE_DEVICE_FACTORY_COUNT  # noqa: S101, SLF001
    finally:
        loop.close()


def test_collect_emits_expected_metrics() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device = FakeDevice("10.0.0.3", "plug-c", make_features())
        options = TapoExporterOptions(devices=["10.0.0.3"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)
        exporter.discovered_devices = {"10.0.0.3": device}
        metrics_snapshot = exporter._build_metrics()  # noqa: SLF001
        with exporter._metrics_lock:  # noqa: SLF001
            exporter._latest_metrics = metrics_snapshot  # noqa: SLF001

        metrics = list(exporter.collect())
        names = {metric.name for metric in metrics}

        assert "tapo_discovered_devices" in names  # noqa: S101
        assert "current_consumption" in names  # noqa: S101

        consumption_metric = next(metric for metric in metrics if metric.name == "current_consumption")
        assert consumption_metric.samples  # noqa: S101
        assert consumption_metric.samples[0].labels == {"host": "10.0.0.3", "alias": "plug-c"}  # noqa: S101
    finally:
        loop.close()


def test_update_uses_retry_runner(monkeypatch) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device = FakeDevice("10.0.0.4", "plug-d", make_features())
        options = TapoExporterOptions(devices=["10.0.0.4"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)
        exporter.discovered_devices = {"10.0.0.4": device}
        exporter._update_device_factories = [  # noqa: SLF001
            lambda: exporter._update_device(device, refresh_interval=0),  # noqa: SLF001
        ]

        called = {"count": 0}

        async def fake_run_tasks_with_retry(factories, **_kwargs):
            called["count"] += 1
            for factory in factories:
                await factory()
            return []

        monkeypatch.setattr(tapo_module, "run_tasks_with_retry", fake_run_tasks_with_retry)

        loop.run_until_complete(exporter.update())

        assert called["count"] == 1  # noqa: S101
        assert device.update_calls == 1  # noqa: S101
    finally:
        loop.close()


def test_run_tasks_with_retry_retries() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        attempts = {"count": 0}

        async def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < EXPECTED_RETRY_ATTEMPTS:
                msg = "transient"
                raise ValueError(msg)
            return "ok"

        factory = lambda: flaky()

        result = loop.run_until_complete(
            run_tasks_with_retry(
                [factory],
                attempts=EXPECTED_RETRY_ATTEMPTS,
                delay=0.0,
                backoff=1.0,
                jitter=0.0,
                retry_exceptions=(ValueError,),
            ),
        )

        assert result == ["ok"]  # noqa: S101
        assert attempts["count"] == EXPECTED_RETRY_ATTEMPTS  # noqa: S101
    finally:
        loop.close()


def test_background_update_populates_cache(monkeypatch) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        device = FakeDevice("10.0.0.5", "plug-e", make_features())

        async def fake_discover(*_args, **_kwargs):
            return {"10.0.0.5": device}

        monkeypatch.setattr(tapo_module.Discover, "discover", fake_discover)

        options = TapoExporterOptions(devices=["10.0.0.5"])
        exporter = TapoPowerPlugPrometheusExporter(asyncio_loop=loop, options=options)

        loop.run_until_complete(exporter.discover())
        loop.run_until_complete(exporter.update_and_collect())

        with exporter._metrics_lock:  # noqa: SLF001
            cached = list(exporter._latest_metrics)  # noqa: SLF001

        assert cached  # noqa: S101
        assert any(metric.name == "tapo_discovered_devices" for metric in cached)  # noqa: S101
    finally:
        loop.close()
