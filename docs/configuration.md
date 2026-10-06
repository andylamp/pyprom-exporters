# Configuration

`prom-exporter` reads `config.yaml` from the working directory.

- If the file does not exist, defaults are written on first start.
- Merged configuration is written back on startup.
- Credentials are scrubbed from persisted YAML.

## Common Fields

- `log_level`: process logging level (`INFO` by default).
- `prometheus_port`: HTTP port for `/metrics` (`8090` by default).
- `exporters.tapo.devices`: explicit device IP list.
- `exporters.tapo.update_timeout`: maximum duration of one whole SDK update (`10.0` seconds by default).
- `exporters.tapo.prometheus_options.refresh_interval`: update mode selector:
  - Integer seconds: periodic background polling is enabled and scrapes read cached metrics.
  - `null` (default): background polling is disabled and devices refresh during each Prometheus scrape.
- `exporters.tapo.discovery_options.*`: discovery settings passed to `python-kasa`.

At startup, the exporter logs one `INFO` message per registered collector indicating whether
automatic polling is enabled and the configured refresh interval.

## Polling Behavior Examples

Set the option internally (Python dataclass value):

```python
# Background polling every 15 seconds.
app_config.exporters.tapo.prometheus_options.refresh_interval = 15

# Default: live probing during every Prometheus scrape.
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

Configurations with an explicit integer keep background polling after upgrading. Change that
value to `null` to enable live probing. If `refresh_interval` is omitted, live probing is used.

## Override Order

Override precedence is:

1. CLI flags
1. Environment variables
1. `config.yaml`

## Bounded Collection and Failure Recovery

- `exporters.tapo.max_concurrent_devices` limits active discovery, update, and disconnect operations
  (default `10`, must be a positive integer).
- `exporters.tapo.update_timeout` bounds one whole `python-kasa` device update, including its native
  request retries (default `10.0` seconds, finite and positive). The exporter does not retry the
  complete SDK update again inside the same attempt. Session-close attempts use at most one second,
  or this update budget if it is smaller.
- `exporters.tapo.prometheus_options.scrape_timeout` bounds how long a scrape waits for an update
  when `refresh_interval` is `null` (default `10.0` seconds, must be finite and positive).
- Overlapping scrapes share one refresh. Each completed device publishes its reading independently;
  there is no fleet-wide publication barrier. Healthy devices run first and release waiting scrapes
  before slower failure recovery finishes. When a scrape's wait expires, it uses the latest
  available readings, including progress from the ongoing pass. A timeout never restarts the fleet
  from its first device. Shutdown cancels outstanding work.
- Discovery publishes initial readings before the HTTP endpoint starts, including in live mode.
- The wait budget covers device refresh, not serialization or network transfer. Set the exporter's
  `scrape_timeout` below Prometheus's scrape timeout; for example, use a 10-second exporter wait and
  a 15-second Prometheus timeout with a 30-second scrape interval. Choose budgets from observed
  fleet latency, or opt into background polling if updates routinely exceed a scrape deadline.
- `collect()` is synchronous: it must run outside the exporter's asyncio event loop to wait for live
  I/O. The bundled HTTP server already does this. Async library callers should instead await
  `update_and_collect()` before collecting or serializing on that loop. Collecting on the owning
  loop, or when that loop is stopped, returns the current snapshot without device I/O.
- A device update failure does not abort healthy updates. Its measurements are removed immediately;
  diagnostic counters and the last-success timestamp remain available. Unavailable measurements
  are omitted rather than reported as zero. Known failed sessions retry in fair, bounded waves of
  at most `max_concurrent_devices`, with a 30-second cooldown, after healthy updates.
- Failed initialization and timed-out updates retire the affected session and retry discovery after
  at least 30 seconds, reusing the original configured hostname when available. Recovery closes the
  retired session before opening its replacement; failed closes wait for the next retry wave. This
  also recovers devices first found through broadcast discovery.
- `discovery_options.timeout` controls individual device requests, not the entire update pass.
  The device library can retry requests, so a failing device can take longer than this setting,
  subject to `update_timeout`. The HTTP scrape wait has its own independent budget.
- Startup attempts discovery of all explicitly configured hosts. Later update passes retry at most
  `max_concurrent_devices` missing hosts after refreshing known devices. Hosts rotate fairly, and
  each failed host waits at least 30 seconds before another attempt. Large offline inventories may
  therefore need several refresh passes for a complete retry sweep.
- Broadcast discovery is performed at startup by `python-kasa`; newly added devices require
  rediscovery or a restart unless their hosts are explicitly configured. The configured-host worker
  bound does not control tasks created internally by broadcast discovery.
- Configured hostnames and broadcast IPs with the same valid MAC address share one device session
  and one set of measurements. The first retained session supplies the `host` label. If a usable MAC
  is unavailable, deduplication falls back to the device host; avoid listing multiple aliases then.
- `tapo_discovered_devices` reports the retained inventory, including temporarily failed initialized
  devices. Retired sessions are excluded until rediscovered. It is not a count of healthy devices.
- Operational metrics expose update outcomes, durations, failures, timeouts, refresh progress and
  HTTP wait timeouts. Per-device diagnostics use `host` and `alias`; never-discovered configured
  hosts use `alias="unknown"`, outcome zero and last-success timestamp zero. No SDK update attempt
  means no update-failure increment. Discovery failures are distinct from SDK update failures.
- `time() - tapo_device_last_success_timestamp_seconds` measures time since successful SDK work,
  not physical sensor freshness. Exclude zero timestamps when plotting age. Counters reset on
  process restart; use Prometheus `rate()` or `increase()` for trends.

Metric names can be customized independently of the measurement selected by each metric type.
Labels must include `host` to keep device series distinct. Optional labels are `alias`, `model`,
`device_type`, `firmware_version`, and `hardware_version`.
An empty `supported_device_families: {}` or `per_device_family_metrics: {plug: {}}` disables plug
measurement families; inventory and operational metrics remain available.
Nonempty metric maps override and merge with defaults: omitted entries remain enabled. Removing
one default entry from a Python configuration dictionary is therefore not preserved as a per-metric
disablement when the configuration is written and reloaded.

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
asyncio tasks, serialization costs, and Python allocations. The reported scrape timing measures
serialization of a completed snapshot on the asyncio loop; it excludes live refresh waits, HTTP
request handling, and network transfer. Use the update and serialization timings separately when
assessing live-mode costs. JSON records `settings.serialization_scope: "cached_snapshot"`.
Memory tracing runs separately from timing measurements. Simulated device measurements do not
establish physical-device capacity.
