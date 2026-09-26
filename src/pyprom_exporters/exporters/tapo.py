# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Tapo Exporter for Prometheus."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from typing import TYPE_CHECKING, TypeVar

from kasa import Credentials, Device
from kasa.discover import DeviceDict, Discover, OnDiscoveredCallable, OnDiscoveredRawCallable, OnUnsupportedCallable
from kasa.exceptions import SMART_AUTHENTICATION_ERRORS, AuthenticationError, DeviceError
from prometheus_client.metrics_core import GaugeMetricFamily, Metric

from . import run_tasks_with_retry
from .base import BasePrometheusCollector, BasePrometheusOptions

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from concurrent.futures import Future


logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_REFRESH_INTERVAL: int = 1
REDISCOVERY_INTERVAL: float = 30.0


@dataclass
class TapoPrometheusOptions(BasePrometheusOptions):
    """Options for the Tapo Prometheus exporter."""

    refresh_interval: int | None = DEFAULT_REFRESH_INTERVAL
    """Refresh interval in seconds; set to ``None`` to refresh on scrape."""
    scrape_timeout: float = 10.0
    """Maximum seconds a scrape waits for a device refresh."""

    def __post_init__(self) -> None:
        """Reject polling configurations that would hang scrapes or busy-loop.

        Raises
        ------
        ValueError
            If the refresh interval or scrape timeout cannot be used safely.

        """
        if self.refresh_interval is not None and (
            not math.isfinite(self.refresh_interval) or self.refresh_interval <= 0
        ):
            message = "refresh_interval must be positive or None"
            raise ValueError(message)
        if not math.isfinite(self.scrape_timeout) or self.scrape_timeout <= 0:
            message = "scrape_timeout must be finite and positive"
            raise ValueError(message)


@dataclass
class TapoCallbacks:
    """Callbacks for Tapo device discovery."""

    on_discovered: OnDiscoveredCallable | None = None
    """Callback for when a device is discovered."""
    on_discovered_raw: OnDiscoveredRawCallable | None = None
    """Callback for when a raw device is discovered."""
    on_unsupported: OnUnsupportedCallable | None = None
    """Callback for when an unsupported device is discovered."""


@dataclass
class TapoDiscoveryOptions:
    """Options for discovering Tapo devices on the network.

    Mirrors the parameters from the python-kasa package.
    """

    perform_discovery: bool = True
    """Whether to perform discovery, default is True."""
    target: str = "255.255.255.255"
    """The target address for discovery, default is broadcast address."""
    discovery_timeout: int = 5
    """Timeout for discovery in seconds."""
    discovery_packets: int = 3
    """The number of discovery packets to send."""
    interface: str | None = None
    """The network interface to use for discovery, if None, the default interface will be used."""
    credentials: Credentials | None = None
    """Credentials for accessing the Tapo devices, if required."""
    port: int | None = None
    """Port to use for discovery, if None, the default port will be used."""
    timeout: int | None = None
    """Timeout for querying devices in seconds, if None, the default timeout will be used."""
    with_update: bool = True
    """Whether to update the device after discovery, default is True."""
    current_consumption_key: str = "current_consumption"
    """Key for current consumption in the device data, default is 'current_consumption'."""
    tapo_username_env_key: str = "TP_LINK_USERNAME"
    """Key for Tapo username in the device data, default is 'TP_LINK_USERNAME'."""
    tapo_password_env_key: str = "TP_LINK_PASSWORD"  # ruff: ignore[hardcoded-password-string]
    """Key for Tapo password in the device data, default is 'TP_LINK_PASSWORD'."""

    def __post_init__(self) -> None:
        """Post-initialization to ensure credentials are set."""
        if self.credentials is None:
            self.credentials = Credentials(
                username=os.getenv(self.tapo_username_env_key, ""),
                password=os.getenv(self.tapo_password_env_key, ""),
            )


