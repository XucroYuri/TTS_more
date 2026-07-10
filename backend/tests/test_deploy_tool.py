from __future__ import annotations

import importlib.util
import json
import os
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


def _repository_fixture_with_three_formal_services() -> list[dict[str, object]]:
    return [
        {
            "name": "GPT-SoVITS-main",
            "provider_type": "gpt-sovits",
            "variant": "main",
            "path": "repo/GPT-SoVITS-main",
            "remote": "https://example.invalid/GPT-SoVITS.git",
            "branch": "main",
            "service_id": "local-gpt-sovits-main",
        },
        {
            "name": "index-tts",
            "provider_type": "indextts",
            "path": "repo/index-tts",
            "remote": "https://example.invalid/index-tts.git",
            "branch": "main",
            "service_id": "local-indextts",
        },
        {
            "name": "CosyVoice",
            "provider_type": "cosyvoice",
            "path": "repo/CosyVoice",
            "remote": "https://example.invalid/CosyVoice.git",
            "branch": "main",
            "service_id": "local-cosyvoice",
        },
    ]


def _topology_payload(*, distributed: bool = True) -> dict:
    worker_nodes = {
        "gpt-worker": {
            "role": "worker",
            "host": "tts-gpt.lan" if distributed else "localhost",
            "bind_host": "0.0.0.0" if distributed else "127.0.0.1",
            "services": ["local-gpt-sovits-main"],
            "resource_group": "gpt-worker:cuda-0" if distributed else "cuda-0",
            "capacity": 1,
        },
        "index-worker": {
            "role": "worker",
            "host": "tts-index.lan" if distributed else "localhost",
            "bind_host": "0.0.0.0" if distributed else "127.0.0.1",
            "services": ["local-indextts"],
            "resource_group": "index-worker:cuda-0" if distributed else "cuda-0",
            "capacity": 2 if distributed else 1,
        },
        "cosy-worker": {
            "role": "worker",
            "host": "tts-cosy.lan" if distributed else "localhost",
            "bind_host": "0.0.0.0" if distributed else "127.0.0.1",
            "services": ["local-cosyvoice"],
            "resource_group": "cosy-worker:cuda-0" if distributed else "cuda-0",
            "capacity": 1,
        },
    }
    if not distributed:
        worker_nodes = {
            "gpu-worker": {
                "role": "worker",
                "host": "localhost",
                "bind_host": "127.0.0.1",
                "services": ["local-gpt-sovits-main", "local-indextts", "local-cosyvoice"],
                "resource_group": "cuda-0",
                "capacity": 1,
            }
        }
    return {
        "schema_version": 1,
        "name": "four-node-lan" if distributed else "single-windows",
        "app_node": "app-controller",
        "nodes": {
            "app-controller": {
                "role": "app",
                "host": "tts-app.lan" if distributed else "localhost",
                "bind_host": "127.0.0.1",
                "services": [],
                "resource_group": "app",
                "capacity": 1,
            },
            **worker_nodes,
        },
    }


