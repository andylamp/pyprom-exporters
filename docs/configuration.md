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
