"""The Prometheus exporter stub."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path
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
    from types import FrameType

    from pyprom_exporters.exporters.base import BasePrometheusCollector

fs_log = logging.getLogger(__name__)


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
        fs_log.warning("Invalid log level %r; falling back to INFO.", level)


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
    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        fs_log.exception("Failed to load configuration from %s: %s", config_path, e)
        sys.exit(1)
    else:
        return config


def load_app_config(config_path: str) -> tuple[PromExporterConfig, DictConfig | ListConfig, bool]:
    """Load and merge the application configuration from a YAML file."""
    config_p = Path(config_path)
    config_exists = config_p.exists()
    if config_exists:
        config_from_file = load_config(config_path)
    else:
        fs_log.warning("Config file %s not found, using defaults.", config_path)
        config_from_file = OmegaConf.structured(PromExporterConfig)
    schema = OmegaConf.structured(PromExporterConfig)

    if isinstance(config_from_file, DictConfig) and "exporters" not in config_from_file:
        # Backward-compat: wrap legacy exporter-only config under exporters.tapo.
        config_from_file = OmegaConf.create({"exporters": {"tapo": config_from_file}})

    cfg = OmegaConf.merge(schema, config_from_file)
    app_config = cast("PromExporterConfig", OmegaConf.to_object(cfg))
    return app_config, cfg, config_exists


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for overriding configuration."""
    parser = argparse.ArgumentParser(description="Run the pyprom-exporters Prometheus exporter.")
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
            fs_log.warning("Invalid PROMETHEUS_PORT value: %s", env_port)

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


def _diff_config_values(current: object, defaults: object) -> object | None:
    if isinstance(current, dict) and isinstance(defaults, dict):
        diff: dict[object, object] = {}
        for key, value in current.items():
            if key in defaults:
                nested = _diff_config_values(value, defaults[key])
                if nested is not None:
                    diff[key] = nested
            else:
                diff[key] = value
        return diff or None

    if isinstance(current, list) and isinstance(defaults, list):
        return current if current != defaults else None

    return current if current != defaults else None


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
        config_p = Path(config_path)
        if minimal:
            defaults = OmegaConf.structured(PromExporterConfig)
            current_container = OmegaConf.to_container(config, resolve=True)
            defaults_container = OmegaConf.to_container(defaults, resolve=True)
            diff = _diff_config_values(current_container, defaults_container)
            yaml_content = OmegaConf.to_yaml(_scrub_sensitive_config(diff or {}))
        else:
            current_container = OmegaConf.to_container(config, resolve=True)
            yaml_content = OmegaConf.to_yaml(_scrub_sensitive_config(current_container))
        config_p.write_text(yaml_content, encoding="utf-8")
        fs_log.info("Successfully wrote merged configuration to %s", config_path)
    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        fs_log.exception("Failed to write configuration to %s: %s", config_path, e)
        sys.exit(1)


def cleanup_func(
    collectors: list[BasePrometheusCollector],
    asyncio_loop: asyncio.AbstractEventLoop,
    loop_thread: threading.Thread | None,
) -> None:
    """Cleanup function to be called on exit."""
    for collector in collectors:
        fs_log.info("Cleaning up collector: %s", collector.__class__.__name__)
        future = asyncio.run_coroutine_threadsafe(collector.cleanup(), asyncio_loop)
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            fs_log.error("Failed during cleanup: %s", exc)
    # closing the asyncio loop
    fs_log.info("Stopping the asyncio event loop...")
    asyncio_loop.call_soon_threadsafe(asyncio_loop.stop)
    if loop_thread is not None:
        loop_thread.join(timeout=5)
    if not asyncio_loop.is_closed():
        fs_log.info("Closing the asyncio event loop...")
        asyncio_loop.close()


def graceful_exit_handler(
    sig_event: Event,
    collectors: list[BasePrometheusCollector],
    asyncio_loop: asyncio.AbstractEventLoop,
    loop_thread: threading.Thread | None,
) -> None:
    """Set up signal handlers for graceful shutdown."""
    fs_log.info("Setting up signal handlers for graceful shutdown...")

    def _signal_handler(signum: int, _frame: FrameType | None) -> None:
        fs_log.info("Received signal %s (%s), shutting down...", signal.Signals(signum).name, signum)
        cleanup_func(collectors, asyncio_loop, loop_thread)
        fs_log.info("Signaling termination...")
        sig_event.set()

    # Register signal handlers for SIGINT and SIGTERM
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    fs_log.info("Signal handlers set up successfully.")


def register_exporters(prom_port: int, collectors: list[BasePrometheusCollector]) -> None:
    """Register Prometheus exporters.

    Parameters
    ----------
    prom_port : int
        The port to run the Prometheus HTTP server on.
    collectors : list[BasePrometheusCollector]
        The list of collectors to register.

    """
    fs_log.info("Starting Prometheus HTTP server on port %s", prom_port)
    start_http_server(prom_port)
    for collector in collectors:
        REGISTRY.register(collector)


