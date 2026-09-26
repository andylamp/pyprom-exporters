# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for configuration persistence and runtime lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import signal
import threading
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from kasa import Credentials
from omegaconf import OmegaConf
from prometheus_client import CollectorRegistry
from prometheus_client.metrics_core import GaugeMetricFamily

from pyprom_exporters import prom_exporter as runtime
from pyprom_exporters.config import PromExporterConfig
from pyprom_exporters.exporters import tapo as tapo_module
from pyprom_exporters.exporters.base import BasePrometheusCollector
from pyprom_exporters.exporters.tapo import TapoExporterOptions
from tests.conftest import FakeDevice, make_features


class StubCollector(BasePrometheusCollector):
    """A collector with observable cleanup and a stable metric name."""

    def __init__(self) -> None:
        """Initialize the observable cleanup state."""
        super().__init__()
        self.cleaned = False

    @staticmethod
    def collect() -> list[GaugeMetricFamily]:
        """Return a stable sample for registration and cleanup tests.

        Returns
        -------
        list[GaugeMetricFamily]
            The single metric exposed by this test collector.
        """
        return [GaugeMetricFamily("runtime_test", "Runtime test metric", value=1)]

    async def cleanup(self) -> None:
        """Record that runtime cleanup reached this collector."""
        self.cleaned = True


@pytest.mark.parametrize("content", ["prometheus_port: 9123\n", "log_level: DEBUG\n", "{}\n", ""])
def test_top_level_config_without_exporters(tmp_path: Path, content: str) -> None:
    """Partial application configurations must not be interpreted as legacy exporter configs."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    app, _, exists = runtime.load_app_config(str(config_path))
    assert exists
    assert isinstance(app, PromExporterConfig)
    if content.startswith("prometheus_port"):
        assert app.prometheus_port == 9123


def test_legacy_exporter_config(tmp_path: Path) -> None:
    """Continue accepting the historical exporter-only YAML layout."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("devices: [192.0.2.1]\n", encoding="utf-8")
    app, _, _ = runtime.load_app_config(str(config_path))
    assert app.exporters.tapo.devices == ["192.0.2.1"]


def test_list_config_rejected(tmp_path: Path) -> None:
    """Reject non-mapping YAML with a clear error."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(TypeError, match="mapping"):
        runtime.load_app_config(str(config_path))


def test_config_custom_credential_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured environment keys override schema construction's default environment keys."""
    monkeypatch.setenv("TP_LINK_USERNAME", "default-user")
    monkeypatch.setenv("TP_LINK_PASSWORD", "default-password")
    monkeypatch.setenv("CUSTOM_USERNAME", "custom-user")
    custom_password = secrets.token_urlsafe()
    monkeypatch.setenv("CUSTOM_PASSWORD", custom_password)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "exporters:\n  tapo:\n    discovery_options:\n"
        "      tapo_username_env_key: CUSTOM_USERNAME\n      tapo_password_env_key: CUSTOM_PASSWORD\n",
        encoding="utf-8",
    )
    app, _, _ = runtime.load_app_config(str(config_path))
    discovery = app.exporters.tapo.discovery_options
    assert discovery is not None
    assert discovery.credentials is not None
    assert discovery.credentials.username == "custom-user"
    assert discovery.credentials.password == custom_password


@pytest.mark.parametrize("interval", [None, 15])
def test_minimal_config_preserves_refresh_mode(tmp_path: Path, interval: int | None) -> None:
    """Minimal configuration must retain live defaults and explicit background polling."""
    app = PromExporterConfig()
    assert app.exporters.tapo.prometheus_options is not None
    app.exporters.tapo.prometheus_options.refresh_interval = interval
    config_path = tmp_path / "config.yaml"
    runtime.write_config(OmegaConf.structured(app), config_path, minimal=True)
    loaded, _, _ = runtime.load_app_config(str(config_path))
    assert loaded.exporters.tapo.prometheus_options is not None
    assert loaded.exporters.tapo.prometheus_options.refresh_interval == interval


