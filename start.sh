#!/usr/bin/env bash
set -euo pipefail

# Set this false only while bringing up a replacement service. It avoids a
# second Telegram getUpdates poller during a hosting-region migration.
if [ "${RUN_TELEGRAM_POLLER:-true}" = "true" ]; then
  python -m app.main &
  BOT_PID=$!
  cleanup() {
    kill "$BOT_PID" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM
fi

exec python -m uvicorn app.api:api --host 0.0.0.0 --port "${PORT:-10000}"
