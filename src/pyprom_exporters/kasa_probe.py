"""important."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import sys
from threading import Event
from typing import TYPE_CHECKING

from kasa import Credentials
from omegaconf import DictConfig, ListConfig, OmegaConf
from prometheus_client import start_http_server
from prometheus_client.registry import REGISTRY

from pyprom_exporters.exporters.tapo import TapoExporterOptions, TapoPowerPlugPrometheusExporter
from pyprom_exporters.task_collector import run_tasks_with_retry

if TYPE_CHECKING:
    from types import FrameType

    from pyprom_exporters.exporters.base import BasePrometheusCollector

logging.basicConfig(level=logging.DEBUG)
fs_log = logging.getLogger()


def load_config(config_path: str) -> DictConfig | ListConfig:
    """Load configuration from a YAML file."""
    try:
        config = OmegaConf.load(config_path)
    except Exception as e:  # noqa: BLE001
        fs_log.exception(f"Failed to load configuration from {config_path}: {e}")
        sys.exit(1)
    else:
        return config


async def inc_async(n: int) -> int:
    """Pretend-I/O task that increments `n` sometimes failing(≈30 % chance) so we can watch the retry logic kick in."""
    await asyncio.sleep(0.05)  # 50 ms I/O delay
    if random.random() < 0.30:  # noqa: S311, PLR2004
        msg = "Transient glitch - please retry"
        raise ValueError(msg)
    return n + 1


async def async_example() -> list[int]:
    nums = [1, 2, 3, 4, 5]
    factories = [lambda n=n: inc_async(n) for n in nums]

    return await run_tasks_with_retry(
        factories,
        concurrency=3,  # ≤3 tasks in flight
        attempts=5,  # 1 try + 4 retries
        delay=0.1,
        backoff=1.5,
        jitter=0.2,
        retry_exceptions=(ValueError,),
    )


async def print_device_info(asyncio_loop: asyncio.AbstractEventLoop) -> None:
    creds = Credentials(username=os.getenv("TP_LINK_USERNAME", ""), password=os.getenv("TP_LINK_PASSWORD", ""))

    tapo_device_set: list[str] = [
        "10.10.2.100",
        "10.10.2.101",
        "10.10.2.102",
        "10.10.2.103",
        "10.10.2.117",
    ]
    # initialise options
    tapo_exporter_options = TapoExporterOptions(devices=tapo_device_set)
    if tapo_exporter_options.discovery_options:
        tapo_exporter_options.discovery_options.credentials = creds
    # now disover devices
    tapo_exporter = TapoPowerPlugPrometheusExporter(options=tapo_exporter_options, asyncio_loop=asyncio_loop)
    await tapo_exporter.discover()
    await tapo_exporter.update()

    if tapo_exporter.discovered_devices is not None:
        for device in tapo_exporter.discovered_devices.values():
            print(f"Device Alias: {device.alias}")
            print(f"Device Host: {device.host}")
            print(f"Device Model: {device.model}")
            print(f"Device Type: {device.device_type}")
            print(f"Device Firmware Version: {device.device_info.firmware_version}")
            print(f"Device Hardware Version: {device.device_info.hardware_version}")
            if device.features is not None and "current_consumption" in device.features:
                print(f"Device Current Consumption: {device.features['current_consumption'].value} W")
            print("-" * 40)

            # dev_obj = tapo_exporter.collect_from_device(device)
            # print(dev_obj)

            # a = 1

        await tapo_exporter.disconnect()

    # dev = await discover_devices()
    # # for addr, dev in devices.items():
    # print(f"Discovered {dev.alias} at {dev.host}")


def cleanup_func(collectors: list[BasePrometheusCollector], asyncio_loop: asyncio.AbstractEventLoop) -> None:
    """Cleanup function to be called on exit."""
    for collector in collectors:
        fs_log.info(f"Cleaning up collector: {collector.__class__.__name__}")
        asyncio_loop.run_until_complete(collector.cleanup())
    # closing the asyncio loop
    fs_log.info("Closing the asyncio event loop...")
    asyncio_loop.close()


def graceful_exit_handler(
    sig_event: Event,
    collectors: list[BasePrometheusCollector],
    asyncio_loop: asyncio.AbstractEventLoop,
) -> None:
    """Set up signal handlers for graceful shutdown."""
    fs_log.info("Setting up signal handlers for graceful shutdown...")

    def _signal_handler(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001
        fs_log.info(f"Received signal {signal.Signals(signum).name} ({signum}), shutting down...")
        cleanup_func(collectors, asyncio_loop)
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
    start_http_server(prom_port)
    for collector in collectors:
        REGISTRY.register(collector)


async def tapo_exporter_init(asyncio_loop: asyncio.AbstractEventLoop) -> TapoPowerPlugPrometheusExporter:
    """Create and return a Tapo Power Plug Prometheus Exporter."""
    tapo_device_dict: list[str] = [
        "10.10.2.100",
        "10.10.2.101",
        "10.10.2.102",
        "10.10.2.103",
        "10.10.2.117",
    ]
    creds = Credentials(username=os.getenv("TP_LINK_USERNAME", ""), password=os.getenv("TP_LINK_PASSWORD", ""))
    tapo_exporter_options = TapoExporterOptions(devices=tapo_device_dict)
    if tapo_exporter_options.discovery_options:
        tapo_exporter_options.discovery_options.credentials = creds
    # now disover devices
    tapo_exporter = TapoPowerPlugPrometheusExporter(options=tapo_exporter_options, asyncio_loop=asyncio_loop)
    await tapo_exporter.discover()
    await tapo_exporter.update()

    return tapo_exporter


def main() -> None:
    exporter_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(exporter_loop)
    exporter_loop.run_until_complete(print_device_info(asyncio_loop=exporter_loop))
    exporter_loop.close()


if __name__ == "__main__":
    # main()

    exporter_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(exporter_loop)
    exporter_list: list[BasePrometheusCollector] = []

    defs = OmegaConf.structured(TapoExporterOptions)
    yy = OmegaConf.to_yaml(defs)
    print(yy)
    # cfg = load_config("config.yaml")  # Load configuration from a YAML file
    node = defs.get("tapo", "default_node")

    try:
        fs_log.info("Starting Tapo Prometheus Exporter...")
        tapo_exporter = exporter_loop.run_until_complete(tapo_exporter_init(exporter_loop))
        exporter_list.append(tapo_exporter)
        termination_sig = Event()
        register_exporters(prom_port=8090, collectors=exporter_list)
        graceful_exit_handler(termination_sig, exporter_list, exporter_loop)
        fs_log.info("Monitoring... waiting for a signal in case of termination...")
    except Exception as e:  # noqa: BLE001
        fs_log.error("An unexpected error occurred execution, details: %s", e)
        cleanup_func(exporter_list, exporter_loop)
        sys.exit(1)
    termination_sig.wait()
    fs_log.debug("Cleanup completed successfully - exiting...")
    sys.exit(0)
