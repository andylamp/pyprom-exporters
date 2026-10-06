# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for portable benchmark reports and artifact publication."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tracemalloc
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock
from urllib.parse import unquote

import pytest

from pyprom_exporters.benchmarks import scalability
from pyprom_exporters.benchmarks.report import render_html

if TYPE_CHECKING:
    from collections.abc import Mapping


class ReportParser(HTMLParser):
    """Collect rendered text and attributes without relying on page layout."""

    def __init__(self) -> None:
        """Initialize the parser and output collections."""
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.table_text: list[str] = []
        self.table_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record element names and decoded attribute values."""
        self.elements.append((tag, dict(attrs)))
        if tag == "table":
            self.table_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """Track table boundaries so embedded JSON cannot satisfy table assertions."""
        if tag == "table":
            self.table_depth -= 1

    def handle_data(self, data: str) -> None:
        """Record decoded visible text and inline resource contents."""
        self.text.append(data)
        if self.table_depth:
            self.table_text.append(data)


@pytest.fixture(name="results")
def benchmark_results() -> dict[str, object]:
    """Provide a complete benchmark result with identifiable metric values.

    Returns
    -------
    dict[str, object]
        Environment details and metrics for every supported report scenario.
    """
    return {
        "environment": {
            "python": "3.12.11 (benchmark test)",
            "platform": "test-platform",
            "recorded_at": "2026-09-26T12:00:00+00:00",
        },
        "settings": {
            "sizes": [111],
            "concurrency": [7],
            "simulated_io_latency_ms": 1.25,
            "serialization_scope": "cached_snapshot",
        },
        "exporter": [
            {
                "devices": 111,
                "concurrency": 7,
                "scenario": "healthy",
                "discovery": {"wall_ms": 112.0, "cpu_ms": 113.0},
                "update": {"wall_ms": 114.0, "cpu_ms": 115.0},
                "serialization": {"wall_ms": 116.0, "cpu_ms": 117.0, "bytes": 118, "samples": 119},
                "cleanup": {"wall_ms": 120.0, "cpu_ms": 121.0},
                "peak_active_io": 7,
                "peak_asyncio_tasks": 122,
                "remaining_asyncio_tasks": 0,
            },
        ],
        "retry_collector": [
            {
                "operations": 211,
                "concurrency": 7,
                "timing": {"wall_ms": 212.0, "cpu_ms": 213.0},
                "attempts": 214,
                "peak_active_io": 7,
                "peak_asyncio_tasks": 215,
                "remaining_asyncio_tasks": 0,
            },
        ],
        "missing_hosts": {
            "configured_devices": 311,
            "missing_devices": 312,
            "concurrency": 7,
            "discovery": {"wall_ms": 313.0, "cpu_ms": 314.0},
            "refresh": {"wall_ms": 315.0, "cpu_ms": 316.0},
            "refresh_io_calls": 317,
            "serialization": {"wall_ms": 318.0, "cpu_ms": 319.0, "bytes": 320, "samples": 321},
            "peak_active_io": 7,
            "peak_asyncio_tasks": 322,
        },
        "memory": {
            "devices": 411,
            "concurrency": 7,
            "repeated_refreshes": 412,
            "warm_bytes": 413,
            "final_bytes": 414,
            "peak_bytes": 415,
            "growth_after_warmup_bytes": -416,
            "after_cleanup_bytes": 417,
            "after_release_bytes": 418,
            "remaining_asyncio_tasks": 0,
        },
    }


def parsed_report(results: Mapping[str, object], *, metrics_filename: str = "metrics.json") -> ReportParser:
    """Render and parse a report for content and portability checks.

    Returns
    -------
    ReportParser
        Parsed HTML elements and text from the rendered benchmark report.
    """
    parser = ReportParser()
    parser.feed(render_html(results, metrics_filename=metrics_filename))
    parser.close()
    return parser


def test_html_contains_all_measurements_and_units(results: dict[str, object]) -> None:
    """The HTML includes every scenario's measurements with explicit units."""
    parser = parsed_report(results)
    text = " ".join(parser.table_text)
    for expected in (
        *range(111, 123),
        *range(211, 216),
        *range(311, 323),
        *range(411, 419),
    ):
        assert str(expected) in text
    assert "-416" in text
    for expected in ("test-platform", "3.12.11", "2026-09-26", "1.25", "ms", "bytes"):
        assert expected in text
    assert "Cached serialization wall (ms)" in text
    assert "Cached serialization CPU (ms)" in text
    assert "cached_snapshot" in text
    assert "not live-scrape latency measurements" in " ".join(parser.text)


def test_html_escapes_metadata_and_filename(results: dict[str, object]) -> None:
    """Untrusted machine metadata and filenames cannot create active markup."""
    payload = '<script>alert("injected")</script><img src=x onerror=alert(1)>&'
    filename = 'metrics" onclick="alert(1).json'
    results["environment"] = {"python": payload, "platform": payload, "recorded_at": payload}
    parser = parsed_report(results, metrics_filename=filename)
    assert payload in " ".join(parser.text)
    assert all(tag not in {"script", "img"} for tag, _ in parser.elements)
    assert all(not attribute.startswith("on") for _, attributes in parser.elements for attribute in attributes)
    links = [attributes["href"] for tag, attributes in parser.elements if tag == "a" and "href" in attributes]
    assert filename in [unquote(link) for link in links if link is not None]


