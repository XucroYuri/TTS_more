# Task 4 transaction journal report

Status: DONE

## Scope

- Changed canonical sync, its tests, and this report only.
- Did not touch forks or `repo.lock.json`; did not push.

## Journal lifecycle

- A successful prior atomic claim is immediately recorded in `prior_claims`; rollback restores every recorded prior claim even when exclusive publication never starts or fails.
- A new exclusive publication is immediately recorded as `pending_new` with staged size and raw SHA-256. Successful consistent capture promotes it to `published`; capture failure rollback atomically claims and validates the pending payload before removal.
- The state machine is `claimed -> pending/captured -> manifest committed`. Successful manifest publication is the commit point; post-commit failures never roll back the committed manifest or new files.
- Prior-claim cleanup metadata is persisted before commit with the expected manifest digest and exact claim identities. Cleanup retries three times. A remaining failure preserves the identifiable claims and journal and reports a post-commit cleanup error.
- A later sync validates the committed manifest digest, containment, and claim identities, completes cleanup idempotently, then proceeds normally.

## TDD evidence

- Initial lifecycle RED: `3 failed`; prior claim was not restored without a published/removed state, capture failure left an untracked new file, and cleanup failure rolled back the committed manifest.
- Final lifecycle GREEN: `3 passed`.

## Verification

- Complete integration sync suite: `93 passed in 120.63s`.
- `py_compile` for sync and tests: passed.
- `git diff --check`: passed; only LF-to-CRLF notices.

## Concerns

None known within task scope.
