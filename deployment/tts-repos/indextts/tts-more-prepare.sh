#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE="${TTS_MORE_MODEL_SOURCE:-Auto}"
[[ "$SOURCE" == "Auto" ]] && SOURCE="${TTS_MORE_RESOLVED_SOURCE:-ModelScope}"

cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
  echo "[indextts] uv sync --all-extras"
  uv sync --all-extras
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="$REPO_ROOT/.venv/bin/python"
  if [[ ! -x "$PYTHON" ]]; then
    python3 -m venv .venv
  fi
  "$PYTHON" -m pip install -U pip wheel setuptools
  "$PYTHON" -m pip install -e .
fi

if [[ "$SOURCE" == "ModelScope" ]]; then
  SOURCE_ARG="modelscope"
else
  SOURCE_ARG="huggingface"
fi

if [[ "$SOURCE" == "HF-Mirror" ]]; then
  export HF_ENDPOINT="https://hf-mirror.com"
else
  unset HF_ENDPOINT
fi

echo "[indextts] download source=$SOURCE model_dir=checkpoints"
"$PYTHON" indextts/cli_v2.py download --source "$SOURCE_ARG" --model-dir checkpoints
"$PYTHON" indextts/cli_v2.py config set model_dir checkpoints
