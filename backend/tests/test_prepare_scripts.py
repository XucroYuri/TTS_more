from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_prepare_failure_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "fixture"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("prepare-tts-repos.sh", "prepare-tts-repos.ps1", "deploy-local-tts.sh", "deploy-local-tts.ps1"):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    repo = root / "repo" / "GPT-SoVITS-main"
    repo.mkdir(parents=True)
    prepare_marker = root / "prepare-ran"
    render_marker = root / "render-ran"
    (repo / "install.sh").write_text(
        '#!/usr/bin/env bash\nprintf prepare > "$TTS_MORE_TEST_PREPARE_MARKER"\n',
        encoding="utf-8",
    )
    (repo / "install.sh").chmod(0o755)
    (repo / "install.ps1").write_text(
        "Set-Content -LiteralPath $env:TTS_MORE_TEST_PREPARE_MARKER -Value prepare\n",
        encoding="utf-8",
    )
    deploy_stub = scripts / "tts_more_deploy.py"
    deploy_stub.write_text(
        """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
repo = {
    "name": "GPT-SoVITS-main",
    "provider_type": "gpt-sovits",
    "variant": "main",
    "service_id": "local-gpt-sovits-main",
    "default_selected": True,
    "absolute_path": os.environ["TTS_MORE_TEST_REPO"],
}
if "probe-network" in args:
    print(json.dumps({"model_source": "ModelScope", "cache_root": "data/cache", "env": {}}))
elif "list-repos" in args:
    print(json.dumps(repo if "--json-lines" in args else [repo]))
elif "render-services" in args:
    Path(os.environ["TTS_MORE_TEST_RENDER_MARKER"]).write_text("render", encoding="utf-8")
    print("[]")
elif "doctor" in args:
    print("{}")
else:
    print("[]")
""",
        encoding="utf-8",
    )
    repo_paths = root / "repo-paths.json"
    repo_paths.write_text(
        json.dumps({"repositories": {"local-gpt-sovits-main": str(repo)}}),
        encoding="utf-8",
    )
    return root, repo_paths, prepare_marker, render_marker


def _prepare_command(root: Path, repo_paths: Path, entrypoint: str, powershell: bool) -> list[str]:
    if powershell:
        if entrypoint == "prepare":
            return [
                str(root / "scripts" / "prepare-tts-repos.ps1"),
                "-Source",
                "ModelScope",
                "-Targets",
                "local-gpt-sovits-main",
                "-RepoPaths",
                str(repo_paths),
                "-SkipDownloads",
            ]
        return [
            str(root / "scripts" / "deploy-local-tts.ps1"),
            "-SkipAppInstall",
            "-SkipRepoSync",
            "-Source",
            "ModelScope",
            "-Targets",
            "local-gpt-sovits-main",
            "-RepoPaths",
            str(repo_paths),
            "-SkipDownloads",
        ]
    if entrypoint == "prepare":
        return [
            "bash",
            str(root / "scripts" / "prepare-tts-repos.sh"),
            "--source",
            "ModelScope",
            "--targets",
            "local-gpt-sovits-main",
            "--repo-paths",
            str(repo_paths),
            "--skip-downloads",
        ]
    return [
        "bash",
        str(root / "scripts" / "deploy-local-tts.sh"),
        "--skip-app-install",
        "--skip-repo-sync",
        "--source",
        "ModelScope",
        "--targets",
        "local-gpt-sovits-main",
        "--repo-paths",
        str(repo_paths),
        "--skip-downloads",
    ]


def test_powershell_prepare_defaults_to_auto_and_calls_probe_network() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")]' in script
    assert '[string]$Source = "Auto"' in script
    assert '"probe-network"' in script
    assert '"--write"' in script
    assert "$ResolvedSource" in script


def test_bash_prepare_defaults_to_auto_and_calls_probe_network() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")

    assert 'SOURCE="Auto"' in script
    assert "probe-network" in script
    assert "--write" in script
    assert "RESOLVED_SOURCE" in script
    assert "export_network_env" in script


