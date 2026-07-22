# Task 4 sync containment report

Status: DONE

## Scope

- Hardened `scripts/sync_integrations.py`.
- Added containment tests in `backend/tests/test_integration_sync.py`.
- Did not modify forks, `repo.lock.json`, or remotes.

## Root cause

The sync boundary used `Path.exists()` and `Path.is_dir()`, which follow Windows junctions and other reparse points. Manifest validation accepted normalized aliases and Windows-invalid path segments, and publication helpers lacked the trusted target root needed for last-moment containment checks.

## Changes

- Target probing now uses `os.path.lexists()` and `os.lstat()`.
- Sync and check reject symlinks, junctions, and other reparse points at the target root, controlled root, controlled ancestors, and controlled leaves, including broken junctions.
- One canonical manifest validator is used for previous and desired paths. It rejects non-verbatim POSIX normalization, traversal, backslashes, absolute paths, ADS colons, trailing dots/spaces, Windows reserved names, and case-insensitive aliases.
- Publish, delete, empty-directory cleanup, rollback, and atomic byte restoration validate lexical containment, resolved containment, and reparse-free path chains immediately before and after filesystem mutation.
- Collision checks are link-aware and never follow an unknown link.

## TDD evidence

Initial containment/canonical run: `16 failed`. Failures reproduced silent junction traversal, broken-junction late failure, missing check validation, accepted Windows-invalid aliases, and missing desired-path preflight.

Focused GREEN after the fix: `18 passed`, covering the 16 new cases plus both existing rollback fault windows.

The real Windows junction cases verified that outside sentinels and root entry bytes remain unchanged for a `tts_more` junction, a nested controlled-ancestor junction, and a broken junction. `--check` reports the reparse boundary in all three cases.

## Verification

- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_integration_sync.py -q`
  - `81 passed in 110.57s`
- `backend\.venv\Scripts\python.exe -m py_compile scripts\sync_integrations.py backend\tests\test_integration_sync.py`
  - passed
- `git diff --check`
  - passed; only Git LF-to-CRLF working-tree notices were emitted.

## Concerns

None known within task scope.
