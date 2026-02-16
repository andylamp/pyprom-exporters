# Python Prometheus Exporters for IoT Devices

Python Prometheus Exporters (`pyprom-exporters`) is a small Python package that exposes Prometheus metrics for IoT / smart-home
devices.

The current concrete exporter targets TP-Link Tapo smart plugs via `python-kasa`.

## What It Does

- Discovers Tapo devices on your LAN (UDP broadcast) and/or monitors an explicit list of device IPs.
- Periodically updates device state on a background asyncio loop, with retries and backoff.
- Exposes cached metrics via a Prometheus HTTP endpoint (scrapes do not talk to devices).

## How It Works

- `prom-exporter` starts an asyncio event loop on a background thread.
- The Tapo exporter runs discovery, performs an initial update pass, then starts periodic background
  updates.
- Prometheus scrapes only read cached metric objects; device I/O happens in the background.

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
uv sync --frozen --group tapo

# Minimal runtime environment
uv sync --frozen --no-dev --group tapo
```

If you use `pip`, you must also install `python-kasa` (it is not part of the base dependencies).

```sh
pip install .
pip install python-kasa
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

1. Scrape metrics:

- `http://localhost:8090/metrics`

### Environment Variable Overrides

The runtime supports a few convenience overrides:

- Precedence: CLI flags override env vars; env vars override `config.yaml`.
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

- `prometheus_port`: exporter listen port.
- `exporters.tapo.devices`: list of device IPs to monitor (used in addition to discovery).
- `exporters.tapo.prometheus_options.refresh_interval`: background update interval (seconds) and
  per-device minimum update interval.
- `exporters.tapo.discovery_options.*`: discovery parameters passed to `python-kasa`.
- `exporters.tapo.discovery_options.tapo_username_env_key` / `tapo_password_env_key`: env var names
  used to populate `python-kasa` `Credentials` by default.
- `exporters.tapo.supported_device_families`: currently only `PLUG`.
- `exporters.tapo.per_device_family_metrics.plug`: plug metrics to export.

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

The container entrypoint runs `uv run prom-exporter` and forwards `PROMETHEUS_PORT` and
`TAPO_PLUG_DEVICES` into CLI flags.

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

```sh
# pytest - for tests
uv run pytest
# ruff - for linting and formatting
uv run ruff check .
# format with ruff (or your editor integration)
uv run ruff format .
# pre-commit - for running all configured pre-commit hooks (ruff, isort, etc.)
uv run pre-commit run --all-files
```

## License

Apache-2.0 (see [LICENSE](LICENSE)).
