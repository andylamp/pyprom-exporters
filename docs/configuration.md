# Configuration

`prom-exporter` reads `config.yaml` from the working directory.

- If the file does not exist, defaults are written on first start.
- Merged configuration is written back on startup.
- Credentials are scrubbed from persisted YAML.

## Common Fields

- `log_level`: process logging level (`INFO` by default).
- `prometheus_port`: HTTP port for `/metrics` (`8090` by default).
- `exporters.tapo.devices`: explicit device IP list.
- `exporters.tapo.prometheus_options.refresh_interval`: update mode selector:
  - Integer seconds: periodic background polling is enabled and scrapes read cached metrics.
  - `null`: background polling is disabled and metrics refresh during each Prometheus scrape.
- `exporters.tapo.discovery_options.*`: discovery settings passed to `python-kasa`.

At startup, the exporter logs one `INFO` message per registered collector indicating whether
automatic polling is enabled and the configured refresh interval.

## Polling Behavior Examples

Set the option internally (Python dataclass value):

```python
# Background polling every 15 seconds.
app_config.exporters.tapo.prometheus_options.refresh_interval = 15

# Disable background polling; refresh on every Prometheus scrape.
app_config.exporters.tapo.prometheus_options.refresh_interval = None
```

When the exporter writes merged configuration back to `config.yaml`, the values look like:

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

## Override Order

Override precedence is:

1. CLI flags
1. Environment variables
1. `config.yaml`

## Bounded Collection and Failure Recovery

- `exporters.tapo.max_concurrent_devices` limits active discovery, update, and disconnect operations
  (default `10`, must be a positive integer).
- `exporters.tapo.prometheus_options.scrape_timeout` bounds how long a scrape waits for an update
  when `refresh_interval` is `null` (default `10.0` seconds, must be finite and positive).
- Overlapping scrapes share a refresh. If it times out, the exporter returns the previous cached
  snapshot. Use periodic background polling when device updates routinely exceed a scrape deadline.
- A device update failure does not abort updates for healthy devices. Failed devices are omitted
  from the next completed snapshot until they recover; unavailable measurements are omitted rather
  than reported as zero.
- Startup attempts discovery of all explicitly configured hosts. Later update passes retry at most
  `max_concurrent_devices` missing hosts before refreshing known devices. Hosts rotate fairly, and
  each failed host waits at least 30 seconds before another attempt. Large offline inventories may
  therefore need several refresh passes for a complete retry sweep.
- Broadcast discovery is performed at startup by `python-kasa`; newly added devices require
  rediscovery or a restart unless their hosts are explicitly configured. The configured-host worker
  bound does not control tasks created internally by broadcast discovery.
- `tapo_discovered_devices` reports the discovered inventory, including temporarily failed devices;
  it is not a count of healthy devices.

Metric names can be customized independently of the measurement selected by each metric type.
Labels must include `host` to keep device series distinct. Optional labels are `alias`, `model`,
`device_type`, `firmware_version`, and `hardware_version`.
An empty `supported_device_families: {}` or `per_device_family_metrics: {plug: {}}` disables plug
measurement families; the inventory metric remains available.

Energy metrics `current_consumption_today` and `current_month_consumption` are in watt-hours.
The previous implementation returned raw kilo-watt-hour values despite describing them as
watt-hours; corrected values are 1000 times larger. Adjust dashboards or alerts that compensated
for the old unit mismatch.

## Configuration Persistence

Use `--config /path/to/config.yaml` to select a configuration file.
Pass `--no-write-config` for read-only configurations or individual Docker file bind mounts.
Without that flag, the file's parent directory must exist and be writable; configuration is atomically replaced on
startup. Temporary files use owner-only permissions. Credentials are removed before persistence,
and configuration parser errors do not print potentially sensitive YAML contents.

With `write_non_default_config: true`, explicit `null` values and empty maps are retained when they
change the defaults. Partial top-level configurations, such as a file containing only
`prometheus_port`, are supported alongside the older exporter-only layout.

## Scaling Measurements

Run the offline benchmark from the repository root to measure fleet-size overhead, failure
recovery, and memory retention with simulated devices:

```sh
uv run --locked benchmark

# Equivalent command:
make benchmark
```

The default workload tests 1, 10, 100, and 1,000 devices with concurrency limits of 1, 10, and 50
and 1 ms of simulated I/O latency. For a shorter run or different settings:

```sh
uv run --locked benchmark --sizes 1 10 100 --concurrency 1 10 --latency-ms 1
uv run --locked benchmark --help
```

Each successful run creates `report/<UTC-timestamp>-<unique-suffix>/report.html` and
`report/<UTC-timestamp>-<unique-suffix>/metrics.json`, and prints both paths. The repository's root
`report/` directory is ignored by Git and excluded from Docker builds. Runs have separate
directories, so previous results are preserved.

Open the HTML file directly in a browser. All report data and styles are embedded, so the file
works offline and can be shared independently. The JSON file contains the complete metrics for
programmatic analysis, including the environment and workload settings. Use
`--output-dir /path/to/reports` to select a different output parent; relative paths use the current
working directory, and custom directories are not automatically ignored by Git.

Results cover healthy and failing devices, retries, missing-host recovery, concurrency limits,
asyncio tasks, serialization costs, and Python allocations. Memory tracing runs separately from
timing measurements. Simulated device measurements do not establish physical-device capacity.
