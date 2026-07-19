# Task 4 sync preservation report

Status: DONE

## Scope

- Changed `scripts/sync_integrations.py`.
- Changed `backend/tests/test_integration_sync.py`.
- Did not modify any fork, `repo.lock.json`, or remote branch.

## Implementation

- Builds and hashes the complete canonical integration in an external staging directory before target mutation.
- Reads and structurally validates the prior manifest; only its declared files and the manifest itself are treated as owned.
- Preflights unknown-file collisions before publication.
- Publishes files atomically, removes only obsolete manifest-owned files, and removes directories only when empty.
- Backs up prior controlled bytes and restores them after any publication failure, including failure after an atomic replace.
- Leaves target-owned extras untouched and excludes them from `--check` drift results.

## TDD evidence

Initial focused RED: `4 failed, 1 passed`; failures demonstrated unknown extras rejected by check, nested target assets removed, collision overwrite, and missing transaction publication seam. The obsolete-controlled-file test already passed because the unsafe whole-tree deletion also removed it.

Post-replace failure RED: `1 failed`; a publisher that raised immediately after atomic replacement left `Build-Package.ps1` behind.

Focused GREEN after implementation: `6 passed, 59 deselected`.

## Verification

- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_integration_sync.py -q`
  - `65 passed in 98.30s`
- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_portable_first_run_harness.py -q -k "not real_micro"`
  - `17 passed, 2 deselected in 2.70s`
- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_prepare_scripts.py -q -k "portable_first_run or portable_python_helpers or portable_runtime_powershell"`
  - `3 passed, 45 deselected in 0.06s`
- `backend\.venv\Scripts\python.exe -m py_compile scripts\sync_integrations.py backend\tests\test_integration_sync.py`
  - passed
- `git diff --check`
  - passed; Git emitted only working-tree LF-to-CRLF notices.

## Self-review

- No remaining known correctness concerns in task scope.
- Transaction rollback records a destination before invoking the publisher, covering both pre-replace and post-replace exceptions.