@dataclass
class TapoPlugGaugeMetric:
    """Metric for Tapo Plug device."""

    name: str
    """The name of the metric to extract."""
    documentation: str = ""
    """The documentation for metric, if available."""
    labels: list[str] = field(default_factory=lambda: ["host", "alias"])
    """Device metadata labels; ``host`` is required to keep device series unique."""

    def get_metric(self, value: float | None = None) -> GaugeMetricFamily:
        """Get the metric as a GaugeMetricFamily.

        Parameters
        ----------
        value : float | None
            The value of the metric to be set -- only set it if you want to have a _constant_ value for the metric.

        Returns
        -------
        GaugeMetricFamily
            The metric as a GaugeMetricFamily object.

        """
        return GaugeMetricFamily(
            name=self.name,
            documentation=self.documentation,
            labels=self.labels,
            value=value,
        )

    def get_metric_with_value(self, dump: TapoPlugDeviceDump, labels: list[str]) -> GaugeMetricFamily:
        """Get the metric with value from the device dump.

        Parameters
        ----------
        dump : TapoPlugDeviceDump
            The device dump to extract the value from.
        labels : list[str]
            Label values for the metric.

        Returns
        -------
        GaugeMetricFamily
            The metric with the value extracted from the device dump.

        """
        value = self.get_value(dump)
        metric = self.get_metric()
        if value is not None:
            metric.add_metric(labels, value)
        return metric

    def get_value(self, dump: TapoPlugDeviceDump) -> float | None:
        """Get the value of the metric from the device dump.

        Parameters
        ----------
        dump : TapoPlugDeviceDump
            The device dump to extract the value from.

        Returns
        -------
        float | None
            The value of the metric, or None if not available.

        """
        return getattr(dump, self.name, None)


class TapoDeviceFamily(StrEnum):
    """Supported Tapo device families."""

    PLUG = "plug"
    """Smart plugs."""


class TapoPerPlugMetricType(StrEnum):
    """Enum for Tapo Plug metric types."""

    CURRENT_VOLTAGE = "current_voltage"
    """Current voltage in volts."""
    CURRENT_CURRENT = "current_current"
    """Current current in amps."""
    CURRENT_RSSI = "current_rssi"
    """Current RSSI (Received Signal Strength Indicator)."""
    CURRENT_MONTH_CONSUMPTION = "current_month_consumption"
    """Energy consumed this month in watt-hours."""
    CURRENT_TODAY_CONSUMPTION = "current_consumption_today"
    """Energy consumed today in watt-hours."""
    CURRENT_CONSUMPTION = "current_consumption"
    """Current consumption in watts."""


DEFAULT_PER_PLUG_METRICS: dict[TapoPerPlugMetricType, TapoPlugGaugeMetric] = {
    TapoPerPlugMetricType.CURRENT_VOLTAGE: TapoPlugGaugeMetric(
        name=TapoPerPlugMetricType.CURRENT_VOLTAGE.value,
        documentation="Current voltage in volts",
    ),
    TapoPerPlugMetricType.CURRENT_CURRENT: TapoPlugGaugeMetric(
        name=TapoPerPlugMetricType.CURRENT_CURRENT.value,
        documentation="Current current in amps",
    ),
    TapoPerPlugMetricType.CURRENT_RSSI: TapoPlugGaugeMetric(
        name=TapoPerPlugMetricType.CURRENT_RSSI.value,
        documentation="Current RSSI (Received Signal Strength Indicator)",
    ),
    TapoPerPlugMetricType.CURRENT_MONTH_CONSUMPTION: TapoPlugGaugeMetric(
        name=TapoPerPlugMetricType.CURRENT_MONTH_CONSUMPTION.value,
        documentation="Energy consumed this month in watt-hours",
    ),
    TapoPerPlugMetricType.CURRENT_TODAY_CONSUMPTION: TapoPlugGaugeMetric(
        name=TapoPerPlugMetricType.CURRENT_TODAY_CONSUMPTION.value,
        documentation="Energy consumed today in watt-hours",
    ),
    TapoPerPlugMetricType.CURRENT_CONSUMPTION: TapoPlugGaugeMetric(
        name=TapoPerPlugMetricType.CURRENT_CONSUMPTION.value,
        documentation="Current consumption in watts",
    ),
}


@dataclass
class TapoDeviceFamilyMetrics:
    """Metrics grouped by device family."""

    plug: dict[TapoPerPlugMetricType, TapoPlugGaugeMetric] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_PER_PLUG_METRICS),
    )
    """Metrics to export for Tapo plug devices."""


