# Task 3 implementation report

Status: DONE

Commit: `bf6a80220e7272d935fa1735fc3630dd8d9a2155`

## RED evidence

- Worker embedded-runtime structural contracts: `5 failed, 2 passed, 83 deselected in 1.34s`; failures showed the worker initializer still depended on the package Conda/toolchain path and the Full builder lacked the required worker runtime audits.
- Worker Stop exact-patch contract: `1 failed`; pinned `3.10.11`/`3.11.9` values were rejected by the old major/minor-only guard.
- Bundle-relative operation progress contract: `1 failed`; copied `portable_install.py` imported `scripts.portable_operations` through the caller working directory instead of its own bundle directory.
- Request-log evidence contract: `1 failed`; the harness did not yet prove real `Range: bytes=8-` resume and first-mirror 503/second-mirror success from the HTTP request log.
- Real harness debugging also exposed and closed three distinct integration defects: the uv asset used the runtime URL, the fixture worker lock was not exact-patch pinned, and the worker initializer lacked the controller-equivalent rollback-safe runtime publication function.

## GREEN evidence

- Final affected aggregate: `435 passed, 1 skipped, 2 deselected in 254.08s`.
- Worker start/lock/adapter aggregate: `156 passed in 135.77s`.
- Real four-package single run-id 44-item lifecycle: `1 passed in 189.22s`.
- Real four-package two-run-id concurrent lifecycle: `1 passed in 199.52s`.
- Generated temporary mirror contract suites: GPT-SoVITS `15/15`, IndexTTS `15/15`, CosyVoice `15/15`.
- PowerShell 5.1 parsing: `AST_OK 7`.
- Worker initializer forbidden-token audit: `FORBIDDEN_AUDIT_OK` for package Conda, bootstrap Python, pip-module, `Scripts\\uv.exe`, and external-base-prefix fallbacks.
- Canonical/mirrored helper digest: `E57AFDAC5A7217EB7046531A05EFD11FB30999FADB1EF62281FE0E4C7EA7FC4C`; `HELPER_NO_DRIFT_OK`.
- `git diff --check`: `DIFF_CHECK_OK`.

## Implementation

- Worker initialization now installs the locked package-contained Python runtime first, verifies the exact patch, performs device selection and all payload downloads with package Python, installs frozen dependencies using the locked uv wheel and `--target --link-mode copy`, and runs uv/import/CUDA probes without package Conda or a host interpreter.
- Worker runtime publication now matches the controller transaction: the old live runtime remains recoverable until install-state commit, with rollback on publication/state failure.
- Worker Stop accepts pinned `3.10.11` and `3.11.9` while retaining legacy `3.10`/`3.11` state compatibility.
- Full worker package builds verify the staged helper against the integration manifest, isolate and remove uv cache, reject reparse points, bytecode, multiply-linked files, forbidden runtime content and build-machine paths, exercise cross-volume runtime copying where possible, and audit the final archive.
- `portable_install.py` resolves sibling operation modules from its bundle directory, so copied integration bundles are independent of the invoking working directory.
- The first-run harness no longer pre-seeds worker live runtimes or model targets. All four packages execute their real `Initialize.cmd -> Start.cmd -> Stop.cmd -> Repair.cmd` lifecycle. Per-component request logs prove 8-byte Range resume plus 503-to-success mirror fallback, and isolated run IDs prove concurrent cleanup ownership.
- Generated user guidance now describes package-contained CPython and locked uv rather than the removed package Conda cache.

## Files and boundaries

Only the canonical TTS More worktree was changed. Production changes are limited to the worker integration initializer/stop/build scripts, portable installer, integration sync guidance, and the first-run acceptance harness; corresponding canonical tests were updated.

No GPT-SoVITS, IndexTTS, or CosyVoice fork worktree was modified or synchronized. Task 4 was not entered. This task does not claim that final production Full ZIPs have been built or certified for external delivery; it supplies and verifies the canonical runtime/build behavior that the next synchronization/build stages consume.

## Review-fix addendum (2026-07-17)

Task 3 review fixes are committed separately by `fix: certify worker exact Python runtimes`. The harness now uses the exact official CPython 3.11.9/3.10.11 embeddable ZIP bytes pinned by each component lock, verifies size/SHA/layout, and performs real worker initialization with an OperationRoot. CosyVoice evidence proves progress under package Python 3.10.11. Worker truth metadata remains false until all three complete the whole lifecycle and network evidence succeeds. The portable installer loads its sibling operation module by exact file path without permanent `sys.path` shadowing. Python 3.10 compatibility also covers operation timestamps, process-record timestamps, PowerShell UTF-8 process inspection, and CIM seven-digit fractional seconds.

Fresh review-fix evidence:

- Focused exact-runtime/import/evidence tests: `7 passed`.
- Official asset smoke: 3.11.9 `11249023` bytes / SHA `009d6bf7...cfd3b`; 3.10.11 `8629277` bytes / SHA `608619f8...7629d`.
- Real four-package 44-item single run-id: `1 passed in 132.33s`.
- Two concurrent run-id harnesses: `1 passed in 142.24s`.
- Affected aggregate (valid non-mirror contexts): `449 passed, 1 skipped, 2 deselected`; mirror-only contracts were then run in generated mirrors.
- Generated mirror contracts: GPT `15/15`, Index `15/15`, Cosy `15/15`.
- Lock/runtime/sync gate: `115 passed in 89.28s`.
- Launcher/Cosy 3.10 compatibility gate: `42 passed`; direct official 3.10 process inspection printed `PY310_INSPECT_OK`.
- PowerShell 5.1 parsing: `AST_OK 7`; helper digest: `E57AFDAC...C4C`; forbidden runtime audit, `sync-integrations --check`, pycompile, and `git diff --check` passed.

## Self-review

- The required worker order is enforced: embedded Python, exact patch probe, device selection, locked payload downloads, locked uv install, dependency/import/device probes, transactional publication, install state.
- Real acceptance uses only package entrypoints and actual HTTP request evidence; there is no worker runtime/model pre-seeding or direct downloader shortcut.
- Full-package audits are fail-closed and cover both staged trees and ZIP content.
- No known Critical or Important concern remains within Task 3 scope.
