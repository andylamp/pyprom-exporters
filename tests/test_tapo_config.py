"""Tests for Tapo OmegaConf/dataclass configuration behavior."""

import pytest
from omegaconf import OmegaConf

from pyprom_exporters.exporters import tapo as tapo_module
from pyprom_exporters.exporters.tapo import TapoDiscoveryOptions, TapoExporterOptions


def test_tapo_discovery_options_uses_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use default env var keys to populate discovery credentials.

    Parameters
    ----------
    monkeypatch
        Pytest fixture used to set environment variables for the duration of the test.

    """
    monkeypatch.setenv("TP_LINK_USERNAME", "test-user")
    monkeypatch.setenv("TP_LINK_PASSWORD", "test-pass")

    options = TapoDiscoveryOptions()

    assert options.credentials is not None  # noqa: S101
    assert options.credentials.username == "test-user"  # noqa: S101
    assert options.credentials.password == "test-pass"  # noqa: S101, S105


def test_tapo_discovery_options_custom_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use custom env var keys to populate discovery credentials.

    Parameters
    ----------
    monkeypatch
        Pytest fixture used to set environment variables for the duration of the test.

    """
    monkeypatch.setenv("CUSTOM_TAPO_USER", "custom-user")
    monkeypatch.setenv("CUSTOM_TAPO_PASS", "custom-pass")

    options = TapoDiscoveryOptions(
        tapo_username_env_key="CUSTOM_TAPO_USER",
        tapo_password_env_key="CUSTOM_TAPO_PASS",  # noqa: S106
    )

    assert options.credentials is not None  # noqa: S101
    assert options.credentials.username == "custom-user"  # noqa: S101
    assert options.credentials.password == "custom-pass"  # noqa: S101, S105


def test_tapo_exporter_options_default_subconfigs() -> None:
    """Ensure Tapo exporter options initialize nested sub-configs with expected defaults."""
    options = TapoExporterOptions()

    assert options.discovery_options is not None  # noqa: S101
    assert options.prometheus_options is not None  # noqa: S101
    assert options.prometheus_options.refresh_interval == tapo_module.DEFAULT_REFRESH_INTERVAL  # noqa: S101
    assert options.supported_device_families == {tapo_module.TapoDeviceFamily.PLUG: True}  # noqa: S101
    assert options.per_device_family_metrics is not None  # noqa: S101
    assert set(options.per_device_family_metrics.plug.keys()) == set(  # noqa: S101
        tapo_module.DEFAULT_PER_PLUG_METRICS.keys()
    )


def test_tapo_exporter_options_metrics_are_independent() -> None:
    """Verify per-device-family metric dicts are not shared between exporter option instances."""
    options_a = TapoExporterOptions()
    options_b = TapoExporterOptions()

    assert options_a.per_device_family_metrics is not None  # noqa: S101
    assert options_b.per_device_family_metrics is not None  # noqa: S101

    metric_key = next(iter(options_a.per_device_family_metrics.plug))
    options_a.per_device_family_metrics.plug.pop(metric_key)

    assert len(options_b.per_device_family_metrics.plug) == len(tapo_module.DEFAULT_PER_PLUG_METRICS)  # noqa: S101


def test_tapo_exporter_options_omegaconf_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roundtrip structured OmegaConf config into a dataclass object with expected values.

    Parameters
    ----------
    monkeypatch
        Pytest fixture used to set environment variables for the duration of the test.

    """
    monkeypatch.setenv("TP_LINK_USERNAME", "roundtrip-user")
    monkeypatch.setenv("TP_LINK_PASSWORD", "roundtrip-pass")

    cfg = OmegaConf.structured(TapoExporterOptions)
    options = OmegaConf.to_object(cfg)

    assert isinstance(options, TapoExporterOptions)  # noqa: S101
    assert options.supported_device_families == {tapo_module.TapoDeviceFamily.PLUG: True}  # noqa: S101
    assert options.per_device_family_metrics is not None  # noqa: S101
    assert options.discovery_options is not None  # noqa: S101
    assert options.discovery_options.credentials is not None  # noqa: S101
    assert options.discovery_options.credentials.username == "roundtrip-user"  # noqa: S101
    assert options.discovery_options.credentials.password == "roundtrip-pass"  # noqa: S101, S105
