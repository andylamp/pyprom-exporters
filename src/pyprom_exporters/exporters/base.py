# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Base class for the prometheus exporters."""

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector


@dataclass
class BasePrometheusOptions:
    """Base options for Prometheus configuration."""

    refresh_interval: int | None = 15
    """Refresh interval in seconds; set to ``None`` to disable background refresh and update on scrape."""


class BasePrometheusCollector(Collector):
    """Abstract base class for Prometheus exporters."""

    @abstractmethod
    def collect(self) -> Iterable[Metric]:
        """Export the metrics in a format suitable for Prometheus.

        Returns
        -------
        Iterable[Metric]
            Metric families exported to Prometheus.

        Raises
        ------
        NotImplementedError
            If the method is not implemented by a subclass.

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
