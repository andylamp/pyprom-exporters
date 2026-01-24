# AGENTS

## Project summary

`pyprom-exporters` is a Python package that provides Prometheus exporters focused on IoT / smart
home devices.
The current concrete exporter targets TP-Link Tapo power plugs using `python-kasa`, with async
discovery and metric collection, exposed via `prometheus_client`.

## Repo layout

- `src/pyprom_exporters/`: Python package source.
- `exporters/`: Exporter implementations and base classes.
- `task_collector/`: Async task runner with retry/backoff.
- `config.py`: OmegaConf-compatible dataclasses for app + exporter config.
- `prom_exporter.py`: Main exporter runtime (Prometheus HTTP server + collector registration).
- `kasa_probe.py`: Device probing / info printing helper and experiments.
- `test.py`, `ex_test.py`: Scratch/demo files (not real tests).
- `config.yaml`: Default runtime configuration.
- `prom.sh`: Helper for running a local Prometheus Docker container.
- `Dockerfile`: Container build (currently references `prom_exporter` module; may be stale).
- `pyproject.toml`: Project metadata and tooling configuration.

## Architecture and modularity

- `exporters/base.py` defines the abstract collector interface:
- `BasePrometheusCollector` is a `prometheus_client` `Collector` with `collect()` and
      optional `cleanup()`.
- `BasePrometheusMetricExporter` adds a device registration hook.
- `exporters/tapo.py` implements the Tapo power plug exporter:
- `TapoExporterOptions` aggregates discovery options, Prometheus options, supported device
      families (map of family to enabled), and per-device-family metrics.
- `TapoDiscoveryOptions` mirrors `python-kasa` discovery parameters and pulls credentials
      from env vars.
- `TapoPowerPlugPrometheusExporter` owns discovery, async update, and metric collection.
- Metrics are modeled via `TapoPlugGaugeMetric` and enumerated in `TapoPerPlugMetricType`.
- `task_collector/async_task_collector.py` provides `run_tasks_with_retry()`:
- Runs coroutine factories concurrently using `asyncio.TaskGroup`.
- Optional concurrency limit via semaphore.
- Retry logic with exponential backoff + jitter for transient errors.

## Runtime flow (prom_exporter.py)

1. Load YAML config with OmegaConf and merge with `PromExporterConfig` schema.
1. Apply CLI overrides for port, device list, and credentials (CLI takes precedence).
1. Start an asyncio event loop on a background thread.
1. Initialize `TapoPowerPlugPrometheusExporter`, run discovery, and cache initial metrics.
1. Start background updates on the asyncio loop; Prometheus scrapes read cached metrics only.
1. Register the collector with `prometheus_client.REGISTRY`.
1. Start HTTP server using `prometheus_port` (default 8090).
1. Handle SIGINT/SIGTERM for graceful shutdown and cleanup.

## Configuration

- `config.yaml` is an OmegaConf-structured config for `PromExporterConfig` with exporter config
    nested under `exporters.tapo`.
- `write_non_default_config` controls whether the config is written back with only non-default
    values.
- Credentials are read from env keys:
- `TP_LINK_USERNAME`
- `TP_LINK_PASSWORD`
- CLI flags can override device IPs, port, and credentials; they always take precedence.
- `prom_exporter.py` will populate Tapo credentials from the configured env var keys if
    `discovery_options.credentials` is not set.
- `prom_exporter.py` writes the merged config back to YAML so defaults are explicit.

## Coding preferences and conventions

- Python 3.11+ (targeted as 3.12 in tooling).
- Heavy use of type hints, `from __future__ import annotations`, and dataclasses.
- Async-first design for device discovery, updates, and retries.
- Docstrings use numpy-style sections.
- Logging via `logging` module; explicit debug/info statements.
- Formatting and linting:
- `ruff` with line length 119, Black-like formatting, isort rules.
- `pylint` enabled with the same max line length.
- `mypy` checks untyped defs.
- `markdownlint` uses `.markdownlint-cli2.jsonc` for shared VS Code + CLI configuration.
- Documentation style: numpy-style docstrings.

## Doc linting

All documentation in this repository should follow the Markdown style guide enforced by
`markdownlint`. The rules live in `.markdownlint-cli2.jsonc`.

## Testing

When crucial functionality is changed or tests are added, run `pytest` to ensure the suite passes.

## Utilities and entry points

- CLI entry points from `pyproject.toml`:
- `prom-exporter = pyprom_exporters.prom_exporter:main`
- `kasa-probe = pyprom_exporters.kasa_probe:main`
- `prom.sh` can run a local Prometheus container for manual testing.
- `Dockerfile` builds an image using `uv` and runs `prom-exporter`, allowing runtime env vars
    to pass devices and credentials; port is exposed based on `PROMETHEUS_PORT` build arg.

## Notes / rough edges

- `main.py` is empty and can be ignored.
- `test.py` and `ex_test.py` are exploratory snippets, not a formal test suite.
- Some script-style code paths in `kasa_probe.py` and `prom_exporter.py` are still experimental
    (e.g., inline device lists and direct print statements).
