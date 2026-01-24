"""Tapo Exporter for Prometheus."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

from kasa import Credentials, Device
from kasa.discover import DeviceDict, Discover, OnDiscoveredCallable, OnDiscoveredRawCallable, OnUnsupportedCallable
from kasa.exceptions import SMART_AUTHENTICATION_ERRORS, AuthenticationError, DeviceError
from prometheus_client.metrics_core import GaugeMetricFamily, Metric

from . import run_tasks_with_retry
from .base import BasePrometheusCollector, BasePrometheusOptions

if TYPE_CHECKING:
    from collections.abc import Iterable


fs_log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_REFRESH_INTERVAL: int = 1


@dataclass
class TapoPrometheusOptions(BasePrometheusOptions):
    """Options for the Tapo Prometheus exporter."""

    refresh_interval: int = DEFAULT_REFRESH_INTERVAL
    """Interval in seconds to refresh the metrics, if less - we use the cached value."""


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
class TapoDiscoveryOptions:  # pylint: disable=too-many-instance-attributes
    """Options for discovering Tapo devices on the network.

    Mimincs the ones from python-kasa package.
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
    tapo_password_env_key: str = "TP_LINK_PASSWORD"  # noqa: S105
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
    """Labels for the metric, default is an empty list."""

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
        metric.add_metric(labels, value if value is not None else 0.0)
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
        return getattr(dump, self.name, None) if hasattr(dump, self.name) else None


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
class TapoExporterOptions:  # pylint: disable=too-many-instance-attributes
    """Options for the Tapo exporter."""

    devices: list[str] = field(default_factory=list)
    """A list of Tapo devices to be monitored without discovery."""
    prometheus_options: TapoPrometheusOptions | None = None
    """Internal variable for holding the base Prometheus exporter options."""
    discovery_options: TapoDiscoveryOptions | None = None
    """Internal variable for holding the tapo discovery options."""
    supported_device_families: dict[TapoDeviceFamily, bool] | None = None
    """Device families to collect metrics from, keyed by family."""
    per_device_family_metrics: TapoDeviceFamilyMetrics | None = None
    """Metrics to be collected per device family."""

    def __post_init__(self) -> None:
        """Post-initialization to ensure discovery options are set."""
        if self.discovery_options is None:
            self.discovery_options = TapoDiscoveryOptions()

        if self.prometheus_options is None:
            self.prometheus_options = TapoPrometheusOptions()

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
                    fs_log.warning("Skipping unknown device family: %s", family)
            self.supported_device_families = normalized_families

        if self.per_device_family_metrics is None:
            self.per_device_family_metrics = TapoDeviceFamilyMetrics()


# pylint: disable=too-many-instance-attributes
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


