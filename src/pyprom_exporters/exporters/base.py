"""Base class for the prometheus exporters."""

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

T = TypeVar("T")


@dataclass
class BasePrometheusOptions:
    """Base options for Prometheus configuration."""

    refresh_interval: int = 15
    """Interval in seconds to refresh the metrics, if less - we use the cached value."""


class BasePrometheusCollector(Collector):
    """Abstract base class for Prometheus exporters."""

    @abstractmethod
    def collect(self) -> Iterable[Metric]:
        """Export the metrics in a format suitable for Prometheus.

        Raises
        ------
        NotImplementedError
            If the method is not implemented by a subclass.

        Returns
        -------
        MetricWrapperBase
            A wrapper around the metrics to be exported.

        """
        msg = "Subclasses must implement this method."
        raise NotImplementedError(msg)

    async def cleanup(self) -> None:
        """Cleanup method to be called when the collector is no longer needed."""


class BasePrometheusMetricExporter(BasePrometheusCollector):
    """Abstract base class for Prometheus exporters."""

    @abstractmethod
    def register_devices(self, devices: list[Any]) -> None:
        """Register devices for monitoring.

        Parameters
        ----------
        devices : list[Any]
            List of devices to register.

        Raises
        ------
        NotImplementedError
            If the method is not implemented by a subclass.

        """
        msg = "Subclasses must implement this method."
        raise NotImplementedError(msg)
