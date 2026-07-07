#!/usr/bin/env bash
# Bash mirror of scripts/start-dev.ps1 for macOS / Linux contributors.
#
# Requires:
#   - .venv at repo root with backend[dev] installed
#   - pnpm on PATH with frontend deps already installed (pnpm install in frontend/)
#
# Logs are written to stdout/stderr of this shell. Use Ctrl-C to stop both
# processes; the EXIT trap cleans up stragglers.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKEND_PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$BACKEND_PYTHON" ]]; then
    echo "error: backend virtualenv not found at $BACKEND_PYTHON" >&2
    echo "create it with: uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e 'backend[dev]'" >&2
    exit 1
fi

if ! command -v pnpm >/dev/null 2>&1; then
    echo "error: pnpm not found on PATH" >&2
    exit 1
fi

BACKEND_LOG="${TTS_MORE_BACKEND_LOG:-/tmp/tts-more-backend.log}"
FRONTEND_LOG="${TTS_MORE_FRONTEND_LOG:-/tmp/tts-more-frontend.log}"

cleanup() {
    [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
    [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "starting backend  -> http://127.0.0.1:8000  (log: $BACKEND_LOG)"
"$BACKEND_PYTHON" -m uvicorn app.main:app \
    --app-dir backend \
    --host 127.0.0.1 \
    --port 8000 \
    --reload >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

echo "starting frontend -> http://127.0.0.1:5173  (log: $FRONTEND_LOG)"
(
    cd "$ROOT/frontend"
    pnpm dev >"$FRONTEND_LOG" 2>&1
) &
FRONTEND_PID=$!

echo
echo "backend  PID: $BACKEND_PID"
echo "frontend PID: $FRONTEND_PID"
echo "press Ctrl-C to stop both"

wait
