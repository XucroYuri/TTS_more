#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${TTS_MORE_BASE_PYTHON:-python3}"
APP_PY="$ROOT/.venv/bin/python"
[[ -x "$APP_PY" ]] || APP_PY="$PYTHON"

SOURCE="Auto"
RESOLVED_SOURCE=""
NETWORK_PROFILE_JSON=""
SOURCE_FALLBACKS=()
PACKAGE_INDEX_FALLBACKS=()
DEVICE="CU128"
TARGETS="default"
REPO_PATHS=""
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
    --repo-paths) REPO_PATHS="$2"; shift 2 ;;
    --sync-repos) SYNC_REPOS=1; shift ;;
    --clean-repos) CLEAN_REPOS=1; shift ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --skip-downloads) SKIP_DOWNLOADS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

run() {
  printf '[run]'
  printf ' %q' "$@"
  printf '\n'
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@"
}

run_plan() {
  printf '[plan]'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

run_in_repo() {
  local working_directory="$1"
  shift
  printf '[run cwd=%q]' "$working_directory"
  printf ' %q' "$@"
  printf '\n'
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  (cd "$working_directory" && "$@")
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
  [[ "$TARGETS" == "default" ]] && return 0
  IFS=',' read -ra parts <<< "$TARGETS"
  for item in "${parts[@]}"; do
    [[ "$item" == "$name" || "$item" == "$provider" || "$item" == "$service_id" || "$item" == "$variant" ]] && return 0
  done
  return 1
}

repo_json() {
  local args=(list-repos --json-lines --service-ids "$TARGETS")
  [[ -n "$REPO_PATHS" ]] && args+=(--repo-paths "$REPO_PATHS")
  "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" --root "$ROOT" "${args[@]}"
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
    read -ra SOURCE_FALLBACKS <<< "$(source_fallbacks "$RESOLVED_SOURCE")"
    read -ra PACKAGE_INDEX_FALLBACKS <<< "$(package_index_fallbacks "https://mirrors.aliyun.com/pypi/simple")"
    echo "[network] source=$RESOLVED_SOURCE"
    return 0
  fi
  NETWORK_PROFILE_JSON="$(run_capture "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" probe-network --write --source "$SOURCE")"
  while IFS='=' read -r key value; do
    [[ -n "$key" ]] && export "$key=$value"
  done < <(export_network_env)
  RESOLVED_SOURCE="$(json_field_from_profile model_source)"
  [[ -z "$RESOLVED_SOURCE" ]] && RESOLVED_SOURCE="$([[ "$SOURCE" == "Auto" ]] && echo ModelScope || echo "$SOURCE")"
  read -ra SOURCE_FALLBACKS <<< "$(source_fallbacks "$RESOLVED_SOURCE")"
  read -ra PACKAGE_INDEX_FALLBACKS <<< "$(package_index_fallbacks "$(json_field_from_profile pip_index_url)")"
  echo "[network] source=$RESOLVED_SOURCE"
}

source_fallbacks() {
  local primary="$1" item
  local ordered=("$primary" ModelScope HF-Mirror HF)
  local result=()
  for item in "${ordered[@]}"; do
    [[ -z "$item" ]] && continue
    local exists=0
    [[ " ${result[*]-} " == *" $item "* ]] && exists=1
    [[ "$exists" == "0" ]] && result+=("$item")
  done
  [[ -n "${result[*]-}" ]] && printf '%s ' "${result[@]}"
}

run_with_source_fallback() {
  local description="$1"
  local candidate
  shift
  local failures=()
  for candidate in "${SOURCE_FALLBACKS[@]}"; do
    echo "[source] $description via $candidate"
    if "$@" "$candidate"; then
      return 0
    fi
    failures+=("$candidate")
    echo "[warn] $description failed via $candidate" >&2
    [[ "$DRY_RUN" == "1" ]] && return 0
  done
  echo "[error] $description failed for all sources: ${failures[*]}" >&2
  return 1
}

package_index_fallbacks() {
  local primary="$1" item
  local ordered=("$primary" "https://mirrors.aliyun.com/pypi/simple" "https://pypi.org/simple")
  local result=()
  for item in "${ordered[@]}"; do
    [[ -z "$item" ]] && continue
    local exists=0
    [[ " ${result[*]-} " == *" $item "* ]] && exists=1
    [[ "$exists" == "0" ]] && result+=("$item")
  done
  [[ -n "${result[*]-}" ]] && printf '%s ' "${result[@]}"
}

set_package_index_env() {
  local index_url="$1"
  if [[ -n "$index_url" ]]; then
    export PIP_INDEX_URL="$index_url"
    export UV_INDEX_URL="$index_url"
  else
    unset PIP_INDEX_URL
    unset UV_INDEX_URL
  fi
}

run_with_package_index_fallback() {
  local description="$1"
  local candidate
  shift
  local failures=()
  for candidate in "${PACKAGE_INDEX_FALLBACKS[@]}"; do
    echo "[package-index] $description via $candidate"
    set_package_index_env "$candidate"
    if "$@" "$candidate"; then
      return 0
    fi
    failures+=("$candidate")
    echo "[warn] $description failed via $candidate" >&2
    [[ "$DRY_RUN" == "1" ]] && return 0
  done
  echo "[error] $description failed for all package indexes: ${failures[*]}" >&2
  return 1
}

ensure_venv() {
  local repo_path="$1"
  local repo_python="$repo_path/.venv/bin/python"
  if [[ ! -x "$repo_python" ]]; then
    run "$PYTHON" -m venv "$repo_path/.venv"
    run_with_package_index_fallback "base Python package upgrade" run_pip_upgrade "$repo_python"
  fi
}

run_pip_upgrade() {
  local repo_python="$1" candidate="$2"
  run "$repo_python" -m pip install -U pip wheel setuptools
}

prepare_gpt() {
  local repo="$1" repo_path="$2" name="$3"
  [[ "$SKIP_INSTALL" == "1" ]] && { echo "[skip] GPT-SoVITS install for $name"; return; }
  if [[ "$DRY_RUN" == "1" ]]; then
    run_with_source_fallback "GPT-SoVITS install for $name" run_gpt_install "$repo_path"
    return
  fi
  if [[ ! -f "$repo_path/install.sh" ]]; then
    echo "Missing GPT-SoVITS installer: $repo_path/install.sh" >&2
    exit 1
  fi
  run_with_source_fallback "GPT-SoVITS install for $name" run_gpt_install "$repo_path"
}

run_gpt_install() {
  local repo_path="$1" candidate="$2"
  run bash "$repo_path/install.sh" --device "$DEVICE" --source "$candidate"
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
      run_with_package_index_fallback "IndexTTS dependency install" run_uv_sync "$repo_path" "$uv_bin"
    else
      local repo_python
      ensure_venv "$repo_path"
      repo_python="$repo_path/.venv/bin/python"
      run_with_package_index_fallback "IndexTTS dependency install" run_pip_editable "$repo_path" "$repo_python"
    fi
  fi
  if [[ "$SKIP_DOWNLOADS" != "1" ]]; then
    local repo_python="$repo_path/.venv/bin/python"
    if [[ ! -x "$repo_python" && -n "$uv_bin" ]]; then
      run_with_package_index_fallback "IndexTTS dependency install" run_uv_sync "$repo_path" "$uv_bin"
    fi
    run_with_source_fallback "IndexTTS model download" run_index_download "$repo_path" "$repo_python"
    run_in_repo "$repo_path" "$repo_python" indextts/cli_v2.py config set model_dir checkpoints
  fi
}

run_uv_sync() {
  local repo_path="$1" uv_bin="$2" candidate="$3"
  run_in_repo "$repo_path" "$uv_bin" sync --all-extras
}

run_pip_editable() {
  local repo_path="$1" repo_python="$2" candidate="$3"
  run_in_repo "$repo_path" "$repo_python" -m pip install -e .
}

run_index_download() {
  local repo_path="$1" repo_python="$2" candidate="$3"
  local source_arg="huggingface"
  [[ "$candidate" == "ModelScope" ]] && source_arg="modelscope"
  if [[ "$candidate" == "HF-Mirror" ]]; then
    export HF_ENDPOINT="https://hf-mirror.com"
  else
    unset HF_ENDPOINT
  fi
  run_in_repo "$repo_path" "$repo_python" indextts/cli_v2.py download --source "$source_arg" --model-dir checkpoints
}

prepare_cosy() {
  local repo_path="$1"
  local repo_python="$repo_path/.venv/bin/python"
  if [[ "$SKIP_INSTALL" != "1" ]]; then
    ensure_venv "$repo_path"
    repo_python="$repo_path/.venv/bin/python"
    run_with_package_index_fallback "CosyVoice dependency install" run_pip_requirements "$repo_path" "$repo_python" requirements.txt
  fi
  if [[ "$SKIP_DOWNLOADS" != "1" ]]; then
    run_with_source_fallback "CosyVoice model download" run_cosy_download "$repo_path" "$repo_python"
  fi
}

run_pip_requirements() {
  local repo_path="$1" repo_python="$2" requirements="$3" candidate="$4"
  run_in_repo "$repo_path" "$repo_python" -m pip install -r "$requirements"
}

run_cosy_download() {
  local repo_path="$1" repo_python="$2" candidate="$3"
  if [[ "$candidate" == "ModelScope" ]]; then
    unset HF_ENDPOINT
    run_in_repo "$repo_path" "$repo_python" -c "from modelscope import snapshot_download; snapshot_download('iic/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
  else
    if [[ "$candidate" == "HF-Mirror" ]]; then
      export HF_ENDPOINT="https://hf-mirror.com"
    else
      unset HF_ENDPOINT
    fi
    run_in_repo "$repo_path" "$repo_python" -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
  fi
}

preflight_gpt_conda() {
  local repo name provider service_id variant
  [[ "$SKIP_INSTALL" == "1" || "$DRY_RUN" == "1" ]] && return 0
  while IFS= read -r repo; do
    [[ -z "$repo" ]] && continue
    name="$(field "$repo" name)"
    provider="$(field "$repo" provider_type)"
    service_id="$(field "$repo" service_id)"
    variant="$(field "$repo" variant)"
    target_enabled "$name" "$provider" "$service_id" "$variant" || continue
    [[ "$provider" == "gpt-sovits" ]] || continue
    if command -v conda >/dev/null 2>&1 && conda --version >/dev/null 2>&1; then
      return 0
    fi
    if command -v micromamba >/dev/null 2>&1; then
      echo "[error] micromamba is installed but is not currently supported by the TTS More GPT-SoVITS prepare workflow; install conda or use --skip-install." >&2
    else
      echo "[error] supported conda executable was not found; GPT-SoVITS dependency preparation cannot continue. Install conda or use --skip-install." >&2
    fi
    return 1
  done <<< "$REPOSITORIES_JSON"
}

resolve_network_profile

if [[ "$SYNC_REPOS" == "1" ]]; then
  args=(sync-repos --service-ids "$TARGETS")
  [[ -n "$REPO_PATHS" ]] && args+=(--repo-paths "$REPO_PATHS")
  [[ "$CLEAN_REPOS" == "1" ]] && args+=(--clean)
  [[ "$DRY_RUN" == "1" ]] && args+=(--dry-run)
  run_plan "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" "${args[@]}"
fi

REPOSITORIES_JSON="$(repo_json)"
preflight_gpt_conda

while IFS= read -r repo; do
  [[ -z "$repo" ]] && continue
  name="$(field "$repo" name)"
  provider="$(field "$repo" provider_type)"
  service_id="$(field "$repo" service_id)"
  variant="$(field "$repo" variant)"
  repo_path="$(field "$repo" absolute_path)"
  target_enabled "$name" "$provider" "$service_id" "$variant" || continue
  case "$provider" in
    gpt-sovits) prepare_gpt "$repo" "$repo_path" "$name" ;;
    indextts) prepare_index "$repo_path" ;;
    cosyvoice) prepare_cosy "$repo_path" ;;
  esac
done <<< "$REPOSITORIES_JSON"

render_args=(render-services --profile local-all --platform posix --service-ids "$TARGETS")
[[ -n "$REPO_PATHS" ]] && render_args+=(--repo-paths "$REPO_PATHS")
[[ "$DRY_RUN" != "1" ]] && render_args+=(--output data/local/services.json)
run_plan "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" "${render_args[@]}"
echo "Prepared selected TTS repositories. Rendered data/local/services.json."