def test_bash_prepare_dry_run_does_not_require_python_for_profile_json() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")

    dry_run_profile_branch = """if [[ "$DRY_RUN" == "1" ]]; then
    echo "[run] $APP_PY $ROOT/scripts/tts_more_deploy.py probe-network --write --source $SOURCE"
    RESOLVED_SOURCE="$([[ "$SOURCE" == "Auto" ]] && echo ModelScope || echo "$SOURCE")"
    read -ra SOURCE_FALLBACKS <<< "$(source_fallbacks "$RESOLVED_SOURCE")"
    read -ra PACKAGE_INDEX_FALLBACKS <<< "$(package_index_fallbacks "https://mirrors.aliyun.com/pypi/simple")"
    echo "[network] source=$RESOLVED_SOURCE"
    return 0
  fi"""
    assert dry_run_profile_branch in script


def test_prepare_scripts_retry_model_downloads_across_full_quality_sources() -> None:
    powershell = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    bash = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")

    assert "Get-SourceFallbacks" in powershell
    assert "Invoke-WithSourceFallback" in powershell
    assert "ModelScope\", \"HF-Mirror\", \"HF" in powershell
    assert "source_fallbacks()" in bash
    assert "run_with_source_fallback()" in bash
    assert 'ModelScope HF-Mirror HF' in bash


def test_prepare_scripts_retry_dependency_installs_across_package_indexes() -> None:
    powershell = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    bash = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")

    assert "Get-PackageIndexFallbacks" in powershell
    assert "Invoke-WithPackageIndexFallback" in powershell
    assert "PIP_INDEX_URL" in powershell
    assert "UV_INDEX_URL" in powershell
    assert "https://pypi.org/simple" in powershell
    assert "package_index_fallbacks()" in bash
    assert "run_with_package_index_fallback()" in bash
    assert "PIP_INDEX_URL" in bash
    assert "UV_INDEX_URL" in bash
    assert "https://pypi.org/simple" in bash


