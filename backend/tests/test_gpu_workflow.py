from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-gpu-validation.yml"
PLAYWRIGHT_SPEC = ROOT / "frontend" / "e2e" / "cuda-workstation.spec.ts"
PLAYWRIGHT_FIXTURE_HELPER = ROOT / "frontend" / "e2e" / "cuda-fixture.ts"
FRONTEND_PACKAGE = ROOT / "frontend" / "package.json"
PLAYWRIGHT_CONFIG = ROOT / "frontend" / "playwright.config.ts"
CUDA_ENTRYPOINT = ROOT / "scripts" / "run-cuda-validation.ps1"
CUDA_VALIDATOR = ROOT / "backend" / "app" / "cuda_validation.py"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required file: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _powershell_function(source: str, name: str) -> str:
    marker = f"function {name} {{"
    start = source.index(marker)
    match = re.search(r"(?m)^function\s+[A-Za-z0-9-]+\s*\{", source[start + len(marker) :])
    end = len(source) if match is None else start + len(marker) + match.start()
    return source[start:end].rstrip()


def test_windows_gpu_workflow_declares_triggers_modes_and_runner_contract() -> None:
    workflow = _read(WORKFLOW)

    assert re.search(r"^name:\s*Windows GPU validation\s*$", workflow, re.MULTILINE)
    assert "workflow_dispatch:" in workflow
    assert "release:" in workflow
    assert "prereleased" in workflow
    assert "published" in workflow
    assert "single-clean" in workflow
    assert "single-release" in workflow
    assert "distributed" in workflow
    assert "topology:" in workflow
    assert "fixture:" in workflow
    assert "require_baseline:" in workflow
    runner_lines = re.findall(r"^\s*runs-on:\s*(.+)$", workflow, re.MULTILINE)
    assert len(runner_lines) == 3
    assert set(runner_lines) == {"[self-hosted, Windows, X64, cuda, tts-more-gpu]"}
    assert "concurrency:" in workflow
    assert "timeout-minutes:" in workflow


def test_windows_gpu_workflow_runs_validation_ui_and_preserves_evidence() -> None:
    workflow = _read(WORKFLOW)

    assert "scripts/run-cuda-validation.ps1" in workflow
    assert "pnpm cuda:e2e" in workflow
    assert "TTS_MORE_RUN_CUDA_E2E" in workflow
    assert workflow.count("pip install faster-whisper") == 2
    assert "TTS_MORE_REQUIRE_BASELINE" in workflow
    assert workflow.count("$validationParameters = @{") == 2
    assert workflow.count("$validationParameters.RequireBaseline = $true") == 2
    assert '"-Mode",' not in workflow
    assert "github.event_name == 'release' || inputs.require_baseline" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert re.search(r"if:\s*\$\{\{\s*always\(\)\s*\}\}", workflow)
    for evidence in (
        "summary.json",
        "junit.xml",
        "wav",
        "logs",
        "nvidia-smi",
        "human-listening-review",
        "orchestration-preflight.json",
    ):
        assert evidence in workflow

    assert "stable-release-gate" in workflow
    assert re.search(r"needs:\s*\[single-validation, distributed-validation\]", workflow)
    assert "needs.single-validation.result" in workflow
    assert "needs.distributed-validation.result" in workflow
    assert not re.search(r"(?im)^\s*(?:&\s*)?(?:ssh|scp)\b", workflow), "remote commands belong in the validator entrypoint"


def test_cuda_entrypoint_runs_fixture_preflight_before_deploy_or_wait() -> None:
    script = _read(CUDA_ENTRYPOINT)
    main_marker = 'try {\n    $hostPreflightArgs = @('
    assert main_marker in script
    entrypoint = script[script.index(main_marker) :]

    preflight = entrypoint.index('"--preflight-only"')
    single_deploy = entrypoint.index("Invoke-SingleNodeDeployment")
    distributed_deploy = entrypoint.index("Invoke-DistributedDeployment")
    worker_wait = entrypoint.index("Wait-ServiceReady $ServicesPath")

    assert preflight < single_deploy
    assert preflight < distributed_deploy
    assert preflight < worker_wait
    assert "& $Python @validatorArgs | Out-Null" in entrypoint[:single_deploy]
    assert "阻塞：input-preflight 有 $blockerCount 个未解决项；证据：summary.json" in entrypoint
    assert "$script:DistributedDeploymentStarted = $false" in script
    assert (
        'if ($Mode -eq "distributed" -and $script:DistributedDeploymentStarted -and '
        '-not $script:DistributedEvidenceCollected)'
    ) in entrypoint


