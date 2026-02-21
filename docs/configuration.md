# Configuration

`prom-exporter` reads `config.yaml` from the working directory.

- If the file does not exist, defaults are written on first start.
- Merged configuration is written back on startup.
- Credentials are scrubbed from persisted YAML.

## Common Fields

- `log_level`: process logging level (`INFO` by default).
- `prometheus_port`: HTTP port for `/metrics` (`8090` by default).
- `exporters.tapo.devices`: explicit device IP list.
- `exporters.tapo.prometheus_options.refresh_interval`: background update interval in seconds.
- `exporters.tapo.discovery_options.*`: discovery settings passed to `python-kasa`.

## Override Order

Override precedence is:

1. CLI flags
1. Environment variables
1. `config.yaml`
