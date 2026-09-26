FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ARG PROMETHEUS_PORT=8090
ENV PROMETHEUS_PORT=${PROMETHEUS_PORT}
EXPOSE ${PROMETHEUS_PORT}

# Runtime reads its environment directly; exec form forwards shutdown signals.
ENTRYPOINT ["prom-exporter"]