def test_cuda_entrypoint_forwards_repo_paths_and_marks_skips_diagnostic() -> None:
    script = _read(CUDA_ENTRYPOINT)
    single_deploy = script[
        script.index("function Invoke-SingleNodeDeployment") : script.index("function Invoke-DistributedDeployment")
    ]

    assert '[string]$RepoPaths = ""' in script
    assert 'if ($RepoPaths -and !(Test-Path -LiteralPath $RepoPathsPath))' in single_deploy
    assert "$deploy.RepoPaths = $RepoPaths" in single_deploy
    assert 'if ($RepoPaths) { $start.RepoPaths = $RepoPaths }' in single_deploy
    assert single_deploy.index("$deploy.RepoPaths = $RepoPaths") < single_deploy.index(
        'Invoke-LocalScript (Join-Path $Root "scripts\\deploy-local-tts.ps1") $deploy'
    )
    assert single_deploy.index("$start.RepoPaths = $RepoPaths") < single_deploy.index(
        'Invoke-LocalScript (Join-Path $Root "scripts\\start-service-workers.ps1") $start'
    )
    assert "$isDiagnostic = $SkipDeploy -or $SkipStart" in script
    assert script.count('$validatorArgs += "--diagnostic"') == 2
    assert 'Write-Host "CUDA diagnostic core 通过（不可认证）：$Output"' in script
    assert "CUDA validation passed" not in script
    assert re.search(
        r'if \(\$isDiagnostic\) \{\s*Write-Host "CUDA diagnostic core 通过（不可认证）：\$Output"[^}]*\}\s*else \{\s*Write-Host "CUDA core 通过，Playwright/人工待完成：\$Output"',
        script,
    )


