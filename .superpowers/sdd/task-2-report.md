# Task 2 implementation report

Status: DONE

Commit: `e8b3f200c617a874904cc9a3e7be94d8b8202993`

## RED evidence

- Controller conversion RED:
  - Command: `.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_portable_install.py backend/tests/test_portable_packages.py backend/tests/test_portable_first_run_harness.py -q -k "runtime or full or initialize"`
  - Result: `3 failed, 78 passed, 1 skipped, 187 deselected`.
  - Expected failures: controller initializer still used package Conda; builder omitted `portable-python.ps1`/Full runtime audit; first-run harness still installed a fixture Conda adapter.
- Exact-patch validation RED:
  - Command: `.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_portable_start_controller.py -q -k "exact_patch or pinned_and_legacy"`
  - Result: `2 failed, 3 passed, 100 deselected`.
  - Expected failures: `3.11.9` was compared with a major/minor-only probe and schema accepted only `3.10`/`3.11`.
- Controller Stop RED:
  - Command: `.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_portable_start_controller.py -q -k "controller_stop_accepts"`
  - Result: `1 failed, 2 passed, 105 deselected`.
  - Expected failure: pinned `3.11.9` was rejected by the old hard-coded `3.11` guard.
- Full-audit self-review RED:
  - Command: `.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_portable_packages.py -q -k "stages_embedded_runtime_helper"`
  - Result: `1 failed, 227 deselected`.
  - Expected failure: the audit did not yet reject the real `Miniforge*` directory-name family.

## GREEN evidence

- Focused Task 2 gate: `81 passed, 1 skipped, 187 deselected in 11.01s`.
- Exact-patch controller/schema gate: `5 passed, 100 deselected in 2.24s`.
- Controller Stop gate: `3 passed, 105 deselected in 1.70s`.
- Original first-run harness run: `16 passed in 317.66s`.
  - TTS More exercised real embedded-Python Initialize/Start/Stop/Repair. GPT-SoVITS, IndexTTS, and CosyVoice exercised package-root lifecycle/concurrency with preseeded fixture runtimes/assets; their real production initialization is reserved for Task 3. Direct downloader scenarios covered 8-byte Range resume and mirror fallback.
- Complete controller Start/Stop suite: `108 passed in 137.92s`.
- Task 1 embedded-runtime regression: `51 passed, 47 deselected in 16.94s`.
- Full-audit prefix regression: `1 passed, 227 deselected in 0.41s`.
- PowerShell 5.1 AST: `AST_OK 8` changed PowerShell files.
- `git diff --check`: passed.
- Initializer forbidden-token audit: passed for `bootstrap-conda.ps1`, `$Conda`, `conda create`, `-m pip`, `Scripts\uv.exe`, and `$BootstrapPython`.

## Implementation

- Controller initialization now installs locked embedded Python first, probes the exact patch, selects the device with package Python, downloads locked models, installs the frozen dependency export with uv `--target` and `--link-mode copy`, runs uv pip check/import probes, and atomically publishes staging to live.
- `portable_install.py` is natively Python 3.10 compatible via `timezone.utc`; the helper runner retains only the isolated-runtime `sys.path` bootstrap needed for sibling operation modules, with no datetime monkey patch.
- Builder stages `portable-python.ps1`, audits Full staging and ZIP entries for `pyvenv.cfg`, Conda directories, and `Miniforge*`, and emits controller metadata pinned to Python `3.11.9`.
- Shared controller validation, Start, Stop, and schema accept pinned `3.11.9`/`3.10.11` while retaining legacy `3.11`/`3.10` read compatibility. Patch expectations compare `platform.python_version()` exactly; legacy expectations compare major/minor.
- The first-run harness synthesizes an embedded Python ZIP and exact uv wheel entry, includes the flat `_ctypes.pyd`/`libffi` runtime dependency set, and no longer creates a Conda adapter.

## Files and boundaries

Production files changed only within the approved Task 2 scope:

- `Build-Package.ps1`
- `scripts/initialize-portable.ps1`
- `scripts/portable_install.py`
- `scripts/portable-python.ps1`
- `integrations/windows/portable-python.ps1` (Task 1 no-drift helper compatibility only)
- `scripts/Portable-Validation.ps1`
- `scripts/Invoke-PortableStart.ps1`
- `scripts/stop-production.ps1`
- `packaging/portable/tts-more-package.schema.json`

Tests/harness changed only in approved files. No worker initializer/start/stop production file was changed, no fork was synchronized, no four-pack was built, and no Full deliverability certification is claimed.

## Self-review

- Exact required order is present and asserted.
- Existing operation/cancellation/model/device/install-state/atomic-live behavior remains in place.
- Controller initializer contains no package Conda, pip module, system Python/uv, or `Scripts\uv.exe` fallback.
- Bootstrap build-tool Conda remains only in source-builder scope, not controller initialization/runtime scope.
- The real harness revealed and closed exact-patch Start/Stop compatibility and fixture-only `_ctypes/libffi` completeness issues.
- No known Critical or Important concern remains within Task 2 scope.

## Independent review closure

The follow-up commit `fix: close embedded controller review gaps` closes the four Important review findings:

- Python and uv download cancellation now crosses the helper boundary as `OperationCanceledException`; the controller maps only this typed signal to exit 20.
- Full staging isolates `UV_CACHE_DIR`, rejects recursive reparse points and multiply-linked files, performs a locked cross-volume runtime probe when another writable volume exists, and chunk-scans runtime executable/config metadata for UTF-8 and UTF-16LE build-machine prefixes.
- Harness evidence explicitly records `worker_real_initialization=false`, controller real initialization, preseeded worker fixture runtime use, and direct-downloader coverage. Scenario names no longer imply real worker initialization.
- Runtime publication retains `runtime/previous` until install-state commit. Move/state failures remove the incomplete candidate and restore the previous runtime while rethrowing the original failure; post-commit previous cleanup is non-destructive.

Fresh review-fix verification:

- Focused review contracts: `8 passed`; real rollback transaction tests: `4 passed`.
- Task 1 runtime and preparation regression: `98 passed in 21.53s`.
- Task 2 focused gate: `91 passed, 1 skipped, 185 deselected in 11.33s`.
- Complete controller Start/Stop suite: `108 passed in 147.15s`.
- Complete truthful first-run harness: `17 passed in 346.85s`.
- Complete portable installer/package suite: `259 passed, 1 skipped in 169.78s`.
- Windows PowerShell 5.1 AST: `AST_OK 5`; helper mirrors byte-identical; forbidden-token audit and `git diff --check` passed.
