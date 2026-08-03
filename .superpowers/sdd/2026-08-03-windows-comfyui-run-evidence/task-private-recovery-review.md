# Private recovery deterministic-gate review

Date: 2026-08-03

Verdict: **NOT READY**

This report covers the deterministic gate for the private recovery namespace
amendment. It does not certify a real ComfyUI process, CUDA inference, any of
the three official model runtimes, supervised preflight, the 47-case matrix,
or WAV production. No live run was started.

## Source binding

- TTS More worktree: branch `dev-xu/windows-comfyui-run-evidence`, gate HEAD
  `f7278f69d9e8df42d3c26094b2866a45168c61e7` before the original Task 6
  documentation commit. Remediation fixture commit:
  `cacf8b28e6d776962bf5fd922caf09138c32ffc3`.
- TTS-Audio-Suite worktree: branch `dev-xu/windows-runner-integration`, current
  remediation HEAD `978a7786d76d32b5065751376aa6437d54be8042`, on top of
  `151e5661e1bbf21fae3e3d3b678808087369acbe`; prior baseline was
  `29891d2d35879c14dbd2cff5329f1671cfa25b77`. The worktree was clean before
  and after the read-only plugin gates.
- Official ComfyUI and GPT-SoVITS/IndexTTS/CosyVoice sources were not modified
  or invoked by Task 6. Their live checkout/model/runtime boundary remains an
  opt-in validation prerequisite.
- Task 6 changed no plugin, official ComfyUI, or engine source. The plugin
  failure below is outside the Task 6 documentation diff.

## Task 1-5 implementation and review binding

| Task | Implementation commits | Independent final verdict |
|---|---|---|
| 1, no-follow namespace | `3d89ada`, `95d231e` | READY in `task-1-rereview.md` |
| 2, bounded redacted snapshot | `c65dab9`, `eac0c16` | READY in `task-2-rereview.md` |
| 3, public evidence binding | `2d915d8`, `79c33ac` | READY in `task-3-rereview.md` |
| 4, supervisor/private integration | `6bcc169`, `4defc33`, `7ab72ae`, `814bf12`, `5a728f2` | READY in `task-4-final-rereview.md` |
| 5, explicit recovery | `46eb173`, `e9bb0e4`, `1ac4f12`, `a42f26c`, `fe55e5d`, `f7278f6` | READY in `task-5-release-final.md`; one non-blocking test-hardening Minor retained |

The task reports and intermediate review rounds remain unchanged under
`.superpowers/sdd/2026-08-03-windows-private-recovery-namespace/`.

## Deterministic gates

All commands were run from the TTS More worktree unless another working
directory is named.

### Required private suites, two consecutive runs

```text
backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_private_recovery.py backend/tests/test_comfyui_reliability_recovery.py backend/tests/test_comfyui_reliability_evidence.py
run 1: exit 0; 146 passed, 1 skipped in 19.54s
run 2: exit 0; 146 passed, 1 skipped in 19.82s
```

The only skip is the existing POSIX-only no-follow/openat behavior on Windows.
Collection and result counts are identical.

### TTS-Audio-Suite unit gate, two consecutive runs

Working directory:
`F:\Code\Github\TTS_more\.worktrees\tts-audio-suite-run-evidence`.
The established isolated ComfyUI interpreter is Python 3.12.3.

```text
$env:COMFYUI_TESTING='1'; & 'F:\venvs\comfyui-tts\Scripts\python.exe' -m pytest tests/unit -q
run 1: exit 1; 336 collected; 1 failed, 333 passed, 2 skipped, 1 warning in 18.87s
run 2: exit 1; 336 collected; 1 failed, 333 passed, 2 skipped, 1 warning in 13.51s
```

Both runs fail only:

```text
tests/unit/test_all_node_registration.py::
test_plugin_loader_probe_keeps_the_parent_process_unchanged
tests/unit/test_all_node_registration.py:120
AssertionError: nodes.py did not register expected node IDs:
['MossClipStagingNode']
```

