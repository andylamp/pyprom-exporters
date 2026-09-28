# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Render benchmark measurements as an accessible, self-contained HTML report."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import cast
from urllib.parse import quote

_STYLE = """
:root { color-scheme: light dark; font: 16px/1.55 system-ui, sans-serif; }
body { max-width: 90rem; margin: auto; padding: 2rem; background: #f6f8fc; color: #19283c; }
h1, h2, h3 { line-height: 1.2; }
h1 { font-size: 2.3rem; margin-bottom: .5rem; }
h2 { margin-top: 0; }
section, header { margin: 0 0 1.5rem; padding: 1.5rem; border-radius: .75rem; background: white; }
header { border-top: .4rem solid #245aab; }
a { color: #1856a6; }
p { max-width: 85ch; }
.lead { font-size: 1.1rem; }
.table-scroll { overflow-x: auto; margin: 1rem 0; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
caption { text-align: left; font-weight: 650; margin-bottom: .65rem; }
th, td { text-align: right; padding: .6rem .8rem; border-bottom: 1px solid #dde3ed; }
th { background: #edf2fa; }
th:first-child, td:first-child { text-align: left; }
tbody tr:nth-child(even) { background: #f8faff; }
th { white-space: nowrap; }
.metadata th, .metadata td { text-align: left; overflow-wrap: anywhere; }
.metadata th { width: 16rem; }
.chart { margin: 1rem 0; padding: 0; list-style: none; }
.chart li { display: grid; grid-template-columns: minmax(13rem, 2fr) 3fr 7rem;
  gap: .8rem; align-items: center; margin: .65rem 0; }
.track { height: 1rem; background: #e9eef7; border-radius: .25rem; }
.bar { height: 100%; background: #245aab; border-radius: .25rem; }
.value { text-align: right; font-variant-numeric: tabular-nums; }
.note { color: #4b5c73; }
pre { overflow-x: auto; padding: 1rem; background: #edf2fa; font-size: .85rem; }
summary { cursor: pointer; font-weight: 650; }
footer { padding: 0 1.5rem; color: #4b5c73; }
@media (max-width: 650px) {
  body { padding: .5rem; } section, header { padding: 1rem; }
  .chart li { grid-template-columns: 1fr 5rem; }
  .chart .label { grid-column: 1 / -1; }
}
@media (prefers-color-scheme: dark) {
  body { background: #101827; color: #e0e8f5; }
  section, header { background: #192538; }
  a { color: #9ec3ff; }
  th, pre, .track { background: #25354e; }
  tbody tr:nth-child(even) { background: #1d2b41; }
  th, td { border-color: #35455d; }
  .bar { background: #79aaf6; }
  .note, footer { color: #b7c6da; }
}
@media print {
  body { background: white; color: black; padding: 0; }
  section, header { border: 1px solid #ccc; break-inside: avoid; }
  .table-scroll { overflow: visible; } th, td { padding: .3rem; font-size: .8rem; }
}
"""


@dataclass(frozen=True)
class _Column:
    """Describe a table column and its path within a measurement row."""

    title: str
    path: tuple[str, ...]


def _mapping(value: object) -> Mapping[str, object]:
    """Return a mapping when present, otherwise an empty measurement.

    Returns
    -------
    Mapping[str, object]
        The supplied mapping, or an empty mapping for other values.

    """
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, object]]:
    """Extract mapping rows while tolerating absent or empty scenarios.

    Returns
    -------
    list[Mapping[str, object]]
        Measurement rows containing mapping values only.

    """
    if not isinstance(value, (list, tuple)):
        return []
    return [_mapping(row) for row in value if isinstance(row, Mapping)]


def _lookup(row: Mapping[str, object], path: tuple[str, ...]) -> object:
    """Read a nested metric without requiring every measurement to be present.

    Returns
    -------
    object
        The nested measurement, or None when its path is unavailable.

    """
    value: object = row
    for key in path:
        value = _mapping(value).get(key)
    return value


def _formatted(value: object) -> str:
    """Format measurements with consistent precision and explicit missing values.

    Returns
    -------
    str
        Human-readable measurement text; absent values use an em dash.

    """
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.3f}" if math.isfinite(value) else "—"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_formatted(item) for item in value) or "—"
    return str(value)


