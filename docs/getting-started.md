# Getting Started

## Install

This project uses `uv` with a checked-in `uv.lock`.

```sh
# Development environment
uv sync --locked

# Runtime-only environment
uv sync --locked --no-dev
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

Optional behavior:

- Use background polling (default): set `exporters.tapo.prometheus_options.refresh_interval` to an
  integer number of seconds.
- Use scrape-triggered refresh: set `exporters.tapo.prometheus_options.refresh_interval: null`.

At startup, the exporter logs whether automatic polling is enabled or disabled for each collector.

Scrape endpoint:

- `http://localhost:8090/metrics`

## Development Checks

The development environment includes `prek` for Git hooks, `ty` for type checking, and SlipCover
for coverage collection. Python 3.11 through 3.14 are tested in CI.

```sh
uv run --locked prek install
uv run --locked pytest
uv run --locked ty check
uv run --locked prek run --all-files
make coverage
```

`uv run --locked pytest` and `make test` run tests in parallel with pytest-xdist, using up to four
workers by default. Pass `-n 2` to pytest to choose a worker count, or `-n 0` for serial debugging.
`make coverage` uses the same parallel settings under SlipCover, merges worker coverage, and
writes `coverage.xml` and `coverage.svg`.
SlipCover currently requires Python older than 3.15.
