from __future__ import annotations

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


def test_windows_prepare_flattens_list_repos_json_before_iteration() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert "$parsedRepositories = $reposJson | ConvertFrom-Json" in script
    assert "$repositories = @($parsedRepositories)" in script
    assert "$repositories = @($reposJson | ConvertFrom-Json)" not in script


def test_windows_prepare_logged_commands_do_not_pollute_function_returns() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    invoke_logged = script.split("function Invoke-Logged", maxsplit=1)[1].split(
        "function Invoke-Captured", maxsplit=1
    )[0]

    assert "& $FilePath @Arguments 2>&1 | Out-Host" in invoke_logged
    assert "$exitCode = $LASTEXITCODE" in invoke_logged
    assert "if ($exitCode -ne 0)" in invoke_logged


def test_windows_prepare_bootstraps_torchcodec_before_upstream_cuda_install() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    prepare_gpt = script.split("function Prepare-GPTSoVITS", maxsplit=1)[1].split(
        "function Prepare-IndexTTS", maxsplit=1
    )[0]

    assert '"GPT-SoVITS torchcodec bootstrap"' in prepare_gpt
    assert '"--no-deps", "torchcodec==0.13"' in prepare_gpt
    assert prepare_gpt.index("GPT-SoVITS torchcodec bootstrap") < prepare_gpt.index(
        '-Description "GPT-SoVITS install for'
    )


def test_windows_prepare_installs_and_verifies_cu128_runtime_for_index_and_cosy() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert "function Install-CU128TorchRuntime" in script
    assert '"torch==${TorchVersion}+cu128"' in script
    assert '"torchaudio==${TorchVersion}+cu128"' in script
    assert "torch.version.cuda == '12.8'" in script
    assert 'Install-CU128TorchRuntime $repoPython $repoPath "2.8.0"' in script
    assert 'Install-CU128TorchRuntime $repoPython $repoPath "2.7.1"' in script


def test_windows_prepare_bootstraps_indextts_auxiliary_models_from_modelscope() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    prepare_index = script.split("function Prepare-IndexTTS", maxsplit=1)[1].split(
        "function Prepare-CosyVoice", maxsplit=1
    )[0]

    assert "function Install-IndexTTSModelScopeAuxiliaryModels" in script
    assert "AI-ModelScope/w2v-bert-2.0" in script
    assert "nv-community/bigvgan_v2_22khz_80band_256x" in script
    assert "semantic_codec/model.safetensors" in script
    assert "iic/speech_campplus_sv_zh-cn_16k-common" in script
    assert "eb890c9660ed6e3414b6812e27257b8ce5454365d5490d3ad581ea60b93be043" in script
    assert "e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced" in script
    assert "b'')" in script
    assert 'b"")' not in script
    assert "Install-IndexTTSModelScopeAuxiliaryModels $repoPython $repoPath" in prepare_index
    assert prepare_index.index("Install-IndexTTSModelScopeAuxiliaryModels") < prepare_index.index(
        'Invoke-Logged $repoPython @("indextts\\cli_v2.py", "download"'
    )


def test_windows_prepare_bootstraps_legacy_whisper_before_cosy_requirements() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    prepare_cosy = script.split("function Prepare-CosyVoice", maxsplit=1)[1].split(
        "if ($SyncRepos)", maxsplit=1
    )[0]

    assert '"setuptools<81"' in prepare_cosy
    assert '"--no-build-isolation", "--no-deps", "openai-whisper==20231117"' in prepare_cosy
    assert "function Get-CosyVoiceRequirementsWithoutTorch" in script
    assert "-notmatch '^(torch|torchaudio)=='" in script
    assert '$Device -eq "CU128"' in prepare_cosy
    assert "Get-CosyVoiceRequirementsWithoutTorch $repoPath" in prepare_cosy
    assert prepare_cosy.index("openai-whisper==20231117") < prepare_cosy.index(
        'Get-CosyVoiceRequirementsWithoutTorch $repoPath'
    )


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


def test_windows_deploy_and_worker_scripts_forward_topology_selection() -> None:
    deploy = (REPO_ROOT / "scripts" / "deploy-local-tts.ps1").read_text(encoding="utf-8")
    workers = (REPO_ROOT / "scripts" / "start-service-workers.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("local-all", "app-only", "worker-node")][string]$Profile = "local-all"' in deploy
    for script in (deploy, workers):
        assert "[string]$Topology" in script
        assert "[string]$Node" in script
        assert '"--topology", $Topology' in script
        assert '"--node", $Node' in script
    assert '"--profile", $Profile' in deploy
    assert '$targetList = (Get-WorkerServiceIds) -join ","' in deploy
    prepare = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")
    assert '$script:TargetItems = @(Get-WorkerServiceIds)' in prepare
    validator = (REPO_ROOT / "scripts" / "run-cuda-validation.ps1").read_text(encoding="utf-8")
    assert "Stop-ConfiguredWorkerListeners" in validator


def test_committed_topology_examples_are_sanitized_and_valid() -> None:
    import importlib.util
    import json

    module_path = REPO_ROOT / "scripts" / "tts_more_deploy.py"
    spec = importlib.util.spec_from_file_location("tts_more_deploy_topology_examples", module_path)
    assert spec is not None and spec.loader is not None
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    selected = {"local-gpt-sovits-main", "local-indextts", "local-cosyvoice"}

    for filename in ("topology.single-windows.example.json", "topology.four-node-lan.example.json"):
        path = REPO_ROOT / "deployment" / "app" / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        deploy.validate_topology(payload, selected_service_ids=selected)
        serialized = json.dumps(payload)
        assert "192.168." not in serialized
        assert "10.0." not in serialized
        assert ".lan" in serialized or "localhost" in serialized


def test_local_topology_and_validation_fixtures_are_ignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "deployment/app/topology*.local.json" in gitignore
    assert "data/validation/*.local.json" in gitignore


def test_prepare_scripts_default_to_release_repositories_and_forward_selection() -> None:
    bash = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert 'TARGETS="default"' in bash
    assert '[string[]]$Targets = @("default")' in powershell
    assert '--service-ids "$TARGETS"' in bash
    assert 'return @($Targets | ForEach-Object' in powershell


def test_windows_prepare_installs_worker_runtime_into_each_repo_environment() -> None:
    powershell = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert "function Install-WorkerRuntime" in powershell
    assert "Install-WorkerRuntime $repoPath" in powershell
    for dependency in ("fastapi>=0.115.0", "uvicorn[standard]>=0.30.0", "pydantic>=2.8.0", "python-multipart>=0.0.9"):
        assert dependency in powershell
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