def _table(title: str, rows: Sequence[Mapping[str, object]], columns: Sequence[_Column]) -> str:
    """Render escaped measurements in a table with an accessible caption.

    Returns
    -------
    str
        An HTML table or a message explaining that measurements are absent.

    """
    if not rows:
        return f"<p>{escape(title)}: no measurements recorded.</p>"
    headers = "".join(f'<th scope="col">{escape(column.title)}</th>' for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(_formatted(_lookup(row, column.path)))}</td>" for column in columns) + "</tr>"
        for row in rows
    )
    return (
        '<div class="table-scroll" tabindex="0" role="region" '
        f'aria-label="{escape(title, quote=True)}">'
        f"<table><caption>{escape(title)}</caption><thead><tr>{headers}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _metadata(title: str, fields: Sequence[tuple[str, object]]) -> str:
    """Render labeled settings or scalar measurements.

    Returns
    -------
    str
        An escaped HTML table containing each label and value.

    """
    body = "".join(
        f'<tr><th scope="row">{escape(label)}</th><td>{escape(_formatted(value))}</td></tr>' for label, value in fields
    )
    return f'<table class="metadata"><caption>{escape(title)}</caption><tbody>{body}</tbody></table>'


def _timing_columns(*phases: str) -> list[_Column]:
    """Create adjacent wall and CPU columns with their millisecond units.

    Returns
    -------
    list[_Column]
        Wall-time and CPU-time columns for each requested phase.

    """
    titles = {"serialization": "Cached serialization"}
    return [
        _Column(
            f"{titles.get(phase, phase.replace('_', ' ').capitalize())} {clock} (ms)", (phase, f"{clock.lower()}_ms")
        )
        for phase in phases
        for clock in ("wall", "CPU")
    ]


def _resources() -> list[_Column]:
    """Describe concurrency and task observations common to timing scenarios.

    Returns
    -------
    list[_Column]
        Columns for configured concurrency and observed I/O and task counts.

    """
    return [
        _Column("Concurrency limit", ("concurrency",)),
        _Column("Peak active I/O", ("peak_active_io",)),
        _Column("Peak asyncio tasks", ("peak_asyncio_tasks",)),
    ]


def _chart(rows: Sequence[Mapping[str, object]]) -> str:
    """Draw refresh scaling with numeric labels and proportional CSS bars.

    Returns
    -------
    str
        An HTML bar chart with inline dimensions and numeric timing labels.

    """
    measurements = []
    for row in rows:
        wall = _lookup(row, ("update", "wall_ms"))
        if isinstance(wall, (float, int)) and math.isfinite(wall) and wall >= 0:
            label = f"{_formatted(row.get('devices'))} devices / {_formatted(row.get('concurrency'))} workers"
            measurements.append((label, float(wall)))
    if not measurements:
        return "<p>No refresh measurements recorded.</p>"
    maximum = max(wall for _, wall in measurements)
    bars = "".join(
        f'<li><span class="label">{escape(label)}</span>'
        f'<div class="track" aria-hidden="true"><div class="bar" '
        f'style="width:{wall / maximum * 100 if maximum else 0:.3f}%"></div></div>'
        f'<span class="value">{wall:,.3f} ms</span></li>'
        for label, wall in measurements
    )
    return f'<ul class="chart" aria-label="Median refresh wall time">{bars}</ul>'


def _exporter_section(rows: Sequence[Mapping[str, object]], *, failures: bool) -> str:
    """Present exporter timings and resource bounds for one scenario.

    Returns
    -------
    str
        The scenario heading, timing table, and scaling chart as HTML.

    """
    scenario = "authentication_failures_10_percent" if failures else "healthy"
    selected = [row for row in rows if row.get("scenario") == scenario]
    title = "Authentication failures" if failures else "Healthy fleet scaling"
    description = (
        "Every tenth device, starting at index zero, fails authentication on update. "
        "The proportion can exceed 10% for small fleets."
        if failures
        else "All configured devices discover and refresh successfully. Bars compare median refresh wall time."
    )
    identity = [_Column("Devices", ("devices",)), _Column("Concurrency limit", ("concurrency",))]
    timings = _table(
        "Exporter timings",
        selected,
        [*identity, *_timing_columns("discovery", "update", "serialization", "cleanup")],
    )
    resources = _table(
        "Exporter resources and Prometheus output",
        selected,
        [
            identity[0],
            *_resources(),
            _Column("Remaining tasks", ("remaining_asyncio_tasks",)),
            _Column("Samples", ("serialization", "samples")),
            _Column("Payload (bytes)", ("serialization", "bytes")),
        ],
    )
    return f"<section><h2>{title}</h2><p>{description}</p>{_chart(selected)}{timings}{resources}</section>"


def _retry_section(results: Mapping[str, object]) -> str:
    """Present transient timeout scheduling overhead and observed task bounds.

    Returns
    -------
    str
        The retry scenario description and its measurement table as HTML.

    """
    table = _table(
        "Retry collector measurements",
        _rows(results.get("retry_collector")),
        [
            _Column("Operations", ("operations",)),
            *_resources(),
            *_timing_columns("timing"),
            _Column("Total I/O attempts", ("attempts",)),
            _Column("Remaining tasks", ("remaining_asyncio_tasks",)),
        ],
    )
    return (
        "<section><h2>Retries under timeouts</h2><p>Every tenth operation times out permanently. "
        "Each failing operation receives three total attempts, with exponential backoff starting at 1 ms "
        "and zero jitter. Other operations complete normally.</p>"
        f"{table}</section>"
    )


def _missing_section(results: Mapping[str, object]) -> str:
    """Present rediscovery cost when most configured hosts are unavailable.

    Returns
    -------
    str
        The unavailable-host scenario and measurement table as HTML.

    """
    missing = _mapping(results.get("missing_hosts"))
    table = _table(
        "Missing-host measurements",
        [missing] if missing else [],
        [
            _Column("Configured devices", ("configured_devices",)),
            _Column("Missing devices", ("missing_devices",)),
            *_resources(),
            *_timing_columns("discovery", "refresh", "serialization"),
            _Column("Refresh I/O calls", ("refresh_io_calls",)),
            _Column("Samples", ("serialization", "samples")),
            _Column("Payload (bytes)", ("serialization", "bytes")),
        ],
    )
    return (
        "<section><h2>Missing hosts</h2><p>The first floor(90% of configured devices) time out during "
        "discovery. Rediscovery is forced during refresh to measure the cost while healthy devices "
        f"continue publishing. This uses the largest fleet and middle configured concurrency.</p>{table}</section>"
    )


def _memory_section(results: Mapping[str, object]) -> str:
    """Report Python allocation retention separately from timing measurements.

    Returns
    -------
    str
        The allocation measurements and interpretation as HTML.

    """
    memory = _mapping(results.get("memory"))
    fields = (
        ("Devices", "devices"),
        ("Concurrency limit", "concurrency"),
        ("Repeated refreshes", "repeated_refreshes"),
        ("Warm allocations (bytes)", "warm_bytes"),
        ("Final allocations (bytes)", "final_bytes"),
        ("Peak allocations (bytes)", "peak_bytes"),
        ("Growth after warmup (bytes)", "growth_after_warmup_bytes"),
        ("After cleanup (bytes)", "after_cleanup_bytes"),
        ("After exporter release (bytes)", "after_release_bytes"),
        ("Remaining tasks", "remaining_asyncio_tasks"),
    )
    table = _metadata("Python allocation measurements", [(label, memory.get(key)) for label, key in fields])
    return (
        "<section><h2>Memory retention</h2><p>A separate zero-latency run uses tracemalloc and garbage "
        "collection. Warm, final, peak, cleanup, and release values are relative to the initial baseline. "
        "Growth is final minus warm allocations. These values measure Python allocations, not process RSS "
        "or device library native allocations. Cleanup retains the exporter object; release deletes it.</p>"
        f"{table}</section>"
    )


def _methodology() -> str:
    """Explain measurement boundaries and simulated-device limitations.

    Returns
    -------
    str
        An HTML section describing measurement scope and limitations.

    """
    return """
<section><h2>Methodology and interpretation</h2>
<ul>
<li>Simulated device discovery, updates, and disconnects await the configured I/O latency.
Real exporter scheduling, metric caching, retry logic, cleanup, and Prometheus text serialization run locally.
No discovery packets, device requests, or real credentials are used.</li>
<li>Discovery and cleanup each measure one operation. Update times are medians of three refreshes;
healthy and authentication-failure cached serialization times are medians of five serializations.
The missing-host scenario measures one refresh and one cached serialization.
CPU and wall medians are computed independently.</li>
<li>Serialization reads the cached snapshot on the exporter event loop, where collect() skips waiting
for a refresh to avoid blocking its own loop. No device I/O occurs during serialization. This measures
cached serialization cost, not live-scrape latency; live-refresh wait time and HTTP transport are excluded.</li>
<li>Wall time uses a monotonic performance clock; CPU time uses process CPU time. Timings include Python
overhead and simulated waiting. These are observations without confidence intervals or pass/fail thresholds.</li>
<li>Scenarios run sequentially to avoid contention between scenarios. Memory tracing runs separately
from the timing cases, after warmup, with repeated refresh and scrape cycles.</li>
<li>Peak active I/O is observed across simulated operations. Peak asyncio tasks is sampled when active I/O
sets a new high, so it is an observation rather than a continuous task-count bound. Peak task counts include
the benchmark driver; remaining task counts exclude it.</li>
<li>Configured concurrency bounds overlapping device operations, while metric storage and serialization
grow with fleet size. The benchmark requires a positive concurrency limit.</li>
<li>This benchmark measures simulated software overhead. It does not establish physical-device capacity,
network throughput, Wi-Fi contention, authentication/cryptography cost, HTTP server performance, or behavior
under real device and network failures. Compare runs on the same machine and Python version; validate
production limits with the intended physical fleet.</li>
</ul></section>
"""


def render_html(results: Mapping[str, object], *, metrics_filename: str = "metrics.json") -> str:
    """Build an offline HTML report with embedded metrics and no external assets.

    Parameters
    ----------
    results
        JSON-compatible measurements returned by the scalability benchmark.
    metrics_filename
        Name of the neighboring JSON artifact, offered as an optional download.

    Returns
    -------
    str
        A complete UTF-8 HTML document usable without the JSON artifact.
    """
    environment = _mapping(results.get("environment"))
    settings = _mapping(results.get("settings"))
    metadata = _metadata(
        "Run environment and settings",
        [
            ("Recorded at (UTC)", environment.get("recorded_at")),
            ("Python", environment.get("python")),
            ("Platform", environment.get("platform")),
            ("Fleet sizes", settings.get("sizes")),
            ("Concurrency limits", settings.get("concurrency")),
            ("Simulated I/O latency (ms)", settings.get("simulated_io_latency_ms")),
            ("Serialization scope", settings.get("serialization_scope")),
        ],
    )
    exporter = _rows(results.get("exporter"))
    sections = "".join(
        (
            _exporter_section(exporter, failures=False),
            _exporter_section(exporter, failures=True),
            _retry_section(results),
            _missing_section(results),
            _memory_section(results),
            _methodology(),
        )
    )
    embedded = escape(json.dumps(dict(results), indent=2, sort_keys=True))
    download = escape(quote(metrics_filename, safe=""), quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exporter scalability benchmark</title><style>{_STYLE}</style></head>
<body><header><h1>Exporter scalability benchmark</h1>
<p class="lead">Simulated fleet measurements for scheduling, refresh, cached serialization, and cleanup.</p>
<p><strong>Serialization measures the cached snapshot only.</strong>
Live-refresh wait time and HTTP transport are excluded; these are not live-scrape latency measurements.</p>
<p class="note">All tables, charts, and raw measurements are embedded in this file. It can be copied
and opened offline on its own. Timings are milliseconds; memory and payload sizes are bytes.</p>
<p><a href="{download}" download>Download metrics JSON</a> (adjacent file)</p>
{metadata}</header><main>{sections}
<section><h2>Embedded metrics</h2><details><summary>View complete metrics JSON</summary>
<pre>{embedded}</pre></details></section></main>
<footer><p>pyprom-exporters · deterministic simulated I/O, measured local runtime overhead.</p></footer>
</body></html>
"""
