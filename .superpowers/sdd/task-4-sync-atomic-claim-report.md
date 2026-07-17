# Task 4 atomic claim report

Status: DONE

## Scope

- Changed canonical sync, its tests, and this report only.
- Did not touch forks or `repo.lock.json`; did not push.

## Implementation

- New-file staging captures expected size and raw SHA-256 before atomic exclusive publication, then captures a consistent destination snapshot before journaling.
- Prior replacement first uses atomic no-replace rename into a transaction claim, validates the claim against the backup identity and digest, and only then publishes the new file.
- Rollback atomically claims the current destination before validation or deletion. A mismatch is moved back with no-replace semantics and preserved; rollback records the error and continues restoring other files.
- Windows uses `os.rename`/MoveFileW no-replace semantics. Linux uses `renameat2(RENAME_NOREPLACE)` through libc. Missing primitives and unsupported platforms fail closed.
- Identity snapshots bind device, inode, size, mtime, Windows attributes, and raw SHA-256; POSIX also binds mode. Windows staged comparison intentionally uses size and digest because Python synthesizes mode bits from the path extension even for the same inode.

## TDD evidence

- Four final reviewer injections initially produced `2 failed, 2 passed`; the remaining failures reproduced prior guard-to-replace overwrite and rollback check-to-unlink deletion.
- Final focused atomic-claim/race set: `10 passed`.
- An existing obsolete-directory test caught claim commit cleanup ordering (`89 passed, 1 failed`); cleanup was corrected and the focused regression set passed `5 passed`.

## Verification

- Complete integration sync suite: `90 passed in 116.98s`.
- `py_compile` for sync and tests: passed.
- `git diff --check`: passed; only LF-to-CRLF notices.

## Concerns

None known within task scope.
