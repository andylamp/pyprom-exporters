# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Configuration and lifecycle management for the Prometheus exporter."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
from contextlib import suppress
from dataclasses import fields
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from typing import TYPE_CHECKING, cast

from kasa import Credentials
from omegaconf import DictConfig, ListConfig, OmegaConf
from prometheus_client import start_http_server
from prometheus_client.registry import REGISTRY

from pyprom_exporters.config import PromExporterConfig
from pyprom_exporters.exporters.tapo import (
    DEFAULT_REFRESH_INTERVAL,
    TapoDiscoveryOptions,
    TapoExporterOptions,
    TapoPowerPlugPrometheusExporter,
)

if TYPE_CHECKING:
    from concurrent.futures import Future
    from types import FrameType
    from wsgiref.simple_server import WSGIServer

    from pyprom_exporters.exporters.base import BasePrometheusCollector

logger = logging.getLogger(__name__)
_UNCHANGED = object()
CLEANUP_TIMEOUT = 10.0
MAX_TCP_PORT = 65535


def _resolve_log_level(value: str | None) -> tuple[int, bool]:
    """Resolve a logging level name/number into a numeric level.

    Parameters
    ----------
    value : str | None
        Level name (e.g., INFO) or integer as a string (e.g., 20).

    Returns
    -------
    tuple[int, bool]
        Numeric level, and whether the input was valid.

    """
    if value is None:
        return logging.INFO, True
    raw = value.strip()
    if not raw:
        return logging.INFO, True
    if raw.isdigit():
        return int(raw), True
    normalized = raw.upper()
    resolved = getattr(logging, normalized, None)
    if isinstance(resolved, int):
        return resolved, True
    return logging.INFO, False


def configure_logging(level: str | None, *, force: bool = False) -> None:
    """Configure root logging for the exporter runtime."""
    resolved_level, valid = _resolve_log_level(level)
    logging.basicConfig(level=resolved_level, force=force)
    if not valid:
        logger.warning("Invalid log level %r; falling back to INFO.", level)


def load_config(config_path: str) -> DictConfig | ListConfig:
    """Load configuration from a YAML file.

    Parameters
    ----------
    config_path : str
        The path to the YAML configuration file.

    Returns
    -------
    DictConfig | ListConfig
        The loaded configuration as a DictConfig or ListConfig.

    """
    try:
        config = OmegaConf.load(config_path)
    except Exception as e:  # ruff: ignore[blind-except]
        # Parser errors can contain source lines with passwords; omit the traceback.
        logger.error(  # ruff: ignore[error-instead-of-exception]
            "Failed to load configuration from %s (%s).",
            config_path,
            type(e).__name__,
        )
        sys.exit(1)
    else:
        return config