@pytest.mark.parametrize("minimal", [False, True])
def test_credentials_never_written(tmp_path: Path, *, minimal: bool) -> None:
    """Both persistence modes scrub usernames and passwords."""
    app = PromExporterConfig()
    assert app.exporters.tapo.discovery_options is not None
    private_password = secrets.token_urlsafe()
    app.exporters.tapo.discovery_options.credentials = Credentials(
        username="private-user",
        password=private_password,
    )
    config_path = tmp_path / "config.yaml"
    runtime.write_config(OmegaConf.structured(app), config_path, minimal=minimal)
    content = config_path.read_text(encoding="utf-8")
    assert "credentials" not in content
    assert "private-user" not in content
    assert private_password not in content


def test_write_failure_preserves_existing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed atomic replacement leaves the original file intact and removes the temporary file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("original", encoding="utf-8")
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(SystemExit):
        runtime.write_config(OmegaConf.structured(PromExporterConfig()), config_path)
    assert config_path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [config_path]


def test_cli_credentials_override_environment_per_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overriding one credential field must preserve the other field from the environment."""
    app = PromExporterConfig()
    monkeypatch.setenv("TAPO_USERNAME", "env-user")
    env_password = secrets.token_urlsafe()
    monkeypatch.setenv("TAPO_PASSWORD", env_password)
    runtime.apply_env_overrides(app)
    args = argparse.Namespace(
        log_level=None, prometheus_port=None, tapo_plug_devices=None, tapo_username="cli-user", tapo_password=None
    )
    runtime.apply_cli_overrides(app, args)
    discovery = app.exporters.tapo.discovery_options
    assert discovery is not None
    assert discovery.credentials is not None
    assert discovery.credentials.username == "cli-user"
    assert discovery.credentials.password == env_password


def test_registration_rolls_back_on_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed bind must not leave collectors registered."""
    registry = CollectorRegistry()
    collector = StubCollector()
    monkeypatch.setattr(runtime, "REGISTRY", registry)
    monkeypatch.setattr(runtime, "start_http_server", Mock(side_effect=OSError("address in use")))
    with pytest.raises(OSError, match="address in use"):
        runtime.register_exporters(8090, [collector])
    assert not list(registry.collect())


def test_registration_rolls_back_on_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration errors occur before opening an HTTP socket."""
    registry = CollectorRegistry(auto_describe=True)
    server = Mock()
    monkeypatch.setattr(runtime, "REGISTRY", registry)
    monkeypatch.setattr(runtime, "start_http_server", server)
    with pytest.raises(ValueError, match="Duplicated"):
        runtime.register_exporters(8090, [StubCollector(), StubCollector()])
    assert not list(registry.collect())
    server.assert_not_called()


def test_cleanup_stops_loop_and_unregisters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown drains collectors, releases registration, and closes the background loop."""
    registry = CollectorRegistry()
    collector = StubCollector()
    registry.register(collector)
    monkeypatch.setattr(runtime, "REGISTRY", registry)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=runtime._run_event_loop, args=(loop,))
    thread.start()
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=2)
    runtime.cleanup_func([collector], loop, thread)
    assert collector.cleaned
    assert loop.is_closed()
    assert not thread.is_alive()
    assert not list(registry.collect())
    runtime.cleanup_func([collector], loop, thread)