@dataclass
class TapoExporterOptions:
    """Options for the Tapo exporter."""

    devices: list[str] = field(default_factory=list)
    """A list of Tapo devices to be monitored without discovery."""
    prometheus_options: TapoPrometheusOptions | None = None
    """Internal variable for holding the base Prometheus exporter options."""
    discovery_options: TapoDiscoveryOptions | None = None
    """Internal variable for holding the tapo discovery options."""
    supported_device_families: dict[TapoDeviceFamily, bool] | None = None
    """Device families to collect metrics from, keyed by family."""
    max_concurrent_devices: int = 10
    """Maximum simultaneous device discovery, update, or disconnect operations."""
    per_device_family_metrics: TapoDeviceFamilyMetrics | None = None
    """Metrics to be collected per device family."""

    def __post_init__(self) -> None:
        """Post-initialization to ensure discovery options are set.

        Raises
        ------
        ValueError
            If the device concurrency limit is not a positive integer.

        """
        if (
            isinstance(self.max_concurrent_devices, bool)
            or not isinstance(self.max_concurrent_devices, int)
            or self.max_concurrent_devices < 1
        ):
            message = "max_concurrent_devices must be a positive integer"
            raise ValueError(message)
        if self.discovery_options is None:
            self.discovery_options = TapoDiscoveryOptions()

        if self.prometheus_options is None:
            self.prometheus_options = TapoPrometheusOptions()

        self._normalize_device_families()

        if self.per_device_family_metrics is None:
            self.per_device_family_metrics = TapoDeviceFamilyMetrics()
        self._validate_metric_definitions()

    def _normalize_device_families(self) -> None:
        """Convert configured family names into the supported enum values."""
        if self.supported_device_families is None:
            self.supported_device_families = {TapoDeviceFamily.PLUG: True}
        else:
            normalized_families: dict[TapoDeviceFamily, bool] = {}
            for family, enabled in self.supported_device_families.items():
                if isinstance(family, TapoDeviceFamily):
                    normalized_families[family] = bool(enabled)
                    continue
                try:
                    normalized_families[TapoDeviceFamily(str(family))] = bool(enabled)
                except ValueError:
                    logger.warning("Skipping unknown device family: %s", family)
            self.supported_device_families = normalized_families

    def _validate_metric_definitions(self) -> None:
        """Require unique names and labels that identify each device.

        Raises
        ------
        ValueError
            If metric names repeat or labels cannot identify devices uniquely.

        """
        if self.per_device_family_metrics is None:
            return
        names = {"tapo_discovered_devices"}
        allowed_labels = {"host", "alias", "model", "device_type", "firmware_version", "hardware_version"}
        for metric in self.per_device_family_metrics.plug.values():
            if metric.name in names:
                message = f"Duplicate metric name: {metric.name}"
                raise ValueError(message)
            names.add(metric.name)
            if (
                "host" not in metric.labels
                or len(set(metric.labels)) != len(metric.labels)
                or not set(metric.labels) <= allowed_labels
            ):
                message = f"Unsupported or duplicate metric labels: {metric.labels}"
                raise ValueError(message)


@dataclass
class TapoPlugDeviceDump:
    """Model for dumping Tapo Plug device information."""

    host: str
    """The IP address of the Tapo device."""
    alias: str | None = None
    """The alias of the Tapo device, if available."""
    model: str | None = None
    """The model of the Tapo device, if available."""
    device_type: str | None = None
    """The type of the Tapo device, if available."""
    firmware_version: str | None = None
    """The firmware version of the Tapo device, if available."""
    hardware_version: str | None = None
    """The hardware version of the Tapo device, if available."""
    current_consumption: float | None = None
    """The current consumption of the Tapo device in watts, if available."""
    current_voltage: float | None = None
    """The current voltage of the Tapo device in volts, if available."""
    current_current: float | None = None
    """The current current of the Tapo device in amps, if available."""
    current_consumption_today: float | None = None
    """The total energy consumed today by the Tapo device in watt-hours, if available."""
    current_month_consumption: float | None = None
    """The total energy consumed this month by the Tapo device in watt-hours, if available."""
    current_rssi: float | None = None
    """The current RSSI (Received Signal Strength Indicator) of the Tapo device, if available."""