def test_html_is_self_contained(results: dict[str, object]) -> None:
    """A report can be opened offline without scripts, stylesheets, or CDNs."""
    parser = parsed_report(results)
    assert any(tag == "style" for tag, _ in parser.elements)
    assert all(tag not in {"script", "link", "iframe", "object", "embed"} for tag, _ in parser.elements)
    for _, attributes in parser.elements:
        for attribute in ("src", "href"):
            if target := attributes.get(attribute):
                assert target == "metrics.json" or target.startswith(("#", "data:"))


def test_runs_publish_html_and_original_metrics_without_overwriting(
    tmp_path: Path, results: dict[str, object]
) -> None:
    """Repeated runs retain their own complete HTML and unchanged JSON metrics."""
    output_dir = tmp_path / "report"
    first_run = scalability.write_reports(results, output_dir)
    first_html = (first_run / "report.html").read_text(encoding="utf-8")
    second_run = scalability.write_reports(results, output_dir)
    assert first_run != second_run
    assert {first_run, second_run} == set(output_dir.iterdir())
    for run in (first_run, second_run):
        assert run.parent == output_dir
        assert {item.name for item in run.iterdir()} == {"metrics.json", "report.html"}
        assert json.loads((run / "metrics.json").read_text(encoding="utf-8")) == results
        assert (run / "report.html").read_text(encoding="utf-8") == first_html


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_metrics_do_not_publish_a_run(tmp_path: Path, results: dict[str, object], invalid: float) -> None:
    """Nonstandard JSON numbers are rejected before incomplete artifacts escape."""
    output_dir = tmp_path / "report"
    results["invalid"] = invalid
    with pytest.raises(ValueError, match="JSON"):
        scalability.write_reports(results, output_dir)
    assert not output_dir.exists() or not list(output_dir.iterdir())


def test_failed_render_does_not_publish_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: dict[str, object]
) -> None:
    """A renderer failure leaves existing results intact and no partial run."""
    output_dir = tmp_path / "report"
    existing_run = scalability.write_reports(results, output_dir)
    monkeypatch.setattr(scalability, "render_html", Mock(side_effect=RuntimeError("render failed")))
    with pytest.raises(RuntimeError, match="render failed"):
        scalability.write_reports(results, output_dir)
    assert list(output_dir.iterdir()) == [existing_run]
    assert json.loads((existing_run / "metrics.json").read_text(encoding="utf-8")) == results


def test_failed_file_write_removes_partial_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: dict[str, object]
) -> None:
    """An I/O failure removes the temporary run even after JSON was written."""
    output_dir = tmp_path / "report"
    write_text = Path.write_text

    def fail_html_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.name == "report.html":
            assert (path.parent / "metrics.json").is_file()
            message = "simulated disk failure"
            raise OSError(message)
        return write_text(path, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", fail_html_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        scalability.write_reports(results, output_dir)
    assert not list(output_dir.iterdir())


def test_cli_defaults_to_report_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real minimal offline run publishes both reports under the default path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--sizes", "1", "--concurrency", "1", "--latency-ms", "0"])
    previous_logging_level = logging.root.manager.disable
    try:
        scalability.main()
    finally:
        logging.disable(previous_logging_level)
    runs = list((tmp_path / "report").iterdir())
    assert len(runs) == 1
    assert {path.name for path in runs[0].iterdir()} == {"metrics.json", "report.html"}
    metrics = json.loads((runs[0] / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["settings"] == {
        "sizes": [1],
        "concurrency": [1],
        "simulated_io_latency_ms": 0.0,
        "serialization_scope": "cached_snapshot",
    }
    assert metrics["exporter"][0]["devices"] == 1
    assert metrics["memory"]["remaining_asyncio_tasks"] == 0
    output = capsys.readouterr().out
    assert "report.html" in output
    assert "metrics.json" in output


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_memory_measurement_stops_tracing_after_failure(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    """A failed or cancelled measurement leaves global allocation tracing disabled."""
    monkeypatch.setattr(scalability, "scrape", Mock(side_effect=failure("injected serialization failure")))
    with pytest.raises(failure, match="injected serialization failure"):
        asyncio.run(scalability.measure_memory(1, 1))
    assert not tracemalloc.is_tracing()


def test_memory_measurement_preserves_existing_tracing() -> None:
    """An existing tracing session is rejected without clearing the caller's measurements."""
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match="tracing is already active"):
            asyncio.run(scalability.measure_memory(1, 1))
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_cached_serialization_performs_no_device_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cached serialization excludes device refresh even when the exporter uses live mode."""
    activity = scalability.Activity(0)
    fleet = scalability.Fleet(activity)
    monkeypatch.setattr("pyprom_exporters.exporters.tapo.Discover.discover_single", fleet.discover)

    async def scenario() -> None:
        exporter = scalability.exporter_for(3, 1)
        try:
            await exporter.discover()
            await exporter.update_and_collect()
            before = activity.calls
            measured = scalability.scrape(exporter, 2)
            assert activity.calls == before
            # Six measurements and five diagnostics per device, plus four global samples.
            assert measured["samples"] == 3 * (6 + 5) + 4
        finally:
            await exporter.cleanup()

    asyncio.run(scenario())
