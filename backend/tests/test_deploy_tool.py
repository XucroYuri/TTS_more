from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
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
                        "default_selected": True,
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
                        "default_selected": False,
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
                        "default_selected": False,
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
                        "default_selected": True,
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
                        "default_selected": True,
                    },
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _init_git_checkout(path: Path, remote: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "tests@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Deployment Tests"], check=True)
    tracked = path / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)


def test_render_local_all_services_from_repo_lock(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    services = deploy.render_services(tmp_path, profile="local-all", platform_name="windows")

    service_ids = [item["service_id"] for item in services]
    assert service_ids == [
        "local-gpt-sovits-main",
        "local-indextts",
        "local-cosyvoice",
    ]
    gpt_main = services[0]
    assert gpt_main["repo_path"] == "repo/GPT-SoVITS-main"
    assert gpt_main["base_url"] == "http://127.0.0.1:9880"
    assert gpt_main["env"]["TTS_MORE_GPTSOVITS_REPO"] == "repo/GPT-SoVITS-main"
    assert gpt_main["start_command"][0] == "repo/GPT-SoVITS-main/.venv/Scripts/python.exe"
    assert services[1]["env"]["TTS_MORE_INDEXTTS_MODEL_DIR"] == "repo/index-tts/checkpoints"
    assert services[2]["env"]["TTS_MORE_COSYVOICE_MODEL_DIR"] == "repo/CosyVoice/pretrained_models/CosyVoice-300M"


def test_render_explicit_all_includes_regression_branches(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    services = deploy.render_services(
        tmp_path,
        profile="local-all",
        platform_name="posix",
        service_ids={"all"},
    )

    assert [item["service_id"] for item in services[:3]] == [
        "local-gpt-sovits-main",
        "local-gpt-sovits-dev",
        "local-gpt-sovits-proplus-hc-dev",
    ]


def test_repo_selection_defaults_to_release_entries_but_accepts_variant_aliases(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repositories = deploy.load_repo_lock(tmp_path)

    assert [repo["variant"] for repo in repositories[:3] if deploy._repo_selected(repo, None)] == ["main"]
    assert [repo["variant"] for repo in repositories[:3] if deploy._repo_selected(repo, {"dev"})] == ["dev"]
    assert all(deploy._repo_selected(repo, {"all"}) for repo in repositories)


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
    assert services[2]["base_url"] == "http://tts-gpu.local:9882"


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
                        "remote": "https://github.com/example/repo.git",
                        "branch": "main",
                        "service_id": "local-bad",
                        "default_selected": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside project root"):
        deploy.sync_repos(tmp_path, dry_run=True)


def test_sync_repos_dry_run_uses_shallow_partial_clone(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    actions = deploy.sync_repos(tmp_path, dry_run=True)

    clone = actions[0]["argv"]
    assert clone[:3] == ["git", "clone", "--depth"]
    assert "1" in clone
    assert "--filter=blob:none" in clone
    assert "--single-branch" in clone
    assert "--branch" in clone


def test_sync_repos_dry_run_does_not_create_nested_repo_parents(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repositories = deploy.load_repo_lock(tmp_path)
    index_repo = next(repo for repo in repositories if repo["service_id"] == "local-indextts")
    index_repo["path"] = "repo/planned/nested/index-tts"

    actions = deploy.sync_repos(
        tmp_path,
        dry_run=True,
        service_ids={"local-indextts"},
        repositories=repositories,
    )

    assert any(action.get("argv", [])[:2] == ["git", "clone"] for action in actions)
    assert not (tmp_path / "repo").exists()


def test_sync_repos_refuses_dirty_existing_repo_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    (target / "tracked.txt").write_text("local patch\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to update dirty service repository"):
        deploy.sync_repos(
            tmp_path,
            dry_run=True,
            service_ids={"local-indextts"},
        )


def test_sync_repos_retries_clone_without_partial_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path) -> None:
        calls.append(command)
        if command[:2] == ["git", "clone"] and "--filter=blob:none" in command:
            raise deploy.subprocess.CalledProcessError(128, command)
        clone_path = Path(command[-1]) if command[:2] == ["git", "clone"] else None
        if clone_path:
            (clone_path / ".git").mkdir(parents=True)

    monkeypatch.setattr(deploy, "_run_git_command", fake_run)
    monkeypatch.setattr(deploy, "_git_output", lambda command: "bf81cdb14a38b674b6e9996dabc97340bc9978d2")
    (tmp_path / "repo.lock.json").write_text(
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
                        "default_selected": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    deploy.sync_repos(tmp_path, dry_run=False)

    assert any("--filter=blob:none" in command for command in calls)
    assert any(command[:2] == ["git", "clone"] and "--filter=blob:none" not in command for command in calls)


def test_run_clone_with_fallback_accepts_positional_helper_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    actions: list[dict[str, object]] = []
    target = tmp_path / "repo" / "GPT-SoVITS-main"

    monkeypatch.setattr(deploy, "_run_git_command", lambda command, *, cwd: None)

    deploy._run_clone_with_fallback(
        tmp_path,
        "https://github.com/XucroYuri/GPT-SoVITS.git",
        "main",
        target,
        True,
        actions,
    )

    assert actions == [
        {
            "action": "git",
            "argv": [
            "git",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--branch",
            "main",
            "--single-branch",
            "--",
            "https://github.com/XucroYuri/GPT-SoVITS.git",
            str(target),
            ],
        }
    ]


def test_sync_repos_rejects_existing_non_git_target_without_modifying_it(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "GPT-SoVITS-main"
    target.mkdir(parents=True)
    marker = target / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    (tmp_path / "repo.lock.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "GPT-SoVITS-main",
                        "provider_type": "gpt-sovits",
                        "path": "repo/GPT-SoVITS-main",
                        "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
                        "branch": "main",
                        "commit": "bf81cdb14a38b674b6e9996dabc97340bc9978d2",
                        "service_id": "local-gpt-sovits-main",
                        "default_selected": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="not a supported Git checkout"):
        deploy.sync_repos(tmp_path, dry_run=False)

    assert marker.exists()


def test_sync_repos_fetches_locked_commit_before_checkout_when_head_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "GPT-SoVITS-main"
    commit = "bf81cdb14a38b674b6e9996dabc97340bc9978d2"
    (tmp_path / "repo.lock.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "GPT-SoVITS-main",
                        "provider_type": "gpt-sovits",
                        "path": "repo/GPT-SoVITS-main",
                        "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
                        "branch": "main",
                        "commit": commit,
                        "service_id": "local-gpt-sovits-main",
                        "default_selected": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path) -> None:
        calls.append(command)
        if command[:2] == ["git", "clone"]:
            (target / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(deploy, "_run_git_command", fake_run)
    monkeypatch.setattr(deploy, "_git_output", lambda command: "0000000000000000000000000000000000000000")

    dry_actions = deploy.sync_repos(tmp_path, dry_run=True)
    deploy.sync_repos(tmp_path, dry_run=False)

    fetch_command = ["git", "-C", str(target), "fetch", "origin", commit]
    checkout_command = ["git", "-C", str(target), "checkout", commit]

    dry_commands = [action["argv"] for action in dry_actions if action["action"] == "git"]
    assert fetch_command in dry_commands
    assert dry_commands.index(fetch_command) < dry_commands.index(checkout_command)
    assert fetch_command in calls
    assert calls.index(fetch_command) < calls.index(checkout_command)


def test_sync_repos_dry_run_skips_locked_commit_actions_when_head_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "GPT-SoVITS-main"
    commit = "bf81cdb14a38b674b6e9996dabc97340bc9978d2"
    _init_git_checkout(target, "git@github.com:XucroYuri/GPT-SoVITS.git")
    commit = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / "repo.lock.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "GPT-SoVITS-main",
                        "provider_type": "gpt-sovits",
                        "path": "repo/GPT-SoVITS-main",
                        "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
                        "branch": "main",
                        "commit": commit,
                        "service_id": "local-gpt-sovits-main",
                        "default_selected": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    actions = deploy.sync_repos(tmp_path, dry_run=True)

    fetch_command = ["git", "-C", str(target), "fetch", "origin", commit]
    checkout_command = ["git", "-C", str(target), "checkout", commit]

    commands = [action["argv"] for action in actions if action["action"] == "git"]
    assert fetch_command not in commands
    assert checkout_command not in commands


def test_resolve_command_rejects_paths_outside_project(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    with pytest.raises(ValueError, match="outside project root"):
        deploy._resolve_command(tmp_path, ["../outside/python", "-m", "uvicorn"])


def test_resolve_network_profile_prefers_healthy_domestic_source(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    timings = {
        "https://www.modelscope.cn": {"ok": True, "latency_ms": 40},
        "https://hf-mirror.com": {"ok": True, "latency_ms": 80},
        "https://huggingface.co": {"ok": True, "latency_ms": 240},
        "https://mirrors.aliyun.com/pypi/simple": {"ok": True, "latency_ms": 35},
        "https://pypi.org/simple": {"ok": True, "latency_ms": 260},
    }

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        result = timings[url]
        return {"url": url, "ok": result["ok"], "latency_ms": result["latency_ms"], "error": ""}

    profile = deploy.resolve_network_profile(
        tmp_path,
        mode="auto",
        source="Auto",
        force=True,
        probe_func=fake_probe,
        environ={},
    )

    assert profile["mode"] == "auto"
    assert profile["model_source"] == "ModelScope"
    assert profile["hf_endpoint"] == ""
    assert profile["pip_index_url"] == "https://mirrors.aliyun.com/pypi/simple"
    assert profile["cache_root"] == "data/cache"
    env = deploy.network_env_from_profile(profile)
    assert env["PIP_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple"
    assert env["PIP_CACHE_DIR"].endswith(os.path.join("data", "cache", "pip"))
    assert env["HF_HOME"].endswith(os.path.join("data", "cache", "huggingface"))
    assert env["MODELSCOPE_CACHE"].endswith(os.path.join("data", "cache", "modelscope"))


def test_resolve_network_profile_reuses_valid_cached_profile_without_probe(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    cache_paths = deploy._cache_paths(tmp_path, {})
    cached_profile = {
        "schema_version": deploy.NETWORK_PROFILE_SCHEMA_VERSION,
        "mode": "auto",
        "source": "cached",
        "expires_at": "2099-01-01T00:00:00Z",
        "cache_root": cache_paths["cache_root"],
        "cache_paths": cache_paths,
        "request_context": {
            "mode": "auto",
            "source": "Auto",
            "cache_root": cache_paths["cache_root"],
            "model_source": "",
            "pip_index_url": "",
            "hf_endpoint": "",
            "extra_pip_index_url": "",
        },
    }
    deploy.write_json(tmp_path / "data" / "local" / "network-profile.json", cached_profile)

    def fail_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        pytest.fail(f"probe should not be called for cached profile: {url}")

    profile = deploy.resolve_network_profile(
        tmp_path,
        force=False,
        probe_func=fail_probe,
        environ={},
    )

    assert profile == cached_profile


def test_resolve_network_profile_rebuilds_cache_when_cache_root_changes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    cache_paths = deploy._cache_paths(tmp_path, {})
    cached_profile = {
        "schema_version": deploy.NETWORK_PROFILE_SCHEMA_VERSION,
        "mode": "auto",
        "source": "cached",
        "expires_at": "2099-01-01T00:00:00Z",
        "cache_root": cache_paths["cache_root"],
        "cache_paths": cache_paths,
        "request_context": {
            "mode": "auto",
            "source": "Auto",
            "cache_root": cache_paths["cache_root"],
            "model_source": "",
            "pip_index_url": "",
            "hf_endpoint": "",
            "extra_pip_index_url": "",
        },
    }
    deploy.write_json(tmp_path / "data" / "local" / "network-profile.json", cached_profile)

    probe_calls: list[str] = []

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        probe_calls.append(url)
        return {"url": url, "ok": True, "latency_ms": 10, "error": ""}

    profile = deploy.resolve_network_profile(
        tmp_path,
        force=False,
        probe_func=fake_probe,
        environ={"TTS_MORE_CACHE_ROOT": "custom-cache"},
    )

    assert probe_calls
    assert profile["cache_root"] == "custom-cache"
    assert profile["cache_paths"]["cache_root"] == "custom-cache"
    assert profile["cache_paths"]["pip_cache_dir"].endswith(os.path.join("custom-cache", "pip"))


def test_probe_url_rejects_client_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    class FakeResponse:
        status = 404

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_urlopen(request, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(deploy, "urlopen", fake_urlopen)

    result = deploy._probe_url("https://example.invalid", 1.0)

    assert result["ok"] is False


def test_probe_url_falls_back_to_get_when_head_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    methods: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
            return False

    def fake_urlopen(request, timeout=None):
        methods.append(request.get_method())
        if request.get_method() == "HEAD":
            raise deploy.URLError("head refused")
        return FakeResponse()

    monkeypatch.setattr(deploy, "urlopen", fake_urlopen)

    result = deploy._probe_url("https://example.invalid", 1.0)

    assert result["ok"] is True
    assert methods == ["HEAD", "GET"]


def test_resolve_network_profile_falls_back_to_global_when_domestic_fails(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        if url in {"https://www.modelscope.cn", "https://hf-mirror.com", "https://mirrors.aliyun.com/pypi/simple"}:
            return {"url": url, "ok": False, "latency_ms": 2000, "error": "timeout"}
        return {"url": url, "ok": True, "latency_ms": 90, "error": ""}

    profile = deploy.resolve_network_profile(
        tmp_path,
        mode="auto",
        source="Auto",
        force=True,
        probe_func=fake_probe,
        environ={},
    )

    assert profile["model_source"] == "HF"
    assert profile["hf_endpoint"] == ""
    assert profile["pip_index_url"] == "https://pypi.org/simple"


def test_manual_source_keeps_cache_env_and_skips_auto_source_choice(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        return {"url": url, "ok": True, "latency_ms": 10, "error": ""}

    profile = deploy.resolve_network_profile(
        tmp_path,
        mode="auto",
        source="HF-Mirror",
        force=True,
        probe_func=fake_probe,
        environ={},
    )

    assert profile["model_source"] == "HF-Mirror"
    assert profile["hf_endpoint"] == "https://hf-mirror.com"
    env = deploy.network_env_from_profile(profile)
    assert env["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert "PIP_CACHE_DIR" in env


def test_probe_network_writes_profile_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    def fake_resolve(root: Path, **kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "mode": "auto",
            "model_source": "ModelScope",
            "hf_endpoint": "",
            "pip_index_url": "https://mirrors.aliyun.com/pypi/simple",
            "cache_root": "data/cache",
            "cache_paths": {"pip_cache_dir": str(root / "data/cache/pip")},
            "env": {"PIP_CACHE_DIR": str(root / "data/cache/pip")},
            "probes": [],
        }

    monkeypatch.setattr(deploy, "resolve_network_profile", fake_resolve)

    profile = deploy.probe_network(tmp_path, write=True)

    profile_path = tmp_path / "data" / "local" / "network-profile.json"
    assert profile["model_source"] == "ModelScope"
    assert json.loads(profile_path.read_text(encoding="utf-8"))["env"]["PIP_CACHE_DIR"].endswith("pip")


def test_probe_network_without_write_does_not_create_profile_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        return {"url": url, "ok": True, "latency_ms": 10, "error": ""}

    monkeypatch.setattr(deploy, "_probe_url", fake_probe)

    profile = deploy.probe_network(tmp_path, write=False, force=True)

    assert profile["model_source"] == "ModelScope"
    assert not (tmp_path / "data" / "local" / "network-profile.json").exists()


def test_written_auto_network_profile_is_reused_without_reprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    for key in (
        "TTS_MORE_MODEL_SOURCE",
        "TTS_MORE_PIP_INDEX_URL",
        "TTS_MORE_HF_ENDPOINT",
        "TTS_MORE_EXTRA_PIP_INDEX_URL",
        "TTS_MORE_CACHE_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)
    calls: list[str] = []

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        calls.append(url)
        return {"url": url, "ok": True, "latency_ms": 10, "error": ""}

    monkeypatch.setattr(deploy, "_probe_url", fake_probe)

    deploy.probe_network(tmp_path, write=True, force=True)
    first_probe_count = len(calls)

    def fail_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        raise AssertionError("cached Auto profile should be reused without probing")

    monkeypatch.setattr(deploy, "_probe_url", fail_probe)
    profile = deploy.resolve_network_profile(tmp_path)

    assert profile["model_source"] == "ModelScope"
    assert len(calls) == first_probe_count


def test_doctor_reports_network_profile_and_cache_paths(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    profile_path = tmp_path / "data" / "local" / "network-profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "auto",
                "model_source": "HF-Mirror",
                "cache_root": "data/cache",
                "cache_paths": {"pip_cache_dir": str(tmp_path / "data/cache/pip")},
                "env": {"HF_ENDPOINT": "https://hf-mirror.com"},
            }
        ),
        encoding="utf-8",
    )

    report = deploy.doctor(tmp_path)

    assert report["network_profile"]["model_source"] == "HF-Mirror"
    assert report["cache_paths"]["cache_root"] == "data/cache"


def test_sync_repos_latest_dry_run_skips_locked_commit_checkout(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    actions = deploy.sync_repos(
        tmp_path,
        dry_run=True,
        latest=True,
        service_ids={"local-indextts"},
    )

    assert len(actions) == 1
    assert actions[0]["argv"][:2] == ["git", "clone"]
    assert "index-tts" in actions[0]["argv"][-1]
    assert not any(action.get("argv", [])[-2:-1] == ["fetch"] for action in actions)


def test_install_update_scripts_writes_repo_local_helpers(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")

    reports = deploy.install_update_scripts(tmp_path, service_ids={"local-indextts"})

    sh_path = target / "tts-more-update.sh"
    ps1_path = target / "tts-more-update.ps1"
    assert reports[0]["name"] == "index-tts"
    assert reports[0]["path"] == "repo/index-tts"
    assert reports[0]["exists"] is True
    assert reports[0]["scripts"] == [
        "repo/index-tts/tts-more-update.sh",
        "repo/index-tts/tts-more-update.ps1",
        "repo/index-tts/tts-more-update.py",
        "repo/index-tts/tts-more-update.json",
    ]
    updater = (target / "tts-more-update.py").read_text(encoding="utf-8")
    sidecar = json.loads((target / "tts-more-update.json").read_text(encoding="utf-8"))
    assert "tts-more-update.py" in sh_path.read_text(encoding="utf-8")
    assert "tts-more-update.py" in ps1_path.read_text(encoding="utf-8")
    assert '"pull", "--ff-only", "origin", branch' in updater
    assert sidecar["branch"] == "main"
    if os.name != "nt":
        assert sh_path.stat().st_mode & stat.S_IXUSR
    exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "tts-more-update.sh" in exclude
    assert "tts-more-update.ps1" in exclude
    assert "tts-more-update.py" in exclude
    assert "tts-more-update.json" in exclude


def test_repo_path_overrides_apply_to_rendered_local_services(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repo_paths = tmp_path / "deployment" / "app" / "repo-paths.local.json"
    repo_paths.parent.mkdir(parents=True)
    repo_paths.write_text(
        json.dumps({"repositories": {"local-indextts": "repo/custom-index-tts"}}),
        encoding="utf-8",
    )

    repositories = deploy.load_deployment_repositories(tmp_path, repo_paths)
    services = deploy.render_services(
        tmp_path,
        platform_name="posix",
        service_ids={"local-indextts"},
        repositories=repositories,
    )

    assert services[0]["repo_path"] == "repo/custom-index-tts"
    assert services[0]["start_command"][0] == "repo/custom-index-tts/.venv/bin/python"
    assert services[0]["env"]["TTS_MORE_INDEXTTS_MODEL_DIR"] == "repo/custom-index-tts/checkpoints"


def test_validate_repo_paths_rejects_paths_outside_project(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repo_paths = tmp_path / "deployment" / "app" / "repo-paths.local.json"
    repo_paths.parent.mkdir(parents=True)
    repo_paths.write_text(
        json.dumps({"repositories": {"local-indextts": "../outside-index-tts"}}),
        encoding="utf-8",
    )

    repositories = deploy.load_deployment_repositories(tmp_path, repo_paths=repo_paths)
    report = deploy.validate_repo_paths(
        tmp_path,
        service_ids={"local-indextts"},
        repositories=repositories,
    )

    assert report[0]["ok"] is False
    assert report[0]["inside_project"] is False
    assert "outside project root" in report[0]["error"]


def test_install_repo_bundles_copies_provider_helpers_and_excludes_them(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "tts-more-prepare.sh").write_text("#!/usr/bin/env bash\necho prepare\n", encoding="utf-8")
    (bundle / "README.md").write_text("IndexTTS helper\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")

    reports = deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    copied = target / "tts-more" / "tts-more-prepare.sh"
    manifest = json.loads((target / "tts-more" / "tts-more-repo.json").read_text(encoding="utf-8"))
    assert reports[0]["installed"] is True
    assert copied.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    if os.name != "nt":
        assert copied.stat().st_mode & stat.S_IXUSR
    assert manifest["service_id"] == "local-indextts"
    exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "tts-more/" in exclude


def test_update_project_dry_run_reports_app_and_repo_actions_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    monkeypatch.setattr(deploy, "_git_output", lambda command: "master" if command[-2:] == ["branch", "--show-current"] else "")

    payload = deploy.update_project(
        tmp_path,
        dry_run=True,
        service_ids={"local-cosyvoice"},
    )

    assert payload["app_actions"] == [
        ["git", "-C", str(tmp_path), "fetch", "--prune", "origin", "master"],
        ["git", "-C", str(tmp_path), "pull", "--ff-only", "origin", "master"],
    ]
    assert any(
        "CosyVoice" in action["argv"][-1]
        for action in payload["repo_actions"]
        if action.get("argv", [])[:2] == ["git", "clone"]
    )
    assert payload["update_scripts"][0]["exists"] is False
    assert payload["services_output"] == "data/local/services.json"
    assert payload["services_rendered"] is False
    assert payload["services_render_policy"] == "missing-only"
    assert not (tmp_path / "data" / "local" / "services.json").exists()


def test_update_project_preserves_existing_local_services_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    services_path = tmp_path / "data" / "local" / "services.json"
    services_path.parent.mkdir(parents=True)
    services_path.write_text('[{"service_id":"custom-cloud"}]\n', encoding="utf-8")
    monkeypatch.setattr(deploy, "_git_output", lambda command: "master" if command[-2:] == ["branch", "--show-current"] else "")
    monkeypatch.setattr(deploy, "sync_repos", lambda *args, **kwargs: [])

    payload = deploy.update_project(tmp_path, skip_app=True)

    assert payload["services_rendered"] is False
    assert json.loads(services_path.read_text(encoding="utf-8"))[0]["service_id"] == "custom-cloud"


def test_update_project_force_render_services_overwrites_local_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    services_path = tmp_path / "data" / "local" / "services.json"
    services_path.parent.mkdir(parents=True)
    services_path.write_text('[{"service_id":"custom-cloud"}]\n', encoding="utf-8")
    monkeypatch.setattr(deploy, "sync_repos", lambda *args, **kwargs: [])

    payload = deploy.update_project(tmp_path, skip_app=True, force_render=True)

    services = json.loads(services_path.read_text(encoding="utf-8"))
    assert payload["services_rendered"] is True
    assert services[0]["service_id"] == "local-gpt-sovits-main"


def test_update_project_refuses_dirty_service_repo_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    (target / "tracked.txt").write_text("local patch\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to update dirty service repository"):
        deploy.update_project(
            tmp_path,
            skip_app=True,
            dry_run=True,
            service_ids={"local-indextts"},
        )


def test_update_project_force_reset_repos_allows_reset_actions_for_dirty_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "git@github.com:XucroYuri/index-tts.git")
    (target / "tracked.txt").write_text("local patch\n", encoding="utf-8")

    payload = deploy.update_project(
        tmp_path,
        skip_app=True,
        dry_run=True,
        service_ids={"local-indextts"},
        force_reset_repos=True,
    )

    assert ["git", "-C", str(target), "reset", "--hard", "origin/main"] in [
        action["argv"] for action in payload["repo_actions"] if action["action"] == "git"
    ]


def test_repo_path_confirmation_requires_exact_complete_service_id_map(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repo_paths = tmp_path / "repo-paths.json"
    repo_paths.write_text(
        json.dumps({"repositories": {"local-indextts": "repo/index-tts"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing confirmed repository paths"):
        deploy.load_deployment_repositories(
            tmp_path,
            repo_paths,
            service_ids={"default"},
            require_complete=True,
        )

    repo_paths.write_text(
        json.dumps(
            {
                "repositories": {
                    "local-gpt-sovits-main": "repo/GPT-SoVITS-main",
                    "local-indextts": "repo/index-tts",
                    "local-cosyvoice": "repo/CosyVoice",
                    "indextts": "repo/ambiguous",
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown repository service_id"):
        deploy.load_deployment_repositories(
            tmp_path,
            repo_paths,
            service_ids={"default"},
            require_complete=True,
        )


def test_repo_manifest_rejects_duplicate_service_ids_and_unmarked_defaults(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    duplicate = {
        "name": "duplicate",
        "provider_type": "indextts",
        "path": "repo/duplicate",
        "remote": "https://github.com/example/duplicate.git",
        "branch": "main",
        "commit": "a" * 40,
        "service_id": "local-duplicate",
        "default_selected": False,
    }
    (tmp_path / "repo.lock.json").write_text(
        json.dumps({"repositories": [duplicate, dict(duplicate, path="repo/duplicate-2")]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate service_id"):
        deploy.load_repo_lock(tmp_path)

    del duplicate["default_selected"]
    (tmp_path / "repo.lock.json").write_text(
        json.dumps({"repositories": [duplicate]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="default_selected"):
        deploy.load_repo_lock(tmp_path)


def test_repo_paths_are_limited_to_dedicated_repo_area(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repo_paths = tmp_path / "repo-paths.json"
    repo_paths.write_text(
        json.dumps({"repositories": {"local-indextts": "deployment/index-tts"}}),
        encoding="utf-8",
    )

    repositories = deploy.load_deployment_repositories(
        tmp_path,
        repo_paths,
        service_ids={"local-indextts"},
        require_complete=True,
    )
    report = deploy.validate_repo_paths(
        tmp_path,
        service_ids={"local-indextts"},
        repositories=repositories,
    )

    assert report[0]["ok"] is False
    assert "dedicated repository area" in report[0]["error"]


def test_existing_repo_origin_must_match_manifest_before_sync(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/example/unrelated.git")
    repositories = deploy.load_repo_lock(tmp_path)

    with pytest.raises(RuntimeError, match="origin mismatch"):
        deploy.sync_repos(
            tmp_path,
            dry_run=True,
            service_ids={"local-indextts"},
            repositories=repositories,
        )

    subprocess.run(
        ["git", "-C", str(target), "remote", "set-url", "origin", "git@github.com:XucroYuri/index-tts.git"],
        check=True,
    )
    actions = deploy.sync_repos(
        tmp_path,
        dry_run=True,
        service_ids={"local-indextts"},
        repositories=repositories,
    )
    assert any(action.get("argv", [])[-2:] == ["origin", "main"] for action in actions)


def test_update_sidecar_rejects_manifest_code_injection_without_execution(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    marker = tmp_path / "manifest-injection-marker"
    repositories = [
        {
            "name": f'IndexTTS"; touch {marker}; #',
            "provider_type": "indextts",
            "path": "repo/index-tts",
            "remote": "https://github.com/XucroYuri/index-tts.git",
            "branch": "main",
            "commit": "a" * 40,
            "service_id": "local-indextts",
            "default_selected": True,
        }
    ]

    deploy.install_update_scripts(tmp_path, repositories=repositories)
    shell_script = (target / "tts-more-update.sh").read_text(encoding="utf-8")
    powershell_script = (target / "tts-more-update.ps1").read_text(encoding="utf-8")
    assert str(marker) not in shell_script
    assert str(marker) not in powershell_script

    sidecar = target / "tts-more-update.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["branch"] = f"main; touch {marker}"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        ["bash", str(target / "tts-more-update.sh")],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="native PowerShell validation runs in Windows CI")
def test_powershell_update_launcher_rejects_manifest_code_injection(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    marker = tmp_path / "powershell-injection-marker"
    repositories = [
        {
            "name": "IndexTTS",
            "provider_type": "indextts",
            "path": "repo/index-tts",
            "remote": "https://github.com/XucroYuri/index-tts.git",
            "branch": "main",
            "commit": "a" * 40,
            "service_id": "local-indextts",
            "default_selected": True,
        }
    ]
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    sidecar = target / "tts-more-update.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["branch"] = f'main"; New-Item "{marker}"; #'
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(target / "tts-more-update.ps1")],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_manifest_rejects_unsafe_branch_and_commit_values(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    payload = json.loads((tmp_path / "repo.lock.json").read_text(encoding="utf-8"))
    payload["repositories"][0]["branch"] = "main$(touch marker)"
    payload["repositories"][0]["commit"] = "HEAD; touch marker"
    (tmp_path / "repo.lock.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="branch"):
        deploy.load_repo_lock(tmp_path)


def test_helper_install_rejects_symlinked_destinations_and_sources(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "tts-more-prepare.sh").write_text("echo safe\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "tts-more").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|reparse"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    assert list(outside.iterdir()) == []

    (target / "tts-more").unlink()
    outside_update = outside / "update.sh"
    outside_update.write_text("keep\n", encoding="utf-8")
    (target / "tts-more-update.sh").symlink_to(outside_update)
    with pytest.raises(ValueError, match="symlink|reparse"):
        deploy.install_update_scripts(tmp_path, service_ids={"local-indextts"})
    assert outside_update.read_text(encoding="utf-8") == "keep\n"

    (target / "tts-more-update.sh").unlink()
    nested = bundle / "nested"
    nested.mkdir()
    (nested / "escaped").symlink_to(outside_update)
    with pytest.raises(ValueError, match="symlink|reparse"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})


def test_helper_install_rejects_junction_or_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("safe\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    redirected = target / "tts-more"
    redirected.mkdir()
    original = deploy._is_link_or_reparse
    monkeypatch.setattr(
        deploy,
        "_is_link_or_reparse",
        lambda path: Path(path) == redirected or original(path),
    )

    with pytest.raises(ValueError, match="symlink|reparse"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})


def test_atomic_json_write_rejects_symlinked_parent(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|reparse"):
        deploy.write_json(
            tmp_path / "data" / "local" / "services.json",
            [],
            boundary=tmp_path,
        )
    assert list(outside.iterdir()) == []


def test_cosyvoice_model_dir_resolves_below_confirmed_repository(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    services = deploy.render_services(
        tmp_path,
        service_ids={"local-cosyvoice"},
        platform_name="posix",
    )

    resolved = deploy._resolve_env(tmp_path, services[0]["env"])
    assert resolved["TTS_MORE_COSYVOICE_MODEL_DIR"] == str(
        tmp_path / "repo" / "CosyVoice" / "pretrained_models" / "CosyVoice-300M"
    )


@pytest.mark.parametrize(
    "raw,match",
    [
        ("", "empty target selector"),
        ("indxetts", "unknown target selector"),
        ("indextts,missing", "unknown target selector"),
        ("indextts,indextts", "duplicate target selector"),
    ],
)
def test_target_selectors_fail_closed(raw: str, match: str, tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repositories = deploy.load_repo_lock(tmp_path)

    with pytest.raises(ValueError, match=match):
        service_ids = deploy._parse_service_ids(raw)
        deploy._select_repositories(repositories, service_ids)


def test_unknown_selector_does_not_overwrite_existing_services(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    output = tmp_path / "data" / "local" / "services.json"
    output.parent.mkdir(parents=True)
    output.write_text('[{"service_id":"keep-me"}]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unknown target selector"):
        deploy.main(
            [
                "--root",
                str(tmp_path),
                "render-services",
                "--service-ids",
                "indxetts",
                "--output",
                "data/local/services.json",
            ]
        )
    assert json.loads(output.read_text(encoding="utf-8")) == [{"service_id": "keep-me"}]


def test_list_repos_emits_canonical_absolute_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    absolute = tmp_path / "repo" / "Index TTS absolute"
    repo_paths = tmp_path / "repo-paths.json"
    repo_paths.write_text(
        json.dumps({"repositories": {"local-indextts": str(absolute)}}),
        encoding="utf-8",
    )

    result = deploy.main(
        [
            "--root",
            str(tmp_path),
            "list-repos",
            "--service-ids",
            "local-indextts",
            "--repo-paths",
            str(repo_paths),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload[0]["absolute_path"] == str(absolute)


def test_bundle_upgrade_removes_stale_owned_files_and_is_stable(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    old_source = bundle / "old-helper.sh"
    old_source.write_text("old\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")

    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    user_file = target / "tts-more" / "user-notes.txt"
    user_file.write_text("preserve\n", encoding="utf-8")
    old_source.unlink()
    (bundle / "new-helper.sh").write_text("new\n", encoding="utf-8")

    reports = deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    assert reports[0]["installed"] is True
    assert not (target / "tts-more" / "old-helper.sh").exists()
    assert (target / "tts-more" / "new-helper.sh").read_text(encoding="utf-8") == "new\n"
    assert user_file.read_text(encoding="utf-8") == "preserve\n"
    manifest_path = target / "tts-more" / "tts-more-repo.json"
    first_manifest = manifest_path.read_bytes()
    assert "installed_at" not in json.loads(first_manifest)

    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    assert manifest_path.read_bytes() == first_manifest


def test_committed_repo_lock_marks_every_default_explicitly() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    repositories = json.loads((repo_root / "repo.lock.json").read_text(encoding="utf-8"))["repositories"]

    assert all(type(repo.get("default_selected")) is bool for repo in repositories)


@pytest.mark.parametrize(
    "remote",
    [
        "ext::sh -c touch marker",
        "helper::payload",
        "-uploader",
        "../local/repo",
        "/tmp/local-repo",
        "file:///tmp/local-repo",
        "https://user@github.com/XucroYuri/index-tts.git",
        "https://user:secret@github.com/XucroYuri/index-tts.git",
        "https://github.com:444/XucroYuri/index-tts.git",
        "ssh://git@github.com:2222/XucroYuri/index-tts.git",
        "ssh://root@github.com/XucroYuri/index-tts.git",
        "git@example.com:XucroYuri/index-tts.git",
        "https://github.com./XucroYuri/index-tts.git",
        "https://github.com/XucroYuri/index-tts.git?x=1",
        "https://github.com/XucroYuri/index-tts.git#fragment",
        "https://github.com/XucroYuri/index%2dtts.git",
        "https://github.com/XucroYuri/index-tts.git\nhelper::payload",
        "https://github.com/XucroYuri/index-tts.git\x00",
    ],
)
def test_remote_policy_rejects_non_github_or_unsafe_transports(remote: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    with pytest.raises(ValueError, match="GitHub remote"):
        deploy._parse_github_remote(remote)


def test_remote_policy_accepts_only_equivalent_default_github_endpoints() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    accepted = [
        "https://github.com/XucroYuri/index-tts.git",
        "https://github.com:443/XucroYuri/index-tts",
        "ssh://git@github.com/XucroYuri/index-tts.git",
        "ssh://git@github.com:22/XucroYuri/index-tts.git",
        "git@github.com:XucroYuri/index-tts.git",
        "https://GITHUB.COM/xucroyuri/INDEX-TTS.git",
    ]

    identities = {deploy._parse_github_remote(remote) for remote in accepted}

    assert identities == {("github.com", "xucroyuri", "index-tts")}


def test_repo_lock_rejects_remote_helper_before_clone(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    payload = json.loads((tmp_path / "repo.lock.json").read_text(encoding="utf-8"))
    payload["repositories"][0]["remote"] = "ext::sh -c touch marker"
    (tmp_path / "repo.lock.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="GitHub remote"):
        deploy.load_repo_lock(tmp_path)


def test_clone_command_terminates_options_before_remote(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    remote = "https://github.com/XucroYuri/index-tts.git"

    command = deploy._clone_command(remote, "main", tmp_path / "repo" / "index-tts")

    assert command[command.index(remote) - 1] == "--"


def test_generated_updater_rejects_unsafe_remote_before_invoking_git(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    sidecar = target / "tts-more-update.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["remote"] = "ext::sh -c touch marker"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "git-invoked"
    fake_git = fake_bin / "git"
    fake_git.write_text(f"#!/usr/bin/env bash\ntouch {marker!s}\nexit 1\n", encoding="utf-8")
    fake_git.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}

    result = subprocess.run(
        [sys.executable, str(target / "tts-more-update.py")],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "GitHub remote" in result.stderr
    assert not marker.exists()


def test_generated_updater_rejects_gitdir_file_before_invoking_git(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    shutil.rmtree(target / ".git")
    (target / ".git").write_text("gitdir: ../../outside\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "git-invoked"
    fake_git = fake_bin / "git"
    fake_git.write_text(f"#!/usr/bin/env bash\ntouch {marker!s}\nexit 1\n", encoding="utf-8")
    fake_git.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}

    result = subprocess.run(
        [sys.executable, str(target / "tts-more-update.py")],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "gitdir files are not supported" in result.stderr
    assert not marker.exists()


def test_clean_is_selection_scoped_and_dry_run_matches_real_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    selected = tmp_path / "repo" / "index-tts"
    unselected = tmp_path / "repo" / "CosyVoice"
    extra = tmp_path / "repo" / "user-extra"
    _init_git_checkout(selected, "https://github.com/XucroYuri/index-tts.git")
    _init_git_checkout(unselected, "https://github.com/XucroYuri/CosyVoice.git")
    extra.mkdir()
    (extra / "keep.txt").write_text("keep\n", encoding="utf-8")
    repositories = deploy.load_repo_lock(tmp_path)

    dry_actions = deploy.sync_repos(
        tmp_path,
        clean=True,
        dry_run=True,
        service_ids={"local-indextts"},
        repositories=repositories,
    )
    monkeypatch.setattr(deploy, "_run_git_command", lambda command, *, cwd: None)
    real_actions = deploy.sync_repos(
        tmp_path,
        clean=True,
        dry_run=False,
        service_ids={"local-indextts"},
        repositories=repositories,
    )

    assert dry_actions == real_actions
    assert dry_actions[0] == {"action": "remove-repository", "path": str(selected)}
    assert any(action.get("argv", [])[:2] == ["git", "clone"] for action in dry_actions)
    assert not selected.exists()
    assert unselected.exists()
    assert (extra / "keep.txt").read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("dry_run", [True, False])
def test_clean_rejects_dirty_selected_repository_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    selected = tmp_path / "repo" / "index-tts"
    _init_git_checkout(selected, "https://github.com/XucroYuri/index-tts.git")
    dirty = selected / "tracked.txt"
    dirty.write_text("local modification\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy,
        "_run_git_command",
        lambda command, *, cwd: (_ for _ in ()).throw(AssertionError("clean reached Git mutation")),
    )

    with pytest.raises(RuntimeError, match="dirty service repository"):
        deploy.sync_repos(
            tmp_path,
            clean=True,
            dry_run=dry_run,
            service_ids={"local-indextts"},
        )

    assert dirty.read_text(encoding="utf-8") == "local modification\n"


@pytest.mark.parametrize("dry_run", [True, False])
def test_clean_rejects_unrecognized_selected_path_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    selected = tmp_path / "repo" / "index-tts"
    selected.mkdir(parents=True)
    marker = selected / "user-data.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy,
        "_run_git_command",
        lambda command, *, cwd: (_ for _ in ()).throw(AssertionError("clean reached Git mutation")),
    )

    with pytest.raises(RuntimeError, match="not a supported Git checkout|unrecognized"):
        deploy.sync_repos(
            tmp_path,
            clean=True,
            dry_run=dry_run,
            service_ids={"local-indextts"},
        )

    assert marker.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("metadata_kind", ["symlink", "gitdir-file", "corrupt-directory"])
def test_git_metadata_policy_rejects_redirected_worktree_or_corrupt_metadata_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_kind: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "index-tts"
    target.mkdir(parents=True)
    dot_git = target / ".git"
    if metadata_kind == "symlink":
        outside = tmp_path / "outside-git"
        outside.mkdir()
        dot_git.symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(
            deploy,
            "_git_output",
            lambda command: (_ for _ in ()).throw(AssertionError("Git ran before metadata validation")),
        )
    elif metadata_kind == "gitdir-file":
        dot_git.write_text("gitdir: ../../outside-git\n", encoding="utf-8")
        monkeypatch.setattr(
            deploy,
            "_git_output",
            lambda command: (_ for _ in ()).throw(AssertionError("Git ran before metadata validation")),
        )
    else:
        dot_git.mkdir()

    with pytest.raises((ValueError, RuntimeError), match="Git metadata|worktree|corrupt"):
        deploy.sync_repos(tmp_path, dry_run=True, service_ids={"local-indextts"})


@pytest.mark.parametrize(
    "service_id",
    [
        "../escape",
        "nested/service",
        r"nested\service",
        "/absolute",
        r"C:\absolute",
        "line\nbreak",
        ".hidden",
        "trailing-",
        "a" * 65,
    ],
)
def test_manifest_rejects_unsafe_service_ids(tmp_path: Path, service_id: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    payload = json.loads((tmp_path / "repo.lock.json").read_text(encoding="utf-8"))
    payload["repositories"][0]["service_id"] = service_id
    (tmp_path / "repo.lock.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="service_id"):
        deploy.load_repo_lock(tmp_path)


def test_worker_log_open_is_strictly_bounded_to_logs_directory(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    logs_dir = tmp_path / "data" / ".runtime" / "logs"
    logs_dir.mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("keep\n", encoding="utf-8")
    (logs_dir / "local-indextts.log").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink|reparse"):
        deploy._open_worker_log(logs_dir, "local-indextts")
    with pytest.raises(ValueError, match="service_id"):
        deploy._open_worker_log(logs_dir, "../../../outside")

    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize(
    "manifest",
    [
        {"owned_files": ["user-notes.txt"]},
        {
            "schema_version": 3,
            "service_id": "local-cosyvoice",
            "provider_type": "cosyvoice",
            "source_bundle": "deployment/tts-repos/cosyvoice",
            "source_hash": "0" * 64,
            "owned_files": {"user-notes.txt": "0" * 64},
        },
    ],
)
def test_bundle_rejects_untrusted_or_cross_provider_ownership_manifest(
    tmp_path: Path, manifest: dict[str, object]
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "current.sh").write_text("current\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    installed = target / "tts-more"
    installed.mkdir()
    user_file = installed / "user-notes.txt"
    user_file.write_text("preserve\n", encoding="utf-8")
    (installed / "tts-more-repo.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="ownership manifest"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    assert user_file.read_text(encoding="utf-8") == "preserve\n"


def test_bundle_refuses_to_delete_or_overwrite_modified_owned_file(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    source = bundle / "owned.sh"
    source.write_text("version one\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    owned = target / "tts-more" / "owned.sh"
    owned.write_text("local modification\n", encoding="utf-8")
    source.unlink()

    with pytest.raises(RuntimeError, match="locally modified owned file"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    assert owned.read_text(encoding="utf-8") == "local modification\n"


def test_bundle_manifest_records_strong_identity_and_content_hashes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    payload = b"helper content\n"
    (bundle / "helper.sh").write_bytes(payload)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")

    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    manifest = json.loads((target / "tts-more" / "tts-more-repo.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["service_id"] == "local-indextts"
    assert manifest["provider_type"] == "indextts"
    assert manifest["source_bundle"] == "deployment/tts-repos/indextts"
    assert manifest["owned_files"] == {"helper.sh": hashlib.sha256(payload).hexdigest()}
    assert len(manifest["source_hash"]) == 64


def test_interrupted_bundle_upgrade_recovers_on_identical_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "a.sh").write_text("a1\n", encoding="utf-8")
    (bundle / "b.sh").write_text("b1\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    (bundle / "a.sh").write_text("a2\n", encoding="utf-8")
    (bundle / "b.sh").write_text("b2\n", encoding="utf-8")
    original = deploy._atomic_write_bytes
    failed = False

    def fail_after_first_bundle_copy(path: Path, payload: bytes, *, boundary: Path, mode: int | None = None) -> None:
        nonlocal failed
        original(path, payload, boundary=boundary, mode=mode)
        if path.name == "a.sh" and not failed:
            failed = True
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(deploy, "_atomic_write_bytes", fail_after_first_bundle_copy)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    pending = target / "tts-more" / "tts-more-install-pending.json"
    assert pending.exists()
    monkeypatch.setattr(deploy, "_atomic_write_bytes", original)

    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    assert not pending.exists()
    assert (target / "tts-more" / "a.sh").read_text(encoding="utf-8") == "a2\n"
    assert (target / "tts-more" / "b.sh").read_text(encoding="utf-8") == "b2\n"


def test_forged_pending_bundle_manifest_cannot_claim_user_files(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "current.sh").write_text("current\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    installed = target / "tts-more"
    installed.mkdir()
    user_file = installed / "user-notes.txt"
    user_file.write_text("preserve\n", encoding="utf-8")
    current_hash = hashlib.sha256(b"current\n").hexdigest()
    desired_manifest = {
        "schema_version": 3,
        "service_id": "local-indextts",
        "provider_type": "indextts",
        "source_bundle": "deployment/tts-repos/indextts",
        "source_hash": deploy._bundle_source_hash({"current.sh": current_hash}),
        "owned_files": {"current.sh": current_hash},
    }
    forged = {
        "schema_version": 1,
        "desired_manifest": desired_manifest,
        "previous_owned_files": {
            "user-notes.txt": hashlib.sha256(b"preserve\n").hexdigest(),
        },
    }
    (installed / "tts-more-install-pending.json").write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(ValueError, match="pending bundle ownership does not match"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    assert user_file.read_text(encoding="utf-8") == "preserve\n"


def test_interrupted_bundle_rerun_rejects_locally_modified_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "new.sh").write_text("managed\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    original = deploy._atomic_write_bytes

    def fail_after_copy(path: Path, payload: bytes, *, boundary: Path, mode: int | None = None) -> None:
        original(path, payload, boundary=boundary, mode=mode)
        if path.name == "new.sh":
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(deploy, "_atomic_write_bytes", fail_after_copy)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    (target / "tts-more" / "new.sh").write_text("local edit\n", encoding="utf-8")
    monkeypatch.setattr(deploy, "_atomic_write_bytes", original)

    with pytest.raises(RuntimeError, match="locally modified owned file"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})


@pytest.mark.skipif(os.name != "nt", reason="native Windows deployment validation")
@pytest.mark.parametrize("powershell", ["powershell.exe", "pwsh.exe"])
def test_windows_native_powershell_launchers_reject_unsafe_remote(
    tmp_path: Path, powershell: str
) -> None:
    executable = shutil.which(powershell)
    assert executable is not None, f"required Windows CI shell is missing: {powershell}"
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    sidecar = target / "tts-more-update.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["remote"] = "ext::cmd /c echo unsafe"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    env = {**os.environ, "TTS_MORE_UPDATE_PYTHON": sys.executable}

    result = subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(target / "tts-more-update.ps1")],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "GitHub remote" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="native Windows deployment validation")
def test_windows_native_drive_junction_and_gitdir_policy(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    absolute = (tmp_path / "repo" / "index-tts").resolve(strict=False)
    assert absolute.drive
    assert deploy._resolve_repo_path(tmp_path, str(absolute)) == absolute
    share_name = f"tts-more-{os.getpid()}"
    share = subprocess.run(
        ["net.exe", "share", f"{share_name}={tmp_path}", "/GRANT:Everyone,FULL"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert share.returncode == 0, share.stderr or share.stdout
    try:
        unc_root = Path(f"\\\\localhost\\{share_name}")
        unc_repo = unc_root / "repo" / "unc-index-tts"
        assert deploy._resolve_repo_path(unc_root, str(unc_repo)) == unc_repo.resolve(strict=False)
    finally:
        removed_share = subprocess.run(
            ["net.exe", "share", share_name, "/delete", "/y"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert removed_share.returncode == 0, removed_share.stderr or removed_share.stdout
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = target / "tts-more"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "helper.ps1").write_text("Write-Host safe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symlink|reparse"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    os.rmdir(junction)
    shutil.rmtree(target / ".git")
    (target / ".git").write_text("gitdir: C:\\outside\\worktree\n", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError), match="worktree|Git metadata"):
        deploy.sync_repos(tmp_path, dry_run=True, service_ids={"local-indextts"})
