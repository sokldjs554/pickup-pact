#!/usr/bin/env sh
set -eu

uvicorn app.main:app --app-dir /app/reconciler --host 127.0.0.1 --port 8000 &
RECONCILER_PID=$!
trap 'kill "$RECONCILER_PID" 2>/dev/null || true' EXIT INT TERM

exec gunicorn --chdir /app/ops-console --bind "0.0.0.0:${PORT:-10000}" --workers 2 --threads 4 --timeout 30 app:app
