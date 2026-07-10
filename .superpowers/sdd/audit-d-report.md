# Audit D: Local Defaults and Makefile Entrypoints

## Scope

- Branch: `dev-xu/macos-lan-cuda-validation`
- Base SHA before this batch: `4f62cd512190287ddfb5ef367778fddc96fac7ff`
- Allowed files changed: `Makefile`, `frontend/package.json`, `backend/tests/test_prepare_scripts.py`, and this report.
- An unrelated concurrent change appeared in `backend/tests/test_deploy_tool.py` during verification; it was left untouched and is excluded from this commit.

## RED

Added regression tests for the frontend dev host and POSIX/Windows `make -n dev` and `make -n workers` entrypoints. Before the implementation change, all five new tests failed:

- frontend dev used `vite --host 0.0.0.0` instead of loopback;
- `ifeq`, `else`, and `endif` were emitted as shell recipe text;
- both branches were emitted instead of one platform-specific command.

## GREEN

Implemented the minimum fix:

- frontend `dev` now uses `vite --host 127.0.0.1`;
- `dev` and `workers` use make-level `ifeq` conditions;
- no explicit LAN entry was added to the default command.

Verification results:

- focused regression tests: `5 passed`;
- `backend/tests/test_prepare_scripts.py`: `23 passed`;
- POSIX `make -n dev`: `scripts/start-dev.sh`;
- POSIX `make -n workers`: `scripts/start-service-workers.sh`;
- Windows `make -n dev`: `powershell -ExecutionPolicy Bypass -File scripts/start-dev.ps1`;
- Windows `make -n workers`: `powershell -ExecutionPolicy Bypass -File scripts/start-service-workers.ps1`;
- frontend tests: `21` test files, `104 passed`;
- frontend build: passed;
- `git diff --check`: passed.

## Focus Points

- Default frontend binding is loopback-only; LAN exposure remains opt-in through existing non-default mechanisms.
- Make conditionals are parsed by make and are no longer shell recipe output.
- Help text and unrelated targets were left unchanged.
- The build emitted only the existing Vite chunk-size warning; it did not fail.

## Hosted Windows CI Follow-Up

- Fix base SHA: `afca8ad84d02bc9d14a5f52f01c3a0fcb65dbf8a`.
- RED: with `PATH=/nonexistent`, the focused run produced `1 passed, 4 failed`; all four make cases raised `FileNotFoundError`, while the frontend loopback test still passed.
- GREEN: both make test groups now use `pytest.mark.skipif(shutil.which("make") is None, reason="make executable is not available on PATH")`.
- No-make verification: `1 passed, 4 skipped`; both skip summaries showed the explicit PATH reason.
- Normal focused verification: `5 passed, 18 deselected`.
- POSIX `make -n dev`: `scripts/start-dev.sh`.
- POSIX `make -n workers`: `scripts/start-service-workers.sh`.
- Simulated Windows `make -n dev`: `powershell -ExecutionPolicy Bypass -File scripts/start-dev.ps1`.
- Simulated Windows `make -n workers`: `powershell -ExecutionPolicy Bypass -File scripts/start-service-workers.ps1`.
- `git diff --check`: passed.
- The skip applies only to make-dependent tests; the frontend loopback assertion always executes.
- Concurrent changes outside the allowed file set were left untouched and excluded from the fix commit.
