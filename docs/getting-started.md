# Getting Started

## Install

This project uses `uv` with a checked-in `uv.lock`.

```sh
# Development environment
uv sync --frozen

# Runtime-only environment
uv sync --frozen --no-dev
```

## Run the Exporter

Set credentials:

```sh
export TP_LINK_USERNAME="you@example.com"
export TP_LINK_PASSWORD="your-password"
```

Run:

```sh
uv run prom-exporter \
  --prometheus-port 8090 \
  --tapo-plug-devices 10.10.2.100,10.10.2.101
```

Scrape endpoint:

- `http://localhost:8090/metrics`
