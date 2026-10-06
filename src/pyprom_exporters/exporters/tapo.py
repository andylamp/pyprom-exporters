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
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import chain, islice
from time import time as epoch_time
from typing import TYPE_CHECKING

from kasa import Credentials, Device
from kasa.discover import DeviceDict, Discover, OnDiscoveredCallable, OnDiscoveredRawCallable, OnUnsupportedCallable
from kasa.exceptions import TimeoutError as KasaTimeoutError
from prometheus_client.metrics_core import CounterMetricFamily, GaugeMetricFamily, Metric

from . import run_tasks_with_retry
from .base import BasePrometheusCollector, BasePrometheusOptions

if TYPE_CHECKING:
    from collections.abc import Iterable
    from concurrent.futures import Future


logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL: int | None = None
REDISCOVERY_INTERVAL: float = 30.0
MAC_HEX_LENGTH = 12
DISCONNECT_TIMEOUT = 1.0
DEVICE_HEALTH_METRICS = {
    "update_success": "Whether the latest device update succeeded",
    "last_success_timestamp_seconds": "Unix timestamp of the last successful SDK update, not sensor sample time",
    "update_duration_seconds": "Duration of the latest SDK update including native retries",
    "update_failures_total": "Failed SDK updates",
    "update_timeouts_total": "SDK updates that timed out",
}
EXPORTER_HEALTH_METRICS = {
    "tapo_refresh_duration_seconds": "Duration of the latest device refresh pass including recovery",
    "tapo_refresh_in_progress": "Whether a device refresh pass is running",
    "tapo_scrape_refresh_timeouts_total": "Scrape callers whose refresh wait timed out",
}
OPERATIONAL_METRIC_NAMES = {
    "tapo_discovered_devices",
    *(f"tapo_device_{name}" for name in DEVICE_HEALTH_METRICS),
    *EXPORTER_HEALTH_METRICS,
}
OPERATIONAL_METRIC_NAMES.update(
    name.removesuffix("_total") + suffix
    for name in tuple(OPERATIONAL_METRIC_NAMES)
    if name.endswith("_total")
    for suffix in ("", "_created")
)


@dataclass
class TapoPrometheusOptions(BasePrometheusOptions):
    """Options for the Tapo Prometheus exporter."""

    refresh_interval: int | None = DEFAULT_REFRESH_INTERVAL
    """Background refresh interval in seconds; ``None`` (default) probes on scrape."""
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
        """Validate discovery settings before resolving credentials.

        Raises
        ------
        ValueError
            If packet counts, timeouts, or an explicit discovery port are invalid.

        """
        if (
            isinstance(self.discovery_packets, bool)
            or not isinstance(self.discovery_packets, int)
            or self.discovery_packets < 1
        ):
            message = "discovery_packets must be a positive integer"
            raise ValueError(message)
        timeouts = {"discovery_timeout": self.discovery_timeout}
        if self.timeout is not None:
            timeouts["timeout"] = self.timeout
        for name, value in timeouts.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                message = f"{name} must be finite and positive"
                raise ValueError(message)
        max_port = 65535
        if self.port is not None and (
            isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= max_port
        ):
            message = f"port must be an integer between 1 and {max_port}, or None"
            raise ValueError(message)
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
    update_timeout: float = 10.0
    """Deadline for one complete SDK update, including its native retries, in seconds."""
    per_device_family_metrics: TapoDeviceFamilyMetrics | None = None
    """Metrics to be collected per device family."""

    def __post_init__(self) -> None:
        """Post-initialization to ensure discovery options are set.

        Raises
        ------
        ValueError
            If the device concurrency limit or whole-update deadline is invalid.

        """
        if (
            isinstance(self.max_concurrent_devices, bool)
            or not isinstance(self.max_concurrent_devices, int)
            or self.max_concurrent_devices < 1
        ):
            message = "max_concurrent_devices must be a positive integer"
            raise ValueError(message)
        if (
            isinstance(self.update_timeout, bool)
            or not isinstance(self.update_timeout, (int, float))
            or not math.isfinite(self.update_timeout)
            or self.update_timeout <= 0
        ):
            message = "update_timeout must be finite and positive"
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
        """Require valid, unique metric names and labels that identify each device.

        Raises
        ------
        ValueError
            If metric names are invalid or repeated, or labels cannot identify devices uniquely.

        """
        if self.per_device_family_metrics is None:
            return
        names = set(OPERATIONAL_METRIC_NAMES)
        allowed_labels = {"host", "alias", "model", "device_type", "firmware_version", "hardware_version"}
        for metric in self.per_device_family_metrics.plug.values():
            # Apply the client's naming rules before discovery starts any device I/O.
            metric.get_metric()
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


