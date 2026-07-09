#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${TTS_MORE_BASE_PYTHON:-python3}"
APP_PY="$ROOT/.venv/bin/python"
[[ -x "$APP_PY" ]] || APP_PY="$PYTHON"

SOURCE="Auto"
RESOLVED_SOURCE=""
NETWORK_PROFILE_JSON=""
DEVICE="CU128"
TARGETS="all"
SYNC_REPOS=0
CLEAN_REPOS=0
SKIP_INSTALL=0
SKIP_DOWNLOADS=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --targets) TARGETS="$2"; shift 2 ;;
    --sync-repos) SYNC_REPOS=1; shift ;;
    --clean-repos) CLEAN_REPOS=1; shift ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --skip-downloads) SKIP_DOWNLOADS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

run() {
  echo "[run] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@"
}

run_capture() {
  echo "[run] $*" >&2
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '{"model_source":"%s","env":{}}\n' "$([[ "$SOURCE" == "Auto" ]] && echo ModelScope || echo "$SOURCE")"
    return 0
  fi
  "$@"
}

target_enabled() {
  local name="$1" provider="$2" service_id="$3" variant="$4"
  [[ "$TARGETS" == "all" ]] && return 0
  IFS=',' read -ra parts <<< "$TARGETS"
  for item in "${parts[@]}"; do
    [[ "$item" == "$name" || "$item" == "$provider" || "$item" == "$service_id" || "$item" == "$variant" ]] && return 0
  done
  return 1
}

repo_json() {
  "$APP_PY" - "$ROOT/repo.lock.json" <<'PY'
import json, sys
for repo in json.load(open(sys.argv[1], encoding="utf-8"))["repositories"]:
    print(json.dumps(repo, ensure_ascii=False))
PY
}

field() {
  "$APP_PY" -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$1" "$2"
}

json_field_from_profile() {
  "$APP_PY" -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$NETWORK_PROFILE_JSON" "$1"
}

export_network_env() {
  "$APP_PY" - "$NETWORK_PROFILE_JSON" <<'PY'
import json
import sys
profile = json.loads(sys.argv[1])
for key, value in (profile.get("env") or {}).items():
    print(f"{key}={value}")
PY
}

resolve_network_profile() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[run] $APP_PY $ROOT/scripts/tts_more_deploy.py probe-network --write --source $SOURCE"
    RESOLVED_SOURCE="$([[ "$SOURCE" == "Auto" ]] && echo ModelScope || echo "$SOURCE")"
    echo "[network] source=$RESOLVED_SOURCE"
    return 0
  fi
  NETWORK_PROFILE_JSON="$(run_capture "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" probe-network --write --source "$SOURCE")"
  while IFS='=' read -r key value; do
    [[ -n "$key" ]] && export "$key=$value"
  done < <(export_network_env)
  RESOLVED_SOURCE="$(json_field_from_profile model_source)"
  [[ -z "$RESOLVED_SOURCE" ]] && RESOLVED_SOURCE="$([[ "$SOURCE" == "Auto" ]] && echo ModelScope || echo "$SOURCE")"
  echo "[network] source=$RESOLVED_SOURCE"
}

ensure_venv() {
  local repo_path="$1"
  local repo_python="$repo_path/.venv/bin/python"
  if [[ ! -x "$repo_python" ]]; then
    run "$PYTHON" -m venv "$repo_path/.venv"
    run "$repo_python" -m pip install -U pip wheel setuptools
  fi
  echo "$repo_python"
}

prepare_gpt() {
  local repo="$1" repo_path="$2" name="$3"
  [[ "$SKIP_INSTALL" == "1" ]] && { echo "[skip] GPT-SoVITS install for $name"; return; }
  if ! command -v conda >/dev/null 2>&1; then
    echo "[warn] conda was not found; GPT-SoVITS official installer requires conda for $name" >&2
    return
  fi
  if [[ ! -f "$repo_path/install.sh" ]]; then
    echo "Missing GPT-SoVITS installer: $repo_path/install.sh" >&2
    exit 1
  fi
  run bash "$repo_path/install.sh" --device "$DEVICE" --source "$RESOLVED_SOURCE"
}

prepare_index() {
  local repo_path="$1"
  local uv_bin=""
  if command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
  elif [[ -x "$repo_path/.uv-bootstrap/bin/uv" ]]; then
    uv_bin="$repo_path/.uv-bootstrap/bin/uv"
  fi
  if [[ "$SKIP_INSTALL" != "1" ]]; then
    if [[ -n "$uv_bin" ]]; then
      (cd "$repo_path" && run "$uv_bin" sync --all-extras)
    else
      local repo_python
      repo_python="$(ensure_venv "$repo_path")"
      (cd "$repo_path" && run "$repo_python" -m pip install -e .)
    fi
  fi
  if [[ "$SKIP_DOWNLOADS" != "1" ]]; then
    local repo_python="$repo_path/.venv/bin/python"
    local source_arg="huggingface"
    [[ "$RESOLVED_SOURCE" == "ModelScope" ]] && source_arg="modelscope"
    [[ "$RESOLVED_SOURCE" == "HF-Mirror" ]] && export HF_ENDPOINT="https://hf-mirror.com"
    (cd "$repo_path" && run "$repo_python" indextts/cli_v2.py download --source "$source_arg" --model-dir checkpoints)
    (cd "$repo_path" && run "$repo_python" indextts/cli_v2.py config set model_dir checkpoints)
  fi
}

prepare_cosy() {
  local repo_path="$1"
  run git -C "$repo_path" submodule update --init --recursive
  local repo_python="$repo_path/.venv/bin/python"
  if [[ "$SKIP_INSTALL" != "1" ]]; then
    repo_python="$(ensure_venv "$repo_path")"
    (cd "$repo_path" && run "$repo_python" -m pip install -r requirements.txt)
  fi
  if [[ "$SKIP_DOWNLOADS" != "1" ]]; then
    if [[ "$RESOLVED_SOURCE" == "ModelScope" ]]; then
      (cd "$repo_path" && run "$repo_python" -c "from modelscope import snapshot_download; snapshot_download('iic/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')")
    else
      [[ "$RESOLVED_SOURCE" == "HF-Mirror" ]] && export HF_ENDPOINT="https://hf-mirror.com"
      (cd "$repo_path" && run "$repo_python" -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')")
    fi
  fi
}

resolve_network_profile

if [[ "$SYNC_REPOS" == "1" ]]; then
  args=(sync-repos)
  [[ "$CLEAN_REPOS" == "1" ]] && args+=(--clean)
  [[ "$DRY_RUN" == "1" ]] && args+=(--dry-run)
  run "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" "${args[@]}"
fi

while IFS= read -r repo; do
  name="$(field "$repo" name)"
  provider="$(field "$repo" provider_type)"
  service_id="$(field "$repo" service_id)"
  variant="$(field "$repo" variant)"
  rel_path="$(field "$repo" path)"
  repo_path="$ROOT/$rel_path"
  target_enabled "$name" "$provider" "$service_id" "$variant" || continue
  case "$provider" in
    gpt-sovits) prepare_gpt "$repo" "$repo_path" "$name" ;;
    indextts) prepare_index "$repo_path" ;;
    cosyvoice) prepare_cosy "$repo_path" ;;
  esac
done < <(repo_json)

run "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" render-services --profile local-all --platform posix --output data/local/services.json
echo "Prepared selected TTS repositories. Rendered data/local/services.json."
