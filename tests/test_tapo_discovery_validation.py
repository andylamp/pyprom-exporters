# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Reject unsafe discovery settings before configuration writes or device I/O."""

from __future__ import annotations

import math
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from kasa import Credentials

from pyprom_exporters import prom_exporter as runtime
from pyprom_exporters.exporters.tapo import TapoDiscoveryOptions

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def isolated_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep configuration checks independent of real device credentials."""
    monkeypatch.delenv("TP_LINK_USERNAME", raising=False)
    monkeypatch.delenv("TP_LINK_PASSWORD", raising=False)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("discovery_packets", 0),
        ("discovery_packets", True),
        ("discovery_packets", 1.5),
        ("discovery_timeout", 0),
        ("discovery_timeout", math.inf),
        ("timeout", -1),
        ("timeout", math.nan),
        ("port", 0),
        ("port", 65536),
        ("port", False),
        ("port", 1.5),
    ],
)
def test_invalid_discovery_values_fail_at_construction(field_name: str, value: object) -> None:
    """Invalid numeric values cannot reach the Kasa discovery implementation."""
    options: dict[str, Any] = {field_name: value, "credentials": Credentials()}
    with pytest.raises(ValueError, match=field_name):
        TapoDiscoveryOptions(**options)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("discovery_packets", 0), ("discovery_timeout", -1), ("timeout", 0), ("port", 65536)],
)
@pytest.mark.usefixtures("isolated_credentials")
def test_structured_yaml_rejects_invalid_discovery_values(tmp_path: Path, field_name: str, value: int) -> None:
    """Dataclass validation also runs when structured YAML becomes runtime options."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"exporters:\n  tapo:\n    discovery_options:\n      {field_name}: {value}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match=field_name):
        runtime.load_app_config(str(config_path))


@pytest.mark.usefixtures("isolated_credentials")
def test_invalid_runtime_config_is_not_written_or_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad packet count fails before rewriting YAML or constructing an exporter."""
    config_path = tmp_path / "config.yaml"
    content = "exporters:\n  tapo:\n    discovery_options:\n      discovery_packets: 0\n"
    config_path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["prom-exporter", "--config", str(config_path)])
    construct_exporter = Mock()
    monkeypatch.setattr(runtime, "TapoPowerPlugPrometheusExporter", construct_exporter)
    with pytest.raises(SystemExit) as caught:
        runtime.main()
    assert caught.value.code == 1
    assert config_path.read_text(encoding="utf-8") == content
    construct_exporter.assert_not_called()


@pytest.mark.usefixtures("isolated_credentials")
def test_optional_discovery_defaults_and_valid_boundaries_remain_supported(tmp_path: Path) -> None:
    """Valid limits and optional library defaults survive structured YAML loading."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "exporters:\n  tapo:\n    discovery_options:\n"
        "      discovery_packets: 1\n      discovery_timeout: 1\n      timeout: null\n      port: 65535\n",
        encoding="utf-8",
    )
    app, _, _ = runtime.load_app_config(str(config_path))
    assert app.exporters.tapo.discovery_options == TapoDiscoveryOptions(
        discovery_packets=1, discovery_timeout=1, timeout=None, port=65535, credentials=Credentials()
    )
    assert TapoDiscoveryOptions(credentials=Credentials()).port is None
