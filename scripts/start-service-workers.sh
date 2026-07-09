#!/usr/bin/env bash
# Start the three non-invasive TTS workers (GPT-SoVITS, IndexTTS, CosyVoice).
#
# POSIX equivalent of scripts/start-service-workers.ps1. Each worker is a
# FastAPI app that imports the upstream model directly and exposes the
# tts-more-v1 contract — no Gradio scraping, no upstream file changes.
#
# The workers run in the upstream repo's own venv (so torch/CUDA resolve).
# Set TTS_MORE_GPTSOVITS_PYTHON / TTS_MORE_INDEXTTS_PYTHON /
# TTS_MORE_COSYVOICE_PYTHON to point at each repo's interpreter; if unset,
# falls back to the backend .venv/bin/python (works when the repo shares the
# backend env).
#
# Ports: GPT-SoVITS 9880, IndexTTS 9881, CosyVoice 9882.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKEND_PY="$ROOT/.venv/bin/python"
[[ -x "$BACKEND_PY" ]] || BACKEND_PY="python3"

GPT_PY="${TTS_MORE_GPTSOVITS_PYTHON:-$BACKEND_PY}"
INDEX_PY="${TTS_MORE_INDEXTTS_PYTHON:-$BACKEND_PY}"
COSY_PY="${TTS_MORE_COSYVOICE_PYTHON:-$BACKEND_PY}"

PIDS=()

start_worker() {
  local name="$1" py="$2" module="$3" port="$4"
  echo "Starting $name worker on http://127.0.0.1:$port ..."
  "$py" -m uvicorn "$module" --app-dir backend --host 127.0.0.1 --port "$port" &
  PIDS+=($!)
  echo "  $name PID: ${PIDS[-1]}"
}

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

start_worker "GPT-SoVITS" "$GPT_PY" "app.workers.gpt_sovits_worker:app" 9880
start_worker "IndexTTS"   "$INDEX_PY" "app.workers.indextts_worker:app" 9881
start_worker "CosyVoice"  "$COSY_PY" "app.workers.cosyvoice_worker:app" 9882

echo ""
echo "All workers started. Press Ctrl-C to stop."
echo "  GPT-SoVITS: http://127.0.0.1:9880/health"
echo "  IndexTTS:   http://127.0.0.1:9881/health"
echo "  CosyVoice:  http://127.0.0.1:9882/health"

wait
