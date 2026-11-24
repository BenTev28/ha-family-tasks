#!/usr/bin/env bash
# Lint + tests in a throwaway Python 3.14 container, with a throwaway Radicale.
# The test container shares Radicale's network namespace so the server is at
# 127.0.0.1 (pytest-homeassistant-custom-component only allows localhost sockets).
set -euo pipefail
cd "$(dirname "$0")"
docker rm -f ft-radicale >/dev/null 2>&1 || true
docker run -d --rm --name ft-radicale \
  -v "$PWD/tests/radicale.conf:/config/config:ro" tomsquest/docker-radicale:latest >/dev/null
trap 'docker rm -f ft-radicale >/dev/null 2>&1 || true' EXIT
docker run --rm --network container:ft-radicale -v "$PWD:/src" -w /src \
  -v family-tasks-pip:/root/.cache/pip \
  -e RADICALE_URL=http://127.0.0.1:5232 python:3.14-slim bash -c '
    set -e
    pip install -q -r requirements_test.txt ruff >/dev/null 2>&1
    ruff check . && ruff format --check .
    python -m pytest -q -p no:cacheprovider "$@"' _ "$@"
