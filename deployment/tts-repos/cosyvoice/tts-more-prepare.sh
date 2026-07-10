#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE="${TTS_MORE_MODEL_SOURCE:-Auto}"
[[ "$SOURCE" == "Auto" ]] && SOURCE="${TTS_MORE_RESOLVED_SOURCE:-ModelScope}"
BASE_PYTHON="${TTS_MORE_BASE_PYTHON:-python3}"

cd "$REPO_ROOT"

git submodule update --init --recursive
if [[ ! -x ".venv/bin/python" ]]; then
  "$BASE_PYTHON" -m venv .venv
fi
PYTHON="$REPO_ROOT/.venv/bin/python"
"$PYTHON" -m pip install -U pip wheel setuptools
"$PYTHON" -m pip install -r requirements.txt

if [[ "$SOURCE" == "ModelScope" ]]; then
  unset HF_ENDPOINT
  CODE="from modelscope import snapshot_download; snapshot_download('iic/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
else
  if [[ "$SOURCE" == "HF-Mirror" ]]; then
    export HF_ENDPOINT="https://hf-mirror.com"
  else
    unset HF_ENDPOINT
  fi
  CODE="from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
fi

echo "[cosyvoice] download source=$SOURCE model_dir=pretrained_models/CosyVoice-300M"
"$PYTHON" -c "$CODE"
