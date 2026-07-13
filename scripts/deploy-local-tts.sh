#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_PYTHON="${TTS_MORE_BASE_PYTHON:-python3}"
PYTHON="$BASE_PYTHON"
APP_PY="$ROOT/.venv/bin/python"

SOURCE="Auto"
DEVICE="CU128"
TARGETS="default"
REPO_PATHS=""
CLEAN_REPOS=0
SKIP_APP_INSTALL=0
SKIP_REPO_SYNC=0
SKIP_REPO_PREPARE=0
SKIP_INSTALL=0
SKIP_DOWNLOADS=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --targets) TARGETS="$2"; shift 2 ;;
    --repo-paths) REPO_PATHS="$2"; shift 2 ;;
    --clean-repos) CLEAN_REPOS=1; shift ;;
    --skip-app-install) SKIP_APP_INSTALL=1; shift ;;
    --skip-repo-sync) SKIP_REPO_SYNC=1; shift ;;
    --skip-repo-prepare) SKIP_REPO_PREPARE=1; shift ;;
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

refresh_python() {
  if [[ -x "$APP_PY" ]]; then
    PYTHON="$APP_PY"
  else
    PYTHON="$BASE_PYTHON"
  fi
}

install_app() {
  [[ "$SKIP_APP_INSTALL" == "1" ]] && { echo "[skip] app dependency install"; return; }
  if [[ ! -x "$APP_PY" ]]; then
    run "$BASE_PYTHON" -m venv "$ROOT/.venv"
  fi
  refresh_python
  run "$PYTHON" -m pip install -e "$ROOT/backend[dev]"
  if command -v pnpm >/dev/null 2>&1; then
    (cd "$ROOT/frontend" && run pnpm install --frozen-lockfile)
  else
    echo "[warn] pnpm was not found; skipping frontend dependency install." >&2
  fi
}

refresh_python
install_app
refresh_python

validate_args=(validate-repo-paths --service-ids "$TARGETS")
[[ -n "$REPO_PATHS" ]] && validate_args+=(--repo-paths "$REPO_PATHS")
run "$PYTHON" "$ROOT/scripts/tts_more_deploy.py" "${validate_args[@]}"

if [[ "$SKIP_REPO_SYNC" != "1" ]]; then
  sync_args=(sync-repos --service-ids "$TARGETS")
  [[ -n "$REPO_PATHS" ]] && sync_args+=(--repo-paths "$REPO_PATHS")
  [[ "$CLEAN_REPOS" == "1" ]] && sync_args+=(--clean)
  [[ "$DRY_RUN" == "1" ]] && sync_args+=(--dry-run)
  run "$PYTHON" "$ROOT/scripts/tts_more_deploy.py" "${sync_args[@]}"
else
  echo "[skip] repo sync"
fi

bundle_args=(install-repo-bundles --service-ids "$TARGETS")
[[ -n "$REPO_PATHS" ]] && bundle_args+=(--repo-paths "$REPO_PATHS")
[[ "$DRY_RUN" == "1" ]] && bundle_args+=(--dry-run)
run "$PYTHON" "$ROOT/scripts/tts_more_deploy.py" "${bundle_args[@]}"

update_script_args=(install-update-scripts --service-ids "$TARGETS")
[[ -n "$REPO_PATHS" ]] && update_script_args+=(--repo-paths "$REPO_PATHS")
[[ "$DRY_RUN" == "1" ]] && update_script_args+=(--dry-run)
run "$PYTHON" "$ROOT/scripts/tts_more_deploy.py" "${update_script_args[@]}"

if [[ "$SKIP_REPO_PREPARE" != "1" ]]; then
  prepare_args=(--source "$SOURCE" --device "$DEVICE" --targets "$TARGETS")
  [[ -n "$REPO_PATHS" ]] && prepare_args+=(--repo-paths "$REPO_PATHS")
  [[ "$SKIP_INSTALL" == "1" ]] && prepare_args+=(--skip-install)
  [[ "$SKIP_DOWNLOADS" == "1" ]] && prepare_args+=(--skip-downloads)
  [[ "$DRY_RUN" == "1" ]] && prepare_args+=(--dry-run)
  run bash "$ROOT/scripts/prepare-tts-repos.sh" "${prepare_args[@]}"
else
  echo "[skip] repo dependency/model prepare"
fi

render_args=(render-services --profile local-all --platform posix --service-ids "$TARGETS" --output data/local/services.json)
[[ -n "$REPO_PATHS" ]] && render_args+=(--repo-paths "$REPO_PATHS")
run "$PYTHON" "$ROOT/scripts/tts_more_deploy.py" "${render_args[@]}"

doctor_args=(doctor --service-ids "$TARGETS")
[[ -n "$REPO_PATHS" ]] && doctor_args+=(--repo-paths "$REPO_PATHS")
run "$PYTHON" "$ROOT/scripts/tts_more_deploy.py" "${doctor_args[@]}"

echo "Local TTS deployment workflow complete."
