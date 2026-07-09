#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

SERVICES=""
DETACH=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --services)
      SERVICES="$2"
      shift 2
      ;;
    --detach)
      DETACH=(--detach)
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

ARGS=(start-workers --platform posix)
if [[ -n "$SERVICES" ]]; then
  ARGS+=(--service-ids "$SERVICES")
fi
ARGS+=("${DETACH[@]}")

exec "$PYTHON" "$ROOT/scripts/tts_more_deploy.py" "${ARGS[@]}"
