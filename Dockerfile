FROM python:3.12-alpine

# Set working directory
WORKDIR /app
# Copy application files
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Build-time argument for runtime default port.
ARG PROMETHEUS_PORT=8090
ARG TAPO_PLUG_DEVICES=""

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PYTHONPATH=.
ENV PROMETHEUS_PORT=${PROMETHEUS_PORT}
ENV TAPO_PLUG_DEVICES=${TAPO_PLUG_DEVICES}

# Install dependencies
RUN uv sync --frozen --no-dev

# Expose Prometheus port
EXPOSE ${PROMETHEUS_PORT}

# Start the Prometheus exporter
ENTRYPOINT ["sh", "-c", "uv run prom-exporter --prometheus-port ${PROMETHEUS_PORT}${TAPO_PLUG_DEVICES:+ --tapo-plug-devices $TAPO_PLUG_DEVICES}"]
