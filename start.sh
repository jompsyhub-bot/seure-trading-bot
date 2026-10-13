#!/usr/bin/env bash
set -euo pipefail

# The legacy Telegram bot at app/main.py was replaced by the Mini App.
# All user interaction now flows through the FastAPI app below, which
# serves the WebApp and runs the auto-trading worker in the background.
exec python -m uvicorn app.api:api --host 0.0.0.0 --port "${PORT:-10000}"
