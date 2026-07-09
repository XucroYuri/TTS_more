from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest


def _load_deploy_module(repo_root: Path):
    module_path = repo_root / "scripts" / "tts_more_deploy.py"
    spec = importlib.util.spec_from_file_location("tts_more_deploy", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_repo_lock(root: Path) -> None:
    (root / "repo.lock.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "GPT-SoVITS-main",
                        "provider_type": "gpt-sovits",
                        "variant": "main",
                        "path": "repo/GPT-SoVITS-main",
                        "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
                        "branch": "main",
                        "commit": "bf81cdb14a38b674b6e9996dabc97340bc9978d2",
                        "service_id": "local-gpt-sovits-main",
                        "port": 9880,
                    },
                    {
                        "name": "GPT-SoVITS-dev",
                        "provider_type": "gpt-sovits",
                        "variant": "dev",
                        "path": "repo/GPT-SoVITS-dev",
                        "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
                        "branch": "dev",
                        "commit": "6ae63b72bd3352356dcfd3961e44add7e04b1a1c",
                        "service_id": "local-gpt-sovits-dev",
                        "port": 9883,
                    },
                    {
                        "name": "GPT-SoVITS-proplus-hc-dev",
                        "provider_type": "gpt-sovits",
                        "variant": "proplus-hc-dev",
                        "path": "repo/GPT-SoVITS-proplus-hc-dev",
                        "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
                        "branch": "xucroyuri/proplus-hc-dev",
                        "commit": "b6b2a9da2eade248cf03f89195c79f49d8cd8e22",
                        "service_id": "local-gpt-sovits-proplus-hc-dev",
                        "port": 9884,
                    },
                    {
                        "name": "index-tts",
                        "provider_type": "indextts",
                        "path": "repo/index-tts",
                        "remote": "https://github.com/XucroYuri/index-tts.git",
                        "branch": "main",
                        "commit": "7264ce2a9a0924becb6b8da3f60725f7663de089",
                        "service_id": "local-indextts",
                        "port": 9881,
                    },
                    {
                        "name": "CosyVoice",
                        "provider_type": "cosyvoice",
                        "path": "repo/CosyVoice",
                        "remote": "https://github.com/XucroYuri/CosyVoice.git",
                        "branch": "main",
                        "commit": "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc",
                        "service_id": "local-cosyvoice",
                        "port": 9882,
                        "submodules": True,
                    },
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_render_local_all_services_from_repo_lock(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    services = deploy.render_services(tmp_path, profile="local-all", platform_name="windows")

    service_ids = [item["service_id"] for item in services]
    assert service_ids[:3] == [
        "local-gpt-sovits-main",
        "local-gpt-sovits-dev",
        "local-gpt-sovits-proplus-hc-dev",
    ]
    gpt_main = services[0]
    assert gpt_main["repo_path"] == "repo/GPT-SoVITS-main"
    assert gpt_main["base_url"] == "http://127.0.0.1:9880"
    assert gpt_main["env"]["TTS_MORE_GPTSOVITS_REPO"] == "repo/GPT-SoVITS-main"
    assert gpt_main["start_command"][0] == "repo/GPT-SoVITS-main/.venv/Scripts/python.exe"
    assert services[3]["env"]["TTS_MORE_INDEXTTS_MODEL_DIR"] == "repo/index-tts/checkpoints"
    assert services[4]["env"]["TTS_MORE_COSYVOICE_MODEL_DIR"] == "pretrained_models/CosyVoice-300M"


def test_render_app_only_services_are_external_and_unmanaged(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    services = deploy.render_services(
        tmp_path,
        profile="app-only",
        platform_name="posix",
        host="tts-gpu.local",
    )

    assert all(item["mode"] == "external" for item in services)
    assert all(item["managed"] is False for item in services)
    assert all(item["start_command"] == [] for item in services)
    assert services[0]["base_url"] == "http://tts-gpu.local:9880"
    assert services[2]["base_url"] == "http://tts-gpu.local:9884"


def test_render_worker_node_keeps_selected_local_worker_manageable(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    services = deploy.render_services(
        tmp_path,
        profile="worker-node",
        platform_name="windows",
        service_ids={"local-gpt-sovits-dev"},
    )

    assert [item["service_id"] for item in services] == ["local-gpt-sovits-dev"]
    assert services[0]["mode"] == "local"
    assert services[0]["managed"] is True
    assert services[0]["repo_path"] == "repo/GPT-SoVITS-dev"
    assert services[0]["start_command"][0] == "repo/GPT-SoVITS-dev/.venv/Scripts/python.exe"


def test_clean_repo_removes_readonly_files(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    readonly = tmp_path / "repo" / "CosyVoice" / ".git" / "objects" / "pack" / "pack.idx"
    readonly.parent.mkdir(parents=True)
    readonly.write_text("pack", encoding="utf-8")
    readonly.chmod(stat.S_IREAD)

    deploy._remove_repo_dir(tmp_path, dry_run=False)

    assert (tmp_path / "repo").exists()
    assert not readonly.exists()


def test_sync_repos_rejects_paths_outside_project(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    (tmp_path / "repo.lock.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "bad",
                        "provider_type": "indextts",
                        "path": "../outside",
                        "remote": "https://example.invalid/repo.git",
                        "branch": "main",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside project root"):
        deploy.sync_repos(tmp_path, dry_run=True)


def test_resolve_command_rejects_paths_outside_project(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    with pytest.raises(ValueError, match="outside project root"):
        deploy._resolve_command(tmp_path, ["../outside/python", "-m", "uvicorn"])
