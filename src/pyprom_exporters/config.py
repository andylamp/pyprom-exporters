"""Application configuration models for pyprom_exporters."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyprom_exporters.exporters.tapo import TapoExporterOptions

DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_PROMETHEUS_PORT = 8090


@dataclass
class ExportersConfig:
    """Exporter-specific configuration blocks."""

    tapo: TapoExporterOptions = field(default_factory=TapoExporterOptions)
    """Tapo exporter configuration."""


@dataclass
class PromExporterConfig:
    """Top-level configuration for the Prometheus exporter runtime."""

    config_file: str = DEFAULT_CONFIG_FILE
    """Default YAML filename to load configuration from."""
    write_non_default_config: bool = False
    """Whether to write only non-default config values back to YAML."""
    prometheus_port: int = DEFAULT_PROMETHEUS_PORT
    """Port used for the Prometheus HTTP server."""
    exporters: ExportersConfig = field(default_factory=ExportersConfig)
    """Exporter configuration sections."""
