#!/usr/bin/env bash
# Start the TTS More backend (uvicorn) and frontend (vite) for local development.
#
# POSIX equivalent of scripts/start-dev.ps1. Uses .venv/bin/python on macOS/Linux.
# Both servers run in the foreground; press Ctrl-C to stop both.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKEND_PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$BACKEND_PYTHON" ]]; then
  echo "Backend virtual environment not found at .venv/bin/python." >&2
  echo "Create it with: uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e 'backend[dev]'" >&2
  exit 1
fi

if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm is required but not found on PATH." >&2
  exit 1
fi

cleanup() {
  [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting backend on http://127.0.0.1:8000 ..."
"$BACKEND_PYTHON" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

echo "Starting frontend on http://127.0.0.1:5173 ..."
(cd "$ROOT/frontend" && pnpm dev) &
FRONTEND_PID=$!

echo "Backend PID: $BACKEND_PID  http://127.0.0.1:8000"
echo "Frontend PID: $FRONTEND_PID http://127.0.0.1:5173"
echo "Press Ctrl-C to stop both."

wait
