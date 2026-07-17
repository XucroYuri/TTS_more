from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-gpu-validation.yml"
MACOS_LAN_WORKFLOW = ROOT / ".github" / "workflows" / "macos-lan-gpu-validation.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PLAYWRIGHT_SPEC = ROOT / "frontend" / "e2e" / "cuda-workstation.spec.ts"
PLAYWRIGHT_FIXTURE_HELPER = ROOT / "frontend" / "e2e" / "cuda-fixture.ts"
FRONTEND_PACKAGE = ROOT / "frontend" / "package.json"
PLAYWRIGHT_CONFIG = ROOT / "frontend" / "playwright.config.ts"
CUDA_ENTRYPOINT = ROOT / "scripts" / "run-cuda-validation.ps1"
CUDA_VALIDATOR = ROOT / "backend" / "app" / "cuda_validation.py"
CUDA_SANITIZER = ROOT / "scripts" / "sanitize-cuda-evidence.py"
CUDA_CLEANUP = ROOT / "scripts" / "cleanup-cuda-validation-processes.ps1"
CUDA_REGISTER = ROOT / "scripts" / "register-cuda-validation-process.ps1"
CUDA_DISTRIBUTED_CLEANUP = ROOT / "scripts" / "cleanup-distributed-cuda-validation-processes.ps1"
CUDA_GPU_MONITOR_START = ROOT / "scripts" / "start-cuda-gpu-monitor.ps1"
CUDA_GPU_MONITOR_STOP = ROOT / "scripts" / "stop-cuda-gpu-monitor.ps1"
LAN_ORCHESTRATOR = ROOT / "backend" / "app" / "lan_orchestration.py"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required file: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _workflow_payload() -> dict[str, object]:
    return yaml.load(_read(WORKFLOW), Loader=yaml.BaseLoader)


def _macos_lan_workflow_payload() -> dict[str, object]:
    return yaml.load(_read(MACOS_LAN_WORKFLOW), Loader=yaml.BaseLoader)


def _powershell_function(source: str, name: str) -> str:
    marker = f"function {name} {{"
    start = source.index(marker)
    match = re.search(r"(?m)^function\s+[A-Za-z0-9-]+\s*\{", source[start + len(marker) :])
    end = len(source) if match is None else start + len(marker) + match.start()
    return source[start:end].rstrip()


