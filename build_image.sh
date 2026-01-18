#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-pyprom-exporters:latest}"

docker buildx build --load -t "${IMAGE_NAME}" .