def load_app_config(config_path: str) -> tuple[PromExporterConfig, DictConfig | ListConfig, bool]:
    """Load and merge the application configuration from a YAML file.

    Returns
    -------
    tuple[PromExporterConfig, DictConfig | ListConfig, bool]
        The validated application config, merged structured config, and file existence.

    Raises
    ------
    TypeError
        If the YAML document is not a mapping.

    """
    config_p = Path(config_path)
    config_exists = config_p.exists()
    if config_exists:
        config_from_file = load_config(config_path)
    else:
        logger.warning("Config file %s not found, using defaults.", config_path)
        config_from_file = OmegaConf.structured(PromExporterConfig)
    schema = OmegaConf.structured(PromExporterConfig)

    if not isinstance(config_from_file, DictConfig):
        message = "The configuration must be a YAML mapping."
        raise TypeError(message)
    if config_from_file and set(config_from_file).issubset({item.name for item in fields(TapoExporterOptions)}):
        # Backward-compat: wrap legacy exporter-only config under exporters.tapo.
        config_from_file = OmegaConf.create({"exporters": {"tapo": config_from_file}})

    # Resolve credentials after merging so custom environment keys take effect.
    schema.exporters.tapo.discovery_options.credentials = None
    cfg = OmegaConf.merge(schema, config_from_file)
    # Empty option maps explicitly disable families/metrics rather than restoring defaults.
    for key in ("supported_device_families", "per_device_family_metrics.plug"):
        path = f"exporters.tapo.{key}"
        configured = OmegaConf.select(config_from_file, path)
        if isinstance(configured, DictConfig) and not configured:
            OmegaConf.update(cfg, path, {}, merge=False)
    app_config = cast("PromExporterConfig", OmegaConf.to_object(cfg))
    return app_config, cfg, config_exists


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for overriding configuration.

    Returns
    -------
    argparse.Namespace
        Parsed configuration paths and optional runtime overrides.

    """
    parser = argparse.ArgumentParser(description="Run the pyprom-exporters Prometheus exporter.")
    parser.add_argument("--config", default=PromExporterConfig().config_file, help="Path to the YAML configuration.")
    parser.add_argument("--no-write-config", action="store_true", help="Do not persist configuration on startup.")
    parser.add_argument(
        "--tapo-plug-devices",
        nargs="*",
        default=None,
        help="Space or comma-separated list of Tapo plug device IPs.",
    )
    parser.add_argument(
        "--prometheus-port",
        type=int,
        default=None,
        help="Port to bind the Prometheus exporter HTTP server.",
    )
    parser.add_argument("--tapo-username", default=None, help="Tapo device username override.")
    parser.add_argument("--tapo-password", default=None, help="Tapo device password override.")
    parser.add_argument(
        "--log-level",
        default=None,
        help="Python logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    return parser.parse_args()


def _split_devices(values: list[str]) -> list[str]:
    devices: list[str] = []
    for value in values:
        devices.extend([item.strip() for item in value.split(",") if item.strip()])
    return devices


def _split_devices_from_string(value: str) -> list[str]:
    return [item for item in value.replace(",", " ").split() if item.strip()]


def apply_env_overrides(app_config: PromExporterConfig) -> None:
    """Apply environment variable overrides to the application configuration."""
    env_log_level = os.getenv("PYPROM_EXPORTERS_LOG_LEVEL") or os.getenv("LOG_LEVEL")
    if env_log_level:
        app_config.log_level = env_log_level

    env_port = os.getenv("PROMETHEUS_PORT")
    if env_port:
        try:
            app_config.prometheus_port = int(env_port)
        except ValueError:
            logger.warning("Invalid PROMETHEUS_PORT value: %s", env_port)

    env_devices = os.getenv("TAPO_PLUG_DEVICES")
    if env_devices:
        app_config.exporters.tapo.devices = _split_devices_from_string(env_devices)

    env_username = os.getenv("TAPO_USERNAME")
    env_password = os.getenv("TAPO_PASSWORD")
    if env_username is not None or env_password is not None:
        if app_config.exporters.tapo.discovery_options is None:
            app_config.exporters.tapo.discovery_options = TapoDiscoveryOptions()

        creds = app_config.exporters.tapo.discovery_options.credentials
        username = env_username if env_username is not None else (creds.username if creds else "")
        password = env_password if env_password is not None else (creds.password if creds else "")
        app_config.exporters.tapo.discovery_options.credentials = Credentials(username=username, password=password)


def apply_cli_overrides(app_config: PromExporterConfig, args: argparse.Namespace) -> None:
    """Apply CLI overrides to the application configuration."""
    if getattr(args, "log_level", None) is not None:
        app_config.log_level = args.log_level

    if args.prometheus_port is not None:
        app_config.prometheus_port = args.prometheus_port

    if args.tapo_plug_devices is not None:
        app_config.exporters.tapo.devices = _split_devices(args.tapo_plug_devices)

    if args.tapo_username is not None or args.tapo_password is not None:
        if app_config.exporters.tapo.discovery_options is None:
            app_config.exporters.tapo.discovery_options = TapoDiscoveryOptions()

        creds = app_config.exporters.tapo.discovery_options.credentials
        username = args.tapo_username if args.tapo_username is not None else (creds.username if creds else "")
        password = args.tapo_password if args.tapo_password is not None else (creds.password if creds else "")
        app_config.exporters.tapo.discovery_options.credentials = Credentials(username=username, password=password)


def _diff_config_values(current: object, defaults: object) -> object:
    if isinstance(current, dict) and isinstance(defaults, dict):
        current_dict = cast("dict[str, object]", current)
        defaults_dict = cast("dict[str, object]", defaults)
        if not current_dict and defaults_dict:
            return {}
        diff: dict[str, object] = {}
        for key, value in current_dict.items():
            if key in defaults_dict:
                nested = _diff_config_values(value, defaults_dict[key])
                if nested is not _UNCHANGED:
                    diff[key] = nested
            else:
                diff[key] = value
        return diff or _UNCHANGED

    if isinstance(current, list) and isinstance(defaults, list):
        return current if current != defaults else _UNCHANGED

    return current if current != defaults else _UNCHANGED


def _scrub_sensitive_config(config: object) -> object:
    if isinstance(config, dict):
        scrubbed: dict[object, object] = {}
        for key, value in config.items():
            if key == "credentials":
                continue
            scrubbed[key] = _scrub_sensitive_config(value)
        return scrubbed
    if isinstance(config, list):
        return [_scrub_sensitive_config(item) for item in config]
    return config


def _serialize_config(config: DictConfig | ListConfig, *, minimal: bool) -> str:
    """Serialize configuration after removing credential values.

    Returns
    -------
    str
        The scrubbed configuration, optionally limited to non-default values, as YAML.

    """
    current_container = OmegaConf.to_container(config, resolve=True)
    if minimal:
        defaults = OmegaConf.structured(PromExporterConfig)
        defaults_container = OmegaConf.to_container(defaults, resolve=True)
        diff = _diff_config_values(current_container, defaults_container)
        current_container = {} if diff is _UNCHANGED else diff
    return OmegaConf.to_yaml(_scrub_sensitive_config(current_container))


def _write_config_atomically(config_path: Path, content: str) -> None:
    """Replace a config file only after writing and flushing its complete contents."""
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=config_path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(config_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_config(
    config: DictConfig | ListConfig,
    config_path: str | Path,
    *,
    minimal: bool = False,
) -> None:
    """Write the OmegaConf configuration to a YAML file.

    Parameters
    ----------
    config : DictConfig | ListConfig
        The configuration object to write.
    config_path : str | Path
        The path to the output YAML file.
    minimal : bool, optional
        If True, write only configuration values that differ from the defaults.

    """
    try:
        yaml_content = _serialize_config(config, minimal=minimal)
        _write_config_atomically(Path(config_path), yaml_content)
        logger.info("Successfully wrote merged configuration to %s", config_path)
    except Exception as e:  # ruff: ignore[blind-except]
        # Serialization errors can expose credentials; report the error class only.
        logger.error(  # ruff: ignore[error-instead-of-exception]
            "Failed to write configuration to %s (%s).",
            config_path,
            type(e).__name__,
        )
        sys.exit(1)


async def _cleanup_collectors(collectors: list[BasePrometheusCollector]) -> None:
    results = await asyncio.gather(*(collector.cleanup() for collector in collectors), return_exceptions=True)
    for collector, result in zip(collectors, results, strict=True):
        if isinstance(result, BaseException):
            logger.error("Failed to clean up %s: %s", type(collector).__name__, result)


def cleanup_func(
    collectors: list[BasePrometheusCollector],
    asyncio_loop: asyncio.AbstractEventLoop,
    loop_thread: threading.Thread | None,
) -> None:
    """Unregister collectors and stop their event loop with bounded cleanup."""
    for collector in collectors:
        with suppress(KeyError):
            REGISTRY.unregister(collector)
    if asyncio_loop.is_closed():
        return
    if asyncio_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_cleanup_collectors(collectors), asyncio_loop)
        try:
            future.result(timeout=CLEANUP_TIMEOUT)
        except Exception:
            future.cancel()
            logger.exception("Failed during cleanup.")
        asyncio_loop.call_soon_threadsafe(asyncio_loop.stop)
    else:
        try:
            asyncio_loop.run_until_complete(asyncio.wait_for(_cleanup_collectors(collectors), CLEANUP_TIMEOUT))
        except Exception:
            logger.exception("Failed during cleanup.")
    if loop_thread is not None:
        loop_thread.join(timeout=CLEANUP_TIMEOUT + 1)
    # A loop may briefly report not running between its shutdown stages.
    if loop_thread is not None and loop_thread.is_alive():
        logger.error("The asyncio thread did not stop before the cleanup deadline.")
        return
    if not asyncio_loop.is_running() and not asyncio_loop.is_closed():
        asyncio_loop.close()


def graceful_exit_handler(sig_event: Event) -> None:
    """Request shutdown on SIGINT/SIGTERM without blocking inside a signal handler."""

    def _signal_handler(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s, shutting down...", signal.Signals(signum).name)
        sig_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


def register_exporters(
    prom_port: int, collectors: list[BasePrometheusCollector]
) -> tuple[WSGIServer, threading.Thread]:
    """Register collectors and start HTTP serving, rolling back on startup failure.

    Returns
    -------
    tuple[WSGIServer, threading.Thread]
        The running HTTP server and its serving thread.

    """
    registered: list[BasePrometheusCollector] = []
    try:
        for collector in collectors:
            REGISTRY.register(collector)
            registered.append(collector)
        logger.info("Starting Prometheus HTTP server on port %s", prom_port)
        return start_http_server(prom_port)
    except BaseException:
        for collector in registered:
            REGISTRY.unregister(collector)
        raise


def _get_collector_hosts(collector: BasePrometheusCollector) -> set[str]:
    """Extract known device hosts for a collector.

    Returns
    -------
    set[str]
        Discovered hosts, or configured hosts when discovery data is unavailable.

    """
    discovered_devices = getattr(collector, "discovered_devices", None)
    if isinstance(discovered_devices, dict):
        return {str(host) for host in discovered_devices}

    options = getattr(collector, "options", None)
    configured_devices = getattr(options, "devices", None)
    if isinstance(configured_devices, list):
        return {str(host) for host in configured_devices}

    return set()


def _get_collector_refresh_interval(collector: BasePrometheusCollector) -> int | str | None:
    """Extract refresh interval from collector options.

    Returns
    -------
    int | str | None
        The interval, None for scrape-triggered updates, or "unknown" if unavailable.

    """
    options = getattr(collector, "options", None)
    prometheus_options = getattr(options, "prometheus_options", None)
    if prometheus_options is None:
        return "unknown"

    refresh_interval = getattr(prometheus_options, "refresh_interval", "unknown")
    if refresh_interval is None or isinstance(refresh_interval, int):
        return refresh_interval
    return "unknown"


def log_startup_summary(collectors: list[BasePrometheusCollector]) -> None:
    """Log a readable startup summary with key runtime values."""
    exporter_names = [collector.__class__.__name__ for collector in collectors]
    hosts: set[str] = set()
    for collector in collectors:
        hosts.update(_get_collector_hosts(collector))

    logger.info(
        ("Startup summary:\n  Registered exporters   : %s (%s)\n  Total devices to scrape: %s"),
        len(exporter_names),
        ", ".join(exporter_names) if exporter_names else "none",
        len(hosts),
    )
    for collector in collectors:
        collector_name = collector.__class__.__name__
        refresh_interval = _get_collector_refresh_interval(collector)
        if isinstance(refresh_interval, int):
            logger.info(
                "Collector %s automatic polling is enabled (refresh_interval=%ss).",
                collector_name,
                refresh_interval,
            )
        elif refresh_interval is None:
            logger.info(
                "Collector %s automatic polling is disabled (refresh_interval=None); refreshing on scrape.",
                collector_name,
            )
        else:
            logger.info(
                "Collector %s automatic polling configuration is unavailable.",
                collector_name,
            )


async def tapo_exporter_init(
    asyncio_loop: asyncio.AbstractEventLoop,
    options: TapoExporterOptions,
) -> TapoPowerPlugPrometheusExporter:
    """Create and return a Tapo Power Plug Prometheus Exporter.

    Returns
    -------
    TapoPowerPlugPrometheusExporter
        The initialized exporter with discovery and optional background polling started.

    """
    if options.discovery_options and options.discovery_options.credentials is None:
        options.discovery_options.credentials = Credentials(
            username=os.getenv(options.discovery_options.tapo_username_env_key, ""),
            password=os.getenv(options.discovery_options.tapo_password_env_key, ""),
        )

    tapo_exporter = TapoPowerPlugPrometheusExporter(options=options, asyncio_loop=asyncio_loop)
    try:
        await tapo_exporter.discover()
        refresh_interval = (
            options.prometheus_options.refresh_interval if options.prometheus_options else DEFAULT_REFRESH_INTERVAL
        )
        if refresh_interval is not None:
            await tapo_exporter.update_and_collect()
        await tapo_exporter.start_background_updates(refresh_interval)
    except BaseException:
        await tapo_exporter.cleanup()
        raise
    return tapo_exporter


async def _cancel_pending_tasks() -> None:
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    for task in pending:
        if not task.cancelling():
            task.cancel()
    if pending:
        _, unfinished = await asyncio.wait(pending, timeout=CLEANUP_TIMEOUT)
        if unfinished:
            logger.error("%s tasks did not stop before shutdown.", len(unfinished))


def _run_event_loop(asyncio_loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(asyncio_loop)
    try:
        asyncio_loop.run_forever()
    finally:
        asyncio_loop.run_until_complete(_cancel_pending_tasks())
        asyncio_loop.run_until_complete(asyncio.wait_for(asyncio_loop.shutdown_asyncgens(), CLEANUP_TIMEOUT))
        asyncio_loop.close()


def _wait_for_startup(
    future: Future[TapoPowerPlugPrometheusExporter], termination_sig: Event
) -> TapoPowerPlugPrometheusExporter | None:
    while not future.done():
        if termination_sig.wait(timeout=0.1) and future.cancel():
            return None
    return future.result()


def _validate_port(port: int) -> None:
    if not 1 <= port <= MAX_TCP_PORT:
        message = f"prometheus_port must be between 1 and {MAX_TCP_PORT}."
        raise ValueError(message)


def _load_effective_config(args: argparse.Namespace) -> PromExporterConfig:
    """Load defaults and apply runtime overrides in precedence order.

    Returns
    -------
    PromExporterConfig
        The merged configuration with a validated HTTP port.

    """
    app_config, _, _ = load_app_config(args.config)
    apply_env_overrides(app_config)
    apply_cli_overrides(app_config, args)
    _validate_port(app_config.prometheus_port)
    return app_config


def _configure_app(args: argparse.Namespace) -> PromExporterConfig:
    """Apply configuration precedence and persist only validated, scrubbed settings.

    Returns
    -------
    PromExporterConfig
        Validated configuration with environment and CLI overrides applied.

    Raises
    ------
    SystemExit
        With status 1 if loading, applying, or validating configuration fails.

    """
    try:
        app_config = _load_effective_config(args)
        configure_logging(app_config.log_level, force=True)
        if not args.no_write_config:
            write_config(OmegaConf.structured(app_config), args.config, minimal=app_config.write_non_default_config)
    except Exception:  # ruff: ignore[blind-except]
        # OmegaConf errors may embed credential values; do not log configuration contents.
        logger.error(  # ruff: ignore[error-instead-of-exception]
            "Invalid exporter configuration. Check the YAML structure and option values.",
        )
        raise SystemExit(1) from None

    return app_config


def main() -> None:
    """Run the exporter and release resources on startup failure or termination.

    Raises
    ------
    SystemExit
        With status 1 if exporter startup or execution fails.

    """
    args = parse_args()
    early_log_level = (
        args.log_level
        or os.getenv("PYPROM_EXPORTERS_LOG_LEVEL")
        or os.getenv("LOG_LEVEL")
        or PromExporterConfig().log_level
    )
    configure_logging(early_log_level, force=True)
    app_config = _configure_app(args)

    exporter_loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=_run_event_loop, args=(exporter_loop,), daemon=True)
    exporter_list: list[BasePrometheusCollector] = []
    termination_sig = Event()
    http_server: WSGIServer | None = None
    http_thread: threading.Thread | None = None
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    # Every startup step belongs to the same cleanup scope, including partial initialization.
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        graceful_exit_handler(termination_sig)
        loop_thread.start()
        logger.info("Starting Tapo Prometheus Exporter...")
        tapo_future = asyncio.run_coroutine_threadsafe(
            tapo_exporter_init(exporter_loop, app_config.exporters.tapo), exporter_loop
        )
        tapo_exporter = _wait_for_startup(tapo_future, termination_sig)
        if tapo_exporter is None:
            return
        exporter_list.append(tapo_exporter)
        if termination_sig.is_set():
            return
        http_server, http_thread = register_exporters(app_config.prometheus_port, exporter_list)
        log_startup_summary(exporter_list)
        logger.info("Monitoring... waiting for a termination signal...")
        termination_sig.wait()
    except Exception:
        # This CLI boundary reports arbitrary device failures while always releasing resources.
        logger.exception("Exporter execution failed.")
        raise SystemExit(1) from None
    finally:
        try:
            if http_server is not None:
                try:
                    http_server.shutdown()
                finally:
                    http_server.server_close()
            if http_thread is not None:
                http_thread.join(timeout=CLEANUP_TIMEOUT)
        finally:
            try:
                cleanup_func(exporter_list, exporter_loop, loop_thread if loop_thread.ident is not None else None)
            finally:
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)


if __name__ == "__main__":
    main()
