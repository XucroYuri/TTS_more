from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


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
