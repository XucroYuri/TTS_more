# Windows CUDA Validation Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Windows CUDA certification path safe, deterministic, readable, and evidence-complete while preserving honest blockers for private assets and human listening review.

**Architecture:** Keep the existing deployment and CUDA validation entrypoints, but separate local input checks from service/GPU checks and run them in cost order. Harden destructive repository operations, isolate ASR from TTS GPU residency, make CI evidence handling explicit, and make one single-node runbook the executable source of truth.

**Tech Stack:** Python 3.11, pytest, PowerShell 5.1+, Git, NVIDIA `nvidia-smi`, PyTorch CU128, FastAPI workers, pnpm, Playwright, GitHub Actions.

## Global Constraints

- Work only on `dev-xu/cuda-e2e-validation`; never rewrite upstream TTS repository history.
- The formal workers are `local-gpt-sovits-main`, `local-indextts`, and `local-cosyvoice` on one `cuda-0` resource group with capacity `1`.
- Every worker must report a CUDA device, CUDA runtime exactly `12.8`, and at least `16000` MiB total memory.
- Python `3.11`, `faster-whisper` model `large-v3`, real reference audio, real GPT/SoVITS weights, and human listening review remain mandatory.
- Never commit local paths, hostnames, IP addresses, fixture values, reviewer identities, reference audio, private weights, raw WAV evidence, or unsanitized logs.
- User-facing output starts with `通过`, `失败`, or `阻塞`, then names the stage, direct cause, shortest next action, and evidence path.
- Production behavior changes follow a witnessed RED → GREEN test cycle. Documentation-only edits require link, command, and governance checks.
- Each task is independently committed and pushed to both GitHub and Gitee; the two remote branch SHAs must match after every push.

---

### Task 1: Freeze the reproduced Windows clean-deployment repairs

**Files:**
- Modify: `scripts/prepare-tts-repos.ps1`
- Modify: `scripts/tts_more_deploy.py`
- Modify: `backend/tests/test_prepare_scripts.py`
- Modify: `backend/tests/test_deploy_tool.py`

**Interfaces:**
- Consumes: existing `prepare-tts-repos.ps1` provider preparation and `tts_more_deploy.py` update-script reports.
- Produces: repeatable CU128 environments and model preparation for the three formal services.

- [ ] **Step 1: Re-read the existing uncommitted diff and map every change to its witnessed deployment failure**

Confirm the diff contains only: Windows JSON array flattening, native stderr/exit-code handling, POSIX report paths, Windows execute-bit test portability, GPT torchcodec bootstrap, explicit CU128 PyTorch probes, verified IndexTTS auxiliary resources, and CosyVoice legacy Whisper/torch filtering.

- [ ] **Step 2: Run focused tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_prepare_scripts.py `
  backend\tests\test_deploy_tool.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run a PowerShell dry-run and real runtime probes**

Run:

```powershell
& powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prepare-tts-repos.ps1 `
  -Device CU128 -Targets default -RepoPaths deployment\app\repo-paths.local.json `
  -SkipInstall -SkipDownloads -DryRun