def test_worker_listener_cleanup_is_scoped_to_current_root_and_formal_module() -> None:
    script = _read(CUDA_ENTRYPOINT)
    predicate = _powershell_function(script, "Test-ConfiguredWorkerProcessOwnership")
    cleanup = _powershell_function(script, "Stop-ConfiguredWorkerListeners")

    assert "GetFullPath" in predicate
    assert "$ExecutablePath" in predicate
    assert "$CommandLine" in predicate
    assert "$WorkerModule" in predicate
    assert "Stop-Process" not in predicate
    assert "$commandOwned" not in predicate
    assert "Get-CimInstance Win32_Process" in cleanup
    assert "Test-ConfiguredWorkerProcessOwnership" in cleanup
    assert "阻塞：端口 $port 被非本次验证进程占用" in cleanup
    assert cleanup.index("Test-ConfiguredWorkerProcessOwnership") < cleanup.index("Stop-Process")
    assert cleanup.count("Get-CimInstance Win32_Process") == 2
    assert cleanup.count("Test-ConfiguredWorkerProcessOwnership") == 2
    assert "CreationDate" in cleanup
    assert "$revalidatedListeners = @()" in cleanup
    assert cleanup.index("$revalidatedListeners = @()") < cleanup.index("Stop-Process")
    assert "阻塞：端口 $($listener.Port) 的本次验证进程停止失败" in cleanup
    stop_block = cleanup[cleanup.index("Stop-Process") :]
    assert "catch {" in stop_block
    assert "Get-Process" not in cleanup
    for module in (
        "app.workers.gpt_sovits_worker:app",
        "app.workers.indextts_worker:app",
        "app.workers.cosyvoice_worker:app",
    ):
        assert module in cleanup


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell ownership predicate is Windows-only")
def test_worker_process_ownership_predicate_is_pure_and_root_scoped() -> None:
    executable = shutil.which("powershell.exe")
    assert executable is not None
    predicate = _powershell_function(
        _read(CUDA_ENTRYPOINT), "Test-ConfiguredWorkerProcessOwnership"
    )
    root = str(ROOT).replace("'", "''")
    command = predicate + f"""
$root = '{root}'
$inside = Join-Path $root '.venv\\Scripts\\python.exe'
$outside = 'C:\\external\\python.exe'
$sibling = $root + '-other\\.venv\\Scripts\\python.exe'
$module = 'app.workers.gpt_sovits_worker:app'
$evilModule = $module + '_evil'
@(
    (Test-ConfiguredWorkerProcessOwnership -CommandLine \"`\"$inside`\" -m uvicorn $module\" -ExecutablePath $inside -ProjectRoot $root -WorkerModule $module),
    (Test-ConfiguredWorkerProcessOwnership -CommandLine \"`\"$outside`\" -m uvicorn $module\" -ExecutablePath $outside -ProjectRoot $root -WorkerModule $module),
    (Test-ConfiguredWorkerProcessOwnership -CommandLine \"`\"$inside`\" -m uvicorn app.main:app\" -ExecutablePath $inside -ProjectRoot $root -WorkerModule $module),
    (Test-ConfiguredWorkerProcessOwnership -CommandLine \"`\"$sibling`\" -m uvicorn $module\" -ExecutablePath $sibling -ProjectRoot $root -WorkerModule $module),
    (Test-ConfiguredWorkerProcessOwnership -CommandLine \"`\"$outside`\" --note `\"$root\\fake $module`\"\" -ExecutablePath $outside -ProjectRoot $root -WorkerModule $module),
    (Test-ConfiguredWorkerProcessOwnership -CommandLine \"`\"$inside`\" -m uvicorn $evilModule\" -ExecutablePath $inside -ProjectRoot $root -WorkerModule $module),
    (Test-ConfiguredWorkerProcessOwnership -CommandLine \"`\"$inside`\" -m uvicorn `\"$module`\"\" -ExecutablePath $inside -ProjectRoot $root -WorkerModule $module)
) | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")

    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [True, False, False, False, False, False, True]


def test_cuda_entrypoint_rewrites_stale_preflight_pass_on_later_stage_failure() -> None:
    script = _read(CUDA_ENTRYPOINT)
    main_marker = 'try {\n    $hostPreflightArgs = @('
    entrypoint = script[script.index(main_marker) :]

    deployment_stage = entrypoint.index('$currentStage = "deployment"')
    deployment_call = min(
        entrypoint.index("Invoke-SingleNodeDeployment"),
        entrypoint.index("Invoke-DistributedDeployment"),
    )
    wait_stage = entrypoint.index('$currentStage = "worker-wait"')
    wait_call = entrypoint.index("Wait-ServiceReady $ServicesPath")

    assert deployment_stage < deployment_call
    assert wait_stage < wait_call
    assert '"--write-blocker-stage", $currentStage' in entrypoint
    assert '"--blocker-message", $failureMessage' in entrypoint
    assert (
        'if ($currentStage -in @("fault-recovery", "evidence-collection")) '
        '{ $blockerArgs += "--preserve-existing" }'
    ) in entrypoint
    assert entrypoint.count('"--preserve-existing"') == 1
    assert 'Write-Host "阻塞：$currentStage 失败；证据：summary.json"' in entrypoint
    catch_index = entrypoint.rindex("} catch {")
    finally_index = entrypoint.rindex("} finally {")
    assert catch_index < finally_index
    assert "--write-blocker-stage" not in entrypoint[finally_index:]


def test_cuda_entrypoint_runs_host_preflight_before_input_and_destructive_work() -> None:
    script = _read(CUDA_ENTRYPOINT)
    main_marker = 'try {\n    $hostPreflightArgs = @('
    assert main_marker in script
    entrypoint = script[script.index(main_marker) :]

    host_preflight = entrypoint.index('"preflight-cuda-host"')
    input_preflight = entrypoint.index('"--preflight-only"')
    single_deploy = entrypoint.index("Invoke-SingleNodeDeployment")
    distributed_deploy = entrypoint.index("Invoke-DistributedDeployment")
    worker_wait = entrypoint.index("Wait-ServiceReady $ServicesPath")

    assert host_preflight < input_preflight < single_deploy
    assert host_preflight < input_preflight < distributed_deploy
    assert host_preflight < input_preflight < worker_wait
    host_failure_gate = entrypoint[host_preflight:input_preflight]
    assert "$hostPreflightExitCode = $LASTEXITCODE" in host_failure_gate
    assert "if ($hostPreflightExitCode -ne 0)" in host_failure_gate
    assert "throw $hostPreflightNextAction" in host_failure_gate
    assert "Invoke-SingleNodeDeployment" not in host_failure_gate
    assert "Invoke-DistributedDeployment" not in host_failure_gate
    assert "CleanRepos" not in host_failure_gate
    assert "Stop-Process" not in host_failure_gate


def test_cuda_host_failure_uses_shared_blocker_writer_for_five_evidence_artifacts() -> None:
    entrypoint = _read(CUDA_ENTRYPOINT)
    validator = _read(CUDA_VALIDATOR)
    host_stage = entrypoint.index('$currentStage = "host-preflight"')
    host_command = entrypoint.index('"preflight-cuda-host"', host_stage)
    input_stage = entrypoint.index('$currentStage = "input-preflight"', host_command)
    catch_marker = '} catch {\n    $failureMessage = $_.Exception.Message'
    catch_block = entrypoint[entrypoint.index(catch_marker) : entrypoint.rindex("} finally {")]

    assert host_stage < host_command < input_stage
    assert '"--write-blocker-stage", $currentStage' in catch_block
    assert '"--blocker-message", $failureMessage' in catch_block
    assert "scripts\\run-cuda-validation.py" in catch_block
    assert '$LASTEXITCODE -notin @(0, 1)' in catch_block
    assert '$writeBlocker = $currentStage -eq "host-preflight"' in catch_block
    assert 'if (-not $writeBlocker) {' in catch_block
    assert (
        'Write-Host "阻塞：CUDA 主机预检未通过；证据：environment-preflight.json、summary.json"'
        in catch_block
    )
    for artifact in (
        "summary.json",
        "junit.xml",
        "human-listening-review.md",
        "worker-log-references.json",
        "nvidia-smi.csv",
    ):
        assert artifact in validator


def test_playwright_cuda_spec_is_a_real_three_service_closed_loop() -> None:
    config = _read(PLAYWRIGHT_CONFIG)
    spec = _read(PLAYWRIGHT_SPEC)

    assert "TTS_MORE_E2E_BASE_URL" in config
    assert "webServer" in config
    assert "TTS_MORE_RUN_CUDA_E2E" in spec
    assert "test.skip" in spec
    assert "/api/projects/" in spec
    assert "/api/services/status" in spec
    assert "/api/jobs/" in spec
    assert "/api/audio" in spec
    assert "getByRole" in spec
    assert "Generate filtered lines" in spec

    for service_id in ("local-gpt-sovits-main", "local-indextts", "local-cosyvoice"):
        assert service_id in spec
    assert "MIXED_QUEUE_SIZE = 30" in spec
    assert "toHaveLength(MIXED_QUEUE_SIZE)" in spec
    assert "maxSimultaneouslyLoaded" in spec
    assert "TTS_MORE_CUDA_VALIDATION_MODE" in spec
    assert "toHaveLength(3)" in spec
    assert "content-type" in spec.lower()
    assert "audio/" in spec
    assert "byteLength" in spec
    assert "> 1024" in spec


def test_playwright_fixture_expansion_precedes_every_validation_api_call() -> None:
    spec = _read(PLAYWRIGHT_SPEC)
    helper = _read(PLAYWRIGHT_FIXTURE_HELPER)
    package = json.loads(_read(FRONTEND_PACKAGE))
    test_body = spec[spec.index('test("imports a CUDA validation project') :]
    load_start = spec.index("function loadFixture")
    load_end = spec.index("function resolveFixturePath", load_start)
    load_fixture = spec[load_start:load_end]

    assert 'import { expandFixtureEnvironment } from "./cuda-fixture"' in spec
    assert test_body.index("const fixture = loadFixture()") < test_body.index(
        "resetValidationProject(request, project)"
    )
    assert load_fixture.index("JSON.parse") < load_fixture.index(
        "expandFixtureEnvironment(rawFixture, process.env)"
    )
    assert "CUDA fixture has unresolved environment variables" in helper
    assert package["scripts"]["test:cuda-fixture"] == (
        "vitest run e2e/cuda-fixture.test.ts"
    )
    assert package["scripts"]["test"] == "vitest run --exclude=e2e/**"
