#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEVICE="${TTS_MORE_DEVICE:-CU128}"
SOURCE="${TTS_MORE_MODEL_SOURCE:-Auto}"
[[ "$SOURCE" == "Auto" ]] && SOURCE="${TTS_MORE_RESOLVED_SOURCE:-ModelScope}"

if ! command -v conda >/dev/null 2>&1; then
  echo "[warn] conda was not found; GPT-SoVITS upstream installer requires conda or micromamba." >&2
fi

if [[ ! -f "$REPO_ROOT/install.sh" ]]; then
  echo "[error] Missing upstream installer: $REPO_ROOT/install.sh" >&2
  exit 1
fi

echo "[gpt-sovits] install device=$DEVICE source=$SOURCE"
exec bash "$REPO_ROOT/install.sh" --device "$DEVICE" --source "$SOURCE"
