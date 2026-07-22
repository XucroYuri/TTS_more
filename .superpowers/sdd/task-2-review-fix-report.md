# Task 2 independent review fix report

Status: DONE

Commit message: `fix: close embedded controller review gaps`

## RED evidence

Command:

`python -m pytest backend/tests/test_portable_install.py backend/tests/test_portable_packages.py backend/tests/test_portable_first_run_harness.py -q -k "typed_cancellation or staging_move_failure or keeps_previous_until_state_commit or proves_staging_runtime_independence or binary_scans_large or truthfully_distinguishes"`

Result before production changes: `6 failed, 269 deselected`. Each failure corresponded to one missing review contract: typed cancellation, move rollback, state-commit ordering, Full runtime independence evidence, large binary prefix scanning, and truthful harness evidence.

## Fixes

1. Cancellation uses `System.OperationCanceledException` in both byte-identical portable-Python helpers. Initial Python download cancellation and uv `ensure-asset` exit 20 remain typed until the root initializer maps them to exit 20. Other failures remain non-20.
2. Full staging uses a package-external isolated uv cache, rejects reparse points and multi-link files through Windows handle metadata, copies and probes the locked runtime on another writable volume when available, and records the outcome in the Full acceptance sidecar. Runtime executable/config metadata is scanned in bounded chunks for UTF-8 and UTF-16LE machine path prefixes without a 5 MB exemption.
3. Acceptance evidence distinguishes TTS More real embedded-Python initialization from the three preseeded worker fixture lifecycles and direct downloader cases. Every record explicitly has `worker_real_initialization=false`.
4. Runtime publication is transactional through install-state commit. Real PowerShell tests inject missing-staging move failure and state-commit failure and verify that the prior runtime and prior state survive with no false success.

## GREEN evidence

- Focused review contracts: `8 passed`.
- Real publish rollback cases: `4 passed`.
- Task 1 runtime/preparation: `98 passed in 21.53s`.
- Task 2 focused gate: `91 passed, 1 skipped, 185 deselected in 11.33s`.
- Controller Start/Stop: `108 passed in 147.15s`.
- Complete real harness: `17 passed in 346.85s`.
- Portable installer/package regression: `259 passed, 1 skipped in 169.78s`.
- PowerShell 5.1 AST: `AST_OK 5`.
- Native C# link-count smoke: `CSHARP_LINKCOUNT_OK 1`.
- Helper mirror byte equality, initializer forbidden-token audit, and `git diff --check`: passed.

## Scope

No worker production initializer or launcher was migrated. No fork synchronization, four-pack build, or Full deliverability certification was performed. Those remain Task 3 and later acceptance work.