def test_signal_handler_only_requests_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated signals only set the event; they never reenter resource cleanup."""
    install = Mock()
    monkeypatch.setattr(signal, "signal", install)
    event = threading.Event()
    runtime.graceful_exit_handler(event)
    handler = install.call_args_list[0].args[1]
    handler(signal.SIGTERM, None)
    handler(signal.SIGTERM, None)
    assert event.is_set()


def test_shutdown_interrupts_startup() -> None:
    """Termination while initialization is pending cancels initialization promptly."""
    future: Future = Future()
    event = threading.Event()
    event.set()
    assert runtime._wait_for_startup(future, event) is None
    assert future.cancelled()


def test_initialization_failure_cleans_partial_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resources acquired before a discovery failure must be closed."""
    exporter = Mock()
    exporter.discover = AsyncMock(side_effect=RuntimeError("discovery failed"))
    exporter.cleanup = AsyncMock()
    monkeypatch.setattr(runtime, "TapoPowerPlugPrometheusExporter", Mock(return_value=exporter))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="discovery failed"):
            await runtime.tapo_exporter_init(asyncio.get_running_loop(), TapoExporterOptions())
        exporter.cleanup.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["supported_device_families", "per_device_family_metrics.plug"])
def test_minimal_config_preserves_empty_maps(tmp_path: Path, key: str) -> None:
    """Empty family and metric maps must continue disabling their defaults after reload."""
    config = OmegaConf.structured(PromExporterConfig())
    path = f"exporters.tapo.{key}"
    OmegaConf.update(config, path, {}, merge=False)
    config_path = tmp_path / "config.yaml"
    runtime.write_config(config, config_path, minimal=True)
    _, reloaded, _ = runtime.load_app_config(str(config_path))
    assert not OmegaConf.select(reloaded, path)


@pytest.mark.parametrize("persist", [True, False])
def test_main_shuts_down_http_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, persist: bool) -> None:
    """The full runtime releases its HTTP server and event loop after a signal."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    args = ["prom-exporter", "--config", str(config_path)]
    if not persist:
        args.append("--no-write-config")
    monkeypatch.setattr("sys.argv", args)
    collector = StubCollector()
    monkeypatch.setattr(runtime, "tapo_exporter_init", AsyncMock(return_value=collector))
    event = threading.Event()
    monkeypatch.setattr(runtime, "Event", lambda: event)
    server, thread = Mock(), Mock()

    def start_server(*_args: object) -> tuple[Mock, Mock]:
        event.set()
        return server, thread

    monkeypatch.setattr(runtime, "register_exporters", start_server)
    runtime.main()
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()
    thread.join.assert_called_once()
    assert collector.cleaned
    assert (config_path.read_text(encoding="utf-8") != "{}") is persist


def test_shutdown_preserves_concurrently_completed_startup() -> None:
    """A completed exporter must be handed to the caller for cleanup even during shutdown."""
    future: Future = Future()
    collector = StubCollector()
    event = Mock(spec=threading.Event)

    def complete_during_wait(*, timeout: float) -> bool:
        assert timeout > 0
        future.set_result(collector)
        return True

    event.wait.side_effect = complete_during_wait
    assert runtime._wait_for_startup(future, event) is collector


@pytest.mark.parametrize("interval", [None, 15])
def test_initialization_publishes_discovery_readings(monkeypatch: pytest.MonkeyPatch, interval: int | None) -> None:
    """Both modes expose initial readings without repeating successful discovery updates."""
    device = FakeDevice("10.0.0.1", "plug", make_features())
    monkeypatch.setattr(tapo_module.Discover, "discover", AsyncMock(return_value={device.host: device}))
    options = TapoExporterOptions()
    assert options.prometheus_options is not None
    options.prometheus_options.refresh_interval = interval

    async def exercise() -> None:
        exporter = await runtime.tapo_exporter_init(asyncio.get_running_loop(), options)
        try:
            assert device.update_calls == 1
            assert (exporter._update_task is None) == (interval is None)
            metrics = {metric.name: metric for metric in exporter.collect()}
            assert metrics["tapo_discovered_devices"].samples[0].value == 1
            assert metrics["current_consumption"].samples[0].value == pytest.approx(5.0)
        finally:
            await exporter.cleanup()
        assert device.disconnect_calls == 1

    asyncio.run(exercise())