def test_prepare_scripts_do_not_default_to_reduced_models() -> None:
    combined = "\n".join(
        [
            (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8"),
            (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8"),
        ]
    ).lower()

    forbidden = ["quantized", "distilled", "small", "low-memory", "int8", "fp8", "q4", "q8"]
    assert not any(token in combined for token in forbidden)


def test_docs_describe_auto_source_cache_and_full_quality_policy() -> None:
    docs = "\n".join(
        [
            (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "open-source-tts-services.md").read_text(encoding="utf-8"),
            (REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
        ]
    )

    assert "probe-network" in docs
    assert "TTS_MORE_NETWORK_PROFILE" in docs
    assert "TTS_MORE_MODEL_SOURCE" in docs
    assert "TTS_MORE_CACHE_ROOT" in docs
    assert "full-quality" in docs
    assert "manual" in docs


def test_update_wrappers_call_single_deploy_update_entrypoint() -> None:
    bash = (REPO_ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "scripts" / "update.ps1").read_text(encoding="utf-8")

    assert "tts_more_deploy.py\" update" in bash
    assert "tts_more_deploy.py\") update" in powershell
    assert 'exec "$PYTHON"' in bash
    assert "exit $LASTEXITCODE" in powershell


def test_prepare_and_worker_scripts_accept_repo_paths_file() -> None:
    combined = "\n".join(
        [
            (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8"),
            (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8"),
            (REPO_ROOT / "scripts" / "start-service-workers.sh").read_text(encoding="utf-8"),
            (REPO_ROOT / "scripts" / "start-service-workers.ps1").read_text(encoding="utf-8"),
        ]
    )

    assert "--repo-paths" in combined
    assert "RepoPaths" in combined
    assert "list-repos" in combined


def test_one_click_deploy_scripts_install_bundles_and_render_services() -> None:
    bash = (REPO_ROOT / "scripts" / "deploy-local-tts.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "scripts" / "deploy-local-tts.ps1").read_text(encoding="utf-8")

    for script in (bash, powershell):
        assert "validate-repo-paths" in script
        assert "sync-repos" in script
        assert "install-repo-bundles" in script
        assert "prepare-tts-repos" in script
        assert "render-services" in script
        assert "doctor" in script
        assert "default" in script
        assert "--service-ids" in script


def test_prepare_scripts_default_to_release_repositories_and_forward_selection() -> None:
    bash = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert 'TARGETS="default"' in bash
    assert '[string[]]$Targets = @("default")' in powershell
    assert '--service-ids "$TARGETS"' in bash
    assert '$TargetItems = @($Targets | ForEach-Object' in powershell
    assert '"--service-ids", ($TargetItems -join ",")' in powershell


@pytest.mark.parametrize("entrypoint", ["prepare", "deploy"])
@pytest.mark.parametrize("micromamba_only", [False, True], ids=("missing-conda", "micromamba-only"))
def test_posix_prepare_and_wrapper_fail_before_gpt_preparation_without_supported_conda(
    tmp_path: Path,
    entrypoint: str,
    micromamba_only: bool,
) -> None:
    root, repo_paths, prepare_marker, render_marker = _write_prepare_failure_fixture(tmp_path)
    fake_bin = root / "test-bin"
    fake_bin.mkdir()
    if micromamba_only:
        micromamba = fake_bin / "micromamba"
        micromamba.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        micromamba.chmod(0o755)
    env = {
        **os.environ,
        "PATH": os.pathsep.join((str(fake_bin), "/usr/bin", "/bin")),
        "TTS_MORE_BASE_PYTHON": sys.executable,
        "TTS_MORE_TEST_REPO": str(root / "repo" / "GPT-SoVITS-main"),
        "TTS_MORE_TEST_PREPARE_MARKER": str(prepare_marker),
        "TTS_MORE_TEST_RENDER_MARKER": str(render_marker),
    }

    result = subprocess.run(
        _prepare_command(root, repo_paths, entrypoint, powershell=False),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    expected = (
        "micromamba is installed but is not currently supported"
        if micromamba_only
        else "supported conda executable was not found"
    )
    assert expected in result.stderr
    assert "Prepared selected TTS repositories" not in result.stdout
    assert "Local TTS deployment workflow complete" not in result.stdout
    assert not prepare_marker.exists()
    assert not render_marker.exists()


def test_prepare_scripts_preflight_conda_and_wrappers_propagate_failure_statically() -> None:
    bash_prepare = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")
    ps_prepare = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    bash_deploy = (REPO_ROOT / "scripts" / "deploy-local-tts.sh").read_text(encoding="utf-8")
    ps_deploy = (REPO_ROOT / "scripts" / "deploy-local-tts.ps1").read_text(encoding="utf-8")
    provider_bash = (
        REPO_ROOT / "deployment" / "tts-repos" / "gpt-sovits" / "tts-more-prepare.sh"
    ).read_text(encoding="utf-8")
    provider_ps = (
        REPO_ROOT / "deployment" / "tts-repos" / "gpt-sovits" / "tts-more-prepare.ps1"
    ).read_text(encoding="utf-8")

    assert bash_prepare.rindex("\npreflight_gpt_conda\n") < bash_prepare.rindex("while IFS= read -r repo")
    assert ps_prepare.rindex("Assert-SupportedCondaForSelectedGPT $repositories") < ps_prepare.index(
        "foreach ($repo in $repositories)"
    )
    for script in (bash_prepare, ps_prepare, provider_bash, provider_ps):
        assert "micromamba is installed but is not currently supported" in script
        assert "supported conda executable was not found" in script
    assert bash_deploy.index('run_plan bash "$ROOT/scripts/prepare-tts-repos.sh"') < bash_deploy.index(
        "Local TTS deployment workflow complete"
    )
    assert 'if ($LASTEXITCODE -ne 0) { throw "Command failed:' in ps_deploy
    assert ps_deploy.index('Invoke-Plan "powershell" $prepareCommandArgs') < ps_deploy.index(
        "Local TTS deployment workflow complete"
    )


def test_deployment_assets_separate_app_and_provider_repo_scripts() -> None:
    assert (REPO_ROOT / "deployment" / "app" / "repo-paths.example.json").exists()
    for provider in ("gpt-sovits", "indextts", "cosyvoice"):
        provider_dir = REPO_ROOT / "deployment" / "tts-repos" / provider
        assert (provider_dir / "README.md").exists()
        assert (provider_dir / "tts-more-prepare.sh").exists()
        assert (provider_dir / "tts-more-prepare.ps1").exists()


def test_committed_manifests_default_to_product_gpt_main() -> None:
    import json

    lock = json.loads((REPO_ROOT / "repo.lock.json").read_text(encoding="utf-8"))["repositories"]
    gpt = {repo["variant"]: repo for repo in lock if repo["provider_type"] == "gpt-sovits"}
    services = json.loads((REPO_ROOT / "data" / "services.json").read_text(encoding="utf-8"))

    assert gpt["main"]["default_selected"] is True
    assert gpt["dev"]["default_selected"] is False
    assert gpt["proplus-hc-dev"]["default_selected"] is False
    assert [service["service_id"] for service in services] == [
        "local-gpt-sovits-main",
        "local-indextts",
        "local-cosyvoice",
    ]


def test_docs_explain_gpt_branch_convergence_and_explicit_regression_targets() -> None:
    docs = "\n".join(
        [
            (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "gpt-sovits-branch-convergence.md").read_text(encoding="utf-8"),
        ]
    )

    assert "default_selected" in docs
    assert "--targets all" in docs
    assert "--targets dev" in docs
    assert "upstream/main → dev" in docs
    assert "proplus-hc-dev" in docs
    assert "CUDA" in docs


def test_one_click_and_prepare_dry_run_check_dirty_repositories_without_resetting() -> None:
    root = Path(__file__).resolve().parents[2]
    managed_root = root / "repo"
    managed_root.mkdir(exist_ok=True)
    commands = []
    with tempfile.TemporaryDirectory(prefix="dirty index ", dir=managed_root) as temp_dir:
        target = Path(temp_dir)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", "git@github.com:XucroYuri/index-tts.git"],
            check=True,
        )
        dirty_file = target / "local_patch.py"
        dirty_file.write_text("local work\n", encoding="utf-8")
        confirmation = target / "repo-paths.json"
        confirmation.write_text(
            json.dumps({"repositories": {"local-indextts": str(target)}}),
            encoding="utf-8",
        )
        commands.extend(
            [
                [
                    "bash",
                    str(root / "scripts" / "deploy-local-tts.sh"),
                    "--skip-app-install",
                    "--skip-repo-prepare",
                    "--targets",
                    "local-indextts",
                    "--repo-paths",
                    str(confirmation),
                    "--dry-run",
                ],
                [
                    "bash",
                    str(root / "scripts" / "prepare-tts-repos.sh"),
                    "--sync-repos",
                    "--skip-install",
                    "--skip-downloads",
                    "--targets",
                    "local-indextts",
                    "--repo-paths",
                    str(confirmation),
                    "--dry-run",
                ],
            ]
        )
        for command in commands:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
            assert result.returncode != 0
            assert "refusing to update dirty service repository" in result.stderr
            assert dirty_file.read_text(encoding="utf-8") == "local work\n"


def test_one_click_dry_run_executes_real_plan_without_writes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    confirmation = tmp_path / "repo-paths.json"
    confirmation.write_text(
        json.dumps({"repositories": {"local-indextts": "repo/index-tts"}}),
        encoding="utf-8",
    )
    services_path = root / "data" / "local" / "services.json"
    before = services_path.read_bytes() if services_path.exists() else None

    result = subprocess.run(
        [
            "bash",
            str(root / "scripts" / "deploy-local-tts.sh"),
            "--skip-app-install",
            "--skip-repo-prepare",
            "--targets",
            "local-indextts",
            "--repo-paths",
            str(confirmation),
            "--dry-run",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"argv": [' in result.stdout
    assert '"git"' in result.stdout
    assert '"clone"' in result.stdout
    assert '"actions"' in result.stdout
    assert '"service_id": "local-indextts"' in result.stdout
    after = services_path.read_bytes() if services_path.exists() else None
    assert after == before


def test_one_click_dry_run_includes_dependency_and_model_plan_without_writes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    plan_root = root / "repo" / f"dry-plan-{tmp_path.name}"
    confirmation = tmp_path / "repo-paths-all.json"
    confirmation.write_text(
        json.dumps(
            {
                "repositories": {
                    "local-gpt-sovits-main": str(plan_root / "GPT-SoVITS-main"),
                    "local-indextts": str(plan_root / "index-tts"),
                    "local-cosyvoice": str(plan_root / "CosyVoice"),
                }
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            str(root / "scripts" / "deploy-local-tts.sh"),
            "--skip-app-install",
            "--repo-paths",
            str(confirmation),
            "--dry-run",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    created_plan_root = plan_root.exists()
    if created_plan_root:
        shutil.rmtree(plan_root)

    assert result.returncode == 0, result.stderr
    assert "GPT-SoVITS install" in result.stdout
    assert "IndexTTS dependency install" in result.stdout
    assert "IndexTTS model download" in result.stdout
    assert "CosyVoice dependency install" in result.stdout
    assert "CosyVoice model download" in result.stdout
    assert '"write-manifest"' in result.stdout
    assert created_plan_root is False


def test_prepare_scripts_consume_canonical_absolute_repo_path() -> None:
    root = Path(__file__).resolve().parents[2]
    bash = (root / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")
    powershell = (root / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert 'repo_path="$(field "$repo" absolute_path)"' in bash
    assert 'repo_path="$ROOT/$rel_path"' not in bash
    assert "$Repo.absolute_path" in powershell
    assert "Join-Path $Root $Repo.path" not in powershell


def test_powershell_wrappers_execute_dry_run_aware_children() -> None:
    root = Path(__file__).resolve().parents[2]
    deploy = (root / "scripts" / "deploy-local-tts.ps1").read_text(encoding="utf-8")
    prepare = (root / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert "function Invoke-Plan" in deploy
    assert "Invoke-Plan $Python $syncArgs" in deploy
    assert "Invoke-Plan \"powershell\" $prepareCommandArgs" in deploy
    assert "function Invoke-Plan" in prepare
    assert "Invoke-Plan $Python $syncArgs" in prepare
    assert "ConvertTo-Json -Compress" in deploy
    assert "ConvertTo-Json -Compress" in prepare
    assert "--force-reset" not in deploy
    assert "--force-reset" not in prepare


def test_manual_copy_docs_have_executable_posix_and_powershell_layout() -> None:
    root = Path(__file__).resolve().parents[2]
    for provider in ("gpt-sovits", "indextts", "cosyvoice"):
        readme = (root / "deployment" / "tts-repos" / provider / "README.md").read_text(encoding="utf-8")
        assert 'mkdir -p "$TTS_REPO/tts-more"' in readme
        assert "Copy-Item" in readme
        assert "tts-more/tts-more-prepare.sh" in readme
        assert "tts-more\\tts-more-prepare.ps1" in readme
        assert "replaces files owned by the TTS More bundle" in readme


def test_all_managed_local_docs_require_complete_repo_confirmation() -> None:
    root = Path(__file__).resolve().parents[2]
    maintained = [
        root / "README.md",
        root / "docs" / "deployment.md",
        root / "docs" / "open-source-tts-services.md",
        root / "docs" / "workers.md",
        root / "docs" / "current-state-and-simplification-plan.md",
        root / "deployment" / "app" / "README.md",
        *(root / "deployment" / "tts-repos").glob("*/README.md"),
    ]
    contents = {path: path.read_text(encoding="utf-8") for path in maintained}
    readme = contents[root / "README.md"]
    combined = "\n".join(contents.values())

    assert len(maintained) == 9
    assert combined.count("repo-paths.example.json") >= 6
    assert "mandatory even when the lock paths are unchanged" in combined
    assert "scripts/deploy-local-tts.sh --device CU128\n" not in readme
    assert ".\\scripts\\deploy-local-tts.ps1 -Device CU128\n" not in readme
    assert "sync-repos --clean\n" not in combined
    assert "render-services --profile local-all --output data/local/services.json\n" not in combined
    managed_tokens = (
        "scripts/update.sh",
        "scripts\\update.ps1",
        "scripts/deploy-local-tts.sh",
        "scripts\\deploy-local-tts.ps1",
        "scripts/prepare-tts-repos.sh",
        "scripts\\prepare-tts-repos.ps1",
        "scripts/start-service-workers.sh",
        "scripts\\start-service-workers.ps1",
        "scripts/tts-more.sh",
        "scripts\\tts-more.ps1",
        "scripts/tts_more_deploy.py",
    )
    bare_managed = re.compile(
        r"^\s*(?:python(?:3)?\s+)?"
        r"(?:tts_more_deploy\.py\s+)?"
        r"(?:sync-repos|render-services|install-update-scripts|install-repo-bundles|"
        r"start-workers|doctor|update|prepare-tts-repos|deploy-local-tts|start-service-workers)"
        r"(?:\.sh|\.ps1)?"
        r"(?=$|[\s`])"
    )
    violations = []
    for path, content in contents.items():
        for line_number, line in enumerate(content.splitlines(), start=1):
            normalized = line.replace(".\\", "").replace("./", "")
            code_spans = re.findall(r"`([^`]+)`", line)
            bare_invocation = any(
                bare_managed.search(segment)
                and (
                    re.search(r"(?:^|\s)-{1,2}[A-Za-z]", segment)
                    or re.search(r"(?:start-service-workers|prepare-tts-repos|deploy-local-tts)\.(?:sh|ps1)", segment)
                    or "验收" in line
                )
                for segment in [normalized, *code_spans]
            )
            if not any(token in normalized for token in managed_tokens) and not bare_invocation:
                continue
            if "probe-network" in line or "--profile app-only" in line:
                continue
            if line.strip().startswith("- 新增"):
                continue
            if not any(command in line for command in ("sync-repos", "render-services", "install-update-scripts", "install-repo-bundles", "start-workers", "doctor", "update", "prepare-tts-repos", "deploy-local-tts", "start-service-workers")):
                continue
            if "repo-paths" not in line.lower() and "RepoPaths" not in line:
                violations.append(f"{path}:{line_number}: {line}")
    assert not violations, "\n".join(violations)


def test_bundle_docs_describe_per_file_atomicity_and_interruption_recovery() -> None:
    root = Path(__file__).resolve().parents[2]
    docs = (root / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "not atomic as a whole bundle" in docs
    assert "rerun the identical install command" in docs
    assert "tts-more-install-pending.json" in docs
    assert "data/local/deployment-ownership" in docs
    assert "--adopt-existing --repo-paths deployment/app/repo-paths.local.json" in docs
    assert "adoption does not upgrade, overwrite, or delete files" in docs
    assert "lost anchor fails closed" in docs
    assert "concurrent parent-swap remains a residual threat" in docs
    assert "Windows handle-based parent protection is not implemented" in docs


def test_update_script_docs_describe_portable_runtime_executable_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    deployment = (root / "docs" / "deployment.md").read_text(encoding="utf-8")
    current_state = (root / "docs" / "current-state-and-simplification-plan.md").read_text(encoding="utf-8")
    combined = deployment + "\n" + current_state

    for name in (
        "tts-more-update.sh",
        "tts-more-update.ps1",
        "tts-more-update.py",
        "tts-more-update.json",
    ):
        assert name in deployment
    assert "does not store installer-host absolute executable paths" in combined
    assert "resolves Git independently on the destination device" in combined
    assert "HTTPS remotes do not require SSH" in combined
    assert "SSH remotes require a trusted SSH executable" in combined
    assert "TTS_MORE_TRUSTED_GIT" in combined
    assert "TTS_MORE_TRUSTED_SSH" in combined
    assert "concurrent parent-swap remains a residual threat" in deployment


def test_deployment_docs_use_prefixed_worker_command_and_exact_updater_limits() -> None:
    workers = (REPO_ROOT / "docs" / "workers.md").read_text(encoding="utf-8")
    deployment = (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    current_state = (REPO_ROOT / "docs" / "current-state-and-simplification-plan.md").read_text(
        encoding="utf-8"
    )
    update_steps = deployment.split("它会按顺序做四件事：", 1)[1].split("常用变体：", 1)[0]

    assert "scripts/start-service-workers.sh --repo-paths" in workers
    assert "`start-service-workers.sh --repo-paths" not in workers
    for name in (
        "tts-more-update.sh",
        "tts-more-update.ps1",
        "tts-more-update.py",
        "tts-more-update.json",
    ):
        assert name in update_steps
    for maintained in (deployment, current_state):
        assert "repositories with submodules do not receive the standalone updater" in maintained
        assert "must be updated from TTS More managed sync-repos" in maintained
    assert "micromamba is not currently supported" in deployment
    assert "conda/micromamba" not in deployment


def test_deployment_docs_describe_final_tree_and_actual_transport_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    deployment = (root / "docs" / "deployment.md").read_text(encoding="utf-8")
    current_state = (root / "docs" / "current-state-and-simplification-plan.md").read_text(
        encoding="utf-8"
    )
    combined = deployment + "\n" + current_state

    for contract in (
        "after the final superproject checkout",
        "relative submodule URLs are resolved against the validated actual origin",
        "every resolved submodule URL must pass the GitHub allowlist",
        "HTTPS-only submodules do not require SSH",
        "any SSH submodule requires trusted SSH",
        "sidecar transport does not override the actual origin transport",
    ):
        assert contract in combined
    assert "concurrent parent-swap remains a residual threat" in deployment


def test_prepare_scripts_do_not_bypass_validated_submodule_sync() -> None:
    root = Path(__file__).resolve().parents[2]
    scripts = (
        root / "scripts" / "prepare-tts-repos.sh",
        root / "scripts" / "prepare-tts-repos.ps1",
        root / "deployment" / "tts-repos" / "cosyvoice" / "tts-more-prepare.sh",
        root / "deployment" / "tts-repos" / "cosyvoice" / "tts-more-prepare.ps1",
    )

    for path in scripts:
        content = path.read_text(encoding="utf-8")
        assert not re.search(r"(?i)\bgit\b[^\n]*\bsubmodule\b[^\n]*\bupdate\b", content), path
    readme = (root / "deployment" / "tts-repos" / "cosyvoice" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "managed sync-repos" in readme
    assert "does not run Git submodule commands" in readme


@pytest.mark.skipif(os.name != "nt", reason="native Windows prepare validation")
@pytest.mark.parametrize("powershell", ["powershell.exe", "pwsh.exe"])
@pytest.mark.parametrize("entrypoint", ["prepare", "deploy"])
@pytest.mark.parametrize("micromamba_only", [False, True], ids=("missing-conda", "micromamba-only"))
def test_windows_native_prepare_and_wrapper_fail_without_supported_conda(
    tmp_path: Path,
    powershell: str,
    entrypoint: str,
    micromamba_only: bool,
) -> None:
    executable = shutil.which(powershell)
    assert executable is not None, f"required Windows CI shell is missing: {powershell}"
    root, repo_paths, prepare_marker, render_marker = _write_prepare_failure_fixture(tmp_path)
    fake_bin = root / "test-bin"
    fake_bin.mkdir()
    if micromamba_only:
        (fake_bin / "micromamba.cmd").write_text("@exit /b 0\n", encoding="utf-8")
    path_dirs = [fake_bin, Path(sys.executable).parent]
    system_root = Path(os.environ["SystemRoot"])
    path_dirs.append(system_root / "System32")
    for required_shell in ("powershell.exe", "pwsh.exe"):
        resolved = shutil.which(required_shell)
        assert resolved is not None, f"required Windows CI shell is missing: {required_shell}"
        path_dirs.append(Path(resolved).parent)
    env = {
        **os.environ,
        "PATH": os.pathsep.join(dict.fromkeys(str(path) for path in path_dirs)),
        "TTS_MORE_BASE_PYTHON": sys.executable,
        "TTS_MORE_TEST_REPO": str(root / "repo" / "GPT-SoVITS-main"),
        "TTS_MORE_TEST_PREPARE_MARKER": str(prepare_marker),
        "TTS_MORE_TEST_RENDER_MARKER": str(render_marker),
    }
    script_command = _prepare_command(root, repo_paths, entrypoint, powershell=True)

    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            *script_command,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    expected = (
        "micromamba is installed but is not currently supported"
        if micromamba_only
        else "supported conda executable was not found"
    )
    assert expected in output
    assert "Prepared selected TTS repositories" not in output
    assert "Local TTS deployment workflow complete" not in output
    assert not prepare_marker.exists()
    assert not render_marker.exists()


def test_windows_ci_executes_native_deployment_validation_without_capability_skip() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Native Windows deployment validation" in workflow
    assert "if: runner.os == 'Windows'" in workflow
    assert "test_windows_native_powershell_launchers_reject_unsafe_remote" in workflow
    assert "test_windows_native_drive_junction_and_gitdir_policy" in workflow
    assert "test_windows_native_prepare_and_wrapper_fail_without_supported_conda" in workflow
    assert "powershell.exe" in workflow
    assert "pwsh.exe" in workflow