The loader's read-only diagnostic reports:

```text
MOSS Clip Staging failed: 'bool' object has no attribute 'view'
```

This is a deterministic plugin registration blocker at plugin commit
`29891d2d35879c14dbd2cff5329f1671cfa25b77`. It is not one of the historical seven TTS More Windows
host-capability limitations and is not caused by Task 6's documentation diff.
No plugin source was changed to hide or repair it.

An initial non-gate attempt used the TTS More Python 3.11 environment and
failed during `tests/conftest.py` import with `ModuleNotFoundError: requests`.
That interpreter is not the plugin's established Python 3.12 environment and
is recorded only to explain environment selection; it is not counted as either
formal plugin run.

Python 3.12 environment integrity:

```text
& 'F:\venvs\comfyui-tts\Scripts\python.exe' -m pip check
exit 0; No broken requirements found.
```

### TTS More focused reliability and supervisor gate

```text
backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_reliability_validation.py backend/tests/test_windows_reliability_supervisor.py
exit 1; 3 failed, 443 passed in 419.23s
```

The three failures are new deterministic compatibility regressions, not host
capability failures:

1. `test_task_12_wrapper_cleans_owned_empty_temp_after_launcher_identity_failure`
   at `test_comfyui_reliability_validation.py:5687`: expected the injected
   launcher identity failure, but stderr was `Supervised reliability contract
   is invalid` and the inner launcher returned 7.
2. `test_task_12_wrapper_preserves_exited_child_startup_logs_and_primary_error`
   at line 6019: expected `Owned process exited before acquiring port 8188`,
   but received only `Supervised reliability contract is invalid`.
3. `test_task_12_wrapper_preserves_primary_error_when_cleanup_cim_query_fails`
   at line 6346: the expected cleanup injection marker did not exist and
   `Path.read_text()` raised `FileNotFoundError`.

Focused reproduction with `-vv --tb=long` collected the same three tests and
returned `3 failed in 6.05s`. The common call path is:

```text
pytest fixture -> powershell.exe -> scripts/run-windows-comfyui-reliability.ps1
-> top-level supervised-contract validation -> exit 7
```

Those legacy fixtures prepare only `OutputRootIdentity` and
`RunRootIdentity`. The inner launcher now correctly also requires the
supervisor-derived `PrivateRecoveryRoot`, `PrivateRecoveryRootIdentity`, and
`PrivateRecoveryNamespaceIdentity`, so execution stops before each intended
launcher/cleanup injection. The compatible repair is to update these behavior
fixtures to prepare and pass the private boundary (or invoke the supervisor);
loosening the non-authoritative inner contract would weaken the approved
architecture.

### Other repository gates

```text
backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_release_governance.py
exit 0; 17 passed in 0.87s

backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_service_queue.py backend/tests/test_subprocess_safety.py
exit 0; 48 passed in 1.98s

pnpm --dir frontend test
exit 0; 26 files, 156 tests passed

pnpm --dir frontend build
exit 0; TypeScript and Vite production build completed

Windows PowerShell 5.1 AST parse command:

$parseFailed = $false; foreach ($script in @('scripts/run-windows-comfyui-reliability.ps1','scripts/run-windows-comfyui-reliability-supervised.ps1','scripts/recover-windows-comfyui-reliability-run.ps1')) { $tokens = $null; $errors = $null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $script), [ref]$tokens, [ref]$errors) | Out-Null; if ($errors.Count -ne 0) { $parseFailed = $true } }; if ($parseFailed) { exit 1 }

scripts/run-windows-comfyui-reliability.ps1: exit 0
scripts/run-windows-comfyui-reliability-supervised.ps1: exit 0
scripts/recover-windows-comfyui-reliability-run.ps1: exit 0

backend\.venv\Scripts\python.exe -m compileall -q backend/app/comfyui backend/tests/test_comfyui_private_recovery.py backend/tests/test_comfyui_reliability_recovery.py backend/tests/test_comfyui_reliability_evidence.py backend/tests/test_windows_reliability_supervisor.py
exit 0

git diff --check
exit 0 before Task 6 documentation edits
```