def test_windows_gpu_workflow_declares_triggers_modes_and_runner_contract() -> None:
    workflow = _workflow_payload()
    triggers = workflow["on"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    jobs = workflow["jobs"]

    assert workflow["name"] == "Windows GPU validation"
    assert set(inputs) == {"mode", "candidate_sha", "require_baseline"}
    assert inputs["candidate_sha"]["required"] == "true"
    assert set(inputs["mode"]["options"]) == {
        "single-clean",
        "single-release",
        "distributed",
    }
    assert triggers["release"]["types"] == ["published"]
    assert workflow["concurrency"] == {
        "group": "windows-gpu-validation-tts-more-cluster",
        "cancel-in-progress": "false",
    }
    assert jobs["single-validation"]["environment"] == "cuda-validation"
    assert jobs["distributed-validation"]["environment"] == "cuda-validation"
    candidate_gate = jobs["candidate-release-gate"]
    assert candidate_gate["environment"] == "cuda-release-approval"
    assert "needs.distributed-validation.result == 'success'" in candidate_gate["if"]
    assert "inputs.require_baseline == true" in candidate_gate["if"]
    assert "always()" not in candidate_gate["if"]
    assert jobs["published-audit"]["if"] == "${{ github.event_name == 'release' && github.event.action == 'published' }}"
    assert "release" not in jobs["single-validation"]["if"]
    assert "release" not in jobs["distributed-validation"]["if"]


def test_windows_gpu_workflow_uses_separate_protected_inputs_and_candidate_sha() -> None:
    workflow = _workflow_payload()
    jobs = workflow["jobs"]
    single = jobs["single-validation"]
    distributed = jobs["distributed-validation"]

    assert single["env"]["TTS_MORE_CUDA_TOPOLOGY"] == "${{ vars.TTS_MORE_SINGLE_TOPOLOGY }}"
    assert single["env"]["TTS_MORE_CUDA_FIXTURE"] == "${{ vars.TTS_MORE_SINGLE_FIXTURE }}"
    assert distributed["env"]["TTS_MORE_CUDA_TOPOLOGY"] == "${{ vars.TTS_MORE_DISTRIBUTED_TOPOLOGY }}"
    assert distributed["env"]["TTS_MORE_CUDA_FIXTURE"] == "${{ vars.TTS_MORE_DISTRIBUTED_FIXTURE }}"
    serialized = json.dumps(workflow, ensure_ascii=False)
    assert "vars.TTS_MORE_CUDA_TOPOLOGY" not in serialized
    assert "vars.TTS_MORE_CUDA_FIXTURE" not in serialized
    assert "inputs.topology" not in serialized
    assert "inputs.fixture" not in serialized
    assert serialized.count('"ref": "${{ inputs.candidate_sha }}"') == 2
    assert serialized.count('"clean": "false"') == 2

    for job_name in ("single-validation", "distributed-validation"):
        steps = jobs[job_name]["steps"]
        checkout_index = next(index for index, step in enumerate(steps) if step.get("uses") == "actions/checkout@v7")
        recover_index = next(index for index, step in enumerate(steps) if step.get("name") == "Recover prior owned processes")
        initialize_index = next(index for index, step in enumerate(steps) if step.get("name") == "Initialize controlled run directories")
        assert steps[checkout_index]["with"]["clean"] == "false"
        assert checkout_index < recover_index < initialize_index

    preflight = jobs["candidate-input-preflight"]
    assert preflight["runs-on"] == "ubuntu-latest"
    assert set(preflight["env"]) == {"REQUESTED_CANDIDATE_SHA"}
    assert "^[0-9a-fA-F]{40}$" in preflight["steps"][0]["run"]
    assert jobs["single-validation"]["needs"] == "candidate-input-preflight"
    assert jobs["distributed-validation"]["needs"] == "candidate-input-preflight"
    for job in jobs.values():
        for step in job.get("steps", []):
            if "run" in step:
                assert "${{ inputs." not in step["run"]
    for protected_name in (
        "TTS_MORE_API_TOKEN",
        "TTS_MORE_VALIDATION_SSH_USER",
        "TTS_MORE_VALIDATION_REMOTE_ROOT",
    ):
        assert protected_name not in workflow["env"]
        assert protected_name in single["env"]
        assert protected_name in distributed["env"]


def test_windows_gpu_workflow_uploads_only_fail_closed_sanitized_evidence() -> None:
    workflow = _workflow_payload()
    jobs = workflow["jobs"]
    workflow_text = _read(WORKFLOW)

    for job_name, raw_name in (
        ("single-validation", "cuda-single"),
        ("distributed-validation", "cuda-distributed"),
    ):
        steps = jobs[job_name]["steps"]
        finalizer_index = next(index for index, step in enumerate(steps) if step.get("id") == "finalize-evidence")
        upload_index = next(index for index, step in enumerate(steps) if step.get("uses") == "actions/upload-artifact@v7")
        cleanup_index = next(index for index, step in enumerate(steps) if step.get("id") == "cleanup-run-processes")
        finalizer = steps[finalizer_index]
        upload = steps[upload_index]
        cleanup = steps[cleanup_index]

        assert finalizer["if"] == "${{ always() }}"
        assert "scripts/sanitize-cuda-evidence.py" in finalizer["run"]
        assert "sanitizer-fallback-raw" in finalizer["run"]
        assert "Primary evidence sanitization failed" in finalizer["run"]
        assert finalizer["run"].index("Automatic evidence must not claim final certification") < finalizer["run"].index(
            "verified=true"
        )
        assert cleanup["if"] == "${{ always() }}"
        assert "scripts/cleanup-cuda-validation-processes.ps1" in cleanup["run"]
        if job_name == "distributed-validation":
            assert "scripts/cleanup-distributed-cuda-validation-processes.ps1" in cleanup["run"]
        assert finalizer_index < upload_index
        assert cleanup_index < upload_index
        assert upload["if"] == "${{ always() && steps.finalize-evidence.outputs.verified == 'true' }}"
        assert "verified=true" in finalizer["run"]
        assert upload["with"]["if-no-files-found"] == "error"
        assert upload["with"]["path"] == f"artifacts/{raw_name}-sanitized"

    upload_blocks = "\n".join(
        step["with"]["path"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if step.get("uses") == "actions/upload-artifact@v7"
    )
    for forbidden in ("wav", "logs", "worker-logs", "test-results", "trace", "video", "screenshot", "controller.log"):
        assert forbidden not in upload_blocks
    assert "stable-release-gate" not in workflow_text
    assert "automatic-gate.json" in workflow_text
    assert "manifest.json" in workflow_text


def test_github_workflows_use_node24_action_majors() -> None:
    combined = _read(WORKFLOW) + "\n" + _read(CI_WORKFLOW)
    for expected in (
        "actions/checkout@v7",
        "actions/setup-python@v6",
        "actions/setup-node@v6",
        "pnpm/action-setup@v6",
        "actions/upload-artifact@v7",
    ):
        assert expected in combined
    for deprecated in (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/setup-node@v4",
        "pnpm/action-setup@v4",
        "actions/upload-artifact@v4",
    ):
        assert deprecated not in combined


def test_windows_gpu_workflow_recovers_owned_processes_across_mode_switches() -> None:
    workflow = _workflow_payload()
    jobs = workflow["jobs"]

    for job_name in ("single-validation", "distributed-validation"):
        recover = next(
            step for step in jobs[job_name]["steps"] if step.get("name") == "Recover prior owned processes"
        )["run"]
        assert "artifacts\\cuda-single\\process-manifest.json" in recover
        assert "artifacts\\cuda-distributed\\process-manifest.json" in recover
        assert "cleanup-distributed-cuda-validation-processes.ps1" in recover
        assert "Distributed recovery settings must be either complete or empty" in recover

    assert jobs["single-validation"]["env"]["TTS_MORE_DISTRIBUTED_RECOVERY_TOPOLOGY"] == (
        "${{ vars.TTS_MORE_DISTRIBUTED_TOPOLOGY }}"
    )


def test_windows_gpu_workflow_uses_unique_playwright_projects_and_never_claims_overall_pass() -> None:
    workflow = _workflow_payload()
    jobs = workflow["jobs"]
    single_id = jobs["single-validation"]["env"]["TTS_MORE_CUDA_E2E_PROJECT_ID"]
    distributed_id = jobs["distributed-validation"]["env"]["TTS_MORE_CUDA_E2E_PROJECT_ID"]

    assert single_id != distributed_id
    for project_id, label in ((single_id, "single"), (distributed_id, "distributed")):
        assert label in project_id
        assert "github.run_id" in project_id
        assert "github.run_attempt" in project_id
    workflow_text = _read(WORKFLOW)
    assert "automatic_passed_human_pending" in workflow_text
    assert workflow_text.count('if ($gate.automatic_result -ne "通过")') == 2
    assert "overall_status" not in workflow_text or 'overall_status = "通过"' not in workflow_text
    assert not re.search(r"(?im)^\s*(?:&\s*)?(?:ssh|scp)\b", workflow_text), "remote commands belong in the validator entrypoint"


def test_cuda_process_cleanup_revalidates_run_local_manifest_ownership() -> None:
    cleanup = _read(CUDA_CLEANUP)

    assert "[Parameter(Mandatory = $true)][string]$Manifest" in cleanup
    assert "[switch]$Required" in cleanup
    assert "cleanup blocked: required process manifest is missing" in cleanup
    assert "Get-CimInstance Win32_Process" in cleanup
    assert "CreationDate" in cleanup
    assert "ExecutablePath" in cleanup
    assert "ProjectRoot" in cleanup
    assert "WorkerModule" in cleanup
    assert "Test-PathInsideRoot" in cleanup
    assert "Test-ExactCommandToken" in cleanup
    assert cleanup.index("Get-CimInstance Win32_Process") < cleanup.index("Stop-Process")
    assert "Get-NetTCPConnection" not in cleanup
    assert "Get-Process" not in cleanup
    assert "Win32_Process |" not in cleanup
    assert "Select-Object -ExpandProperty OwningProcess" not in cleanup
    assert "cleanup blocked: process ownership changed" in cleanup
    assert "cleanup blocked: unable to stop an owned validation process" in cleanup
    for module in (
        "app.main:app",
        "app.workers.gpt_sovits_worker:app",
        "app.workers.indextts_worker:app",
        "app.workers.cosyvoice_worker:app",
    ):
        assert module in cleanup


def test_cuda_process_registration_records_only_owned_current_checkout_process() -> None:
    register = _read(CUDA_REGISTER)

    assert "Get-CimInstance Win32_Process" in register
    assert "CreationDate" in register
    assert "ExecutablePath" in register
    assert "Test-PathInsideRoot" in register
    assert "Test-ExactCommandToken" in register
    assert "validation process registration blocked" in register
    assert "Get-NetTCPConnection" not in register
    assert "Stop-Process" not in register
    assert "OwningProcess" not in register
    assert '"frontend-vite"' in register
    assert "$CommandToken" in register
    assert "node.exe" in register


def test_distributed_worker_replacement_and_cleanup_use_remote_owned_manifests() -> None:
    entrypoint = _read(CUDA_ENTRYPOINT)
    deployment = _powershell_function(entrypoint, "Invoke-DistributedDeployment")
    recovery = _powershell_function(entrypoint, "Invoke-DistributedFaultRecovery")
    cleanup = _read(CUDA_DISTRIBUTED_CLEANUP)

    assert "cleanup-cuda-validation-processes.ps1" in deployment
    assert "-PidManifest" in deployment
    assert "cuda-validation-processes.json" in deployment
    assert "Get-NetTCPConnection" in deployment
    assert "Stop-Process" not in deployment
    assert "-ServiceId" in recovery
    assert "cleanup-cuda-validation-processes.ps1" in recovery
    assert "Select-Object -ExpandProperty OwningProcess" not in recovery
    kill_command = recovery[recovery.index("$killCommand =") : recovery.index("$stopwatch =")]
    assert "Stop-Process" not in kill_command
    assert "[Parameter(Mandatory = $true)][string]$Topology" in cleanup
    assert "EncodedCommand" in cleanup
    assert "cleanup-cuda-validation-processes.ps1" in cleanup
    assert "Stop-Process" not in cleanup

    control_plane = _powershell_function(entrypoint, "Start-ValidationControlPlane")
    assert "register-cuda-validation-process.ps1" in control_plane
    assert "TTS_MORE_CUDA_PID_MANIFEST" in control_plane
    assert "Get-NetTCPConnection" in control_plane
    assert control_plane.index("Start-Process") < control_plane.index("register-cuda-validation-process.ps1")


def test_workflow_registers_explicit_vite_process_in_owned_manifest() -> None:
    workflow = _workflow_payload()
    for job_name in ("single-validation", "distributed-validation"):
        playwright = next(
            step for step in workflow["jobs"][job_name]["steps"] if step.get("id") == "playwright"
        )["run"]
        assert "frontend\\node_modules\\vite\\bin\\vite.js" in playwright
        assert "frontend-vite" in playwright
        assert "-CommandToken $vitePath" in playwright
        assert "Start-Process" in playwright
        assert "register-cuda-validation-process.ps1" in playwright

    cleanup = _read(CUDA_CLEANUP)
    assert '"frontend-vite"' in cleanup
    assert "command_token" in cleanup
    assert "vite.js" in cleanup


def test_distributed_gpu_monitor_stop_revalidates_owned_process_identity() -> None:
    entrypoint = _read(CUDA_ENTRYPOINT)
    start = _read(CUDA_GPU_MONITOR_START)
    stop = _read(CUDA_GPU_MONITOR_STOP)
    distributed_cleanup = _read(CUDA_DISTRIBUTED_CLEANUP)

    assert "start-cuda-gpu-monitor.ps1" in entrypoint
    assert "stop-cuda-gpu-monitor.ps1" in entrypoint
    assert "nvidia-smi.pid" not in entrypoint
    assert "Get-CimInstance Win32_Process" in start
    assert "CreationDate" in start
    assert "ExecutablePath" in start
    assert "ConvertTo-Json" in start
    assert "Get-NetTCPConnection" not in start
    assert stop.count("Get-CimInstance Win32_Process") == 2
    assert "CreationDate" in stop
    assert "ExecutablePath" in stop
    assert "nvidia-smi.exe" in stop
    assert "Get-Command nvidia-smi.exe" in stop
    assert "CommandLine" in stop
    assert "Test-ExactCommandToken" in stop
    assert "--query-gpu=timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu" in stop
    assert "--loop-ms=2000" in stop
    assert stop.index("Get-CimInstance Win32_Process") < stop.index("Stop-Process")
    assert "Get-NetTCPConnection" not in stop
    assert "stop-cuda-gpu-monitor.ps1" in distributed_cleanup


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


def test_cuda_entrypoint_gives_manual_runs_a_run_local_process_manifest() -> None:
    script = _read(CUDA_ENTRYPOINT)

    assert "$previousCudaPidManifest = $env:TTS_MORE_CUDA_PID_MANIFEST" in script
    assert 'Join-Path $Output "process-manifest.json"' in script
    assert "$env:TTS_MORE_CUDA_PID_MANIFEST = $runProcessManifest" in script
    assert "$env:TTS_MORE_CUDA_PID_MANIFEST = $previousCudaPidManifest" in script


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


def test_playwright_distinguishes_both_lan_modes() -> None:
    spec = _read(PLAYWRIGHT_SPEC)

    assert '"distributed", "lan-distributed"' in spec
    assert '"single-clean", "single-release", "lan-shared"' in spec


def test_macos_lan_workflow_has_strict_manual_atomic_validation_contract() -> None:
    workflow = _macos_lan_workflow_payload()
    triggers = workflow["on"]
    dispatch = triggers["workflow_dispatch"]
    inputs = dispatch["inputs"]
    job = workflow["jobs"]["validate"]

    assert set(triggers) == {"workflow_dispatch"}
    assert set(dispatch) == {"inputs"}
    assert {
        name: {
            key: value
            for key, value in input_definition.items()
            if key in {"type", "required", "default", "options"}
        }
        for name, input_definition in inputs.items()
    } == {
        "mode": {
            "type": "choice",
            "required": "true",
            "options": ["lan-shared", "lan-distributed"],
        },
        "deployment": {
            "type": "choice",
            "required": "true",
            "options": ["clean", "release"],
        },
        "topology": {"type": "string", "required": "true"},
        "fixture": {"type": "string", "required": "true"},
        "ssh_config": {"type": "string", "required": "true"},
        "remote_root": {"type": "string", "required": "true"},
        "require_baseline": {
            "type": "boolean",
            "required": "true",
            "default": "false",
        },
    }
    assert workflow["concurrency"] == {
        "group": "macos-lan-gpu-validation-tts-more-cluster",
        "cancel-in-progress": "false",
    }
    assert job["runs-on"] == ["self-hosted", "macOS", "tts-more-lan-controller"]
    assert {
        key: job["env"][key]
        for key in (
            "VALIDATION_MODE",
            "DEPLOYMENT",
            "TOPOLOGY",
            "FIXTURE",
            "SSH_CONFIG",
            "REMOTE_ROOT",
            "REQUIRE_BASELINE",
        )
    } == {
        "VALIDATION_MODE": "${{ inputs.mode }}",
        "DEPLOYMENT": "${{ inputs.deployment }}",
        "TOPOLOGY": "${{ inputs.topology }}",
        "FIXTURE": "${{ inputs.fixture }}",
        "SSH_CONFIG": "${{ inputs.ssh_config }}",
        "REMOTE_ROOT": "${{ inputs.remote_root }}",
        "REQUIRE_BASELINE": "${{ inputs.require_baseline }}",
    }

    steps = job["steps"]
    validation_steps = [step for step in steps if step.get("id") == "validation"]
    assert len(validation_steps) == 1
    validation = validation_steps[0]
    validation_run = validation["run"]
    assert validation["shell"] == "bash"
    assert validation_run.count("scripts/run-lan-validation.sh") == 1
    for flag, variable in (
        ("--mode", "VALIDATION_MODE"),
        ("--deployment", "DEPLOYMENT"),
        ("--topology", "TOPOLOGY"),
        ("--fixture", "FIXTURE"),
        ("--ssh-config", "SSH_CONFIG"),
        ("--remote-root", "REMOTE_ROOT"),
        ("--output", "RAW_OUTPUT"),
    ):
        assert f'{flag} "${variable}"' in validation_run
    assert 'arguments+=(--require-baseline)' in validation_run
    assert 'scripts/run-lan-validation.sh "${arguments[@]}"' in validation_run
    executable_playwright = re.compile(
        r"(?m)^\s*pnpm\s+--dir\s+frontend\s+cuda:e2e(?:\s|$)"
    )
    assert not any(
        executable_playwright.search(step.get("run", "")) for step in steps
    )

    finalizer = next(step for step in steps if step.get("id") == "finalize-evidence")
    finalizer_run = finalizer["run"]
    assert finalizer["if"] == "${{ always() }}"
    assert finalizer_run.count("scripts/sanitize-cuda-evidence.py") == 2
    assert '--raw "$RAW_OUTPUT"' in finalizer_run
    assert '--output "$SANITIZED_OUTPUT"' in finalizer_run
    assert '--core-outcome "${{ steps.validation.outcome }}"' in finalizer_run
    assert '--playwright-outcome "${{ steps.validation.outcome }}"' in finalizer_run

    uploads = [
        step
        for step in steps
        if step.get("uses") == "actions/upload-artifact@v7"
    ]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["path"] == "${{ env.SANITIZED_OUTPUT }}"
    assert upload["with"]["retention-days"] == "30"
    assert "RAW_OUTPUT" not in upload["with"]["path"]

    orchestrator = _read(LAN_ORCHESTRATOR)
    orchestration_loop = orchestrator[orchestrator.index("class LanOrchestrator:") :]
    control_plane_index = orchestration_loop.index(
        "with control_plane(services_path, self.options.output):"
    )
    playwright_index = orchestration_loop.index(
        "run_workstation_e2e(", control_plane_index
    )
    recovery_index = orchestration_loop.index(
        "run_fault_recovery(", playwright_index
    )
    assert control_plane_index < playwright_index < recovery_index
    assert 'junit_path = options.output / "playwright-junit.xml"' in orchestrator
    assert '"PLAYWRIGHT_JUNIT_OUTPUT_FILE": str(junit_path)' in orchestrator