def _get_collector_hosts(collector: BasePrometheusCollector) -> set[str]:
    """Extract known device hosts for a collector."""
    discovered_devices = getattr(collector, "discovered_devices", None)
    if isinstance(discovered_devices, dict):
        return {str(host) for host in discovered_devices}

    options = getattr(collector, "options", None)
    configured_devices = getattr(options, "devices", None)
    if isinstance(configured_devices, list):
        return {str(host) for host in configured_devices}

    return set()


def _get_collector_refresh_interval(collector: BasePrometheusCollector) -> int | None | str:
    """Extract refresh interval from collector options."""
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

    fs_log.info(
        ("Startup summary:\n  Registered exporters   : %s (%s)\n  Total devices to scrape: %s"),
        len(exporter_names),
        ", ".join(exporter_names) if exporter_names else "none",
        len(hosts),
    )
    for collector in collectors:
        collector_name = collector.__class__.__name__
        refresh_interval = _get_collector_refresh_interval(collector)
        if isinstance(refresh_interval, int):
            fs_log.info(
                "Collector %s automatic polling is enabled (refresh_interval=%ss).",
                collector_name,
                refresh_interval,
            )
        elif refresh_interval is None:
            fs_log.info(
                "Collector %s automatic polling is disabled (refresh_interval=None); refreshing on scrape.",
                collector_name,
            )
        else:
            fs_log.info(
                "Collector %s automatic polling configuration is unavailable.",
                collector_name,
            )


async def tapo_exporter_init(
    asyncio_loop: asyncio.AbstractEventLoop,
    options: TapoExporterOptions,
) -> TapoPowerPlugPrometheusExporter:
    """Create and return a Tapo Power Plug Prometheus Exporter."""
    if options.discovery_options and options.discovery_options.credentials is None:
        options.discovery_options.credentials = Credentials(
            username=os.getenv(options.discovery_options.tapo_username_env_key, ""),
            password=os.getenv(options.discovery_options.tapo_password_env_key, ""),
        )

    tapo_exporter = TapoPowerPlugPrometheusExporter(options=options, asyncio_loop=asyncio_loop)
    await tapo_exporter.discover()
    refresh_interval: int | None = DEFAULT_REFRESH_INTERVAL
    if options.prometheus_options is not None:
        refresh_interval = options.prometheus_options.refresh_interval
    if refresh_interval is not None:
        await tapo_exporter.update_and_collect()
    await tapo_exporter.start_background_updates(refresh_interval)

    return tapo_exporter


def _run_event_loop(asyncio_loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(asyncio_loop)
    asyncio_loop.run_forever()


def main() -> None:
    """Primary entry point for the Prometheus exporter."""
    args = parse_args()

    early_log_level = (
        getattr(args, "log_level", None)
        or os.getenv("PYPROM_EXPORTERS_LOG_LEVEL")
        or os.getenv("LOG_LEVEL")
        or PromExporterConfig().log_level
    )
    configure_logging(early_log_level, force=True)

    config_path = PromExporterConfig().config_file
    app_config, cfg, config_exists = load_app_config(config_path)
    if not config_exists:
        write_config(cfg, config_path, minimal=app_config.write_non_default_config)
    apply_env_overrides(app_config)
    apply_cli_overrides(app_config, args)
    configure_logging(app_config.log_level, force=True)
    cfg = OmegaConf.structured(app_config)
    if config_exists:
        write_config(cfg, config_path, minimal=app_config.write_non_default_config)
    exporter_loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=_run_event_loop, args=(exporter_loop,), daemon=True)
    loop_thread.start()
    exporter_list: list[BasePrometheusCollector] = []

    try:
        fs_log.info("Starting Tapo Prometheus Exporter...")
        tapo_future = asyncio.run_coroutine_threadsafe(
            tapo_exporter_init(exporter_loop, app_config.exporters.tapo),
            exporter_loop,
        )
        tapo_exporter = tapo_future.result()
        exporter_list.append(tapo_exporter)
        termination_sig = Event()
        register_exporters(prom_port=app_config.prometheus_port, collectors=exporter_list)
        log_startup_summary(exporter_list)
        graceful_exit_handler(termination_sig, exporter_list, exporter_loop, loop_thread)
        fs_log.info("Monitoring... waiting for a signal in case of termination...")
    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        fs_log.error("An unexpected error occurred execution, details: %s", e)
        cleanup_func(exporter_list, exporter_loop, loop_thread)
        sys.exit(1)
    termination_sig.wait()
    fs_log.debug("Cleanup completed successfully - exiting...")
    sys.exit(0)


if __name__ == "__main__":
    main()
