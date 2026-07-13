#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing project Python environment" >&2
  exit 1
fi

exec "$PYTHON" "$ROOT/scripts/run-lan-validation.py" "$@"
