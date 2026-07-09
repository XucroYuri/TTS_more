# Task 3 Report: Faster Repository Sync With Partial Clone Fallback

## Summary
Implemented shallow partial clone support for repository sync, with a fallback to a plain shallow clone when the partial clone path fails. Existing network-profile behavior was left unchanged.

## RED Evidence
Command:
```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_sync_repos_dry_run_uses_shallow_partial_clone -q
```
Result:
`FAILED` because the clone command still started with `git clone --branch` instead of `git clone --depth 1 ... --filter=blob:none`.

Command:
```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_sync_repos_retries_clone_without_partial_filter -q
```
Result:
`FAILED` with `AttributeError` because `_run_git_command` did not exist yet.

## GREEN Evidence
Focused verification:
```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_sync_repos_dry_run_uses_shallow_partial_clone backend\tests\test_sync_repos_retries_clone_without_partial_filter backend\tests\test_sync_repos_rejects_paths_outside_project -q
```
Result:
`3 passed`

Full deploy-tool verification:
```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py -q
```
Result:
`16 passed`

## Files Changed
- `backend/tests/test_deploy_tool.py`
- `scripts/tts_more_deploy.py`

## Commit
- Short SHA: `pending`

## Self-Review
- Clone command now uses `--depth 1` and `--filter=blob:none` in the primary path.
- Fallback retry drops the partial filter only after a clone failure.
- Repository path safety checks still pass.
- No prepare scripts or docs were modified beyond the required task report.