The full-backend command was started exactly as required:

```text
backend\.venv\Scripts\python.exe -m pytest -q backend
```

The controller's bounded wait exceeded the agreed approximately 12-minute
window and returned without a pytest result. That controller return did not
terminate the owned pytest process tree: process IDs 6060 and 42684 remained
running with the exact `backend\.venv\Scripts\python.exe -m pytest -q backend`
command/path/start-time identity. Consequently there is no authoritative
pytest exit code or collection/pass/fail count for this run.

At `2026-08-03 23:34:15 +08:00`, the controller cleanup explicitly ran
`Stop-Process -Id 42684,6060 -Force`. After approximately 500 ms,
`Get-CimInstance Win32_Process -Filter "ProcessId = 6060 OR ProcessId = 42684"`
returned no process, proving both owned orphan PIDs were gone. This cleanup is
not a pytest result and must not be described as normal test termination.
Therefore Task 6 cannot verify that the historical seven Windows
host-capability limitations have unchanged counts and causes. They are not
reclassified, waived, or claimed as current.

## Boundary/readiness conclusion

The private/public separation, redacted snapshot, recovery ownership proof,
and immutable public evidence suites are deterministically green. Static
governance, queue/process, frontend, parser, compile, and dependency checks are
also green. However, two release gates are not green:

- TTS-Audio-Suite has one repeatable all-node registration failure.
- TTS More has three repeatable inner-launcher fixture regressions, and the
  full backend gate did not complete within the bounded window.

Consequently the independent READY review must not be requested on this tree,
and no supervised preflight, CUDA smoke, three-engine synthesis, 47-case
matrix, PR, merge, or remote synchronization is authorized by this report.
The next implementation round must first behavior-fix the plugin registration
failure and the three TTS More private-boundary fixtures, then rerun every
failed/incomplete deterministic gate.

## Remediation round — 2026-08-04

The two deterministic blockers above were behavior-fixed and independently
reviewed as READY. The TTS More fixture repair is bound to commit
`cacf8b28e6d776962bf5fd922caf09138c32ffc3`. The plugin repair is bound to
`151e5661e1bbf21fae3e3d3b678808087369acbe` plus
`978a7786d76d32b5065751376aa6437d54be8042`. These are remediation bindings;
the earlier review reports remain unchanged.

Independent review artifacts for these remediation bindings are:

- TTS More fixture READY review:
  `.superpowers/sdd/2026-08-03-windows-comfyui-run-evidence/task-fixture-remediation-review.md`,
  covering `6c5ba28..cacf8b28e6d776962bf5fd922caf09138c32ffc3`.
- TTS-Audio-Suite plugin READY review:
  `F:\\Code\\Github\\TTS_more\\.worktrees\\tts-audio-suite-run-evidence\\docs\\reviews\\2026-08-04-mossclip-loader-review.md`,
  committed in review-docs SHA
  `e6da700fa64d744083a741f9a287696d13539b78`, covering
  `29891d2..978a7786d76d32b5065751376aa6437d54be8042`.

### Remediation deterministic gates

Required private/evidence/recovery suites, run twice from the TTS More
worktree:

```text
backend\\.venv\\Scripts\\python.exe -m pytest -q backend/tests/test_comfyui_private_recovery.py backend/tests/test_comfyui_reliability_recovery.py backend/tests/test_comfyui_reliability_evidence.py
run 1: exit 0; 146 passed, 1 skipped in 19.57s
run 2: exit 0; 146 passed, 1 skipped in 19.50s
```

The sole skip remains the existing POSIX-only openat/no-follow behavior on
Windows.

