# AGENTS

## Project summary and layout

`pyprom-exporters` provides Prometheus exporters for IoT devices. The Tapo plug exporter
uses `python-kasa` for asynchronous discovery and updates and `prometheus_client` for metrics.

- `src/pyprom_exporters/exporters/`: collector interfaces, Tapo configuration and exporter.
- `src/pyprom_exporters/task_collector/`: concurrent coroutine factories with retry/backoff.
- `src/pyprom_exporters/config.py`: OmegaConf-compatible application configuration.
- `src/pyprom_exporters/prom_exporter.py`: configuration loading, CLI and runtime lifecycle.
- `src/pyprom_exporters/benchmarks/`: offline device simulation and self-contained HTML reports.
- `tests/`: regression tests using fake devices; no physical plugs are needed.
- `docs/`: Sphinx/MyST documentation.
- `pyproject.toml` and `uv.lock`: package metadata, tools and locked dependencies.
- `Dockerfile`: runtime image built with uv, entering through `prom-exporter`.
- `config.yaml`: local runtime configuration, ignored by Git.
- `scratch/`: local audit and investigation notes, ignored by Git.
- `report/`: generated benchmark HTML and JSON, ignored by Git.

## Runtime and ownership

1. Merge YAML with the application schema, then apply environment and CLI overrides.
1. Persist the effective configuration atomically unless `--no-write-config` is supplied.
   Credentials are removed before serialization to disk.
1. Start the asyncio loop thread and retain ownership of the exporter before initializing it.
1. Discover devices and publish initial readings before starting the HTTP endpoint.
1. Probe on scrape by default (`refresh_interval: null`). Overlapping live scrapes share a refresh;
   healthy device results publish independently and release waiting scrapes before bounded recovery work.
   Timed-out callers read the latest available device snapshots while that refresh continues.
1. A positive integer refresh interval enables background polling and cached scrapes.
1. On SIGINT/SIGTERM, stop HTTP serving, cancel outstanding work, close devices and stop the loop.

Device operations run on the owning asyncio loop and are serialized by the device lock.
Metric snapshots have a separate thread lock. Synchronous `collect()` cannot wait on its own
asyncio loop; async callers should await `update_and_collect()` before collecting there.

`run_tasks_with_retry()` accepts coroutine factories, preserves input order, and uses a lazy
worker pool when concurrency is bounded. Retry waits occupy worker slots. Cancellation propagates
and joins sibling cleanup; it is never returned as an ordinary device failure.
Configured-host discovery, updates and disconnects use the device concurrency limit. Broadcast
discovery is delegated to python-kasa and is outside that worker bound.

Each whole SDK update has a finite `update_timeout` (default 10 seconds); native Kasa retries stay
inside that budget. Failed initialized devices and missing hosts recover in fair bounded waves
after healthy updates. Retired sessions must close before replacement, with bounded close attempts.
Device snapshots are immutable; update publication is O(1) per device and metric aggregation is
lazy O(N). Operational timestamp metrics describe successful SDK updates, not physical sensor freshness.

## Configuration

- Exporter settings live under `exporters.tapo`; CLI overrides environment, which overrides YAML.
- Credentials default to `TP_LINK_USERNAME` and `TP_LINK_PASSWORD`; configurable environment key
  names and explicit CLI/environment overrides are supported.
- `write_non_default_config` writes only values differing from defaults.
- Configuration persistence preserves explicit empty maps used to disable metric families.
- Metrics require a `host` label to distinguish devices. Missing/failed readings are omitted.
- Kasa energy readings are converted from kWh to the documented Wh units.

## Coding and documentation conventions

- Python 3.11+; CI covers Python 3.11 through 3.14. Ruff and ty target Python 3.11.
- Use type hints, `from __future__ import annotations`, dataclasses and NumPy-style docstrings.
- Ruff enables all stable and preview rules, with line length 119 and narrow documented exceptions.
- Use Ruff formatting and import ordering; ty checks typed and untyped function bodies.
- Documentation follows `.markdownlint-cli2.jsonc`, shared by editor and CLI checks.
- Keep audit and performance investigation notes in `scratch/`, not tracked documentation.

## Mandatory checks

After changes, run and ensure both commands pass:

```sh
uv run --locked pytest
uv run prek run --all-files
```

Pytest-xdist uses up to four workers by default. Use `-n 0` for serial debugging.
Build changed documentation with `uv run --locked docs`.

## Utilities

- `uv run prom-exporter`: run the exporter.
- `uv run docs` or `make docs`: build documentation.
- `uv run benchmark` or `make benchmark`: simulate fleet scaling; each run creates an ignored
  `report/<timestamp>-<suffix>/` directory containing `report.html` and `metrics.json`.
- `make coverage`: run parallel tests with SlipCover and generate the coverage badge.
- `make check`: run all prek hooks.

Benchmark serialization timings read a completed snapshot; they exclude live refresh waits and
HTTP serving. Simulations characterize software overhead, not physical-device capacity.
