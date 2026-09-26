# Python Prometheus Exporters for IoT Devices

[![Linters](https://github.com/andylamp/pyprom-exporters/actions/workflows/ci.yml/badge.svg)](https://github.com/andylamp/pyprom-exporters/actions/workflows/ci.yml)
[![Release](https://github.com/andylamp/pyprom-exporters/actions/workflows/release.yml/badge.svg)](https://github.com/andylamp/pyprom-exporters/actions/workflows/release.yml)
[![Coverage](https://raw.githubusercontent.com/andylamp/pyprom-exporters/main/coverage.svg)](https://github.com/andylamp/pyprom-exporters/actions/workflows/ci.yml)

Python Prometheus Exporters (`pyprom-exporters`) is a small Python package that exposes Prometheus
metrics for IoT / smart-home
devices.

The current concrete exporter targets TP-Link Tapo smart plugs via `python-kasa`.

## What It Does

- Discovers Tapo devices on your LAN (UDP broadcast) and/or monitors an explicit list of device IPs.
- Updates device state on a background asyncio loop (or on scrape), with retries and backoff.
- Exposes metrics via a Prometheus HTTP endpoint.

## How It Works

- `prom-exporter` starts an asyncio event loop on a background thread.
- The Tapo exporter runs discovery, then chooses update mode from
  `exporters.tapo.prometheus_options.refresh_interval`:
  - Integer value: performs an initial update pass and periodic background updates, and scrapes read
    cached metrics.
  - `null`: disables background updates and refreshes metrics when Prometheus calls `collect()`.

To reduce device/network traffic, set `refresh_interval` to match your Prometheus scrape interval
(or higher). Set it to `null` when you want scrape-time freshness.

## Project Layout

- `src/pyprom_exporters/prom_exporter.py`: CLI entry point and runtime wiring.
- `src/pyprom_exporters/config.py`: OmegaConf-compatible dataclass configuration models.
- `src/pyprom_exporters/exporters/tapo.py`: Tapo smart plug exporter implementation.
- `src/pyprom_exporters/task_collector/async_task_collector.py`: async retry runner used for device
  updates.
- `tests/`: unit tests for config/exporter behavior.

## Requirements

- Python 3.11+ (uses `asyncio.TaskGroup`).
- Network reachability from the exporter host to the devices.
- Tapo credentials (provided via env vars or CLI; never written to `config.yaml`).

## Installation

This repo is set up to use `uv` and a checked-in `uv.lock`.

```sh
# Development (includes the dev dependency group by default)
uv sync --locked

# Minimal runtime environment
uv sync --locked --no-dev
```

## Running

1. Provide credentials (default env keys):

```sh
export TP_LINK_USERNAME="you@example.com"
export TP_LINK_PASSWORD="your-password"
```

1. Run the exporter:

```sh
uv run prom-exporter \
  --prometheus-port 8090 \
  --tapo-plug-devices 10.10.2.100,10.10.2.101
```

You can also reduce log verbosity:

```sh
uv run prom-exporter --log-level WARNING
```

1. Scrape metrics:

- `http://localhost:8090/metrics`

### Environment Variable Overrides

The runtime supports a few convenience overrides:

- Precedence: CLI flags override env vars; env vars override `config.yaml`.
- `PYPROM_EXPORTERS_LOG_LEVEL` (or `LOG_LEVEL`): overrides `log_level`.
- `PROMETHEUS_PORT`: overrides `prometheus_port`.
- `TAPO_PLUG_DEVICES`: overrides `exporters.tapo.devices` (space or comma-separated).
- `TAPO_USERNAME` / `TAPO_PASSWORD`: overrides credentials directly.

## Configuration (`config.yaml`)

`prom-exporter` reads `config.yaml` from the working directory.

- If `config.yaml` does not exist, it writes one with defaults.
- If `config.yaml` exists, it writes back the merged configuration on startup so defaults are
  explicit.
- Credentials are always scrubbed from the written YAML; provide them via env vars or CLI.
- `write_non_default_config: true` writes only values that differ from defaults.

Important fields:

- `log_level`: root logging level for the process.
- `prometheus_port`: exporter listen port.
- `exporters.tapo.devices`: list of device IPs to monitor (used in addition to discovery).
- `exporters.tapo.prometheus_options.refresh_interval`: `null` by default, which probes devices
  during each scrape. A positive integer enables background polling with that interval in seconds;
  scrapes then read the latest completed snapshot.
- `exporters.tapo.discovery_options.*`: discovery parameters passed to `python-kasa`.
- `exporters.tapo.discovery_options.tapo_username_env_key` / `tapo_password_env_key`: env var names
  used to populate `python-kasa` `Credentials` by default.
- `exporters.tapo.supported_device_families`: currently only `PLUG`.
- `exporters.tapo.per_device_family_metrics.plug`: plug metrics to export.

### Polling Behavior Examples

Set the option internally (Python dataclass value):

```python
# Background polling every 15 seconds.
app_config.exporters.tapo.prometheus_options.refresh_interval = 15

# Default: live probing during every Prometheus scrape.
app_config.exporters.tapo.prometheus_options.refresh_interval = None
```

When `config.yaml` is written back, those values appear as:

```yaml
exporters:
  tapo:
    prometheus_options:
      refresh_interval: 15
```

```yaml
exporters:
  tapo:
    prometheus_options:
      refresh_interval: null
```

Existing configurations with an integer `refresh_interval` keep background polling; change it to
`null` to enable live probing. Overlapping live scrapes share one device refresh. Each scrape waits
up to `scrape_timeout` seconds (default `10.0`); on timeout it returns the last completed snapshot
while the shared refresh continues. Initial discovery also publishes a snapshot.
See [collection and failure recovery](docs/configuration.md#bounded-collection-and-failure-recovery)
for timeout tuning and the synchronous collector interface.

Discovery note: broadcast discovery generally does not work across VLAN boundaries. If your devices
are on a separate IoT VLAN, set `exporters.tapo.devices` (or use `--tapo-plug-devices`) to the
device IPs.

## Metrics

The Tapo plug exporter currently emits:

- `tapo_discovered_devices`: number of discovered devices (gauge).
- `current_consumption{host,alias}`: watts (gauge).
- `current_voltage{host,alias}`: volts (gauge).
- `current_current{host,alias}`: amps (gauge).
- `current_consumption_today{host,alias}`: watt-hours (gauge).
- `current_month_consumption{host,alias}`: watt-hours (gauge).
- `current_rssi{host,alias}`: RSSI value reported by the device (gauge).

Only devices that report the `current_consumption` feature are exported.

## Prometheus Scrape Config

Example `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: pyprom-exporters
    scrape_interval: 30s
    scrape_timeout: 15s  # Allow headroom above the exporter's 10-second device wait.
    static_configs:
      - targets: ["<exporter-host>:8090"]
```

## Docker

Build:

```sh
docker build -t pyprom-exporters:latest .
```

Run:

```sh
docker run --rm \
  -e TP_LINK_USERNAME="you@example.com" \
  -e TP_LINK_PASSWORD="your-password" \
  -e PROMETHEUS_PORT=8090 \
  -e TAPO_PLUG_DEVICES="10.10.2.100,10.10.2.101" \
  -p 8090:8090 \
  pyprom-exporters:latest
```

The container runs `prom-exporter` directly, reads the same environment overrides as the local CLI,
and forwards shutdown signals to the exporter. Additional CLI arguments can be appended to
`docker run`; they take precedence over environment variables.

## Troubleshooting

- No devices discovered:
  - Set `exporters.tapo.devices` (or use `--tapo-plug-devices`) instead of relying on broadcast
    discovery.
  - Check firewall rules and IoT VLAN routing.
- Metrics are missing for a device:
  - The exporter skips devices that do not report a `current_consumption` feature.
- Authentication failures:
  - Ensure `TP_LINK_USERNAME` / `TP_LINK_PASSWORD` (or `TAPO_USERNAME` / `TAPO_PASSWORD`) are set.

## Development

Install the development dependencies with `uv sync --locked`. The hook runner is `prek`; it uses
`.pre-commit-config.yaml` and the Python tools from the locked project environment.

```sh
# Install the Git hook once per checkout.
uv run --locked prek install

# Run tests.
uv run --locked pytest

# Check types, including annotated and unannotated functions, with ty.
uv run --locked ty check

# Run Ruff linting and formatting.
uv run --locked ruff check .
uv run --locked ruff format .

# Run all configured hooks.
uv run --locked prek run --all-files
```

Ruff replaces Pylint and enables all stable and preview lint rules with `select = ["ALL"]`,
`preview = true`, and NumPy-style docstrings. The only global rule exception is
`missing-trailing-comma` (`COM812`), because Ruff's formatter owns trailing commas. Test-only exceptions
permit pytest assertions, literal expected values, and internal-state regression checks. Standalone
Sphinx configuration is exempt from the package-directory rule. Individual source suppressions are
limited to documented cases such as optional imports, safe credential-error reporting, and retry
jitter. Blanket and unused suppressions are checked.

Pytest uses [pytest-xdist](https://pytest-xdist.readthedocs.io/en/stable/distribution.html) to run
tests in parallel by default, including `make test` and CI. Automatic worker selection is capped
at four processes to bound startup and memory overhead; work stealing balances uneven test times.
Use `uv run --locked pytest -n 2` to choose a worker count or `uv run --locked pytest -n 0` for
serial debugging. Worker startup can outweigh parallelism benefits for small test selections.

CI tests Python 3.11, 3.12, 3.13, and 3.14. Ruff and ty target the minimum supported version, 3.11.
Sphinx 9.0.4 is used on Python 3.11; Python 3.12+ uses Sphinx 9.1 or later.

### Performance diagnostics

The offline benchmark in `src/pyprom_exporters/benchmarks/scalability.py` measures discovery,
update, serialization, failure recovery, and memory retention using simulated devices. From a
repository checkout, run:

```sh
uv run --locked benchmark

# Equivalent command:
make benchmark

# A smaller run with explicit fleet sizes, concurrency limits, and simulated I/O latency:
uv run --locked benchmark --sizes 1 10 100 --concurrency 1 10 --latency-ms 1

# Show all options:
uv run --locked benchmark --help
```

The defaults cover 1, 10, 100, and 1,000 devices at concurrency limits of 1, 10, and 50, with 1 ms
of simulated latency per I/O operation. Each successful run prints the paths to two files in a new
UTC-stamped directory with a unique suffix:

```text
report/
  <UTC-timestamp>-<unique-suffix>/
    report.html
    metrics.json
```

The root `report/` directory is ignored by Git and excluded from Docker builds. Previous runs are
preserved. Open `report.html` directly in a browser: its styles and data are embedded, so it works
offline and can be shared as a single file. `metrics.json` contains the complete raw metrics for
further analysis. Both include the environment and workload settings for reproducibility.
Use `--output-dir /path/to/reports` to choose another output parent; relative paths are resolved
from the current working directory. Only the repository's root `report/` is ignored automatically.

The report covers healthy devices, authentication failures, transient retries, missing hosts,
concurrency and task counts, and Python allocation measurements. Timing and memory measurements
run separately to keep allocation tracing out of the timing results. These measurements
characterize application overhead; they do not certify physical fleet capacity.

### Coverage

Coverage collection uses [SlipCover](https://github.com/plasma-umass/slipcover). Its native Cobertura
XML output feeds the coverage badge generator. SlipCover activates in each pytest-xdist worker
and merges their coverage automatically, so coverage runs use the same parallel defaults:

```sh
uv run --locked python -m slipcover --source src/pyprom_exporters --xml --out coverage.xml -m pytest
uv run --locked genbadge coverage -i coverage.xml -o coverage.svg -l

# Equivalent command:
make coverage
```

SlipCover currently supports Python 3.11 through 3.14 in this project. Its dependency is conditional
so the rest of the development environment remains installable on newer Python versions.
CI uploads coverage reports for each tested Python version and updates the checked-in badge only
after successful checks on `main`.

Pytest can report `PytestAssertRewriteWarning` for the already imported `slipcover` package.
This concerns assertion rewriting in the tool itself and does not affect application coverage.

## Documentation

Documentation is generated with Sphinx using Markdown (`MyST`) sources in `docs/`.

```sh
# Build HTML docs locally with uv
uv run --locked docs

# Build HTML docs locally with make
make docs
```

## License

Apache-2.0 (see [LICENSE](LICENSE)).
