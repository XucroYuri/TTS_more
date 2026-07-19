# Task 4 sync race-safety report

Status: DONE

## Scope

- Changed only canonical `scripts/sync_integrations.py`, its integration sync tests, and this report.
- Did not modify forks or `repo.lock.json`; did not push.

## Root cause and implementation

- The transaction previously journaled a path before publication and used replacing publication for new files. A pre-publication failure could therefore delete an external racing file, while a racing regular file could be overwritten.
- New paths now publish a same-directory temporary file with atomic exclusive `os.link`; Windows hard-link-unavailable filesystems fall back to same-volume `os.rename`/MoveFileW no-replace semantics.
- Prior paths are accepted for replacement only while device, inode, mode, size, mtime, Windows attributes, and raw SHA-256 still match the captured backup.
- Publication is journaled only after the exclusive link or replacement succeeds and a post-publication identity snapshot is captured.
- Rollback deletes/restores only paths that still match the transaction snapshot. It handles each path independently, collects errors, continues restoring other prior files, and never follows a reparse point.

## TDD evidence

- Initial race RED: `4 failed`; reproduced racing overwrite, pre-publication external deletion, fail-fast junction rollback, and failure to restore another prior file.
- Race GREEN with prior fault regressions: `6 passed`.
- Hard-link fallback RED: `1 failed` with `OSError 50`.
- Final focused GREEN including fallback: `7 passed`.

## Verification

- Complete `backend/tests/test_integration_sync.py`: `86 passed in 120.10s`.
- `python -m py_compile scripts/sync_integrations.py backend/tests/test_integration_sync.py`: passed.
- `git diff --check`: passed; only LF-to-CRLF working-tree notices.

## Concerns

None known within task scope.