foreach ($repo in 'GPT-SoVITS-main','index-tts','CosyVoice') {
  & ".\repo\$repo\.venv\Scripts\python.exe" -c `
    "import torch; assert torch.version.cuda == '12.8'; assert torch.cuda.is_available(); print(torch.__version__)"
}
```

Expected: dry-run exits `0`; all three probes exit `0` and print a `+cu128` build.

- [ ] **Step 4: Run the full non-GPU regression**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest backend\tests -q --basetemp F:\Temp\TTS_more_cuda_e2e\pytest-task1
pnpm --dir frontend test -- --run
pnpm --dir frontend build
& .\.venv\Scripts\python.exe -m compileall -q backend scripts
git diff --check
```

Expected: zero failures; only the already documented Starlette deprecation and Vite chunk-size warnings may remain.

- [ ] **Step 5: Commit and publish hosted-Windows portability**

```powershell
git add backend/tests/test_deploy_tool.py scripts/tts_more_deploy.py
git commit -m "fix: normalize deployment helpers on Windows"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

- [ ] **Step 6: Commit and publish CU128 preparation**

```powershell
git add backend/tests/test_prepare_scripts.py scripts/prepare-tts-repos.ps1
git commit -m "fix: harden Windows CU128 repository preparation"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

- [ ] **Step 7: Open or update one draft GitHub PR for hosted CI evidence**

Create a draft PR from `dev-xu/cuda-e2e-validation` to `master` only after both commits are on GitHub. Do not merge it. Record Ubuntu and Windows CI links; the Windows job must prove the path-separator regression is fixed.

### Task 2: Limit clean synchronization to selected locked repositories

**Files:**
- Modify: `scripts/tts_more_deploy.py`
- Modify: `backend/tests/test_deploy_tool.py`
- Modify: `scripts/deploy-local-tts.ps1`
- Modify: `backend/tests/test_prepare_scripts.py`

**Interfaces:**
- Consumes: `sync_repos(..., clean=True, service_ids=..., repositories=...)`.
- Produces: `_remove_selected_repo_paths(root, repositories, service_ids, dry_run)` that removes only resolved selected repository paths and returns their project-relative labels.

- [ ] **Step 1: Write the failing clean-scope regression**

Add a test equivalent to:

```python
def test_clean_sync_preserves_unselected_repo_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deploy = _load_deploy_module(Path(__file__).resolve().parents[2])
    repositories = _repository_fixture_with_three_formal_services()
    selected = tmp_path / "repo" / "index-tts"
    unrelated = tmp_path / "repo" / "research-checkout"
    selected.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(deploy, "_run_clone_with_fallback", lambda *args, **kwargs: None)

    deploy.sync_repos(
        tmp_path,
        clean=True,
        service_ids={"local-indextts"},
        repositories=repositories,
    )

    assert not selected.exists()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep"
```

- [ ] **Step 2: Run the test and witness RED**

Run the exact new pytest node. Expected: fail because `_remove_repo_dir` removes `research-checkout`.

- [ ] **Step 3: Implement selected-path cleanup**

Resolve and validate every selected `repo["path"]` with `_resolve_project_path`, reject the project root or `repo/` itself, and call `_remove_path` only for those selected paths. Emit a concise line such as `clean repository: repo/index-tts`; never enumerate private file contents.

```python
def _remove_selected_repo_paths(
    root: Path,
    repositories: list[dict[str, Any]],
    service_ids: set[str] | None,
    *,
    dry_run: bool,
) -> list[str]:
    removed: list[str] = []
    root_resolved = root.resolve(strict=False)
    repo_root = (root / "repo").resolve(strict=False)
    for repo in repositories:
        if not _repo_selected(repo, service_ids):
            continue
        target = _resolve_project_path(root, str(repo["path"]))
        if target in {root_resolved, repo_root}:
            raise RuntimeError(f"refusing to clean repository root: {target}")
        label = target.relative_to(root_resolved).as_posix()
        print(f"clean repository: {label}")
        if target.exists() and not dry_run:
            _remove_path(target)
        removed.append(label)
    return removed
```

Call this after materializing `repositories` and before the synchronization loop; remove the whole-`repo/` call.

- [ ] **Step 4: Add command-preview safety**

Make the PowerShell one-click deployment print selected repository labels before forwarding `--clean`. The output must say that models and repo-local venvs inside those selected paths are removed.

- [ ] **Step 5: Verify RED → GREEN and full deploy-tool tests**

Run the new node, then all of `backend/tests/test_deploy_tool.py` and `backend/tests/test_prepare_scripts.py`. Expected: zero failures and the unrelated directory preserved.

- [ ] **Step 6: Commit and publish**

```powershell
git add scripts/tts_more_deploy.py scripts/deploy-local-tts.ps1 backend/tests/test_deploy_tool.py backend/tests/test_prepare_scripts.py
git commit -m "fix: scope clean deployment to selected repositories"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

### Task 3: Run local fixture input preflight before deployment or worker waits

**Files:**
- Modify: `backend/app/cuda_validation.py`
- Modify: `backend/tests/test_cuda_validation.py`
- Modify: `scripts/run-cuda-validation.ps1`
- Modify: `backend/tests/test_gpu_workflow.py`

**Interfaces:**
- Produces: `validate_fixture_inputs(fixture_path: Path, *, mode: str, require_baseline: bool) -> tuple[ValidationFixture | None, list[str]]`.
- Produces: `CUDAValidationRunner.run_input_preflight() -> dict[str, Any]`.
- Adds CLI flag: `--preflight-only`.
- Adds PowerShell parameter: `-RepoPaths`, forwarded to single-node deployment.
- Marks every `-SkipDeploy` or `-SkipStart` run as `certifiable: false` and `certification_status: diagnostic`.
- PowerShell calls the flag before `Invoke-SingleNodeDeployment`, `Invoke-DistributedDeployment`, or `Wait-ServiceReady`.

- [ ] **Step 1: Write failing Python tests**

Cover these behaviors:

```python
def test_input_preflight_rejects_missing_weight_files_without_network(...):
    fixture = _fixture_payload(...)
    fixture["gpt_weights"]["v2Pro"]["gpt"] = str(tmp_path / "missing.ckpt")
    report = runner.run_input_preflight()
    assert report["passed"] is False
    assert report["stage"] == "input-preflight"
    assert "weight v2Pro.gpt not found" in report["preflight"][0]["message"]
    assert network_calls == []
    assert monitor_starts == 0

def test_preflight_only_cli_returns_zero_for_valid_local_inputs(...):
    assert main([..., "--preflight-only"]) == 0
    assert json.loads((output / "summary.json").read_text())["passed"] is True
```

- [ ] **Step 2: Run both tests and witness RED**

Expected failures: missing `run_input_preflight`, missing CLI flag, and missing weight existence check.

- [ ] **Step 3: Implement fixture-only validation and shared report construction**

Move local fixture checks out of `_preflight`: schema expansion, unresolved variables, reference existence, all four weight-file existence checks, baseline requirement, and `faster_whisper` import availability. Do not construct clients, call worker URLs, start `nvidia-smi`, or load the ASR model in `run_input_preflight`.

Write `summary.json`, `junit.xml`, `human-listening-review.md`, `worker-log-references.json`, and the header-only `nvidia-smi.csv` on failure. Add stable fields:

```json
{
  "stage": "input-preflight",
  "passed": false,
  "blocker_count": 7,
  "next_action": "Fill the unresolved reference audio and GPT/SoVITS weight paths, then rerun the same command."
}
```

Use one shared helper so the fast and full paths cannot drift:

```python
def _append_required_file_failure(failures: list[str], kind: str, label: str, raw_path: str) -> None:
    if _contains_unresolved_environment(raw_path):
        failures.append(f"{kind} {label} contains an unresolved environment variable")
    elif not Path(raw_path).is_file():
        failures.append(f"{kind} {label} not found")


def validate_fixture_inputs(
    fixture_path: Path,
    *,
    mode: str,
    require_baseline: bool,
) -> tuple[ValidationFixture | None, list[str]]:
    try:
        fixture = load_fixture(fixture_path)
    except Exception as exc:
        return None, [f"fixture validation failed: {exc}"]
    failures: list[str] = []
    for label, raw_path in fixture.references.model_dump().items():
        _append_required_file_failure(failures, "reference", label, str(raw_path))
    for version, pair in fixture.gpt_weights.model_dump(by_alias=True).items():
        for kind, raw_path in pair.items():
            _append_required_file_failure(failures, "weight", f"{version}.{kind}", str(raw_path))
    if require_baseline and mode != "single-clean" and fixture.performance_baseline is None:
        failures.append("an approved performance baseline is required")
    if importlib.util.find_spec("faster_whisper") is None:
        failures.append("ASR gate requires faster-whisper with large-v3")
    required_reviewers = 2 if mode == "single-clean" else 1
    if len(fixture.reviewers) < required_reviewers:
        failures.append(f"{mode} requires {required_reviewers} listening reviewers")
    return fixture, failures
```

- [ ] **Step 4: Write and witness the PowerShell ordering test**

Assert the first `--preflight-only` invocation occurs before both deployment branches and before `Wait-ServiceReady`. Expected RED before editing `run-cuda-validation.ps1`.

- [ ] **Step 5: Reorder the PowerShell entrypoint**

Start the transcript, run input preflight, and stop immediately on failure with one human-readable line: `阻塞：input-preflight 有 N 个未解决项；证据：summary.json`. Continue to deployment and worker waiting only when the preflight exit code is `0`.

Forward `-RepoPaths` into `deploy-local-tts.ps1` so custom in-project repository paths can complete a formal run without `-SkipDeploy`. When either skip switch is used, append `--diagnostic` to the Python validator, keep core results available for debugging, set `certifiable` to `false`, and never print `CUDA validation passed`.

- [ ] **Step 6: Verify focused and full CUDA unit tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest backend\tests\test_cuda_validation.py backend\tests\test_gpu_workflow.py -q
```

Then run the real unresolved local fixture and confirm completion in under 30 seconds with no worker listener required.

- [ ] **Step 7: Commit and publish**

```powershell
git add backend/app/cuda_validation.py backend/tests/test_cuda_validation.py scripts/run-cuda-validation.ps1 backend/tests/test_gpu_workflow.py
git commit -m "fix: fail fast on incomplete CUDA fixtures"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

### Task 4: Validate the Windows CUDA host before destructive work

**Files:**
- Modify: `scripts/tts_more_deploy.py`
- Modify: `backend/tests/test_deploy_tool.py`
- Modify: `scripts/run-cuda-validation.ps1`
- Modify: `backend/tests/test_gpu_workflow.py`

**Interfaces:**
- Produces: `inspect_cuda_host(mode, *, command_runner, which, disk_usage, python_version) -> dict[str, Any]`.
- Adds CLI command `preflight-cuda-host`; `--mode` accepts `single-clean`, `single-release`, or `distributed`, and `--output` receives an output JSON path.
- Produces sanitized `environment-preflight.json` with checks, versions, free-space values, aggregate GPU memory, and no absolute paths or process names.

- [ ] **Step 1: Write parameterized RED tests**

Use injected command, executable, disk, and version providers. Cover Python other than `3.11`, missing `conda`, missing `git/node/pnpm/nvidia-smi`, GPU total below `16000` MiB, initial GPU use above `1024` MiB, clean repo free space below `40` GiB, clean temp free space below `10` GiB, release repo free space below `15` GiB, missing CUDA float16 in CTranslate2, and missing Playwright Chromium.

```python
@pytest.mark.parametrize(
    ("check", "message"),
    [
        ("python", "Python 3.11 is required"),
        ("conda", "conda is required for GPT-SoVITS on Windows"),
        ("gpu_total", "GPU memory must be at least 16000 MiB"),
        ("gpu_idle", "GPU must use no more than 1024 MiB before certification"),
    ],
)
def test_cuda_host_preflight_reports_actionable_blockers(check: str, message: str, fake_host) -> None:
    fake_host.fail(check)
    report = inspect_cuda_host("single-clean", **fake_host.providers())
    assert report["passed"] is False
    assert any(message in item["message"] for item in report["checks"] if not item["passed"])
```

- [ ] **Step 2: Witness every RED category**

Run the parameterized node and confirm each row fails because `inspect_cuda_host` or the required check is absent.

- [ ] **Step 3: Implement deterministic host inspection**

Use `sys.version_info`, `shutil.which`, `shutil.disk_usage`, and `nvidia-smi --query-gpu=memory.total,memory.used,driver_version`. Treat shared repo/temp volumes once, using the stricter threshold. Probe `ctranslate2.get_supported_compute_types("cuda")` for `float16`. Ask Node for `chromium.executablePath()` and verify the path exists.

```python
HOST_LIMITS_GIB = {
    "single-clean": {"repo": 40.0, "temp": 10.0},
    "single-release": {"repo": 15.0, "temp": 5.0},
    "distributed": {"repo": 15.0, "temp": 5.0},
}
MIN_GPU_TOTAL_MIB = 16000
MAX_INITIAL_GPU_USED_MIB = 1024
```

- [ ] **Step 4: Add an actual ASR model smoke probe**

After cheap host checks pass, launch a short child process that constructs `WhisperModel("large-v3", device="cuda", compute_type="float16")`, then deletes it and exits. Bound the probe with a documented timeout and capture only the exception type plus sanitized message. A missing model/cache/network or CUDA library is a blocker before deployment.

- [ ] **Step 5: Invoke host preflight before input preflight and deployment**

The PowerShell order becomes: start transcript → host preflight → fixture input preflight → deployment → worker readiness → core validation. On host failure, write `environment-preflight.json`, `summary.json`, JUnit, and one natural-language blocker line without deleting or resetting repositories.

- [ ] **Step 6: Verify tests and current host behavior**

Run deploy-tool and workflow tests. On the current machine, confirm the check honestly blocks while unrelated GPU usage exceeds the threshold, then rerun after only user-approved GPU cleanup; never terminate foreign GPU processes automatically.

- [ ] **Step 7: Commit and publish**

```powershell
git add scripts/tts_more_deploy.py backend/tests/test_deploy_tool.py scripts/run-cuda-validation.ps1 backend/tests/test_gpu_workflow.py
git commit -m "feat: add Windows CUDA host preflight"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

### Task 5: Protect GPU residency, monitoring, and process ownership

**Files:**
- Modify: `backend/app/cuda_validation.py`
- Modify: `backend/tests/test_cuda_validation.py`
- Modify: `scripts/run-cuda-validation.ps1`
- Modify: `backend/tests/test_gpu_workflow.py`

**Interfaces:**
- Produces: deferred ASR queue entries recorded after each WAV is written.
- Produces: `FasterWhisperTranscriber.close()` that releases the model and triggers CUDA cache cleanup when available.
- Produces: worker-listener ownership validation before stopping PIDs on ports 9880–9882.

- [ ] **Step 1: Write a failing ASR ordering test**

Use an event list and assert the required order:

```python
assert events == ["load", "synthesize", "unload", "memory-recovered", "asr"]
```

The current implementation must fail because it transcribes before `unload`.

- [ ] **Step 2: Move ASR after unload recovery**

Record the WAV and synthesis status first, unload the TTS model, wait for memory recovery, and only then call the transcriber. Preserve per-case CER reporting and continue collecting all cases after a failure.

```python
asr_queue: list[tuple[dict[str, Any], Path, str, str]] = []
for case in validation_cases(fixture):
    case_report = self._run_case(case, endpoint, client, perf_source)
    report["cases"].append(case_report)
    if case_report.get("audio", {}).get("passed"):
        asr_queue.append((case_report, self.output_dir / case_report["output_path"], case.text, case.language))

for case_report, wav_path, reference, language in asr_queue:
    hypothesis = self.transcriber(wav_path, language)
    cer_items.append((case_report["name"], reference, hypothesis))
```

The ASR loop runs only after every `_run_case` has completed its `finally` unload path.

- [ ] **Step 3: Add ASR release coverage**

Test that `close()` drops the cached Whisper model after each transcription batch and that cleanup runs in a `finally` block. The production implementation must not retain the ASR model across provider loads on a single 16 GB GPU.

```python
def close(self) -> None:
    self._model = None
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
```

- [ ] **Step 4: Make GPU monitoring a required gate**

Write a failing test proving missing `nvidia-smi` or an immediately exited monitor adds a preflight failure instead of silently creating an empty CSV. Add a stable error: `GPU monitor is unavailable; nvidia-smi evidence is required for certification`.

- [ ] **Step 5: Add worker listener ownership checks**

Before stopping a configured port, inspect the PID executable and command line. Stop only a TTS More worker whose command contains the current project root and formal service module. If another process owns the port, fail with `阻塞：端口 988X 被非本次验证进程占用` and leave it running.

- [ ] **Step 6: Verify CUDA tests and local worker health**

Run all CUDA validation tests, then start the three current workers and verify `/health`, `/capabilities`, and `/status` without terminating unrelated GPU processes.

- [ ] **Step 7: Commit and publish**

```powershell
git add backend/app/cuda_validation.py backend/tests/test_cuda_validation.py scripts/run-cuda-validation.ps1 backend/tests/test_gpu_workflow.py
git commit -m "fix: isolate CUDA validation resources and processes"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

### Task 6: Correct fixture, warm-performance, listening, and certification semantics

**Files:**
- Modify: `backend/app/cuda_validation.py`
- Modify: `backend/tests/test_cuda_validation.py`
- Modify: `frontend/e2e/cuda-workstation.spec.ts`
- Create: `frontend/e2e/cuda-fixture.ts`
- Create: `frontend/e2e/cuda-fixture.test.ts`
- Modify: `frontend/package.json`
- Modify: `backend/tests/test_gpu_workflow.py`

**Interfaces:**
- Produces: `expandFixtureEnvironment(value, env)` for Playwright fixture values.
- Adds `WARM_SYNTHESIS_REPEATS = 2`; warm p95 uses only repeats while the model remains loaded.
- Adds safe `fixture_sha256`, `certifiable`, and `certification_status` fields to `summary.json`.
- Produces one nine-column listening row per `(reviewer, synthesis case)` and one signature block per reviewer.

- [ ] **Step 1: Write and witness the Playwright fixture RED test**

Use a fixture containing `${TTS_MORE_VALIDATION_GPT_REF}` and assert the helper returns the environment value. Also assert an unresolved `${...}` throws `CUDA fixture has unresolved environment variables` before any API project reset or submission.

- [ ] **Step 2: Implement recursive environment expansion**

Support `${NAME}` and `%NAME%` in strings nested in objects and arrays. Do not print resolved private values. Make `cuda-workstation.spec.ts` call the helper immediately after JSON parsing.

```typescript
export function expandFixtureEnvironment(value: unknown, env: NodeJS.ProcessEnv): unknown {
  if (Array.isArray(value)) return value.map((item) => expandFixtureEnvironment(item, env));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, expandFixtureEnvironment(item, env)]),
    );
  }
  if (typeof value !== "string") return value;
  const expanded = value
    .replace(/\$\{([^}]+)\}/g, (_match, name: string) => env[name] ?? _match)
    .replace(/%([^%]+)%/g, (_match, name: string) => env[name] ?? _match);
  if (/\$\{[^}]+\}|%[^%]+%/.test(expanded)) {
    throw new Error("CUDA fixture has unresolved environment variables");
  }
  return expanded;
}
```

- [ ] **Step 3: Write and witness warm-sample RED tests**

Use a fake client and event clock. Assert one load, one primary synthesis, two warm synthesis repetitions, one unload, and that `warm_p95_seconds` excludes the primary synthesis duration.

- [ ] **Step 4: Implement true warm repetitions**

Keep the loaded profile resident for two additional synthesis calls, measure each repetition, and store `warm_synthesis_seconds` per case. ASR evaluates only the primary WAV after Task 4 has unloaded the TTS model.

- [ ] **Step 5: Write and witness listening-template RED tests**

For `single-clean`, two reviewers and six case outputs must produce twelve data rows with exactly nine columns plus two independent signature blocks. A single reviewer must add an input-preflight blocker. For `single-release`, at least one reviewer remains required.

- [ ] **Step 6: Implement reviewer and report identity rules**

Require two reviewers for first certification, build rows per reviewer, and keep the Markdown template in the controlled raw run directory. Add fixture file SHA-256, never the fixture absolute path or expanded asset paths.

- [ ] **Step 7: Add explicit certification status**

Use stable values: `blocked`, `core_failed`, `diagnostic_core_passed`, `core_passed_ui_pending`, and `automatic_passed_human_pending`. The core validator may set `passed: true` for its own gate, but it must not set `certification_status: certified`.

- [ ] **Step 8: Verify Python and TypeScript suites**

Run all CUDA Python tests, the new Vitest fixture test, the normal frontend unit suite, and the Playwright test in skipped mode. Expected: unresolved variables fail before API calls; all other tests pass.

- [ ] **Step 9: Commit and publish**

```powershell
git add backend/app/cuda_validation.py backend/tests/test_cuda_validation.py frontend/e2e frontend/package.json backend/tests/test_gpu_workflow.py
git commit -m "fix: make CUDA certification evidence semantically complete"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

### Task 7: Rewrite the Windows CUDA documentation around one executable path

**Files:**
- Modify: `docs/cuda-e2e-single-node.md`
- Modify: `docs/cuda-windows-codex-handoff-prompt.md`
- Modify: `docs/cuda-e2e-validation.md`
- Modify: `docs/cuda-e2e-acceptance-record.md`
- Modify: `docs/deployment.md`
- Modify: `deployment/app/README.md`
- Modify: `deployment/tts-repos/gpt-sovits/README.md`
- Modify: `deployment/tts-repos/indextts/README.md`
- Modify: `deployment/tts-repos/cosyvoice/README.md`
- Modify: `README.md`
- Modify: `backend/tests/test_prepare_scripts.py`
- Modify: `backend/tests/test_release_governance.py`

**Interfaces:**
- `cuda-e2e-single-node.md` becomes the only copy-paste Windows single-node runbook.
- The handoff prompt contains boundaries and reporting rules, then links to the runbook.
- The cross-topology document contains contracts and evidence semantics, not duplicate commands.

- [ ] **Step 1: Write failing governance assertions**

Assert all CUDA documents use root `repo.lock.json`, mention conda and Python 3.11, provide the separate local Playwright command, list `/capabilities`, use four final states, and contain no PowerShell pseudo-syntax such as `single-clean|single-release|distributed` or angle-bracket run-ID tokens inside executable code blocks.

- [ ] **Step 2: Run governance tests and witness RED**

Expected: failures for the wrong lock path, missing local Playwright command, missing conda, and two-state acceptance template.

- [ ] **Step 3: Rewrite the single-node runbook**

Use this order: environment → ignored local configuration → input preflight → one of two mutually exclusive paths (default total entrypoint or custom RepoPaths manual deploy + explicit worker start + `-SkipDeploy`) → worker/application endpoints → core validator → Playwright → evidence → listening review → baseline → release rerun.

Include one Mermaid flowchart and legal copy-paste PowerShell commands. State selected-path cleanup scope next to every clean command.

- [ ] **Step 4: Shorten the Agent handoff prompt**

Keep authorization boundaries, private-data rules, stop conditions, required evidence, four final states, and concise command routing. Remove duplicated runbook prose and incorrect `deployment/app/repo.lock.json` references.

- [ ] **Step 5: Align the remaining references**

Add exact `/capabilities` and application health/status commands; document unique `TTS_MORE_CUDA_E2E_PROJECT_ID`; distinguish raw controlled evidence from sanitized GitHub/PR evidence; remove claims of an HTML Playwright report unless the reporter is added.

- [ ] **Step 6: Verify links, commands, and governance tests**

Run the two governance test files, `git diff --check`, a Markdown relative-link checker, and PowerShell parser validation for every fenced PowerShell block marked copy-paste.

- [ ] **Step 7: Commit and publish**

```powershell
git add README.md docs deployment backend/tests/test_prepare_scripts.py backend/tests/test_release_governance.py
git commit -m "docs: make Windows CUDA certification executable"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

### Task 8: Make GitHub Actions evidence-safe and status-complete

**Files:**
- Modify: `.github/workflows/windows-gpu-validation.yml`
- Modify: `backend/tests/test_gpu_workflow.py`
- Modify: `docs/cuda-e2e-validation.md`

**Interfaces:**
- The workflow consumes the same preflight and validation entrypoint as local runs.
- The workflow publishes only an explicitly sanitized evidence bundle; raw WAV, trace, fixture, and logs remain in runner-local controlled storage unless a protected upload is explicitly approved.
- Single-node and distributed jobs consume separate protected topology and fixture variables.
- Shared GPU hardware uses one fixed concurrency group and an `always()` cleanup step driven by a run-local PID manifest.

- [ ] **Step 1: Write failing workflow tests**

Assert both single and distributed jobs run input preflight before deployment, always upload `summary.json` and JUnit on failure, create unique Playwright project IDs, use separate topology/fixture variables, fail when the sanitized manifest is missing, use protected environments, and do not upload raw `wav/**`, worker logs, GPU UUIDs, reviewer identities, or trace media in the default artifact.

- [ ] **Step 2: Run workflow tests and witness RED**

Expected: current artifact paths include raw WAV/log data and no unique Playwright project ID.

- [ ] **Step 3: Split controlled and sanitized evidence**

Create a PowerShell sanitization step that copies only summary/JUnit, GPU CSV, hash-only worker references, and a reviewer-state template with identities removed into `artifacts/*-sanitized`. Keep raw run directories runner-local and print their controlled location without uploading contents.

- [ ] **Step 4: Separate topology, fixture, and hardware ownership**

Use `TTS_MORE_SINGLE_TOPOLOGY`, `TTS_MORE_SINGLE_FIXTURE`, `TTS_MORE_DISTRIBUTED_TOPOLOGY`, and `TTS_MORE_DISTRIBUTED_FIXTURE`. Bind both jobs to a protected `cuda-validation` environment, use one cluster-wide concurrency key, and add an `always()` cleanup that stops only PIDs listed in the current run's worker manifest.

```yaml
concurrency:
  group: windows-gpu-validation-tts-more-cluster
  cancel-in-progress: false

jobs:
  single-validation:
    environment: cuda-validation
    env:
      TTS_MORE_CUDA_TOPOLOGY: ${{ vars.TTS_MORE_SINGLE_TOPOLOGY }}
      TTS_MORE_CUDA_FIXTURE: ${{ vars.TTS_MORE_SINGLE_FIXTURE }}
  distributed-validation:
    environment: cuda-validation
    env:
      TTS_MORE_CUDA_TOPOLOGY: ${{ vars.TTS_MORE_DISTRIBUTED_TOPOLOGY }}
      TTS_MORE_CUDA_FIXTURE: ${{ vars.TTS_MORE_DISTRIBUTED_FIXTURE }}
```

- [ ] **Step 5: Make final state explicit**

After core and Playwright steps, write `automatic-gate.json` with `通过`, `失败`, or `阻塞`. Do not write overall `通过` while human review is pending; stable-release gate requires the controlled acceptance record outside the automatic job.

- [ ] **Step 6: Move stable release control before publication**

Treat `release.published` as post-release audit only. Document and implement a manually approved candidate-SHA workflow path; stable publication happens only after the protected acceptance record references successful single, distributed, Playwright, and human evidence.

- [ ] **Step 7: Validate workflow syntax and tests**

Run `backend/tests/test_gpu_workflow.py`, parse the YAML with the repository's available YAML loader, run `actionlint` when installed, and inspect the rendered expressions for release and workflow-dispatch modes.

- [ ] **Step 8: Commit and publish**

```powershell
git add .github/workflows/windows-gpu-validation.yml backend/tests/test_gpu_workflow.py docs/cuda-e2e-validation.md
git commit -m "ci: publish sanitized CUDA validation evidence"
git push github HEAD:dev-xu/cuda-e2e-validation
git push origin HEAD:dev-xu/cuda-e2e-validation
```

### Task 9: Whole-branch verification and independent review

**Files:**
- Modify only if review findings require a tested correction.

**Interfaces:**
- Consumes: all prior task commits and the design requirements.
- Produces: a reviewed branch with synchronized GitHub/Gitee SHAs and an honest blocker list.

- [ ] **Step 1: Run full verification from a clean application test state**

Run backend tests, compileall, frontend tests/build, deployment doctor, three CU128 runtime probes, worker endpoint checks, unresolved-fixture fast preflight, YAML parsing, Markdown links, and `git diff --check`.

- [ ] **Step 2: Request independent whole-branch code review**

Give the reviewer the merge-base-to-HEAD review package, the design, this plan, and the exact full-suite evidence. Fix every Critical or Important finding with a RED → GREEN test and request re-review.

- [ ] **Step 3: Re-run the full verification after review fixes**

No success claim or final push is allowed without this fresh run.

- [ ] **Step 4: Publish final branch state**

Push GitHub, push Gitee, and verify local/GitHub/Gitee full SHAs are identical. Read GitHub Actions status for the branch; distinguish hosted/static checks from self-hosted GPU jobs.

- [ ] **Step 5: Record only irreducible wake-up items**

The final wake-up list may contain only: three real reference audio files, four private GPT/SoVITS weight files, any missing protected runner variables, confirmation to stop unrelated GPU workloads before baseline measurement, two real listening reviewers, and approval of the first complete warm p95 baseline.

