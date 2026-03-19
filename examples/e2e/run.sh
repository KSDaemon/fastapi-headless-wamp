#!/usr/bin/env bash
#
# End-to-end demo for fastapi-headless-wamp using Wampy.js client.
#
# This script:
#   1. Installs Node.js dependencies (wampy, ws) if needed
#   2. Starts the FastAPI WAMP server in the background
#   3. Runs the Node.js client that exercises RPC + PubSub
#   4. Shuts everything down and reports results
#
# Usage:
#   cd examples/e2e
#   bash run.sh
#
# Prerequisites:
#   - Python 3.12+ with uvicorn and fastapi-headless-wamp installed
#   - Node.js 18+
#   - npm
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PORT=8080
URL="ws://localhost:${PORT}/ws"

# Colours (if terminal supports them)
if [ -t 1 ]; then
  BOLD="\033[1m"
  DIM="\033[2m"
  GREEN="\033[32m"
  RED="\033[31m"
  CYAN="\033[36m"
  RESET="\033[0m"
else
  BOLD="" DIM="" GREEN="" RED="" CYAN="" RESET=""
fi

banner() { echo -e "\n${BOLD}${CYAN}$1${RESET}\n"; }
info()   { echo -e "${DIM}$1${RESET}"; }
ok()     { echo -e "${GREEN}$1${RESET}"; }
fail()   { echo -e "${RED}$1${RESET}"; }

cleanup() {
  if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    info "Stopping server (PID $SERVER_PID)..."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ── 1. Install Node.js dependencies ─────────────────────────────────

banner "1/4  Installing Node.js dependencies"

cd "$SCRIPT_DIR"

npm install --no-fund --no-audit 2>&1 | sed 's/^/  /'

# ── 2. Start the FastAPI server ──────────────────────────────────────

banner "2/4  Starting FastAPI WAMP server on port $PORT"

cd "$PROJECT_ROOT"

# Activate the project venv if it exists and uvicorn is not on PATH
if ! command -v uvicorn &>/dev/null && [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
  info "  Activating project virtualenv"
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.venv/bin/activate"
fi

# Kill any leftover process on the port
if lsof -i ":$PORT" -t >/dev/null 2>&1; then
  info "  Port $PORT is busy — killing existing process"
  kill "$(lsof -i ":$PORT" -t)" 2>/dev/null || true
  sleep 1
fi

uvicorn examples.e2e.server:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --log-level warning \
  &
SERVER_PID=$!

info "  Server PID: $SERVER_PID"

# Wait for the server to be ready
info "  Waiting for server to start..."
for i in $(seq 1 30); do
  if curl -s -o /dev/null "http://localhost:${PORT}/" 2>/dev/null; then
    ok "  Server is ready!"
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    fail "  Server process died unexpectedly."
    exit 1
  fi
  sleep 0.2
done

# ── 3. Run the Node.js client ───────────────────────────────────────

banner "3/4  Running end-to-end client (Wampy.js)"

cd "$SCRIPT_DIR"

CLIENT_EXIT=0
node client.mjs "$URL" || CLIENT_EXIT=$?

# ── 4. Done ──────────────────────────────────────────────────────────

banner "4/4  Done"

if [ "$CLIENT_EXIT" -eq 0 ]; then
  ok "  End-to-end demo completed successfully!"
else
  fail "  End-to-end demo had failures (exit code $CLIENT_EXIT)."
fi

exit "$CLIENT_EXIT"