Plugin full unit suite, twice, in the established F Python 3.12.3
environment (`F:\\venvs\\comfyui-tts`), with `COMFYUI_TESTING=1`:

```text
pytest tests/unit -q
run 1: exit 0; 335 passed, 2 skipped, 1 warning in 16.07s
run 2: exit 0; 335 passed, 2 skipped, 1 warning in 15.92s
pip check: exit 0; No broken requirements found.
```

TTS More reliability and supervisor suites after the fixture repair:

```text
backend\\.venv\\Scripts\\python.exe -m pytest -q backend/tests/test_comfyui_reliability_validation.py backend/tests/test_windows_reliability_supervisor.py
exit 0; 446 passed in 418.15s (0:06:58)
```

### Full-backend bounded run and cleanup

The exact required command was launched under a process-tree-aware bounded
harness:

```text
backend\\.venv\\Scripts\\python.exe -m pytest -q backend
```

Owned root PID `7512` and child PID `42608` started at
`2026-08-04 00:10:20.561 +08:00`. Stdout was redirected to
`%TEMP%\\tts-more-remediation-full-backend-2.stdout.log` and stderr to the
matching `.stderr.log` (stderr stayed empty). The approximately 15-minute
deadline elapsed around `00:25:20-00:25:30 +08:00`; stdout reached 59% and
contained ten progress `F` markers, but no pytest summary, result, or
authoritative exit code. This run is therefore incomplete, not a claimed
ten-test failure result.

The first recursive cleanup attempt at `00:25:44 +08:00` used the read-only
PowerShell variable name `$pid` and consequently did not stop the tree. The
controller was corrected to use `$procId`, walk the owned descendants, and
force-stop the explicit owned IDs. At `00:26:18 +08:00`, an exact query for
root `7512`, child `42608`, and their recorded descendants returned no rows:
`OWNED_IDS_REMAINING=<none>`. The transient wrapper PID seen while querying
was the current command wrapper because its command text contained the pytest
arguments; it was not part of the owned test tree.

Targeted diagnostics explain the observed progress markers but do not turn the
incomplete full run into an authoritative result. The deploy/GPU/portable
control selection returned `7 failed, 374 passed, 4 skipped` in 37.27s; the
seven failures are the unchanged Windows host-capability class (missing
required shell, symlink privilege WinError 1314, missing `pwsh.exe`, and
`net share` access denied). No new product cause was found. Portable control
alone returned `108 passed`.

The portable first-run subset returned `22 passed, 2 failed, 2 deselected` in
5.21s. Both failures are deterministic missing-repository-workflow checks:
`test_portable_release_workflow_runs_harness_unit_tests_and_single_package_smoke`
and
`test_windows_ci_uses_short_pytest_base_temp_without_weakening_package_path_guard`
cannot find `.github/workflows/portable-release.yml`. The isolated real-package
test `test_harness_runs_real_packages_below_spaced_unicode_temp_root` passed
(`1 passed`), so the missing workflow remains the separate blocker. Because
the full run stopped before a summary, one progress-only marker cannot be
assigned to an authoritative test name.

### Remediation static gates

```text
test_release_governance.py: 17 passed in 1.65s
test_service_queue.py + test_subprocess_safety.py: 48 passed in 2.14s
frontend Vitest: 26 files, 156 tests passed
frontend production build: exit 0
PowerShell 5.1 parser: all three reliability scripts PARSE_OK, exit 0
compileall: exit 0
git diff --check: exit 0
```

### Latest readiness

The private/evidence/recovery, plugin, reliability/supervisor, governance,
queue/process, frontend, parser, compile, dependency, and diff gates are now
green for their recorded scopes. The required full-backend gate remains
incomplete, and the targeted portable release checks still expose the missing
`.github/workflows/portable-release.yml` blocker. Accordingly the latest
Task 6 verdict remains **NOT READY**. No supervised preflight, CUDA smoke,
three-engine synthesis, 47-case matrix, PR, merge, or remote synchronization
was run or authorized by this report.
