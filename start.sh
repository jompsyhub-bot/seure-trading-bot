#!/usr/bin/env bash
set -euo pipefail

# The Telegram poller and the Mini App/API intentionally run in one Render
# instance while SQLite is used. This prevents split databases between services.
python -m app.main &
BOT_PID=$!

cleanup() {
  kill "$BOT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec python -m uvicorn app.api:api --host 0.0.0.0 --port "${PORT:-10000}"