@dataclass(frozen=True)
class TapoDeviceSnapshot:
    """An immutable reading and operational state safe to hand to scrape threads."""

    host: str
    alias: str = "unknown"
    identity: str | None = None
    dump: TapoPlugDeviceDump | None = None
    update_success: bool = False
    last_success_timestamp_seconds: float = 0.0
    update_duration_seconds: float = 0.0
    update_failures_total: int = 0
    update_timeouts_total: int = 0


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
        self._asyncio_loop = asyncio_loop
        self.callbacks = callbacks or TapoCallbacks()
        self._failed_devices: set[str] = set()
        self._failed_retry_hosts: dict[str, float] = {}
        self._last_successful_updates: dict[str, float] = {}
        self._pending_hosts: dict[str, float] = {}
        self._discovery_hosts: dict[str, str] = {}
        # Retired sessions remain owned until closing them actually succeeds.
        self._pending_disconnects: dict[int, Device] = {}
        self._retired_devices: dict[str, Device] = {}
        self._metrics_lock = threading.Lock()
        self._scrape_refresh_lock = threading.Lock()
        self._refresh_future: Future[None] | None = None
        self._device_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._cleanup_complete = False
        self._latest_metrics: list[Metric] = []
        self._device_snapshots: dict[str, TapoDeviceSnapshot] = {}
        self._health_hosts: dict[str, str] = {}
        self._metrics_dirty = True
        self._inventory_count = 0
        self._refresh_in_progress = False
        self._refresh_duration = 0.0
        self._scrape_timeouts = 0
        self._refresh_ready = threading.Event()
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
            # Retired inventory must not remain visible if rediscovery fails or is cancelled.
            with self._metrics_lock:
                self._latest_metrics = []
                self._device_snapshots = {
                    host: replace(snapshot, dump=None, update_success=False)
                    for host, snapshot in self._device_snapshots.items()
                }
                self._inventory_count = 0
                self._metrics_dirty = True
            await self._disconnect_devices()
            self.discovered_devices = {}
            self._discovery_hosts.clear()
            self._last_successful_updates.clear()
            self._failed_devices.clear()
            self._failed_retry_hosts.clear()
            self._pending_hosts = dict.fromkeys(sorted(set(self.options.devices)), 0.0)
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
                self._discovery_hosts = {device.host: device.host for device in self.discovered_devices.values()}

            self._pending_hosts = dict.fromkeys(
                sorted(set(self.options.devices) - self.discovered_devices.keys()), 0.0
            )
            for host in self._pending_hosts:
                self._seed_missing_host(host)
            await self._discover_configured_devices(full_scan=True)
            self._prune_diagnostic_history()
            logger.info("Discovered %s Tapo devices.", len(self.discovered_devices))
            if options.with_update:
                await self._update_devices()
            if self._closed:
                return
            # Publish discovery readings without another device update, including in live mode.
            metrics = self._build_metrics()
            with self._metrics_lock:
                self._latest_metrics = metrics

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

        devices = self.discovered_devices
        identities: dict[str, Device] = {}
        for device in devices.values():
            if (identity := self._device_identity(device)) is not None:
                identities.setdefault(identity, device)

        async def discover_host(host: str) -> None:
            try:
                await self._close_retired_device(host)
                async with asyncio.timeout(float(options.discovery_timeout)):
                    device = await Discover.discover_single(
                        host=host,
                        credentials=options.credentials,
                        discovery_timeout=options.discovery_timeout,
                        port=options.port,
                        timeout=options.timeout,
                        on_discovered_raw=self.callbacks.on_discovered_raw,
                        on_unsupported=self.callbacks.on_unsupported,
                    )
                if device is not None:
                    await self._retain_discovered_device(host, device, devices, identities)
            except Exception as exc:  # ruff: ignore[blind-except]
                # Isolate expected per-host failures without retaining traceback graphs
                # in worker results or in queued/buffering logging handlers.
                error_message = str(exc)
                logger.warning("Could not discover configured device %s: %s", host, error_message)

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
            await run_tasks_with_retry(
                (lambda host=host: discover_host(host) for host in hosts),
                concurrency=self.options.max_concurrent_devices,
                attempts=1,
            )
        finally:
            retry_at = time.monotonic() + REDISCOVERY_INTERVAL
            for host in hosts:
                if host in self._pending_hosts:
                    self._pending_hosts.pop(host)
                    self._pending_hosts[host] = retry_at

    @staticmethod
    def _device_identity(device: Device) -> str | None:
        """Normalize a known hardware address without conflating unknown identities.

        Returns
        -------
        str | None
            Twelve hexadecimal MAC digits, or None when discovery has no valid address.

        """
        try:
            mac = getattr(device, "mac", None)
        except Exception:
            logger.debug("Device identity is unavailable for %s", device.host, exc_info=True)
            return None
        if not isinstance(mac, str):
            return None
        identity = mac.replace(":", "").replace("-", "").lower()
        if (
            len(identity) != MAC_HEX_LENGTH
            or identity in {"0" * MAC_HEX_LENGTH, "f" * MAC_HEX_LENGTH}
            or any(char not in "0123456789abcdef" for char in identity)
        ):
            return None
        return identity

    async def _retain_discovered_device(
        self, host: str, device: Device, devices: DeviceDict, identities: dict[str, Device]
    ) -> None:
        """Keep the first session for a device and own duplicates until their close succeeds."""
        identity = self._device_identity(device)
        existing = devices.get(device.host)
        if existing is None and identity is not None:
            existing = identities.get(identity)
        self._pending_hosts.pop(host, None)
        if existing is None:
            devices[device.host] = device
            self._discovery_hosts[device.host] = host
            self._retain_health_target(host, device)
            if identity is not None:
                identities[identity] = device
        else:
            # Keep configured names usable for recovery even when discovery returned an IP.
            self._discovery_hosts[existing.host] = host
            self._retain_health_target(host, existing)
            if existing is not device and id(device) not in self._pending_disconnects:
                self._pending_disconnects[id(device)] = device
                await self._disconnect_device(device)
                self._pending_disconnects.pop(id(device))

    async def _update_devices(self) -> None:
        """Publish independent device results before attempting slower recovery work."""
        if self._closed or self.discovered_devices is None:
            return
        started = time.monotonic()
        with self._metrics_lock:
            self._refresh_in_progress = True
            self._inventory_count = len(self.discovered_devices)
            self._metrics_dirty = True
        try:
            devices = list(self.discovered_devices.values())
            known_ids = {id(device) for device in devices}
            for device in devices:
                self._seed_device_snapshot(device)
            healthy = [device for device in devices if device.host not in self._failed_devices]
            failed = [device for device in devices if device.host in self._failed_devices]
            await self._update_batch(healthy)
            if healthy:
                # Scrapes may use fresh healthy readings while failed peers recover.
                self._refresh_ready.set()
            await self._retry_failed_devices(failed)
            if time.monotonic() >= next(iter(self._pending_hosts.values()), math.inf):
                await self._discover_configured_devices()
                await self._update_batch(
                    [device for device in self.discovered_devices.values() if id(device) not in known_ids]
                )
        finally:
            with self._metrics_lock:
                self._refresh_in_progress = False
                self._refresh_duration = time.monotonic() - started
                self._inventory_count = len(self.discovered_devices or {})
                self._metrics_dirty = True
            self._refresh_ready.set()

    async def _retry_failed_devices(self, devices: list[Device]) -> None:
        """Retry one fair wave of failed sessions without delaying later healthy passes."""
        by_host = {device.host: device for device in devices}
        for host in by_host:
            self._failed_retry_hosts.setdefault(host, 0.0)
        now = time.monotonic()
        eligible = list(
            islice(
                (
                    by_host[host]
                    for host, ready_at in self._failed_retry_hosts.items()
                    if host in by_host and ready_at <= now
                ),
                self.options.max_concurrent_devices,
            )
        )
        await self._update_batch(eligible)

    async def _update_batch(self, devices: list[Device]) -> None:
        """Run one bounded attempt per device; Kasa owns retries inside that attempt."""
        if not devices:
            return
        refresh_interval = self._get_refresh_interval()
        await run_tasks_with_retry(
            (lambda device=device: self._update_device(device, refresh_interval) for device in devices),
            concurrency=self.options.max_concurrent_devices,
            attempts=1,
            return_exceptions=True,
        )

    async def update(self) -> None:
        """Update devices without overlapping other I/O on their sessions."""
        async with self._device_lock:
            await self._update_devices()

    async def update_and_collect(self) -> None:
        """Complete a refresh pass, publishing each device independently as it finishes."""
        async with self._device_lock:
            if self._closed:
                return
            await self._update_devices()
            if not self._closed:
                self._build_metrics()

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
        """Cancel and await periodic updates.

        Raises
        ------
        CancelledError
            If the caller is cancelled while awaiting the background task.

        """
        caller = asyncio.current_task()
        if caller is not None and caller.cancelling():
            # Deliver pending cancellation before treating the count as historical
            # or interrupting the owned background task.
            await asyncio.sleep(0)
        initial_cancellations = caller.cancelling() if caller is not None else 0
        if self._update_task is None:
            return
        self._update_task.cancel()
        try:
            await self._update_task
        except asyncio.CancelledError:
            # Joining the cancelled child is expected. Only a new cancellation
            # request should interrupt this caller and preserve its deadline.
            if caller is not None and caller.cancelling() > initial_cancellations:
                raise
        self._update_task = None

    async def _update_device(self, device: Device, refresh_interval: int | None) -> TapoDeviceUpdateResult:
        """Bound the entire SDK update and publish its reading or failure independently.

        Returns
        -------
        TapoDeviceUpdateResult
            Whether the attempted update succeeded, or an empty status when no update was due.
        """
        last_update = self._last_successful_updates.get(device.host)
        if (
            refresh_interval is not None
            and last_update is not None
            and time.monotonic() - last_update < refresh_interval
        ):
            return TapoDeviceUpdateResult(host=device.host)
        uninitialized = last_update is None
        started = time.monotonic()
        try:
            uninitialized = uninitialized and not device.features
            async with asyncio.timeout(self.options.update_timeout):
                await device.update()
        except Exception as exc:  # ruff: ignore[blind-except]
            timed_out = isinstance(exc, (TimeoutError, KasaTimeoutError))
            self._failed_devices.add(device.host)
            self._failed_retry_hosts.pop(device.host, None)
            self._failed_retry_hosts[device.host] = time.monotonic() + REDISCOVERY_INTERVAL
            self._record_device_snapshot(device, started, success=False, timed_out=timed_out)
            error_message = str(exc) or type(exc).__name__
            logger.warning("Update failed for device %s: %s", device.host, error_message)
            if uninitialized or timed_out:
                await self._retire_device(device)
            return TapoDeviceUpdateResult(host=device.host, auth_failed=True)
        self._last_successful_updates[device.host] = time.monotonic()
        self._failed_devices.discard(device.host)
        self._failed_retry_hosts.pop(device.host, None)
        self._record_device_snapshot(device, started, success=True)
        return TapoDeviceUpdateResult(host=device.host, auth_failed=False)

    async def _retire_device(self, device: Device) -> None:
        """Recover failed initialization or interrupted updates through a fresh session."""
        host = self._discovery_hosts.pop(device.host, device.host)
        self._pending_disconnects[id(device)] = device
        self._retired_devices[host] = device
        if self.discovered_devices is not None:
            self.discovered_devices.pop(device.host, None)
        self._last_successful_updates.pop(device.host, None)
        self._failed_devices.discard(device.host)
        self._failed_retry_hosts.pop(device.host, None)
        self._pending_hosts.pop(host, None)
        self._pending_hosts[host] = time.monotonic() + REDISCOVERY_INTERVAL
        with self._metrics_lock:
            self._inventory_count = len(self.discovered_devices or {})
            self._metrics_dirty = True
        try:
            await self._close_retired_device(host)
        except Exception as exc:  # ruff: ignore[blind-except]
            error_message = str(exc) or type(exc).__name__
            logger.warning("Disconnect failed for retired device %s: %s", device.host, error_message)

    async def _disconnect_device(self, device: Device) -> None:
        """Bound session closure while allowing cancellation to preserve ownership."""
        async with asyncio.timeout(min(self.options.update_timeout, DISCONNECT_TIMEOUT)):
            await device.disconnect()

    async def _close_retired_device(self, host: str) -> None:
        """Close a retired session before allocating another for the same discovery target."""
        if (device := self._retired_devices.get(host)) is not None:
            await self._disconnect_device(device)
            self._pending_disconnects.pop(id(device), None)
            self._retired_devices.pop(host, None)

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
        devices = {
            id(device): device
            for device in chain((self.discovered_devices or {}).values(), self._pending_disconnects.values())
        }
        results = await run_tasks_with_retry(
            (lambda device=device: self._disconnect_device(device) for device in devices.values()),
            concurrency=self.options.max_concurrent_devices,
            return_exceptions=True,
        )
        for device, result in zip(devices.values(), results, strict=True):
            if isinstance(result, Exception):
                self._pending_disconnects[id(device)] = device
                logger.warning("Disconnect failed for device %s: %s", device.host, result)
            else:
                self._pending_disconnects.pop(id(device), None)
        self._retired_devices = {
            host: device for host, device in self._retired_devices.items() if id(device) in self._pending_disconnects
        }

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
            self._refresh_ready.set()
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
                    self._device_snapshots.clear()
                    self._health_hosts.clear()
                    self._metrics_dirty = False
            self.discovered_devices = {}
            self._last_successful_updates.clear()
            self._failed_devices.clear()
            self._failed_retry_hosts.clear()
            self._pending_hosts.clear()
            self._discovery_hosts.clear()
            self._cleanup_complete = not self._pending_disconnects

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

    @staticmethod
    def _device_alias(device: Device) -> str:
        """Read a usable alias even when device initialization is incomplete.

        Returns
        -------
        str
            The device alias, or the stable unknown placeholder.
        """
        try:
            return str(device.alias or "unknown")
        except Exception:  # ruff: ignore[blind-except]
            return "unknown"

    def _read_device_dump(self, device: Device) -> TapoPlugDeviceDump | None:
        """Extract measurements without keeping SDK objects in the scrape cache.

        Returns
        -------
        TapoPlugDeviceDump | None
            Supported available readings, or None when extraction is unavailable.
        """
        try:
            if self._get_device_family(device) != TapoDeviceFamily.PLUG or not self._metric_definitions():
                return None
            options = self.options.discovery_options
            return self.collect_from_plug_device(
                device, options.current_consumption_key if options else "current_consumption"
            )
        except Exception:
            logger.exception("Could not collect metrics for device %s", device.host)
            return None

    def _seed_missing_host(self, host: str) -> None:
        with self._metrics_lock:
            key = self._health_hosts.setdefault(host, host)
            self._device_snapshots.setdefault(key, TapoDeviceSnapshot(host=key))
            self._metrics_dirty = True

    def _prune_diagnostic_history(self) -> None:
        """Discard targets removed by an explicit full inventory replacement."""
        targets = {
            *self.options.devices,
            *self._discovery_hosts.values(),
            *self._pending_hosts,
            *self._retired_devices,
        }
        with self._metrics_lock:
            self._health_hosts = {target: host for target, host in self._health_hosts.items() if target in targets}
            retained_hosts = {
                *(device.host for device in (self.discovered_devices or {}).values()),
                *(self._health_hosts.get(target, target) for target in self._pending_hosts),
                *(device.host for device in self._retired_devices.values()),
            }
            self._device_snapshots = {
                host: snapshot for host, snapshot in self._device_snapshots.items() if host in retained_hosts
            }
            self._metrics_dirty = True

    def _retain_health_target(self, target: str, device: Device) -> None:
        """Move shared diagnostic history when discovery resolves an alias to a new address."""
        alias = self._device_alias(device)
        identity = self._device_identity(device)
        with self._metrics_lock:
            previous_host = self._health_hosts.get(target, target)
            previous = self._device_snapshots.get(previous_host)
            current = self._device_snapshots.get(device.host)
            same_device = (
                previous is not None
                and (previous.identity is None or identity is None or previous.identity == identity)
                and (
                    previous_host == device.host
                    or previous_host not in (self.discovered_devices or {})
                    or (identity is not None and previous.identity == identity)
                )
            )
            if same_device and previous is not None:
                if previous_host != device.host:
                    self._device_snapshots.pop(previous_host, None)
                    if previous.last_success_timestamp_seconds or previous.update_failures_total:
                        self._move_health_aliases(previous_host, device.host)
                if current is None or (
                    previous.update_failures_total > current.update_failures_total
                    or previous.last_success_timestamp_seconds > current.last_success_timestamp_seconds
                ):
                    current = replace(
                        previous,
                        host=device.host,
                        alias=alias,
                        identity=identity,
                        dump=None,
                        update_failures_total=max(
                            previous.update_failures_total, current.update_failures_total if current else 0
                        ),
                        update_timeouts_total=max(
                            previous.update_timeouts_total, current.update_timeouts_total if current else 0
                        ),
                    )
            if current is None or (current.identity and identity and current.identity != identity):
                current = TapoDeviceSnapshot(host=device.host, alias=alias, identity=identity)
            self._health_hosts[target] = device.host
            self._device_snapshots[device.host] = current
            self._inventory_count = len(self.discovered_devices or {})
            self._metrics_dirty = True

    def _move_health_aliases(self, previous_host: str, host: str) -> None:
        """Keep existing aliases attached to their shared diagnostic history."""
        for target, recorded_host in self._health_hosts.items():
            if recorded_host == previous_host:
                self._health_hosts[target] = host

    def _seed_device_snapshot(self, device: Device, *, read_measurements: bool = False) -> None:
        previous = self._device_snapshots.get(device.host)
        if previous is not None and (
            not read_measurements
            or previous.dump is not None
            or previous.last_success_timestamp_seconds
            or previous.update_failures_total
        ):
            return
        snapshot = replace(
            previous or TapoDeviceSnapshot(host=device.host),
            alias=self._device_alias(device),
            identity=self._device_identity(device),
            dump=self._read_device_dump(device) if read_measurements else None,
        )
        with self._metrics_lock:
            self._device_snapshots[device.host] = snapshot
            self._metrics_dirty = True

    def _record_device_snapshot(
        self, device: Device, started: float, *, success: bool, timed_out: bool = False
    ) -> None:
        dump = self._read_device_dump(device) if success else None
        alias = self._device_alias(device)
        identity = self._device_identity(device)
        finished = time.monotonic()
        with self._metrics_lock:
            if self._closed:
                return
            previous = self._device_snapshots.get(device.host, TapoDeviceSnapshot(host=device.host))
            self._device_snapshots[device.host] = replace(
                previous,
                alias=alias if alias != "unknown" else previous.alias,
                identity=identity or previous.identity,
                dump=dump,
                update_success=success,
                last_success_timestamp_seconds=epoch_time() if success else previous.last_success_timestamp_seconds,
                update_duration_seconds=finished - started,
                update_failures_total=previous.update_failures_total + (not success),
                update_timeouts_total=previous.update_timeouts_total + timed_out,
            )
            self._metrics_dirty = True

    @staticmethod
    def _operational_families() -> dict[str, GaugeMetricFamily | CounterMetricFamily]:
        """Describe operational metric families without any device or network access.

        Returns
        -------
        dict[str, GaugeMetricFamily | CounterMetricFamily]
            Fixed operational families keyed by their exported names.
        """
        families: dict[str, GaugeMetricFamily | CounterMetricFamily] = {}
        for field_name, description in DEVICE_HEALTH_METRICS.items():
            name = f"tapo_device_{field_name}"
            family_type = CounterMetricFamily if name.endswith("_total") else GaugeMetricFamily
            families[name] = family_type(name, description, labels=["host", "alias"])
        for name, description in EXPORTER_HEALTH_METRICS.items():
            family_type = CounterMetricFamily if name.endswith("_total") else GaugeMetricFamily
            families[name] = family_type(name, description)
        return families

    def describe(self) -> Iterable[Metric]:
        """Describe configured measurement and fixed operational families without I/O.

        Yields
        ------
        Metric
            An empty metric family for registry registration.
        """
        yield GaugeMetricFamily("tapo_discovered_devices", "Number of discovered Tapo devices")
        for metric in self._metric_definitions().values():
            yield metric.get_metric()
        yield from self._operational_families().values()

    def _render_metrics(self) -> list[Metric]:
        """Aggregate immutable device snapshots while the metric cache lock is held.

        Returns
        -------
        list[Metric]
            Measurement and operational families for the latest independent device results.
        """
        definitions = self._metric_definitions()
        families = {metric_type: metric.get_metric() for metric_type, metric in definitions.items()}
        operational = self._operational_families()
        for snapshot in self._device_snapshots.values():
            if (dump := snapshot.dump) is not None:
                for metric_type, definition in definitions.items():
                    if (value := getattr(dump, str(metric_type), None)) is not None:
                        labels = [str(getattr(dump, label) or "unknown") for label in definition.labels]
                        families[metric_type].add_metric(labels, value)
            for field_name in DEVICE_HEALTH_METRICS:
                operational[f"tapo_device_{field_name}"].add_metric(
                    [snapshot.host, snapshot.alias], float(getattr(snapshot, field_name))
                )
        operational["tapo_refresh_duration_seconds"].add_metric([], self._refresh_duration)
        operational["tapo_refresh_in_progress"].add_metric([], float(self._refresh_in_progress))
        operational["tapo_scrape_refresh_timeouts_total"].add_metric([], self._scrape_timeouts)
        return [
            GaugeMetricFamily(
                "tapo_discovered_devices", "Number of discovered Tapo devices", value=self._inventory_count
            ),
            *families.values(),
            *operational.values(),
        ]

    def _build_metrics(self) -> list[Metric]:
        """Seed unobserved devices and build one aggregate snapshot per completed pass.

        Returns
        -------
        list[Metric]
            Complete metric families from cached per-device readings.
        """
        for device in (self.discovered_devices or {}).values():
            self._seed_device_snapshot(device, read_measurements=True)
        with self._metrics_lock:
            if self._closed:
                return []
            self._inventory_count = len(self.discovered_devices or {})
            self._latest_metrics = self._render_metrics()
            self._metrics_dirty = False
            return self._latest_metrics

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
                self._refresh_ready = threading.Event()
                coroutine = self.update_and_collect()
                try:
                    self._refresh_future = asyncio.run_coroutine_threadsafe(coroutine, self._asyncio_loop)
                except RuntimeError:
                    coroutine.close()
                    return
            future = self._refresh_future
            ready = self._refresh_ready
        future.add_done_callback(lambda _future: ready.set())
        self._wait_for_refresh(future, ready, timeout)

    def _wait_for_refresh(self, future: Future[None], ready: threading.Event, timeout: float) -> None:
        """Wait for healthy readings while retaining the shared full-pass operation."""
        if not ready.wait(timeout=timeout):
            # The deadline bounds this HTTP request, not the shared refresh.
            # Cancelling the pass here would repeatedly starve later devices
            # in fleets whose total update time exceeds the scrape budget.
            with self._metrics_lock:
                self._scrape_timeouts += 1
                self._metrics_dirty = True
            logger.warning("Scrape refresh exceeded %s seconds; returning available metrics.", timeout)
            return
        try:
            if future.done():
                future.result()
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
            if self._closed:
                return
            if self._metrics_dirty:
                self._latest_metrics = self._render_metrics()
                self._metrics_dirty = False
            metrics = list(self._latest_metrics)
        yield from metrics  # ruff: ignore[unnecessary-assign-before-yield]
