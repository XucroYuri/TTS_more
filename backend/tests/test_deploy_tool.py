from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _write_complete_repo_paths(root: Path) -> Path:
    path = root / "repo-paths.json"
    path.write_text(
        json.dumps(
            {
                "repositories": {
                    "local-gpt-sovits-main": "repo/GPT-SoVITS-main",
                    "local-indextts": "repo/index-tts",
                    "local-cosyvoice": "repo/CosyVoice",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _init_git_checkout(path: Path, remote: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    tracked = path / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=Deployment Tests",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)


def _commit_index(path: Path, message: str) -> str:
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=Deployment Tests",
            "commit",
            "-qm",
            message,
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
def _repository_fixture_with_three_formal_services() -> list[dict[str, object]]:
    return [
        {
            "name": "GPT-SoVITS-main",
            "provider_type": "gpt-sovits",
            "variant": "main",
            "path": "repo/GPT-SoVITS-main",
            "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
            "branch": "main",
            "service_id": "local-gpt-sovits-main",
        },
        {
            "name": "index-tts",
            "provider_type": "indextts",
            "path": "repo/index-tts",
            "remote": "https://github.com/XucroYuri/index-tts.git",
            "branch": "main",
            "service_id": "local-indextts",
        },
        {
            "name": "CosyVoice",
            "provider_type": "cosyvoice",
            "path": "repo/CosyVoice",
            "remote": "https://github.com/XucroYuri/CosyVoice.git",
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
    assert (
        services[2]["env"]["TTS_MORE_COSYVOICE_MODEL_DIR"]
        == "repo/CosyVoice/pretrained_models/CosyVoice-300M"
    )


def test_cosyvoice_worker_env_qualifies_repo_relative_model_dir() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    repo = {
        "provider_type": "cosyvoice",
        "path": "vendors/custom-cosyvoice",
        "model_dir": "models/CosyVoice-300M",
    }

    env = deploy._worker_env(repo, "windows")

    assert (
        env["TTS_MORE_COSYVOICE_MODEL_DIR"]
        == "vendors/custom-cosyvoice/models/CosyVoice-300M"
    )


@pytest.mark.parametrize(
    ("platform_name", "model_dir"),
    [
        ("windows", r"D:\Models\CosyVoice-300M"),
        ("posix", "/srv/models/CosyVoice-300M"),
    ],
)
def test_cosyvoice_worker_env_preserves_target_absolute_model_dir(
    platform_name: str, model_dir: str
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    repo = {
        "provider_type": "cosyvoice",
        "path": "repo/CosyVoice",
        "model_dir": model_dir,
    }

    env = deploy._worker_env(repo, platform_name)

    assert env["TTS_MORE_COSYVOICE_MODEL_DIR"] == model_dir


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


def test_start_workers_writes_run_local_owned_process_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    executable = tmp_path / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    services = [
        {
            "service_id": "local-gpt-sovits-main",
            "health_url": "http://127.0.0.1:9880/health",
            "start_command": [
                str(executable),
                "-m",
                "uvicorn",
                "app.workers.gpt_sovits_worker:app",
            ],
            "env": {},
        }
    ]

    class FakeProcess:
        pid = 4321

        def terminate(self) -> None:
            raise AssertionError("tracked process must not be terminated")

    monkeypatch.setattr(deploy, "render_services", lambda *_args, **_kwargs: services)
    monkeypatch.setattr(deploy, "_resolve_command", lambda _root, command: command)
    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(deploy, "_windows_process_creation_date", lambda _pid: "20260711010000.000000+480")
    monkeypatch.setattr(deploy, "_git_output", lambda _args: "a" * 40)
    manifest_path = tmp_path / "artifacts" / "run-processes.json"

    result = deploy.start_workers(
        tmp_path,
        platform_name="windows",
        detach=True,
        pid_manifest=manifest_path,
    )

    assert result == 0
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "processes": [
            {
                "pid": 4321,
                "creation_date": "20260711010000.000000+480",
                "executable_path": str(executable.resolve()),
                "project_root": str(tmp_path.resolve()),
                "worker_module": "app.workers.gpt_sovits_worker:app",
                "service_id": "local-gpt-sovits-main",
            }
        ],
    }


def test_start_workers_cli_forwards_pid_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    observed: dict[str, object] = {}

    def fake_start_workers(_root: Path, **kwargs) -> int:
        observed.update(kwargs)
        return 0

    monkeypatch.setattr(deploy, "start_workers", fake_start_workers)
    _write_repo_lock(tmp_path)
    repo_paths = _write_complete_repo_paths(tmp_path)

    exit_code = deploy.main(
        [
            "--root",
            str(tmp_path),
            "start-workers",
            "--platform",
            "windows",
            "--detach",
            "--pid-manifest",
            "artifacts/run-processes.json",
            "--repo-paths",
            str(repo_paths),
        ]
    )

    assert exit_code == 0
    assert observed["pid_manifest"] == "artifacts/run-processes.json"
    assert observed["detach"] is True


def test_render_services_cli_accepts_topology_and_node(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    topology_path = _write_topology(tmp_path, _topology_payload())
    repo_paths = _write_complete_repo_paths(tmp_path)
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
            "--repo-paths",
            str(repo_paths),
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
    target = tmp_path / "repo" / "CosyVoice"
    _init_git_checkout(target, "https://github.com/XucroYuri/CosyVoice.git")
    readonly = target / ".git" / "objects" / "pack" / "pack.idx"
    readonly.parent.mkdir(parents=True, exist_ok=True)
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
    _init_git_checkout(selected, "https://github.com/XucroYuri/index-tts.git")
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
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    marker = target / "checkpoints" / "model.bin"
    marker.parent.mkdir(parents=True)
    marker.write_text("model", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "checkpoints/model.bin"], check=True)
    _commit_index(target, "add model marker")

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

    with pytest.raises(ValueError, match="dedicated repository area|below dedicated repository area"):
        deploy.sync_repos(
            tmp_path,
            clean=True,
            service_ids={"local-indextts"},
            repositories=repositories,
        )


@pytest.mark.parametrize("unsafe_path", [".", "repo"])
def test_clean_sync_validates_all_selected_paths_before_deleting_any(
    tmp_path: Path, unsafe_path: str
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    formal_repositories = _repository_fixture_with_three_formal_services()
    repositories = [
        formal_repositories[1],
        {**formal_repositories[2], "path": unsafe_path},
    ]
    marker = tmp_path / "repo" / "index-tts" / "checkpoints" / "model.bin"
    marker.parent.mkdir(parents=True)
    marker.write_text("model", encoding="utf-8")

    with pytest.raises(ValueError, match="dedicated repository area|below dedicated repository area"):
        deploy.sync_repos(
            tmp_path,
            clean=True,
            service_ids={"local-indextts", "local-cosyvoice"},
            repositories=repositories,
        )

    assert marker.read_text(encoding="utf-8") == "model"


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


def test_sync_repos_dry_run_uses_shallow_full_clone(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    actions = deploy.sync_repos(tmp_path, dry_run=True)

    clone = actions[0]["argv"]
    assert clone[:3] == ["git", "clone", "--depth"]
    assert "1" in clone
    assert "--filter=blob:none" not in clone
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


def test_sync_repos_uses_one_full_clone_before_pinned_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path) -> None:
        calls.append(command)
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

    clone_calls = [command for command in calls if command[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1
    assert "--filter=blob:none" not in clone_calls[0]
    assert any(command[:4] == ["git", "-C", str(tmp_path / "repo" / "GPT-SoVITS-main"), "fetch"] for command in calls)


def test_run_clone_records_one_portable_full_clone_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    actions: list[dict[str, object]] = []
    target = tmp_path / "repo" / "GPT-SoVITS-main"

    monkeypatch.setattr(deploy, "_run_git_command", lambda command, *, cwd: None)

    deploy._run_clone(
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


def test_gitmodules_parser_resolves_relative_urls_from_validated_origin(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "superproject"
    target.mkdir()
    (target / ".gitmodules").write_text(
        """[submodule "https-child"]
\tpath = deps/https-child
\turl = ../https-child.git
[submodule "third_party/Matcha-TTS"]
\tpath = third_party/Matcha-TTS
\turl = git@github.com:example/ssh-child.git
""",
        encoding="utf-8",
    )

    assert deploy._load_validated_submodules(
        target,
        "https://github.com/example/superproject.git",
    ) == [
        {
            "name": "https-child",
            "path": "deps/https-child",
            "url": "https://github.com/example/https-child.git",
        },
        {
            "name": "third_party/Matcha-TTS",
            "path": "third_party/Matcha-TTS",
            "url": "git@github.com:example/ssh-child.git",
        },
    ]


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        (
            """[submodule "child"]
path = deps/one
url = https://github.com/example/one.git
[submodule "CHILD"]
path = deps/two
url = https://github.com/example/two.git
""",
            "duplicate submodule name",
        ),
        (
            """[submodule "child"]
path = deps/child
url = https://github.com/example/child.git
update = !marker-command
""",
            "unknown .gitmodules key",
        ),
        (
            """[submodule "child"]
path = ../outside
url = https://github.com/example/child.git
""",
            "unsafe submodule path",
        ),
        (
            """[submodule "child"]
path = deps/child
url = file:///tmp/child.git
""",
            "unsupported GitHub remote",
        ),
        (
            """[submodule "one"]
path = deps/child
url = https://github.com/example/one.git
[submodule "two"]
path = DEPS/CHILD
url = https://github.com/example/two.git
""",
            "duplicate submodule path",
        ),
    ],
)
def test_gitmodules_parser_rejects_duplicate_unknown_or_unsafe_metadata(
    tmp_path: Path,
    contents: str,
    error: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "superproject"
    target.mkdir()
    (target / ".gitmodules").write_text(contents, encoding="utf-8")

    with pytest.raises((RuntimeError, ValueError), match=error):
        deploy._load_validated_submodules(
            target,
            "https://github.com/example/superproject.git",
        )


def _write_nested_submodule_config(
    superproject: Path,
    child: Path,
    remote: str,
    extra: str = "",
) -> None:
    git_dir = superproject / ".git" / "modules" / "deps" / "child"
    git_dir.mkdir(parents=True)
    child.mkdir(parents=True)
    (child / ".git").write_text(
        f"gitdir: {os.path.relpath(git_dir, child)}\n",
        encoding="utf-8",
    )
    worktree = os.path.relpath(child, git_dir)
    (git_dir / "config").write_text(
        f"""[core]
repositoryformatversion = 0
filemode = true
bare = false
logallrefupdates = true
worktree = {worktree}
[remote "origin"]
url = {remote}
fetch = +refs/heads/*:refs/remotes/origin/*
{extra}""",
        encoding="utf-8",
    )


def test_nested_submodule_gitdir_audit_returns_validated_actual_origin(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    superproject = tmp_path / "superproject"
    child = superproject / "deps" / "child"
    actual_remote = "git@github.com:example/child.git"
    _write_nested_submodule_config(superproject, child, actual_remote)

    assert deploy._audit_nested_submodule_config(
        child,
        superproject,
        "https://github.com/example/child.git",
    ) == actual_remote


def test_nested_submodule_gitdir_audit_rejects_url_rewrite(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    superproject = tmp_path / "superproject"
    child = superproject / "deps" / "child"
    marker_remote = "https://github.com/example/child.git"
    _write_nested_submodule_config(
        superproject,
        child,
        marker_remote,
        """[url "https://attacker.invalid/"]
insteadOf = https://github.com/
""",
    )

    with pytest.raises(RuntimeError, match="not allowlisted"):
        deploy._audit_nested_submodule_config(child, superproject, marker_remote)


@pytest.mark.parametrize(
    ("latest", "expected_remote"),
    [
        (False, "https://github.com/example/locked-child.git"),
        (True, "git@github.com:example/tip-child.git"),
    ],
)
def test_sync_repos_updates_submodules_only_after_final_superproject_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    latest: bool,
    expected_remote: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "superproject"
    child_source = tmp_path / "child-source"
    super_remote = "https://github.com/example/superproject.git"
    _init_git_checkout(child_source, "https://github.com/example/child-source.git")
    locked_child = subprocess.run(
        ["git", "-C", str(child_source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (child_source / "tracked.txt").write_text("tip\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(child_source), "add", "tracked.txt"], check=True)
    tip_child = _commit_index(child_source, "tip child")

    _init_git_checkout(target, super_remote)
    subprocess.run(["git", "-C", str(target), "branch", "-M", "main"], check=True)
    (target / ".gitmodules").write_text(
        """[submodule "child"]
\tpath = deps/child
\turl = ../locked-child.git
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(target), "add", ".gitmodules"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{locked_child},deps/child",
        ],
        check=True,
    )
    locked_super = _commit_index(target, "locked superproject")
    (target / ".gitmodules").write_text(
        """[submodule "child"]
\tpath = deps/child
\turl = git@github.com:example/tip-child.git
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(target), "add", ".gitmodules"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "update-index",
            "--cacheinfo",
            f"160000,{tip_child},deps/child",
        ],
        check=True,
    )
    tip_super = _commit_index(target, "tip superproject")
    calls: list[tuple[list[str], tuple[str, ...] | None]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        validated_submodule_remotes: tuple[str, ...] | None = None,
    ) -> None:
        calls.append((command, validated_submodule_remotes))
        if "submodule" in command:
            gitlink = subprocess.run(
                ["git", "-C", str(target), "ls-tree", "HEAD", "deps/child"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()[2]
            child_target = target / "deps" / "child"
            if child_target.exists():
                shutil.rmtree(child_target)
            child_target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", str(child_source), str(child_target)], check=True)
            subprocess.run(["git", "-C", str(child_target), "checkout", "-q", gitlink], check=True)
            assert validated_submodule_remotes is not None
            subprocess.run(
                ["git", "-C", str(child_target), "remote", "set-url", "origin", validated_submodule_remotes[0]],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "submodule", "absorbgitdirs", "--", "deps/child"],
                check=True,
            )
        elif "checkout" in command:
            subprocess.run(command, cwd=cwd, check=True, capture_output=True)

    monkeypatch.setattr(deploy, "_run_git_command", fake_run)
    repositories = [
        {
            "name": "superproject",
            "provider_type": "cosyvoice",
            "path": "repo/superproject",
            "remote": super_remote,
            "branch": "main",
            "commit": locked_super,
            "service_id": "local-superproject",
            "default_selected": True,
            "submodules": True,
        }
    ]

    deploy.sync_repos(
        tmp_path,
        latest=latest,
        force_reset=True,
        repositories=repositories,
    )

    expected_super = tip_super if latest else locked_super
    expected_child = tip_child if latest else locked_child
    assert subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == expected_super
    assert subprocess.run(
        ["git", "-C", str(target / "deps" / "child"), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == expected_child
    submodule_call = next(item for item in calls if "submodule" in item[0])
    assert submodule_call[1] == (expected_remote,)
    final_selection = (
        ["git", "-C", str(target), "reset", "--hard", "origin/main"]
        if latest
        else ["git", "-C", str(target), "checkout", expected_super]
    )
    command_calls = [item[0] for item in calls]
    assert command_calls.index(final_selection) < command_calls.index(submodule_call[0])


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


def test_doctor_reports_missing_gpt_worker_prerequisites(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    monkeypatch.setattr(deploy, "_platform_name", lambda: "windows")
    monkeypatch.setattr(deploy.shutil, "which", lambda _name: None)
    _write_repo_lock(tmp_path)
    gpt_repo = tmp_path / "repo" / "GPT-SoVITS-main"
    _init_git_checkout(gpt_repo, "https://github.com/XucroYuri/GPT-SoVITS.git")
    (gpt_repo / "GPT_SoVITS").mkdir(parents=True)
    metadata = gpt_repo / ".venv" / "Lib" / "site-packages" / "onnxruntime_gpu-1.27.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nVersion: 1.27.0\n", encoding="utf-8")

    report = deploy.doctor(tmp_path)
    gpt_report = next(item for item in report["repositories"] if item["name"] == "GPT-SoVITS-main")

    prerequisites = gpt_report["worker_prerequisites"]
    assert prerequisites["ready"] is False
    assert {check["id"] for check in prerequisites["checks"]} >= {
        "gpt_package_dir",
        "ffmpeg_shared_dll",
        "conda_executable",
        "onnxruntime_cuda12_compatible",
    }
    onnxruntime_check = next(
        check for check in prerequisites["checks"] if check["id"] == "onnxruntime_cuda12_compatible"
    )
    assert onnxruntime_check["passed"] is False
    assert "1.27.0" in onnxruntime_check["message"]
    assert "prepare-tts-repos.ps1" in prerequisites["next_action"]
    assert "Install Conda" in prerequisites["next_action"]


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


def test_install_update_scripts_reports_submodule_repo_as_managed_sync_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    target = tmp_path / "repo" / "CosyVoice"
    _init_git_checkout(target, "https://github.com/XucroYuri/CosyVoice.git")
    repo_paths = tmp_path / "repo-paths.json"
    repo_paths.write_text(
        json.dumps({"repositories": {"local-cosyvoice": "repo/CosyVoice"}}),
        encoding="utf-8",
    )

    result = deploy.main(
        [
            "--root",
            str(tmp_path),
            "install-update-scripts",
            "--service-ids",
            "local-cosyvoice",
            "--repo-paths",
            str(repo_paths),
        ]
    )

    reports = json.loads(capsys.readouterr().out)
    assert result == 0
    assert reports == [
        {
            "name": "CosyVoice",
            "path": "repo/CosyVoice",
            "exists": True,
            "standalone_updater": False,
            "managed_sync_required": True,
            "message": (
                "submodule repositories must be updated from TTS More managed sync-repos; "
                "the standalone updater is not installed"
            ),
            "scripts": [],
            "actions": [],
        }
    ]
    for name in (
        "tts-more-update.sh",
        "tts-more-update.ps1",
        "tts-more-update.py",
        "tts-more-update.json",
    ):
        assert not (target / name).exists()


def test_manifest_repo_path_overrides_apply_to_rendered_local_services(tmp_path: Path) -> None:
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


def test_repository_loader_rejects_paths_outside_project(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repo_paths = tmp_path / "deployment" / "app" / "repo-paths.local.json"
    repo_paths.parent.mkdir(parents=True)
    repo_paths.write_text(
        json.dumps({"repositories": {"local-indextts": "../outside-index-tts"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside project root"):
        deploy.load_deployment_repositories(tmp_path, repo_paths=repo_paths)


def test_install_repo_bundles_owns_provider_helpers_and_excludes_them(tmp_path: Path) -> None:
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

    repositories = deploy.load_repo_lock(tmp_path)
    deploy.apply_repo_path_overrides(repositories, {"local-indextts": "../outside-index-tts"})
    report = deploy.validate_repo_paths(
        tmp_path,
        service_ids={"local-indextts"},
        repositories=repositories,
    )

    assert report[0]["ok"] is False
    assert report[0]["inside_project"] is False
    assert "outside project root" in report[0]["error"]


def test_install_repo_bundles_copies_portable_launchers_and_excludes_them(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "tts-more-prepare.sh").write_text("#!/usr/bin/env bash\necho prepare\n", encoding="utf-8")
    (bundle / "README.md").write_text("IndexTTS helper\n", encoding="utf-8")
    launchers = bundle / "launchers"
    launchers.mkdir()
    (launchers / "Start.cmd").write_text("@echo off\nstart\n", encoding="utf-8")
    (launchers / "Stop.cmd").write_text("@echo off\nstop\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")

    reports = deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    copied = target / "tts-more" / "tts-more-prepare.sh"
    manifest = json.loads((target / "tts-more" / "tts-more-repo.json").read_text(encoding="utf-8"))
    assert reports[0]["installed"] is True
    assert reports[0]["launchers"] == ["repo/index-tts/Start.cmd", "repo/index-tts/Stop.cmd"]
    assert copied.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert (target / "Start.cmd").read_text(encoding="utf-8").splitlines() == ["@echo off", "start"]
    assert (target / "Stop.cmd").read_text(encoding="utf-8").splitlines() == ["@echo off", "stop"]
    if os.name != "nt":
        assert copied.stat().st_mode & stat.S_IXUSR
    assert manifest["service_id"] == "local-indextts"
    exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "tts-more/" in exclude
    assert "Start.cmd" in exclude
    assert "Stop.cmd" in exclude


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

    with pytest.raises(ValueError, match="dedicated repository area"):
        deploy.load_deployment_repositories(
            tmp_path,
            repo_paths,
            service_ids={"local-indextts"},
            require_complete=True,
        )


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


@pytest.mark.parametrize(
    "command",
    [
        ["sync-repos", "--latest", "--write-lock"],
        [
            "update",
            "--skip-app",
            "--latest-repos",
            "--write-lock",
            "--no-install-scripts",
            "--no-render",
        ],
    ],
    ids=("sync-repos", "update"),
)
def test_cli_write_lock_updates_only_pristine_manifest_commit_with_confirmed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    lock_path = tmp_path / "repo.lock.json"
    before = json.loads(lock_path.read_text(encoding="utf-8"))
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repo_paths = tmp_path / "repo-paths.json"
    repo_paths.write_text(
        json.dumps({"repositories": {"local-indextts": str(target)}}),
        encoding="utf-8",
    )
    new_commit = "1" * 40
    original_git_output = deploy._git_output

    def fake_git_output(argv: list[str]) -> str:
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return new_commit
        return original_git_output(argv)

    monkeypatch.setattr(deploy, "_git_output", fake_git_output)
    monkeypatch.setattr(deploy, "_run_git_command", lambda *args, **kwargs: None)

    result = deploy.main(
        [
            "--root",
            str(tmp_path),
            *command,
            "--service-ids",
            "local-indextts",
            "--repo-paths",
            str(repo_paths),
        ]
    )

    after = json.loads(lock_path.read_text(encoding="utf-8"))
    assert result == 0
    expected = json.loads(json.dumps(before))
    selected = next(
        repo for repo in expected["repositories"] if repo["service_id"] == "local-indextts"
    )
    selected["commit"] = new_commit
    assert after == expected
    serialized = lock_path.read_text(encoding="utf-8")
    for forbidden in ("path_source", "path_confirmed", str(target)):
        assert forbidden not in serialized


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
    deploy._remove_path(target / ".git")
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


def _write_marker_command(path: Path, marker: Path) -> str:
    path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed\\n', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{path}"'


def _set_local_git_config(path: Path, key: str, value: str) -> None:
    subprocess.run(["git", "-C", str(path), "config", "--local", key, value], check=True)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.alternateRefsCommand", "marker-command"),
        ("http.curloptResolve", "+github.com:443:127.0.0.1"),
        ("http.sslCAInfo", "attacker-ca.pem"),
        ("http.sslCAPath", "attacker-ca"),
        ("diff.external", "marker-command"),
        ("gc.recentObjectsHook", "marker-command"),
        ("maintenance.strategy", "incremental"),
        ("merge.tool", "marker-command"),
        ("filter.attack.clean", "marker-command"),
        ("submodule.attack.url", "https://github.com/attacker/repo.git"),
        ("remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/attacker"),
        ("remote.origin.promisor", "true"),
        ("remote.origin.partialCloneFilter", "blob:none"),
        ("extensions.partialClone", "origin"),
        ("unknown.setting", "value"),
    ],
)
def test_local_git_config_audit_rejects_every_non_allowlisted_key_without_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    _set_local_git_config(target, key, value)
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Git executed during config audit")),
    )

    with pytest.raises(RuntimeError, match=r"local Git config key is not allowlisted.*" + re.escape(key)):
        deploy._audit_local_git_config(target, environment={})


def test_app_rejects_alternate_refs_command_without_executing_marker(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    marker = tmp_path / "app-alternate-refs-marker"
    command = _write_marker_command(tmp_path / "app-alternate-refs.py", marker)
    _set_local_git_config(target, "core.alternateRefsCommand", command)

    with pytest.raises(RuntimeError, match="core.alternateRefsCommand"):
        deploy._validate_git_checkout(target)

    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX marker executable fixture")
def test_app_git_resolution_ignores_checkout_and_relative_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    marker = tmp_path / "checkout-git-marker"
    fake_git = target / "git"
    fake_git.write_text(f"#!/bin/sh\nprintf executed > {marker!s}\nexit 1\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{target}{os.pathsep}{os.pathsep}relative-bin")

    deploy._validate_git_checkout(target)

    assert not marker.exists()


def test_trusted_git_resolution_never_searches_cwd_on_any_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_git = tmp_path / ("git.exe" if os.name == "nt" else "git")
    fake_git.write_text("checkout-controlled executable\n", encoding="utf-8")
    if os.name != "nt":
        fake_git.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f".{os.pathsep}{os.pathsep}relative-bin")

    executable = Path(deploy._trusted_git_executable(managed_roots=(tmp_path,)))

    assert executable.is_absolute()
    assert executable != fake_git
    assert not executable.resolve(strict=True).is_relative_to(tmp_path.resolve(strict=True))


def test_windows_trusted_candidates_use_system_api_without_path_or_cwd_lookup() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    source = (repo_root / "scripts" / "tts_more_deploy.py").read_text(encoding="utf-8")
    updater = deploy._service_update_script_py()

    assert source.count("GetWindowsDirectoryW") >= 2
    assert "GetWindowsDirectoryW" in updater
    for content in (source, updater):
        assert 'shutil.which("git")' not in content
        assert 'shutil.which("ssh")' not in content
        assert 'os.environ.get("PATH")' not in content


def test_update_sidecar_uses_exact_portable_https_policy_without_host_paths(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"

    deploy.install_update_scripts(tmp_path, repositories=repositories)

    sidecar = json.loads((target / "tts-more-update.json").read_text(encoding="utf-8"))
    assert sidecar == {
        "schema_version": 3,
        "executable_policy": "fixed-dirs-or-explicit-env-v1",
        "requires_ssh": False,
        "service_id": "local-indextts",
        "name": "index-tts",
        "remote": "https://github.com/XucroYuri/index-tts.git",
        "branch": "main",
        "commit": "7264ce2a9a0924becb6b8da3f60725f7663de089",
    }


def test_generated_updater_rejects_tampered_portable_policy_before_git(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    marker = tmp_path / "fake-git-marker"
    fake_git = target / ("git.exe" if os.name == "nt" else "git")
    fake_git.write_text("checkout-controlled executable\n", encoding="utf-8")
    sidecar_path = target / "tts-more-update.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["executable_policy"] = "trust-sidecar-path-v0"
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    (target / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    env = {**os.environ, "PATH": f"{target}{os.pathsep}{os.pathsep}relative-bin"}

    result = subprocess.run(
        [sys.executable, str(target / "tts-more-update.py")],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "updater executable policy" in result.stderr
    assert not marker.exists()


def test_generated_updater_rejects_requires_ssh_mismatch_before_git(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    sidecar_path = target / "tts-more-update.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["requires_ssh"] = True
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    (target / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(target / "tts-more-update.py")],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "requires_ssh does not match remote" in result.stderr


def test_install_https_updater_does_not_resolve_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    monkeypatch.setattr(
        deploy,
        "_trusted_ssh_executable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("HTTPS resolved SSH")),
    )

    deploy.install_update_scripts(tmp_path, repositories=repositories)

    sidecar = json.loads((target / "tts-more-update.json").read_text(encoding="utf-8"))
    assert sidecar["requires_ssh"] is False


def test_generated_https_updater_resolves_only_current_host_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    (target / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    source = (target / "tts-more-update.py").read_text(encoding="utf-8")
    namespace = {"__name__": "portable_https_test", "__file__": str(target / "tts-more-update.py")}
    exec(compile(source, str(target / "tts-more-update.py"), "exec"), namespace)
    calls: list[str] = []
    trusted_git = deploy._trusted_git_executable(managed_roots=(target,))

    def resolve(name: str, *, root: Path, git_executable: Path | None = None) -> str:
        calls.append(name)
        if name == "ssh":
            raise AssertionError("HTTPS updater resolved SSH")
        return trusted_git

    monkeypatch.setitem(namespace, "resolve_trusted_executable", resolve)

    with pytest.raises(RuntimeError, match="dirty repository"):
        namespace["main"]([])

    assert calls == ["git"]


def test_generated_ssh_updater_requires_current_host_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    ssh_remote = "git@github.com:XucroYuri/index-tts.git"
    _init_git_checkout(target, ssh_remote)
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    repositories[0]["remote"] = ssh_remote
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    sidecar = json.loads((target / "tts-more-update.json").read_text(encoding="utf-8"))
    assert sidecar["requires_ssh"] is True
    source = (target / "tts-more-update.py").read_text(encoding="utf-8")
    namespace = {"__name__": "portable_ssh_test", "__file__": str(target / "tts-more-update.py")}
    exec(compile(source, str(target / "tts-more-update.py"), "exec"), namespace)
    calls: list[str] = []
    trusted_git = deploy._trusted_git_executable(managed_roots=(target,))

    def resolve(name: str, *, root: Path, git_executable: Path | None = None) -> str:
        calls.append(name)
        if name == "ssh":
            raise RuntimeError("trusted SSH executable is required")
        return trusted_git

    monkeypatch.setitem(namespace, "resolve_trusted_executable", resolve)

    with pytest.raises(RuntimeError, match="trusted SSH executable is required"):
        namespace["main"]([])

    assert calls == ["git", "ssh"]


@pytest.mark.parametrize(
    ("expected_remote", "actual_remote", "expected_resolvers"),
    [
        (
            "https://github.com/XucroYuri/index-tts.git",
            "git@github.com:XucroYuri/index-tts.git",
            ["git", "ssh"],
        ),
        (
            "git@github.com:XucroYuri/index-tts.git",
            "https://github.com/XucroYuri/index-tts.git",
            ["git"],
        ),
    ],
)
def test_generated_updater_selects_ssh_from_actual_origin_after_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_remote: str,
    actual_remote: str,
    expected_resolvers: list[str],
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "index-tts"
    target.mkdir()
    updater_path = target / "tts-more-update.py"
    source = deploy._service_update_script_py()
    updater_path.write_text(source, encoding="utf-8")
    (target / "tts-more-update.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "executable_policy": "fixed-dirs-or-explicit-env-v1",
                "requires_ssh": deploy._github_remote_requires_ssh(expected_remote),
                "service_id": "local-indextts",
                "name": "index-tts",
                "remote": expected_remote,
                "branch": "main",
                "commit": "",
            }
        ),
        encoding="utf-8",
    )
    namespace = {"__name__": "actual_transport_test", "__file__": str(updater_path)}
    exec(compile(source, str(updater_path), "exec"), namespace)
    events: list[tuple[object, ...]] = []

    def resolve(name: str, *, root: Path, git_executable: Path | None = None) -> str:
        events.append(("resolve", name))
        return f"/trusted/{name}"

    def validate(root: Path, git_executable: str, ssh_executable: str | None) -> None:
        events.append(("validate", ssh_executable))

    def output(
        args: list[str],
        root: Path,
        git_executable: str,
        ssh_executable: str | None,
    ) -> str:
        events.append(("output", tuple(args[1:]), ssh_executable))
        if args[1:] == ["remote", "get-url", "origin"]:
            return actual_remote
        if args[1:] == ["status", "--porcelain"]:
            return "dirty"
        raise AssertionError(f"unexpected updater output command: {args!r}")

    monkeypatch.setitem(namespace, "resolve_trusted_executable", resolve)
    monkeypatch.setitem(namespace, "validate_git_checkout", validate)
    monkeypatch.setitem(namespace, "output", output)

    with pytest.raises(RuntimeError, match="dirty repository"):
        namespace["main"]([])

    expected_ssh = "/trusted/ssh" if expected_resolvers[-1] == "ssh" else None
    assert [event[1] for event in events if event[0] == "resolve"] == expected_resolvers
    assert events == [
        ("resolve", "git"),
        ("validate", None),
        ("output", ("remote", "get-url", "origin"), None),
        *([("resolve", "ssh")] if expected_ssh else []),
        ("output", ("status", "--porcelain"), expected_ssh),
    ]


def test_generated_updater_rejects_origin_identity_before_resolving_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "index-tts"
    target.mkdir()
    updater_path = target / "tts-more-update.py"
    source = deploy._service_update_script_py()
    updater_path.write_text(source, encoding="utf-8")
    (target / "tts-more-update.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "executable_policy": "fixed-dirs-or-explicit-env-v1",
                "requires_ssh": False,
                "service_id": "local-indextts",
                "name": "index-tts",
                "remote": "https://github.com/XucroYuri/index-tts.git",
                "branch": "main",
                "commit": "",
            }
        ),
        encoding="utf-8",
    )
    namespace = {"__name__": "identity_before_transport_test", "__file__": str(updater_path)}
    exec(compile(source, str(updater_path), "exec"), namespace)
    resolvers: list[str] = []

    def resolve(name: str, *, root: Path, git_executable: Path | None = None) -> str:
        resolvers.append(name)
        if name == "ssh":
            raise AssertionError("SSH resolved before origin identity validation")
        return "/trusted/git"

    monkeypatch.setitem(namespace, "resolve_trusted_executable", resolve)
    monkeypatch.setitem(namespace, "validate_git_checkout", lambda root, git, ssh: None)
    monkeypatch.setitem(
        namespace,
        "output",
        lambda args, root, git, ssh: "git@github.com:attacker/other.git",
    )

    with pytest.raises(RuntimeError, match="origin mismatch"):
        namespace["main"]([])

    assert resolvers == ["git"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX destination-prefix executable fixture")
def test_copied_updater_resolves_git_from_destination_prefix(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    source_target = tmp_path / "repo" / "source-index-tts"
    destination = tmp_path / "repo" / "destination-index-tts"
    remote = "https://github.com/XucroYuri/index-tts.git"
    _init_git_checkout(source_target, remote)
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/source-index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    _init_git_checkout(destination, remote)
    for name in ("tts-more-update.sh", "tts-more-update.ps1", "tts-more-update.py", "tts-more-update.json"):
        shutil.copy2(source_target / name, destination / name)
    tools = tmp_path / "destination-tools"
    tools.mkdir()
    marker = tmp_path / "destination-git-marker"
    real_git = deploy._trusted_git_executable(managed_roots=(tmp_path,))
    destination_git = tools / "git"
    destination_git.write_text(
        f"#!/bin/sh\nprintf used > {marker!s}\nexec {shlex.quote(real_git)} \"$@\"\n",
        encoding="utf-8",
    )
    destination_git.chmod(0o755)
    (destination / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    env = {
        **os.environ,
        "TTS_MORE_TRUSTED_GIT": str(destination_git),
        "PATH": f"{destination}{os.pathsep}{os.pathsep}relative-bin",
    }
    env.pop("TTS_MORE_TRUSTED_SSH", None)

    result = subprocess.run(
        [sys.executable, str(destination / "tts-more-update.py")],
        cwd=destination,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "dirty repository" in result.stderr
    assert marker.read_text(encoding="utf-8") == "used"


def test_git_runner_removes_config_environment_injection_before_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    marker = tmp_path / "fsmonitor-marker"
    command = _write_marker_command(tmp_path / "fsmonitor.py", marker)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    monkeypatch.setenv("GIT_SSH_COMMAND", command)
    monkeypatch.setenv("GIT_ASKPASS", command)

    assert deploy._repo_status(target) == ""
    assert not marker.exists()
    environment = deploy._git_environment()
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_CONFIG_KEY_0" not in environment
    assert "GIT_CONFIG_VALUE_0" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_SSH_COMMAND" not in environment
    assert "GIT_ASKPASS" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable hook fixture")
def test_git_runner_disables_default_checkout_hooks(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    subprocess.run(["git", "-C", str(target), "branch", "next"], check=True)
    marker = tmp_path / "post-checkout-marker"
    hook = target / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\nprintf executed > {marker!s}\n", encoding="utf-8")
    hook.chmod(0o755)

    deploy._run_git_command(["git", "-C", str(target), "checkout", "next"], cwd=tmp_path)

    assert not marker.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.fsmonitor", "marker-command"),
        ("core.hooksPath", "custom-hooks"),
        ("core.sshCommand", "marker-command"),
        ("credential.helper", "!marker-command"),
        ("url.https://evil.example/.insteadOf", "https://github.com/"),
        ("filter.attack.process", "marker-command"),
        ("submodule.attack.update", "!marker-command"),
        ("include.path", "../attacker.gitconfig"),
        ("http.sslVerify", "false"),
        ("core.askPass", "marker-command"),
        ("extensions.worktreeConfig", "true"),
    ],
)
def test_git_checkout_rejects_executable_or_rewriting_local_config(
    tmp_path: Path, key: str, value: str
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    subprocess.run(["git", "-C", str(target), "config", "--local", key, value], check=True)

    with pytest.raises(RuntimeError, match="local Git config key is not allowlisted"):
        deploy._validate_git_checkout(target)


def test_generated_updater_rejects_executable_local_config_before_status(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    marker = tmp_path / "updater-fsmonitor-marker"
    command = _write_marker_command(tmp_path / "updater-fsmonitor.py", marker)
    subprocess.run(["git", "-C", str(target), "config", "--local", "core.fsmonitor", command], check=True)

    result = subprocess.run(
        [sys.executable, str(target / "tts-more-update.py")],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "local Git config key is not allowlisted" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("key", ["core.alternateRefsCommand", "unknown.setting"])
def test_generated_updater_rejects_unknown_config_and_checkout_fake_git_before_execution(
    tmp_path: Path, key: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    repositories = [repo for repo in deploy.load_repo_lock(repo_root) if repo["service_id"] == "local-indextts"]
    repositories[0]["path"] = "repo/index-tts"
    deploy.install_update_scripts(tmp_path, repositories=repositories)
    marker = tmp_path / "alternate-refs-marker"
    command = _write_marker_command(tmp_path / "alternate-refs.py", marker)
    _set_local_git_config(target, key, command)
    fake_git = target / ("git.exe" if os.name == "nt" else "git")
    fake_git.write_text("not executable by the updater\n", encoding="utf-8")
    env = {**os.environ, "PATH": f"{target}{os.pathsep}{os.pathsep}relative-bin"}

    result = subprocess.run(
        [sys.executable, str(target / "tts-more-update.py")],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "local Git config key is not allowlisted" in result.stderr
    assert key in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    ("remotes", "expected"),
    [
        (("https://github.com/example/one.git",), False),
        (("git@github.com:example/one.git",), True),
        (
            (
                "https://github.com/example/one.git",
                "ssh://git@github.com/example/two.git",
            ),
            True,
        ),
    ],
)
def test_submodule_classifier_uses_all_prevalidated_transports(
    remotes: tuple[str, ...],
    expected: bool,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    assert deploy._git_command_requires_ssh(
        ["git", "-C", "repo/superproject", "submodule", "update"],
        local_config={},
        validated_submodule_remotes=remotes,
    ) is expected


def test_submodule_classifier_fails_closed_without_prevalidated_remotes() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    with pytest.raises(RuntimeError, match="prevalidated remotes"):
        deploy._git_command_requires_ssh(
            ["git", "-C", "repo/superproject", "submodule", "update"],
            local_config={},
        )


def test_https_only_submodule_update_does_not_resolve_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "superproject"
    target.mkdir(parents=True)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        deploy,
        "_trusted_ssh_executable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("HTTPS submodule resolved SSH")),
    )
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda command, **kwargs: captured.append(command) or subprocess.CompletedProcess(command, 0, "", ""),
    )

    deploy._run_git_process(
        ["git", "-C", str(target), "submodule", "update"],
        cwd=tmp_path,
        check=True,
        validated_submodule_remotes=("https://github.com/example/child.git",),
    )

    assert captured
    assert "core.sshCommand=tts-more-ssh-disabled" in captured[0]


def test_any_ssh_submodule_resolves_trusted_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    target = tmp_path / "repo" / "superproject"
    target.mkdir(parents=True)
    trusted_ssh = deploy._trusted_ssh_executable(managed_roots=(tmp_path,))
    ssh_calls: list[dict[str, object]] = []
    captured: list[list[str]] = []

    def resolve_ssh(**kwargs: object) -> str:
        ssh_calls.append(kwargs)
        return trusted_ssh

    monkeypatch.setattr(deploy, "_trusted_ssh_executable", resolve_ssh)
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda command, **kwargs: captured.append(command) or subprocess.CompletedProcess(command, 0, "", ""),
    )

    deploy._run_git_process(
        ["git", "-C", str(target), "submodule", "update"],
        cwd=tmp_path,
        check=True,
        validated_submodule_remotes=(
            "https://github.com/example/https-child.git",
            "git@github.com:example/ssh-child.git",
        ),
    )

    assert len(ssh_calls) == 1
    ssh_override = next(item for item in captured[0] if item.startswith("core.sshCommand="))
    assert trusted_ssh in ssh_override


@pytest.mark.parametrize(
    "remotes",
    [
        ("https://github.com/example/one.git",),
        ("ssh://git@github.com:22/example/one.git",),
        (
            "https://github.com/example/one.git",
            "git@github.com:example/two.git",
        ),
    ],
)
def test_app_and_generated_updater_transport_classifiers_match_for_submodules(
    tmp_path: Path,
    remotes: tuple[str, ...],
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    generated_path = tmp_path / "tts-more-update.py"
    source = deploy._service_update_script_py()
    generated_path.write_text(source, encoding="utf-8")
    namespace = {"__name__": "transport_parity_test", "__file__": str(generated_path)}
    exec(compile(source, str(generated_path), "exec"), namespace)

    for remote in remotes:
        generated_requires_ssh = namespace["remote_requires_ssh"](remote)
        assert deploy._github_remote_requires_ssh(remote) is generated_requires_ssh
        for verb in ("fetch", "pull"):
            assert deploy._git_command_requires_ssh(
                ["git", verb, "origin", "main"],
                local_config={'remote "origin".url': remote},
            ) is generated_requires_ssh
    assert deploy._git_command_requires_ssh(
        ["git", "-C", "repo/superproject", "submodule", "update"],
        local_config={},
        validated_submodule_remotes=remotes,
    ) is any(namespace["remote_requires_ssh"](remote) for remote in remotes)


@pytest.mark.parametrize("verb", ["status", "config", "fetch", "checkout", "pull", "submodule"])
@pytest.mark.parametrize("requires_ssh", [False, True])
def test_app_and_generated_updater_use_identical_hardened_git_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    requires_ssh: bool,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    generated_path = tmp_path / "tts-more-update.py"
    source = deploy._service_update_script_py()
    generated_path.write_text(source, encoding="utf-8")
    namespace = {"__name__": "policy_test", "__file__": str(generated_path)}
    exec(compile(source, str(generated_path), "exec"), namespace)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "marker-command")
    monkeypatch.setenv("GIT_SSH_COMMAND", "marker-command")
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path / "attacker-exec"))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "attacker-template"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "marker-command")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ext")
    logical = ["git", verb]
    git_executable = deploy._trusted_git_executable(managed_roots=(tmp_path,))
    ssh_executable = deploy._trusted_ssh_executable(
        managed_roots=(tmp_path,),
        git_executable=git_executable,
    )

    app_command = deploy._harden_git_command(
        logical,
        trusted_file=generated_path,
        git_executable=git_executable,
        ssh_executable=ssh_executable if requires_ssh else None,
        managed_roots=(tmp_path,),
        requires_ssh=requires_ssh,
    )
    updater_command = namespace["harden_git_command"](
        logical,
        git_executable,
        ssh_executable if requires_ssh else None,
    )
    app_environment = deploy._git_environment()
    updater_environment = namespace["git_environment"]()

    assert Path(app_command[0]).is_absolute()
    assert Path(updater_command[0]).is_absolute()
    assert app_command[1:] == updater_command[1:]
    expected_autocrlf = "true" if os.name == "nt" else "input"
    assert f"core.autocrlf={expected_autocrlf}" in app_command
    for key in (
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_ATTR_NOSYSTEM",
        "GIT_TERMINAL_PROMPT",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_PAGER",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_ALLOW_PROTOCOL",
    ):
        assert app_environment[key] == updater_environment[key]
    for key in (
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_SSH_COMMAND",
        "GIT_EXEC_PATH",
        "GIT_TEMPLATE_DIR",
        "GIT_EXTERNAL_DIFF",
        "GIT_SSL_NO_VERIFY",
    ):
        assert key not in app_environment
        assert key not in updater_environment
    assert "protocol.allow=never" in app_command
    assert "protocol.https.allow=always" in app_command
    assert "protocol.ssh.allow=always" in app_command
    ssh_override = next(item for item in app_command if item.startswith("core.sshCommand="))
    if requires_ssh:
        assert "ProxyCommand=none" in ssh_override
        assert "PermitLocalCommand=no" in ssh_override
    else:
        assert ssh_override == "core.sshCommand=tts-more-ssh-disabled"
    assert app_environment["GIT_ALLOW_PROTOCOL"] == "https:ssh"


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


@pytest.mark.parametrize("dry_run", [True, False])
def test_selected_repository_set_rejects_duplicate_canonical_paths_before_git_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repositories = deploy.load_repo_lock(tmp_path)
    for repo in repositories[:3]:
        repo["path"] = "repo/shared-gpt"
    monkeypatch.setattr(
        deploy,
        "_git_output",
        lambda command: (_ for _ in ()).throw(AssertionError("Git ran before selected-set validation")),
    )
    monkeypatch.setattr(
        deploy,
        "_run_git_command",
        lambda command, *, cwd: (_ for _ in ()).throw(AssertionError("Git mutated before selected-set validation")),
    )

    with pytest.raises(ValueError, match="same canonical repository path"):
        deploy.sync_repos(
            tmp_path,
            clean=True,
            dry_run=dry_run,
            service_ids={"local-gpt-sovits-main", "local-gpt-sovits-dev", "local-gpt-sovits-proplus-hc-dev"},
            repositories=repositories,
        )

    assert not (tmp_path / "repo").exists()


def test_selected_repository_set_rejects_nested_paths_before_git(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repositories = deploy.load_repo_lock(tmp_path)
    repositories[3]["path"] = "repo/shared"
    repositories[4]["path"] = "repo/shared/nested"

    with pytest.raises(ValueError, match="nested repository paths"):
        deploy.sync_repos(
            tmp_path,
            dry_run=True,
            service_ids={"local-indextts", "local-cosyvoice"},
            repositories=repositories,
        )


def test_selected_repository_set_uses_platform_normcase_for_equivalent_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repositories = deploy.load_repo_lock(tmp_path)
    repositories[3]["path"] = "repo/Index-TTS"
    repositories[4]["path"] = "repo/index-tts"
    original_normcase = deploy.os.path.normcase
    monkeypatch.setattr(deploy.os.path, "normcase", lambda value: original_normcase(value).casefold())

    with pytest.raises(ValueError, match="same canonical repository path"):
        deploy.sync_repos(
            tmp_path,
            dry_run=True,
            service_ids={"local-indextts", "local-cosyvoice"},
            repositories=repositories,
        )


def test_update_rejects_selected_path_conflicts_before_app_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    repositories = deploy.load_repo_lock(tmp_path)
    repositories[3]["path"] = "repo/shared"
    repositories[4]["path"] = "repo/shared"
    monkeypatch.setattr(
        deploy,
        "_git_output",
        lambda command: (_ for _ in ()).throw(AssertionError("app Git ran before selected-set validation")),
    )

    with pytest.raises(ValueError, match="same canonical repository path"):
        deploy.update_project(
            tmp_path,
            dry_run=False,
            service_ids={"local-indextts", "local-cosyvoice"},
            repositories=repositories,
        )


def test_complete_confirmation_rejects_duplicate_selected_paths(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    confirmation = tmp_path / "repo-paths.json"
    confirmation.write_text(
        json.dumps(
            {
                "repositories": {
                    "local-indextts": "repo/shared",
                    "local-cosyvoice": "repo/shared",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="same canonical repository path"):
        deploy.load_deployment_repositories(
            tmp_path,
            confirmation,
            service_ids={"local-indextts", "local-cosyvoice"},
            require_complete=True,
        )


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


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd,
    reason="POSIX directory-relative no-follow open is unavailable",
)
def test_worker_log_opens_logs_directory_with_no_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    logs_dir = tmp_path / "data" / ".runtime" / "logs"
    logs_dir.mkdir(parents=True)
    original_open = deploy.os.open
    directory_flags: list[int] = []

    def tracking_open(path, flags, *args, **kwargs):
        if Path(path) == logs_dir:
            directory_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(deploy.os, "open", tracking_open)
    monkeypatch.setattr(deploy.os, "supports_dir_fd", {*deploy.os.supports_dir_fd, tracking_open})
    with deploy._open_worker_log(logs_dir, "local-indextts") as handle:
        handle.write(b"test\n")

    assert directory_flags
    assert directory_flags[0] & os.O_NOFOLLOW


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

    with pytest.raises(RuntimeError, match="unanchored bundle ownership"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    assert user_file.read_text(encoding="utf-8") == "preserve\n"


def test_same_identity_schema3_manifest_without_app_anchor_cannot_delete_user_file(tmp_path: Path) -> None:
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
    forged_owned = {"user-notes.txt": hashlib.sha256(b"preserve\n").hexdigest()}
    forged_manifest = {
        "schema_version": 3,
        "service_id": "local-indextts",
        "provider_type": "indextts",
        "source_bundle": "deployment/tts-repos/indextts",
        "source_hash": deploy._bundle_source_hash(forged_owned),
        "owned_files": forged_owned,
    }
    (installed / "tts-more-repo.json").write_text(json.dumps(forged_manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unanchored bundle ownership"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    assert user_file.read_text(encoding="utf-8") == "preserve\n"
    assert not (installed / "current.sh").exists()


def test_first_bundle_install_creates_app_owned_anchor_outside_checkout(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "helper.sh").write_text("managed\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")

    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    manifest_path = target / "tts-more" / "tts-more-repo.json"
    anchor_path = tmp_path / "data" / "local" / "deployment-ownership" / "local-indextts.json"
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    assert anchor_path.resolve(strict=False).is_relative_to(tmp_path.resolve(strict=False))
    assert target.resolve(strict=False) not in anchor_path.resolve(strict=False).parents
    assert anchor == {
        "schema_version": 1,
        "state": "installed",
        "service_id": "local-indextts",
        "provider_type": "indextts",
        "source_bundle": "deployment/tts-repos/indextts",
        "repo_path": "repo/index-tts",
        "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


def test_lost_bundle_anchor_fails_closed_without_deleting_owned_files(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    old_source = bundle / "old.sh"
    old_source.write_text("old\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    anchor = tmp_path / "data" / "local" / "deployment-ownership" / "local-indextts.json"
    anchor.unlink()
    old_source.unlink()
    (bundle / "new.sh").write_text("new\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unanchored bundle ownership"):
        deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})

    assert (target / "tts-more" / "old.sh").read_text(encoding="utf-8") == "old\n"
    assert not (target / "tts-more" / "new.sh").exists()


def test_explicit_bundle_adoption_only_anchors_existing_manifest_then_requires_rerun(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    bundle = tmp_path / "deployment" / "tts-repos" / "indextts"
    bundle.mkdir(parents=True)
    (bundle / "new.sh").write_text("new\n", encoding="utf-8")
    target = tmp_path / "repo" / "index-tts"
    _init_git_checkout(target, "https://github.com/XucroYuri/index-tts.git")
    installed = target / "tts-more"
    installed.mkdir()
    old_file = installed / "old.sh"
    old_file.write_text("old\n", encoding="utf-8")
    old_owned = {"old.sh": hashlib.sha256(old_file.read_bytes()).hexdigest()}
    old_manifest = {
        "schema_version": 3,
        "service_id": "local-indextts",
        "provider_type": "indextts",
        "source_bundle": "deployment/tts-repos/indextts",
        "source_hash": deploy._bundle_source_hash(old_owned),
        "owned_files": old_owned,
    }
    manifest_path = installed / "tts-more-repo.json"
    manifest_path.write_text(json.dumps(old_manifest, indent=2) + "\n", encoding="utf-8")

    reports = deploy.install_repo_bundles(
        tmp_path,
        service_ids={"local-indextts"},
        adopt_existing=True,
    )

    assert reports[0]["adopted"] is True
    assert old_file.read_text(encoding="utf-8") == "old\n"
    assert not (installed / "new.sh").exists()
    deploy.install_repo_bundles(tmp_path, service_ids={"local-indextts"})
    assert not old_file.exists()
    assert (installed / "new.sh").read_text(encoding="utf-8") == "new\n"


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
    deploy._remove_path(target / ".git")
    (target / ".git").write_text("gitdir: C:\\outside\\worktree\n", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError), match="worktree|Git metadata"):
        deploy.sync_repos(tmp_path, dry_run=True, service_ids={"local-indextts"})


class _FakeCudaHost:
    def __init__(self) -> None:
        self.failures: set[str] = set()
        self.commands: list[list[str]] = []
        self.disk_calls: list[str] = []
        self.free_gib = {"repo": 80.0, "temp": 20.0}
        self.repo_path = Path("C:/workspace/tts-more")
        self.temp_path = Path("D:/temp")

    def fail(self, check: str) -> None:
        self.failures.add(check)

    def which(self, name: str) -> str | None:
        if name in self.failures:
            return None
        return f"C:/private/tools/{name}.exe"

    def disk_usage(self, path: str | os.PathLike[str]):
        raw = str(path)
        self.disk_calls.append(raw)
        label = "temp" if "temp" in raw.lower() else "repo"
        free = int(self.free_gib[label] * 1024**3)
        return SimpleNamespace(total=100 * 1024**3, used=100 * 1024**3 - free, free=free)

    def path_exists(self, path: str | os.PathLike[str]) -> bool:
        return "playwright" not in self.failures and bool(path)

    def run(self, command: list[str], **_kwargs):
        self.commands.append([str(item) for item in command])
        rendered = " ".join(str(item) for item in command)
        executable = Path(str(command[0])).stem.lower()
        if "WhisperModel" in rendered:
            if "asr_timeout" in self.failures:
                raise subprocess.TimeoutExpired(command, _kwargs.get("timeout", 0))
            if "asr_smoke" in self.failures:
                return SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps(
                        {
                            "ok": False,
                            "error_type": "RuntimeError",
                            "message": (
                                r"C:\private\models\large-v3\model.bin failed in "
                                "secret-worker.exe 123e4567-e89b-12d3-a456-426614174000 "
                                "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                            ),
                        }
                    ),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")
        if "get_supported_compute_types" in rendered:
            compute_types = ["int8"] if "ctranslate2" in self.failures else ["float16", "int8_float16"]
            return SimpleNamespace(returncode=0, stdout=json.dumps(compute_types), stderr="")
        if "chromium.executablePath" in rendered:
            return SimpleNamespace(
                returncode=0,
                stdout="C:/private/playwright/chromium.exe\n",
                stderr="",
            )
        if "--query-gpu=memory.total,memory.used,driver_version" in rendered:
            if "nvidia_query" in self.failures:
                return SimpleNamespace(returncode=1, stdout="", stderr="driver query failed")
            total = 12000 if "gpu_total" in self.failures else 24576
            used = 2048 if "gpu_idle" in self.failures else 512
            return SimpleNamespace(returncode=0, stdout=f"{total}, {used}, 555.42\n", stderr="")
        versions = {
            "conda": "conda 24.1.0",
            "git": "git version 2.45.0.windows.1",
            "node": "v20.15.0",
            "pnpm": "9.12.0",
        }
        return SimpleNamespace(returncode=0, stdout=versions.get(executable, ""), stderr="")

    def providers(self) -> dict[str, object]:
        version = (3, 10, 14) if "python" in self.failures else (3, 11, 9)
        return {
            "command_runner": self.run,
            "which": self.which,
            "disk_usage": self.disk_usage,
            "python_version": version,
            "repo_path": self.repo_path,
            "temp_path": self.temp_path,
            "path_exists": self.path_exists,
            "smoke_timeout_seconds": 5.0,
        }


def test_cuda_host_volume_keys_preserve_windows_drive_identity_on_posix_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    monkeypatch.setattr(deploy.os, "path", posixpath)

    assert deploy._host_volume_key("C:/workspace/tts-more") == "c:"
    assert deploy._host_volume_key(r"C:\temp") == "c:"
    assert deploy._host_volume_key("D:/temp") == "d:"
    assert deploy._host_volume_key(r"\\server\share\models") == "//server/share"


@pytest.mark.parametrize(
    ("check", "message"),
    [
        ("python", "Python 3.11 is required"),
        ("conda", "conda is required for GPT-SoVITS on Windows"),
        ("git", "git is required"),
        ("node", "node is required"),
        ("pnpm", "pnpm is required"),
        ("nvidia-smi", "nvidia-smi is required"),
        ("gpu_total", "GPU memory must be at least 16000 MiB"),
        ("gpu_idle", "GPU must use no more than 1024 MiB before certification"),
        ("ctranslate2", "CTranslate2 CUDA float16 support is required"),
        ("playwright", "Playwright Chromium is required"),
    ],
)
def test_cuda_host_preflight_reports_actionable_cheap_blockers(check: str, message: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.fail(check)

    report = deploy.inspect_cuda_host("single-clean", **fake_host.providers())

    assert report["passed"] is False
    assert any(message in item["message"] for item in report["checks"] if not item["passed"])


@pytest.mark.parametrize(
    ("mode", "volume", "free_gib", "required_gib"),
    [
        ("single-clean", "repo", 39.0, 40.0),
        ("single-clean", "temp", 9.0, 10.0),
        ("single-release", "repo", 14.0, 15.0),
        ("single-release", "temp", 4.0, 5.0),
        ("distributed", "repo", 14.0, 15.0),
        ("distributed", "temp", 4.0, 5.0),
    ],
)
def test_cuda_host_preflight_enforces_mode_disk_thresholds(
    mode: str, volume: str, free_gib: float, required_gib: float
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.free_gib[volume] = free_gib

    report = deploy.inspect_cuda_host(mode, **fake_host.providers())

    assert report["passed"] is False
    failed_disk = [item for item in report["checks"] if item["id"].startswith("disk_") and not item["passed"]]
    assert len(failed_disk) == 1
    assert failed_disk[0]["required_gib"] == required_gib


def test_cuda_host_preflight_checks_shared_repo_temp_volume_once_at_stricter_threshold() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.repo_path = Path("C:/workspace/tts-more")
    fake_host.temp_path = Path("C:/temp")
    fake_host.free_gib["repo"] = 20.0

    report = deploy.inspect_cuda_host("single-clean", **fake_host.providers())

    disk_checks = [item for item in report["checks"] if item["id"].startswith("disk_")]
    assert len(fake_host.disk_calls) == 1
    assert len(disk_checks) == 1
    assert disk_checks[0]["required_gib"] == 40.0
    assert disk_checks[0]["passed"] is False
    assert report["disk"]["volumes"] == [
        {"label": "repository-and-temp", "free_gib": 20.0, "required_gib": 40.0}
    ]


def test_cuda_host_preflight_skips_large_v3_smoke_when_any_cheap_check_fails() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.fail("python")

    report = deploy.inspect_cuda_host("single-clean", **fake_host.providers())

    assert report["asr_smoke"] == {"attempted": False, "passed": False, "status": "skipped"}
    assert not any("WhisperModel" in " ".join(command) for command in fake_host.commands)


def test_cuda_host_preflight_runs_bounded_large_v3_smoke_after_cheap_checks_pass() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()

    report = deploy.inspect_cuda_host("single-clean", **fake_host.providers())

    smoke_commands = [command for command in fake_host.commands if "WhisperModel" in " ".join(command)]
    assert report["passed"] is True
    assert report["asr_smoke"] == {"attempted": True, "passed": True, "status": "passed"}
    assert len(smoke_commands) == 1
    assert "large-v3" in " ".join(smoke_commands[0])
    assert "device='cuda'" in " ".join(smoke_commands[0])
    assert "compute_type='float16'" in " ".join(smoke_commands[0])


def test_cuda_host_preflight_sanitizes_smoke_error_paths_processes_and_uuids() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.fail("asr_smoke")

    report = deploy.inspect_cuda_host("single-release", **fake_host.providers())
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["passed"] is False
    assert report["asr_smoke"]["error_type"] == "RuntimeError"
    assert "C:\\private" not in serialized
    assert "secret-worker.exe" not in serialized
    assert "123e4567-e89b-12d3-a456-426614174000" not in serialized
    assert "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" not in serialized
    assert len(report["asr_smoke"]["message"]) <= 200


def test_cuda_host_preflight_marks_nvidia_query_failure_without_corrupting_disk_checks() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.fail("nvidia_query")

    report = deploy.inspect_cuda_host("single-clean", **fake_host.providers())

    nvidia_check = next(item for item in report["checks"] if item["id"] == "nvidia-smi")
    disk_checks = [item for item in report["checks"] if item["id"].startswith("disk_")]
    assert nvidia_check["passed"] is False
    assert all(item["passed"] for item in disk_checks)
    assert report["asr_smoke"]["status"] == "skipped"


def test_cuda_host_preflight_records_sanitized_large_v3_timeout() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.fail("asr_timeout")

    report = deploy.inspect_cuda_host("single-release", **fake_host.providers())

    assert report["passed"] is False
    assert report["asr_smoke"]["error_type"] == "TimeoutExpired"
    assert report["asr_smoke"]["message"] == "large-v3 CUDA float16 smoke exceeded 5 seconds"
    assert len(report["asr_smoke"]["message"]) <= 200


def test_cuda_host_preflight_next_action_combines_independent_cheap_blockers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    fake_host = _FakeCudaHost()
    fake_host.fail("conda")
    fake_host.fail("gpu_idle")

    report = deploy.inspect_cuda_host("single-clean", **fake_host.providers())

    assert "Install or repair required tools: conda." in report["next_action"]
    assert "Wait for unrelated GPU work to finish" in report["next_action"]
    assert "never stops GPU processes" in report["next_action"]


@pytest.mark.parametrize(("passed", "expected_exit"), [(True, 0), (False, 1)])
def test_cuda_host_preflight_cli_writes_environment_report_and_returns_gate_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passed: bool, expected_exit: int
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    report = {
        "schema_version": 1,
        "stage": "host-preflight",
        "mode": "single-release",
        "passed": passed,
        "checks": [],
        "versions": {"python": "3.11.9"},
        "disk": {"volumes": []},
        "gpu": {"count": 1, "aggregate_total_mib": 24576, "aggregate_used_mib": 512},
        "asr_smoke": {"attempted": True, "passed": passed, "status": "passed" if passed else "failed"},
        "next_action": "Continue to input preflight and deployment." if passed else "Resolve the failed host checks, then rerun.",
    }
    monkeypatch.setattr(deploy, "inspect_cuda_host", lambda mode, **_kwargs: {**report, "mode": mode})
    output = tmp_path / "environment-preflight.json"

    exit_code = deploy.main(
        [
            "--root",
            str(tmp_path),
            "preflight-cuda-host",
            "--mode",
            "single-release",
            "--output",
            str(output),
        ]
    )

    assert exit_code == expected_exit
    assert json.loads(output.read_text(encoding="utf-8")) == report