@dataclass
class TapoDeviceUpdateResult:
    """Result of attempting to update a device."""

    host: str | None
    auth_failed: bool | None = None


class TapoPowerPlugPrometheusExporter(BasePrometheusCollector):
    """Exporter with bounded device I/O and thread-safe metric snapshots."""

    def __init__(
        self,
        asyncio_loop: asyncio.AbstractEventLoop,
        options: TapoExporterOptions | None = None,
        callbacks: TapoCallbacks | None = None,
    ) -> None:
        """Initialize the exporter on the event loop used for all device I/O."""
        super().__init__()
        self.options = options or TapoExporterOptions()
        self.discovered_devices: DeviceDict | None = None
        self._update_device_factories: list[Callable[[], Awaitable[TapoDeviceUpdateResult]]] | None = None
        self._asyncio_loop = asyncio_loop
        self.callbacks = callbacks or TapoCallbacks()
        self._auth_failed_devices: set[str] = set()
        self._failed_devices: set[str] = set()
        self._last_successful_updates: dict[str, float] = {}
        self._pending_hosts: dict[str, float] = {}
        self._next_discovery_time = 0.0
        self._metrics_lock = threading.Lock()
        self._scrape_refresh_lock = threading.Lock()
        self._refresh_future: Future[None] | None = None
        self._device_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._cleanup_complete = False
        self._latest_metrics: list[Metric] = []
        self._update_task: asyncio.Task[None] | None = None
        self._closed = False

    def _get_refresh_interval(self) -> int | None:
        """Return the configured refresh interval.

        Returns
        -------
        int | None
            The interval in seconds, or None for refreshes triggered by scrapes.

        """
        if self.options.prometheus_options is None:
            return DEFAULT_REFRESH_INTERVAL
        return self.options.prometheus_options.refresh_interval

    async def discover(self) -> None:
        """Discover devices, isolating unavailable explicitly configured hosts.

        Raises
        ------
        ValueError
            If discovery options have not been configured.

        """
        async with self._device_lock:
            if self._closed:
                return
            await self._disconnect_devices()
            self.discovered_devices = {}
            self._last_successful_updates.clear()
            self._auth_failed_devices.clear()
            self._failed_devices.clear()
            options = self.options.discovery_options
            if options is None:
                message = "Discovery options are not set"
                raise ValueError(message)
            if options.perform_discovery:
                self.discovered_devices = await Discover.discover(
                    target=options.target,
                    credentials=options.credentials,
                    on_discovered=self.callbacks.on_discovered,
                    on_discovered_raw=self.callbacks.on_discovered_raw,
                    on_unsupported=self.callbacks.on_unsupported,
                    discovery_timeout=options.discovery_timeout,
                    discovery_packets=options.discovery_packets,
                    interface=options.interface,
                    port=options.port,
                    timeout=options.timeout,
                )

            self._pending_hosts = dict.fromkeys(
                sorted(set(self.options.devices) - self.discovered_devices.keys()), 0.0
            )
            await self._discover_configured_devices(full_scan=True)
            logger.info("Discovered %s Tapo devices.", len(self.discovered_devices))
            if options.with_update:
                await self._update_devices()

    async def _discover_configured_devices(self, *, full_scan: bool = False) -> None:
        """Retry one wave of missing hosts with fair per-host cooldowns.

        Initial discovery scans all configured hosts. Refreshes attempt at most
        ``max_concurrent_devices`` eligible hosts so a large offline inventory
        adds only one wave of discovery I/O before healthy devices refresh.
        Failed hosts move to the queue's end with a cooldown measured from the
        end of the wave; the queue therefore remains ordered by eligibility.
        """
        options = self.options.discovery_options
        if options is None or self.discovered_devices is None:
            return

        async def discover_host(host: str) -> None:
            device = await Discover.discover_single(
                host=host,
                credentials=options.credentials,
                discovery_timeout=options.discovery_timeout,
                port=options.port,
                timeout=options.timeout,
                on_discovered_raw=self.callbacks.on_discovered_raw,
                on_unsupported=self.callbacks.on_unsupported,
            )
            if device is not None and self.discovered_devices is not None:
                # Hostnames and IP addresses may refer to the same device.
                if device.host in self.discovered_devices:
                    if device is not self.discovered_devices[device.host]:
                        await device.disconnect()
                else:
                    self.discovered_devices[device.host] = device
                self._pending_hosts.pop(host, None)

        now = time.monotonic()
        hosts = (
            list(self._pending_hosts)
            if full_scan
            else [
                host
                for host, ready_at in islice(self._pending_hosts.items(), self.options.max_concurrent_devices)
                if ready_at <= now
            ]
        )
        try:
            results = await run_tasks_with_retry(
                [lambda host=host: discover_host(host) for host in hosts],
                concurrency=self.options.max_concurrent_devices,
                attempts=1,
                return_exceptions=True,
            )
            for host, result in zip(hosts, results, strict=True):
                if isinstance(result, Exception):
                    logger.warning("Could not discover configured device %s: %s", host, result)
        finally:
            retry_at = time.monotonic() + REDISCOVERY_INTERVAL
            for host in hosts:
                if host in self._pending_hosts:
                    self._pending_hosts.pop(host)
                    self._pending_hosts[host] = retry_at
            self._next_discovery_time = next(iter(self._pending_hosts.values()), math.inf)
            # A scrape deadline can cancel discovery after some hosts have succeeded.
            self._update_device_factories = [
                lambda device=device: self._update_device(device, self._get_refresh_interval())
                for device in self.discovered_devices.values()
            ]

    async def _update_devices(self) -> None:
        """Refresh devices while allowing healthy devices to survive peer failures."""
        if self._closed or self.discovered_devices is None:
            return
        if self._pending_hosts and time.monotonic() >= self._next_discovery_time:
            await self._discover_configured_devices()
        if self._update_device_factories is None:
            return
        results = await run_tasks_with_retry(
            self._update_device_factories,
            concurrency=self.options.max_concurrent_devices,
            return_exceptions=True,
        )
        for device, result in zip(self.discovered_devices.values(), results, strict=False):
            if isinstance(result, Exception):
                self._failed_devices.add(device.host)
                logger.warning("Update failed for device %s: %s", device.host, result)
                continue
            if result.host is None or result.auth_failed is None:
                continue
            self._failed_devices.discard(result.host)
            if result.auth_failed:
                self._auth_failed_devices.add(result.host)
            else:
                self._auth_failed_devices.discard(result.host)

    async def update(self) -> None:
        """Update devices without overlapping other I/O on their sessions."""
        async with self._device_lock:
            await self._update_devices()

    async def update_and_collect(self) -> None:
        """Publish a complete snapshot after the update pass finishes."""
        async with self._device_lock:
            if self._closed:
                return
            await self._update_devices()
            if self._closed:
                return
            metrics = self._build_metrics()
            with self._metrics_lock:
                self._latest_metrics = metrics

    async def _background_update_loop(self, interval: float) -> None:
        """Refresh at a fixed delay after each completed update pass."""
        while True:
            try:
                await self.update_and_collect()
            except Exception:
                logger.exception("Background update failed.")
            await asyncio.sleep(interval)

    async def start_background_updates(self, interval: float | None = None) -> None:
        """Start periodic updates unless scrape-triggered refreshing is enabled.

        Raises
        ------
        ValueError
            If the requested refresh interval is non-finite or not positive.

        """
        if self._closed or (self._update_task and not self._update_task.done()):
            return
        refresh_interval = interval if interval is not None else self._get_refresh_interval()
        if refresh_interval is None:
            return
        if not math.isfinite(refresh_interval) or refresh_interval <= 0:
            message = "refresh_interval must be finite and positive"
            raise ValueError(message)
        self._update_task = asyncio.create_task(self._background_update_loop(refresh_interval))

    async def stop_background_updates(self) -> None:
        """Cancel and await periodic updates."""
        if self._update_task is None:
            return
        self._update_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._update_task
        self._update_task = None

    async def _update_device(self, device: Device, refresh_interval: int | None) -> TapoDeviceUpdateResult:
        """Update one device and record timestamps independently of kasa internals.

        Returns
        -------
        TapoDeviceUpdateResult
            The device host and authentication status, if an update was due.

        Raises
        ------
        DeviceError
            If the device reports an error unrelated to authentication.

        """
        last_update = self._last_successful_updates.get(device.host)
        if (
            refresh_interval is not None
            and last_update is not None
            and time.monotonic() - last_update < refresh_interval
        ):
            return TapoDeviceUpdateResult(host=device.host)
        try:
            await device.update()
        except AuthenticationError:
            logger.exception("Authentication failed for device %s", device.host)
            return TapoDeviceUpdateResult(host=device.host, auth_failed=True)
        except DeviceError as exc:
            if exc.error_code in SMART_AUTHENTICATION_ERRORS:
                logger.exception("Authentication failed for device %s", device.host)
                return TapoDeviceUpdateResult(host=device.host, auth_failed=True)
            raise
        self._last_successful_updates[device.host] = time.monotonic()
        return TapoDeviceUpdateResult(host=device.host, auth_failed=False)

    @staticmethod
    def collect_from_plug_device(
        device: Device, current_consumption_key: str = "current_consumption"
    ) -> TapoPlugDeviceDump | None:
        """Read available energy features, converting kasa's kWh values to Wh.

        Returns
        -------
        TapoPlugDeviceDump | None
            Available readings and metadata, or None for a device without energy features.

        """
        features = device.features or {}
        if features.get(current_consumption_key) is None:
            return None

        def safe_float(feature_name: str, scale: float = 1.0) -> float | None:
            feature = features.get(feature_name)
            if feature is None:
                return None
            try:
                value = feature.value
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    scaled = float(value) * scale
                    return scaled if math.isfinite(scaled) else None
            except Exception:
                logger.debug("Unavailable feature %s on %s", feature_name, device.host, exc_info=True)
            return None

        device_info = device.device_info
        return TapoPlugDeviceDump(
            host=device.host,
            alias=device.alias,
            model=device.model,
            device_type=device.device_type.value,
            firmware_version=device_info.firmware_version,
            hardware_version=device_info.hardware_version,
            current_consumption=safe_float(current_consumption_key),
            current_voltage=safe_float("voltage"),
            current_current=safe_float("current"),
            current_consumption_today=safe_float("consumption_today", 1000.0),
            current_month_consumption=safe_float("consumption_this_month", 1000.0),
            current_rssi=safe_float("rssi"),
        )

    @staticmethod
    def _get_device_family(device: Device) -> TapoDeviceFamily | None:
        """Return the configured family corresponding to the kasa device type.

        Returns
        -------
        TapoDeviceFamily | None
            The supported device family, or None when the device type is unsupported.

        """
        if device.device_type is None:
            return None
        device_type = device.device_type.value if hasattr(device.device_type, "value") else str(device.device_type)
        try:
            return TapoDeviceFamily(device_type)
        except ValueError:
            return None

    async def _disconnect_devices(self) -> None:
        """Attempt every session close even when another device fails."""
        devices = list((self.discovered_devices or {}).values())
        results = await run_tasks_with_retry(
            [device.disconnect for device in devices],
            concurrency=self.options.max_concurrent_devices,
            return_exceptions=True,
        )
        for device, result in zip(devices, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("Disconnect failed for device %s: %s", device.host, result)

    async def disconnect(self) -> None:
        """Disconnect all devices after any in-flight update has finished."""
        async with self._device_lock:
            await self._disconnect_devices()

    async def cleanup(self) -> None:
        """Close sessions once, allowing an interrupted cleanup to be retried."""
        async with self._cleanup_lock:
            if self._cleanup_complete:
                return
            self._closed = True
            try:
                await self.stop_background_updates()
                with self._scrape_refresh_lock:
                    if self._refresh_future is not None:
                        self._refresh_future.cancel()
                        self._refresh_future = None
                await self.disconnect()
            finally:
                # Retain sessions for another close attempt after cancellation,
                # but never serve stale metrics once shutdown has started.
                with self._metrics_lock:
                    self._latest_metrics = []
            self.discovered_devices = {}
            self._update_device_factories = []
            self._last_successful_updates.clear()
            self._auth_failed_devices.clear()
            self._failed_devices.clear()
            self._pending_hosts.clear()
            self._cleanup_complete = True

    def _metric_definitions(self) -> dict[TapoPerPlugMetricType, TapoPlugGaugeMetric]:
        """Return the enabled plug metric definitions.

        Returns
        -------
        dict[TapoPerPlugMetricType, TapoPlugGaugeMetric]
            Configured plug metrics, or an empty mapping when plugs are disabled.

        """
        families = self.options.supported_device_families or {}
        configured = self.options.per_device_family_metrics
        return configured.plug if configured and families.get(TapoDeviceFamily.PLUG, False) else {}

    def describe(self) -> Iterable[Metric]:
        """Describe families without refreshing devices during registry registration.

        Yields
        ------
        Metric
            An empty family describing each configured metric and the device count.

        """
        yield GaugeMetricFamily("tapo_discovered_devices", "Number of discovered Tapo devices")
        for metric in self._metric_definitions().values():
            yield metric.get_metric()

    def _add_device_metrics(
        self,
        device: Device,
        definitions: dict[TapoPerPlugMetricType, TapoPlugGaugeMetric],
        families: dict[TapoPerPlugMetricType, GaugeMetricFamily],
        consumption_key: str,
    ) -> None:
        """Append available readings from one supported device to shared families."""
        if self._get_device_family(device) != TapoDeviceFamily.PLUG or not definitions:
            return
        dump = self.collect_from_plug_device(device, consumption_key)
        if dump is None:
            return
        for metric_type, metric in definitions.items():
            value = getattr(dump, str(metric_type), None)
            if value is not None:
                labels = [str(getattr(dump, label) or "unknown") for label in metric.labels]
                families[metric_type].add_metric(labels, value)

    def _build_metrics(self) -> list[Metric]:
        """Build one family per metric and omit unavailable or stale samples.

        Returns
        -------
        list[Metric]
            Metric families containing samples only from healthy supported devices.

        """
        devices = self.discovered_devices or {}
        definitions = self._metric_definitions()
        families = {metric_type: metric.get_metric() for metric_type, metric in definitions.items()}
        consumption_key = (
            self.options.discovery_options.current_consumption_key
            if self.options.discovery_options
            else "current_consumption"
        )
        for device in devices.values():
            if device.host in self._auth_failed_devices or device.host in self._failed_devices:
                continue
            try:
                self._add_device_metrics(device, definitions, families, consumption_key)
            except Exception:
                logger.exception("Could not collect metrics for device %s", device.host)
        return [
            GaugeMetricFamily("tapo_discovered_devices", "Number of discovered Tapo devices", value=len(devices)),
            *families.values(),
        ]

    def _refresh_on_scrape(self) -> None:
        """Coalesce overlapping scrapes and cap their wait for device I/O."""
        if self._closed or not self._asyncio_loop.is_running():
            return
        try:
            if asyncio.get_running_loop() is self._asyncio_loop:
                # Blocking this loop would prevent the refresh coroutine from ever running.
                return
        except RuntimeError:
            pass
        options = self.options.prometheus_options
        timeout = options.scrape_timeout if options else 10.0
        with self._scrape_refresh_lock:
            if self._closed:
                return
            if self._refresh_future is None or self._refresh_future.done():
                coroutine = self.update_and_collect()
                try:
                    self._refresh_future = asyncio.run_coroutine_threadsafe(coroutine, self._asyncio_loop)
                except RuntimeError:
                    coroutine.close()
                    return
            future = self._refresh_future
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            logger.warning("Scrape refresh exceeded %s seconds; returning cached metrics.", timeout)
        except Exception:
            logger.exception("Scrape refresh failed; returning cached metrics.")

    def collect(self) -> Iterable[Metric]:
        """Return a complete cached snapshot, optionally refreshing within a timeout.

        Yields
        ------
        Metric
            Each metric family in a snapshot copied while holding the cache lock.

        """
        if self._get_refresh_interval() is None:
            self._refresh_on_scrape()
        # Copy under the lock, then release it before handing control to the caller.
        with self._metrics_lock:
            metrics = list(self._latest_metrics)
        yield from metrics  # ruff: ignore[unnecessary-assign-before-yield]