class TapoPowerPlugPrometheusExporter(BasePrometheusCollector):  # pylint: disable=too-many-instance-attributes
    """Exporter for Tapo Power Plug metrics."""

    def __init__(
        self,
        asyncio_loop: asyncio.AbstractEventLoop,
        options: TapoExporterOptions | None = None,
        callbacks: TapoCallbacks | None = None,
    ) -> None:
        """Initialize the exporter with a list of devices.

        Parameters
        ----------
        asyncio_loop : asyncio.AbstractEventLoop
            The asyncio event loop to run the exporter in.
        options : TapoExporterOptions | None, optional
            Options for the Tapo exporter.
        callbacks : TapoCallbacks | None, optional
            Callbacks for Tapo device discovery, by default None.

        """
        super().__init__()
        # initialize the exporter with the provided options
        self.options: TapoExporterOptions = options or TapoExporterOptions()
        # update internal configuration
        self._update_device_factories: list | None = None  # type: ignore[var-annotated]

        # update the publicly accessible attributes
        self.discovered_devices: DeviceDict | None = None
        self._asyncio_loop: asyncio.AbstractEventLoop = asyncio_loop
        self.callbacks: TapoCallbacks = callbacks or TapoCallbacks()
        self._auth_failed_devices: set[str] = set()
        self._metrics_lock = threading.Lock()
        self._latest_metrics: list[Metric] = []
        self._update_task: asyncio.Task | None = None

    async def discover(self) -> None:
        """Discover Tapo devices on the network.

        Please note that this method will not work when you are using a separate VLAN for IoT devices,
        as the Tapo devices will not be discoverable from the host running this code.

        For a workaround please see here:
            https://github.com/python-kasa/python-kasa/issues/1431

        """
        if (options := self.options.discovery_options) is None:
            msg = "Discovery options are not set, cannot perform discovery."
            fs_log.error(msg)
            raise ValueError(msg)

        # attempt to discover devices on the network while passing the provided options
        discovered: DeviceDict = {}
        if options.perform_discovery:
            discovered = await Discover.discover(
                target=options.target,
                credentials=options.credentials,
                on_discovered=self.callbacks.on_discovered,
                on_discovered_raw=self.callbacks.on_discovered_raw,
                on_unsupported=self.callbacks.on_unsupported,
                discovery_timeout=options.discovery_timeout,
                discovery_packets=options.discovery_packets,
                interface=options.interface,
                username=options.credentials.username if options.credentials else None,
                password=options.credentials.password if options.credentials else None,
                port=options.port,
                timeout=options.timeout,
            )

        fs_log.debug("Discovered Tapo devices automatically: %s", len(discovered))

        for candidate_device in self.options.devices:
            if candidate_device not in discovered:
                fs_log.warning(
                    "Device %s not found during discovery -- attempting to discover it.",
                    candidate_device,
                )
                discovered_device = await Discover.discover_single(
                    credentials=options.credentials,
                    host=candidate_device,
                    discovery_timeout=options.discovery_timeout,
                    port=options.port,
                    timeout=options.timeout,
                    username=options.credentials.username if options.credentials else None,
                    password=options.credentials.password if options.credentials else None,
                    on_discovered_raw=self.callbacks.on_discovered_raw,
                    on_unsupported=self.callbacks.on_unsupported,
                )
                if discovered_device is not None:
                    fs_log.info("Discovered device: %s at %s", candidate_device, discovered_device.host)
                    discovered[candidate_device] = discovered_device
            else:
                fs_log.info("Device: %s already discovered", candidate_device)

        self.discovered_devices = discovered
        self._auth_failed_devices.intersection_update(self.discovered_devices.keys())

        fs_log.info("Discovered %s Tapo devices.", len(discovered))

        # use a predefined value if missing
        refresh_interval = (
            self.options.prometheus_options.refresh_interval
            if self.options.prometheus_options
            else DEFAULT_REFRESH_INTERVAL
        )

        # generate factories for updating each discovered device
        self._update_device_factories = [
            lambda d=d: self._update_device(d, refresh_interval)
            for d in self.discovered_devices.values()
            if d is not None
        ]

    async def update(self) -> None:
        """Update the discovered devices."""
        if self.discovered_devices is None or self._update_device_factories is None:
            msg = "No discovered devices or factories to update."
            fs_log.warning(msg)
            return
        fs_log.debug("Updating discovered Tapo devices...")

        # attempt to update all discovered devices concurrently
        results = await run_tasks_with_retry(
            self._update_device_factories,
        )
        for result in results:
            if result.host is None or result.auth_failed is None:
                continue
            if result.auth_failed:
                self._auth_failed_devices.add(result.host)
            else:
                self._auth_failed_devices.discard(result.host)
        fs_log.debug("Finished updating discovered Tapo devices.")

    async def update_and_collect(self) -> None:
        """Update devices and refresh cached metrics."""
        await self.update()
        metrics = self._build_metrics()
        with self._metrics_lock:
            self._latest_metrics = metrics

    async def _background_update_loop(self, interval: float) -> None:
        """Run periodic updates and refresh cached metrics."""
        try:
            while True:
                try:
                    await self.update_and_collect()
                except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                    fs_log.exception("Background update failed.")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            fs_log.debug("Background update loop cancelled.")
            raise

    async def start_background_updates(self, interval: float | None = None) -> None:
        """Start periodic updates on the asyncio loop."""
        if self._update_task and not self._update_task.done():
            return
        refresh_interval = interval
        if refresh_interval is None:
            refresh_interval = (
                self.options.prometheus_options.refresh_interval
                if self.options.prometheus_options
                else DEFAULT_REFRESH_INTERVAL
            )
        self._update_task = asyncio.create_task(self._background_update_loop(refresh_interval))

    async def stop_background_updates(self) -> None:
        """Stop periodic updates."""
        if self._update_task is None:
            return
        self._update_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._update_task
        self._update_task = None

    @staticmethod
    async def _update_device(device: Device, refresh_interval: int) -> TapoDeviceUpdateResult:
        """Update a single Tapo device.

        Parameters
        ----------
        device : Device
            The Tapo device to update.
        refresh_interval: int
            The refresh interval in seconds.

        """
        if device is None:
            fs_log.debug("No device instance available for update.")
            return TapoDeviceUpdateResult(host=None)

        force_update = False
        if hasattr(device, "internal_state") and device.internal_state is None:
            # Force an update to populate device metadata/state.
            force_update = True

        last_update = getattr(device, "_last_update_time", None)
        if last_update is None:
            # if the device does not have a last update time, set it to 0
            fs_log.debug(
                "Device %s at %s does not have a last update time, setting it to 0.",
                device.alias,
                device.host,
            )
            last_update = 0.0
            force_update = True

        current_time = time.monotonic()

        if not force_update and (current_time - last_update) < refresh_interval:
            fs_log.debug(
                "Device %s at %s was updated recently, skipping update. "
                "Last update time: %s, current time: %s, refresh interval: %s",
                device.alias,
                device.host,
                last_update,
                current_time,
                refresh_interval,
            )
            return TapoDeviceUpdateResult(host=device.host, auth_failed=None)

        try:
            await device.update()
        except AuthenticationError as exc:
            fs_log.error("Authentication failed for device %s: %s", device.host, exc)
            return TapoDeviceUpdateResult(host=device.host, auth_failed=True)
        except DeviceError as exc:
            if exc.error_code in SMART_AUTHENTICATION_ERRORS:
                fs_log.error("Authentication failed for device %s: %s", device.host, exc)
                return TapoDeviceUpdateResult(host=device.host, auth_failed=True)
            raise
        fs_log.debug("Updated device: %s at %s", device.alias, device.host)
        return TapoDeviceUpdateResult(host=device.host, auth_failed=False)

    @staticmethod
    def collect_from_plug_device(device: Device) -> TapoPlugDeviceDump | None:
        """Export the device information as a TapoPlugDeviceDump.

        Parameters
        ----------
        device : Device
            The Tapo device to export.

        Returns
        -------
        TapoPlugDeviceDump
            The exported device information.

        """
        # only export the device if it has current consumption feature
        features = device.features or {}
        if features.get("current_consumption") is None:
            fs_log.debug(
                "Device %s does not have current consumption feature, skipping export.",
                device.host,
            )
            return None

        def _get_safe_float_value(feature_name: str) -> float | None:
            feature = features.get(feature_name)
            if feature is not None and feature.value is not None and isinstance(feature.value, (int, float)):
                return float(feature.value)
            return None

        return TapoPlugDeviceDump(
            host=device.host,
            alias=device.alias,
            model=device.model,
            device_type=device.device_type.value,
            firmware_version=device.device_info.firmware_version,
            hardware_version=device.device_info.hardware_version,
            current_consumption=_get_safe_float_value("current_consumption"),
            current_voltage=_get_safe_float_value("voltage"),
            current_current=_get_safe_float_value("current"),
            current_consumption_today=_get_safe_float_value("consumption_today"),
            current_month_consumption=_get_safe_float_value("consumption_this_month"),
            current_rssi=_get_safe_float_value("rssi"),
        )

    @staticmethod
    def _get_device_family(device: Device) -> TapoDeviceFamily | None:
        if device is None or device.device_type is None:
            return None
        device_type = device.device_type.value if hasattr(device.device_type, "value") else str(device.device_type)
        try:
            return TapoDeviceFamily(device_type)
        except ValueError:
            fs_log.debug("Unknown device family for %s: %s", device.host, device_type)
            return None

    async def disconnect(self) -> None:
        """Disconnect from all discovered Tapo devices."""
        if self.discovered_devices is None:
            msg = "No discovered devices to disconnect."
            fs_log.warning(msg)
            return

        disconnect_device_factories = [
            lambda d=d: d.disconnect() for d in self.discovered_devices.values() if d is not None
        ]

        await run_tasks_with_retry(
            disconnect_device_factories,
        )

    async def cleanup(self) -> None:
        """Cleanup method to be called when the collector is no longer needed."""
        fs_log.debug("Cleaning up Tapo Power Plug Prometheus Exporter...")
        await self.stop_background_updates()
        await self.disconnect()
        fs_log.debug("Cleanup complete.")

    def _build_metrics(self) -> list[Metric]:  # noqa: C901
        """Build Prometheus metrics from the latest device state."""
        if not self.discovered_devices:
            fs_log.warning("No discovered devices to collect metrics from.")
            return []

        metrics: list[Metric] = [
            GaugeMetricFamily(
                "tapo_discovered_devices",
                "Number of discovered Tapo devices",
                value=len(self.discovered_devices),
            ),
        ]

        for device in self.discovered_devices.values():
            if device is None:
                continue

            if device.host in self._auth_failed_devices:
                fs_log.error("Skipping device %s due to authentication failure.", device.host)
                continue

            if hasattr(device, "internal_state") and device.internal_state is None:
                fs_log.debug("Device %s has empty internal state, skipping.", device.host)
                continue

            device_family = self._get_device_family(device)
            if device_family is None:
                fs_log.debug("Skipping device with unknown family: %s", device.host)
                continue

            if self.options.supported_device_families and not self.options.supported_device_families.get(
                device_family,
                False,
            ):
                fs_log.debug("Skipping device %s with family %s", device.host, device_family.value)
                continue

            if device_family == TapoDeviceFamily.PLUG:
                if (dump := self.collect_from_plug_device(device)) is None:
                    continue

                fs_log.debug("Building metrics for device: %s with address: %s", dump.alias, dump.host)

                metrics_map = (
                    self.options.per_device_family_metrics.plug if self.options.per_device_family_metrics else {}
                )
                for metric_type, metric in metrics_map.items():
                    fs_log.debug("Collecting metric: %s for device: %s", metric_type.value, dump.alias)
                    metrics.append(metric.get_metric_with_value(dump, labels=[dump.host, dump.alias or "unknown"]))

        return metrics

    def collect(self) -> Iterable[Metric]:
        """Export the metrics in a format suitable for Prometheus.

        Raises
        ------
        NotImplementedError
            If the method is not implemented by a subclass.

        Returns
        -------
        Iterable[Metric]
            Prometheus Metric Iterable with the collected metrics.

        """
        with self._metrics_lock:
            metrics = list(self._latest_metrics)
        yield from metrics
