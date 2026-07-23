#!/bin/sh
set -eu

mkdir -p "${DATA_DIR:-/data}/jobs"

python -m app.worker &
worker_pid=$!

cleanup() {
  kill "$worker_pid" 2>/dev/null || true
  wait "$worker_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
