# Task 2 final audit report

Status: DONE

Commit message: `fix: exclude build paths from full runtime`

## RED evidence

Focused command:

`python -m pytest backend/tests/test_portable_packages.py -q -k "actual_build_and_initialization_roots or removes_and_rejects_python_bytecode or every_runtime_file"`

Initial result: `3 failed, 230 deselected`. The Full builder did not include its actual work/stage/uv-cache roots in the runtime prefix set, did not remove or forbid Python bytecode, and selected runtime files by extension instead of scanning every file.

## Fix

- Full runtime prefix auditing now includes the actual source root, unique work root, package stage, work base, isolated uv cache, system temporary root, `TEMP`, `TMP`, and user roots. Each prefix is encoded as UTF-8 and UTF-16LE.
- A bounded native `FileStream` scanner checks every file under `runtime/live`, independent of extension and size. It carries prefix-length overlap between 1 MiB chunks and does not load model/runtime files into memory at once.
- Other package files retain a size-bounded, explicit text-extension allowlist scan.
- After Full initialization, the builder safely removes runtime `__pycache__` directories and `*.pyc` files. Full staging and final ZIP audits reject any remaining Python bytecode.

## GREEN evidence

- Focused Full audit: `5 passed, 228 deselected in 0.29s`.
- Native cross-chunk binary prefix smoke: `BOUNDED_PREFIX_SCAN_OK`.
- Complete portable installer/package suite: `262 passed, 1 skipped in 161.02s`.
- Windows PowerShell 5.1 AST: `AST_OK 1`.
- `git diff --check`: passed.

## Scope

Only `Build-Package.ps1`, portable package tests, and this report changed. No worker migration, fork synchronization, package build, or Task 3 work was performed.
