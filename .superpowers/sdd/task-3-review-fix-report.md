# Task 3 review-fix report

Status: DONE

Commit: pending at report creation; see the commit named `fix: certify worker exact Python runtimes`.

## Findings fixed

- `portable_operations.py` and `portable_launcher.py` now use `timezone.utc`, so the official CosyVoice Python 3.10.11 runtime can import them.
- `portable_launcher.py` normalizes PowerShell CIM's seven-digit fractional timestamps to Python's microsecond precision, and process queries use explicit UTF-8 transport.
- `portable_install.py` loads the same-bundle `portable_operations.py` by exact file location and never mutates global `sys.path`.
- The first-run harness preserves each package's exact lock: TTS More/GPT/Index use official 3.11.9; CosyVoice uses official 3.10.11. Downloads occur once per immutable asset inside the run-id before restricted-PATH package execution and are verified by exact size, SHA-256, and ZIP entries.
- Worker Initialize receives a real UUID OperationRoot/CancelFile pair. The harness checks actual download events and probes the exact package Python version.
- Worker evidence starts false and is finalized only after GPT, Index, and Cosy each complete Initialize/Start/Stop/Repair plus network evidence. Failed runs remain false; the observed failed Cosy resume run produced 38 records with every worker flag false.
- Package-child cleanup is scoped by package executable/command identity so exited parent PID reuse cannot create a false residual-process result.
- The controlled mirror contract now requires exact-file sibling loading instead of the removed permanent import-path insertion.

## RED evidence

- Initial focused run: `4 failed`; official Python 3.10.11 raised `ImportError: cannot import name 'UTC'`, installer import changed `sys.path`, harness rewrote Cosy to 3.11.9, and evidence was unconditionally true.
- Evidence timing probe failed because no lifecycle finalization gate existed.
- First real run reached Cosy managed initialization but failed Start because mirrored `portable_launcher.py` still imported `datetime.UTC`; all worker evidence remained false.
- Official Python 3.10.11 focused regression reproduced `ValueError` for a CIM timestamp with seven fractional digits.
- Generated mirror contract initially rejected the new exact-file loader because its old assertion required permanent `sys.path` insertion.

## GREEN evidence

- Focused review tests: `7 passed in 3.87s`.
- Static affected tests: `63 passed, 2 deselected`.
- Portable launcher plus actual official 3.10.11 regression: `42 passed`; direct `_inspect_process` smoke returned `PY310_INSPECT_OK`.
- Single four-package harness: `1 passed in 132.33s`; matrix size `44`.
- Dual concurrent harness: `1 passed in 142.24s`; each run produced its own 44-item result.
- Generated mirror contracts: `15 passed` for each of GPT-SoVITS, IndexTTS, and CosyVoice.
- Lock/runtime/integration suite: `115 passed in 89.28s`.
- Task 3 affected aggregate: `449 passed, 1 skipped, 2 real-harness tests deselected`; the mirror-only contract file was intentionally validated in generated mirror roots instead of the canonical root.
- Official asset verification passed for Python 3.11.9 (`11249023`, SHA `009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b`) and Python 3.10.11 (`8629277`, SHA `608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d`).
- `AST_OK 7`, `HELPER_NO_DRIFT_OK E57AFDAC5A7217EB7046531A05EFD11FB30999FADB1EF62281FE0E4C7EA7FC4C`, `FORBIDDEN_AUDIT_OK`, `SYNC_CHECK_OK`, pycompile, and `DIFF_CHECK_OK`.

## Boundaries

Only the canonical TTS More worktree changed. No fork worktree was modified, no Task 4 synchronization was performed, and no final Full ZIP delivery claim is made here.