def _write_topology(root: Path, payload: dict) -> Path:
    path = root / "deployment" / "app" / "topology.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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
    assert services[2]["env"]["TTS_MORE_COSYVOICE_MODEL_DIR"] == "pretrained_models/CosyVoice-300M"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(schema_version=2), "schema_version"),
        (lambda payload: payload.update(app_node="missing"), "app_node"),
        (lambda payload: payload["nodes"]["app-controller"].update(role="worker"), "role app"),
        (lambda payload: payload["nodes"]["gpt-worker"].update(role="gpu"), "role"),
        (lambda payload: payload["nodes"]["gpt-worker"].update(host=""), "host"),
        (lambda payload: payload["nodes"]["gpt-worker"].update(bind_host=""), "bind_host"),
        (lambda payload: payload["nodes"]["gpt-worker"].update(host="localhost"), "non-loopback"),
        (
            lambda payload: payload["nodes"]["index-worker"].update(host="tts-gpt.lan"),
            "distinct host",
        ),
        (lambda payload: payload["nodes"]["gpt-worker"].update(capacity=0), "capacity"),
        (lambda payload: payload["nodes"]["app-controller"].update(services=["local-gpt-sovits-main"]), "app node services must be empty"),
        (lambda payload: payload["nodes"]["gpt-worker"].update(services=[]), "exactly one worker"),
        (
            lambda payload: (
                payload["nodes"]["gpt-worker"]["services"].append("local-indextts"),
                payload["nodes"]["index-worker"].update(services=[]),
            ),
            "distributed worker gpt-worker must own exactly one service",
        ),
        (
            lambda payload: payload["nodes"]["index-worker"]["services"].append("local-gpt-sovits-main"),
            "exactly one worker",
        ),
    ],
)
def test_topology_validation_rejects_invalid_manifests(tmp_path: Path, mutate, message: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    payload = _topology_payload()
    mutate(payload)
    topology_path = _write_topology(tmp_path, payload)

    with pytest.raises(ValueError, match=message):
        deploy.load_topology(
            tmp_path,
            topology_path,
            selected_service_ids={"local-gpt-sovits-main", "local-indextts", "local-cosyvoice"},
        )


def test_render_app_only_uses_each_assigned_worker_endpoint(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    topology_path = _write_topology(tmp_path, _topology_payload())

    services = deploy.render_services(
        tmp_path,
        profile="app-only",
        platform_name="windows",
        topology=topology_path,
        node="app-controller",
    )

    by_id = {service["service_id"]: service for service in services}
    assert by_id["local-gpt-sovits-main"]["base_url"] == "http://tts-gpt.lan:9880"
    assert by_id["local-indextts"]["base_url"] == "http://tts-index.lan:9881"
    assert by_id["local-cosyvoice"]["base_url"] == "http://tts-cosy.lan:9882"
    assert all(service["mode"] == "external" for service in services)
    assert all(service["managed"] is False for service in services)
    assert all(service["network_scope"] == "lan" for service in services)
    assert all(service["source_profile"] == "lan_endpoint" for service in services)
    assert all("artifact-transfer" in service["capabilities"] for service in services)
    assert by_id["local-indextts"]["resource_group"] == "index-worker:cuda-0"
    assert by_id["local-indextts"]["capacity"] == 2


def test_render_worker_node_selects_assignments_and_binds_node_host(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    topology_path = _write_topology(tmp_path, _topology_payload())

    services = deploy.render_services(
        tmp_path,
        profile="worker-node",
        platform_name="windows",
        topology=topology_path,
        node="index-worker",
    )

    assert [service["service_id"] for service in services] == ["local-indextts"]
    service = services[0]
    assert service["base_url"] == "http://tts-index.lan:9881"
    assert service["mode"] == "local"
    assert service["managed"] is True
    assert service["resource_group"] == "index-worker:cuda-0"
    assert service["capacity"] == 2
    assert service["env"]["TTS_MORE_WORKER_ALLOW_PATH_DELIVERY"] == "0"
    host_index = service["start_command"].index("--host")
    assert service["start_command"][host_index + 1] == "0.0.0.0"


def test_render_local_all_single_topology_shares_cuda_resource_group(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    topology_path = _write_topology(tmp_path, _topology_payload(distributed=False))

    services = deploy.render_services(
        tmp_path,
        profile="local-all",
        platform_name="windows",
        topology=topology_path,
    )

    assert [service["service_id"] for service in services] == [
        "local-gpt-sovits-main",
        "local-indextts",
        "local-cosyvoice",
    ]
    assert {service["resource_group"] for service in services} == {"cuda-0"}
    assert {service["capacity"] for service in services} == {1}
    assert {service["env"]["TTS_MORE_WORKER_ALLOW_PATH_DELIVERY"] for service in services} == {"1"}


def test_start_workers_uses_local_profile_for_single_topology_without_node(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    profiles: list[str] = []

    def fake_render_services(*_args, **kwargs):
        profiles.append(kwargs["profile"])
        return []

    monkeypatch.setattr(deploy, "render_services", fake_render_services)

    assert deploy.start_workers(tmp_path, topology="single.local.json", node=None) == 0
    assert deploy.start_workers(tmp_path, topology="four.local.json", node="gpt-worker") == 0
    assert profiles == ["local-all", "worker-node"]


def test_render_services_cli_accepts_topology_and_node(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    topology_path = _write_topology(tmp_path, _topology_payload())
    output = tmp_path / "services.json"

    exit_code = deploy.main(
        [
            "--root",
            str(tmp_path),
            "render-services",
            "--profile",
            "worker-node",
            "--platform",
            "windows",
            "--topology",
            str(topology_path),
            "--node",
            "gpt-worker",
            "--output",
            str(output.relative_to(tmp_path)),
        ]
    )

    assert exit_code == 0
    services = json.loads(output.read_text(encoding="utf-8"))
    assert [service["service_id"] for service in services] == ["local-gpt-sovits-main"]


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


def test_render_without_topology_preserves_local_profile_network_markers(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    services = deploy.render_services(
        tmp_path,
        profile="local-all",
        platform_name="posix",
        host="custom-host.lan",
    )

    assert all(item["source_profile"] == "local_endpoint" for item in services)
    assert all(item["network_scope"] == "localhost" for item in services)


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


def test_clean_sync_removes_readonly_files_from_selected_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    readonly = tmp_path / "repo" / "CosyVoice" / ".git" / "objects" / "pack" / "pack.idx"
    readonly.parent.mkdir(parents=True)
    readonly.write_text("pack", encoding="utf-8")
    readonly.chmod(stat.S_IREAD)
    monkeypatch.setattr(deploy, "_run_clone_with_fallback", lambda *args, **kwargs: None)

    deploy.sync_repos(
        tmp_path,
        clean=True,
        service_ids={"local-cosyvoice"},
        repositories=_repository_fixture_with_three_formal_services(),
    )

    assert (tmp_path / "repo").exists()
    assert not readonly.exists()


def test_clean_sync_preserves_unselected_repo_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
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


def test_clean_sync_dry_run_does_not_delete_selected_repository(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    marker = tmp_path / "repo" / "index-tts" / "checkpoints" / "model.bin"
    marker.parent.mkdir(parents=True)
    marker.write_text("model", encoding="utf-8")

    deploy.sync_repos(
        tmp_path,
        clean=True,
        dry_run=True,
        service_ids={"local-indextts"},
        repositories=_repository_fixture_with_three_formal_services(),
    )

    assert marker.read_text(encoding="utf-8") == "model"


@pytest.mark.parametrize("unsafe_path", [".", "repo"])
def test_clean_sync_refuses_repository_root_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_path: str
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    repositories = _repository_fixture_with_three_formal_services()
    repositories[1] = {**repositories[1], "path": unsafe_path}
    monkeypatch.setattr(deploy, "_run_clone_with_fallback", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="refusing to clean repository root"):
        deploy.sync_repos(
            tmp_path,
            clean=True,
            service_ids={"local-indextts"},
            repositories=repositories,
        )


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


def test_sync_repos_dry_run_uses_shallow_partial_clone(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    actions = deploy.sync_repos(tmp_path, dry_run=True)

    clone = actions[0]
    assert clone[:3] == ["git", "clone", "--depth"]
    assert "1" in clone
    assert "--filter=blob:none" in clone
    assert "--single-branch" in clone
    assert "--branch" in clone


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
    actions: list[list[str]] = []
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
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--branch",
            "main",
            "--single-branch",
            "https://github.com/XucroYuri/GPT-SoVITS.git",
            str(target),
        ]
    ]


def test_sync_repos_preserves_existing_non_git_target_on_clone_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run(command: list[str], cwd: Path) -> None:
        if command[:2] == ["git", "clone"] and "--filter=blob:none" in command:
            raise deploy.subprocess.CalledProcessError(128, command)
        if command[:2] == ["git", "clone"]:
            (Path(command[-1]) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(deploy, "_run_git_command", fake_run)
    monkeypatch.setattr(deploy, "_git_output", lambda command: "bf81cdb14a38b674b6e9996dabc97340bc9978d2")

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

    assert fetch_command in dry_actions
    assert dry_actions.index(fetch_command) < dry_actions.index(checkout_command)
    assert fetch_command in calls
    assert calls.index(fetch_command) < calls.index(checkout_command)


def test_sync_repos_dry_run_skips_locked_commit_actions_when_head_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "GPT-SoVITS-main"
    commit = "bf81cdb14a38b674b6e9996dabc97340bc9978d2"
    (target / ".git").mkdir(parents=True)
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
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(deploy, "_git_output", lambda command: commit if command[-2:] == ["rev-parse", "HEAD"] else "")

    actions = deploy.sync_repos(tmp_path, dry_run=True)

    fetch_command = ["git", "-C", str(target), "fetch", "origin", commit]
    checkout_command = ["git", "-C", str(target), "checkout", commit]

    assert fetch_command not in actions
    assert checkout_command not in actions


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
    assert actions[0][:2] == ["git", "clone"]
    assert "index-tts" in actions[0][-1]
    assert not any(command[-2:-1] == ["fetch"] for command in actions)


def test_install_update_scripts_writes_repo_local_helpers(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "index-tts"
    (target / ".git").mkdir(parents=True)

    reports = deploy.install_update_scripts(tmp_path, service_ids={"local-indextts"})

    sh_path = target / "tts-more-update.sh"
    ps1_path = target / "tts-more-update.ps1"
    assert reports == [
        {
            "name": "index-tts",
            "path": "repo/index-tts",
            "exists": True,
            "scripts": ["repo/index-tts/tts-more-update.sh", "repo/index-tts/tts-more-update.ps1"],
        }
    ]
    assert "git pull --ff-only origin" in sh_path.read_text(encoding="utf-8")
    assert "git pull --ff-only origin" in ps1_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert sh_path.stat().st_mode & stat.S_IXUSR
    exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "tts-more-update.sh" in exclude
    assert "tts-more-update.ps1" in exclude


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
    (target / ".git").mkdir(parents=True)

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
    assert any("CosyVoice" in command[-1] for command in payload["repo_actions"] if command[:2] == ["git", "clone"])
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
    (target / ".git").mkdir(parents=True)

    def fake_git_output(command: list[str]) -> str:
        if command[-2:] == ["status", "--porcelain"]:
            return " M local_patch.py"
        if command[-2:] == ["branch", "--show-current"]:
            return "master"
        return ""

    monkeypatch.setattr(deploy, "_git_output", fake_git_output)

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
    (target / ".git").mkdir(parents=True)
    monkeypatch.setattr(deploy, "_git_output", lambda command: " M local_patch.py" if command[-2:] == ["status", "--porcelain"] else "")

    payload = deploy.update_project(
        tmp_path,
        skip_app=True,
        dry_run=True,
        service_ids={"local-indextts"},
        force_reset_repos=True,
    )

    assert ["git", "-C", str(target), "reset", "--hard", "origin/main"] in payload["repo_actions"]
