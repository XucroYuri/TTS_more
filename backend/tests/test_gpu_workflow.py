from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-gpu-validation.yml"
PLAYWRIGHT_SPEC = ROOT / "frontend" / "e2e" / "cuda-workstation.spec.ts"
PLAYWRIGHT_CONFIG = ROOT / "frontend" / "playwright.config.ts"
CUDA_ENTRYPOINT = ROOT / "scripts" / "run-cuda-validation.ps1"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required file: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


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
    main_marker = 'try {\n    $validatorArgs = @('
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
    assert "$deploy.RepoPaths = $RepoPaths" in single_deploy
    assert "$isDiagnostic = $SkipDeploy -or $SkipStart" in script
    assert script.count('$validatorArgs += "--diagnostic"') == 2
    assert 'Write-Host "CUDA diagnostic completed: $Output"' in script
    assert re.search(
        r'if \(\$isDiagnostic\) \{\s*Write-Host "CUDA diagnostic completed: \$Output"[^}]*\}\s*else \{\s*Write-Host "CUDA validation passed: \$Output"',
        script,
    )


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
