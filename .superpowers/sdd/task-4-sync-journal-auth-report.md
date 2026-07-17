# Task 4 cleanup journal authorization report

Status: DONE

## Scope

- Changed canonical sync, its tests, and this report only.
- Did not touch forks or `repo.lock.json`; did not push.

## Security change

- Removed cross-process cleanup-journal recovery and all parsing/execution of claim paths at startup.
- Sync startup and `--check` inspect only the cleanup journal filesystem object: containment, no reparse point, and regular-file type.
- Discovery of any `.integration-cleanup-*.json` fails closed with a manual-cleanup diagnostic before target mutation. Journal contents are not read and neither the journal nor any referenced path is removed.
- Same-process post-commit cleanup still retries its own in-memory claims up to three times. If it cannot finish, it preserves the claim and journal for explicit manual handling.

## TDD evidence

- RED: `2 failed`; a genuine leftover was automatically consumed on the next sync and a forged journal was not rejected by check.
- GREEN: `2 passed`; forged `data/user/.tts-more-victim`, journal bytes, genuine claims, and journal bytes remain unchanged.

## Verification

- Complete integration sync suite: `94 passed in 119.94s`.
- `py_compile` for sync and tests: passed.
- `git diff --check`: passed; only LF-to-CRLF notices.

## Concerns

None known within task scope.
