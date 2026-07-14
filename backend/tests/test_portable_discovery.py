from __future__ import annotations

import json
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.portable_discovery import (
    PortablePackageRegisterRequest,
    discover_portable_packages,
    endpoint_from_portable_package,
    read_portable_package,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_package(
    root: Path, *, schema_version: int, component: str, port: int, completed_v2: bool = True
) -> Path:
    for launcher in ("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "Build-Package.ps1"):
        (root / launcher).parent.mkdir(parents=True, exist_ok=True)
        (root / launcher).write_text("@echo off\n", encoding="utf-8")
    (root / "tts_more" / "locks").mkdir(parents=True)
    (root / "tts_more" / "locks" / "runtime.lock.json").write_text("{}", encoding="utf-8")
    (root / "tts_more" / "locks" / "models.lock.json").write_text("{}", encoding="utf-8")
    (root / "THIRD_PARTY_NOTICES.json").write_text("{}", encoding="utf-8")
    if schema_version == 1:
        payload = {
            "schema_version": 1,
            "component": component,
            "version": "0.1.0",
            "build_id": "legacy-test",
            "api_contract": "tts-more-v1",
            "default_endpoint": f"http://127.0.0.1:{port}",
            "port": port,
            "launcher": "Start.cmd",
            "health_path": "/health",
            "capabilities": ["tts", "artifact-transfer"],
            "model_profile": "default",
            "runtime": "runtime/runtime.zip",
            "sha256_manifest": "SHA256SUMS.txt",
        }
    else:
        payload = {
            "schema_version": 2,
            "component": component,
            "version": "0.2.0",
            "build_id": "portable-v2-test",
            "package_profile": "bootstrap",
            "platform": "windows-x64",
            "api_contract": "tts-more-v1",
            "source": {"repository": "https://example.invalid/repo", "revision": "a" * 40},
            "integration": {"version": "2.0.0", "source_revision": "b" * 40, "bundle_sha256": "c" * 64},
            "runtime": {
                "python_version": "3.11",
                "device_profiles": ["auto", "cpu"],
                "lock": "tts_more/locks/runtime.lock.json",
                "state_path": "data/local/install-state.json",
            },
            "models": {"lock": "tts_more/locks/models.lock.json", "required": True},
            "data_root": "data/local",
            "launchers": {
                "initialize": "Initialize.cmd",
                "start": "Start.cmd",
                "stop": "Stop.cmd",
                "repair": "Repair.cmd",
                "build": "Build-Package.ps1",
            },
            "endpoint": {
                "default_url": f"http://127.0.0.1:{port}",
                "port": port,
                "health_path": "/health",
                "capabilities_path": "/capabilities",
                "bind_policy": "loopback",
            },
            "capabilities": ["tts", "artifact-transfer"],
            "sha256_manifest": "SHA256SUMS.txt",
            "licenses": "THIRD_PARTY_NOTICES.json",
        }
        if completed_v2:
            payload.update(
                {
                    "package_id": component,
                    "release_version": "0.2.1",
                    "protocol": {
                        "name": "tts-more-v1",
                        "version": "1.0",
                        "controller_range": ">=0.2.0,<0.3.0",
                    },
                    "data": {
                        "user": "data/user",
                        "local": "data/local",
                        "cache": "data/cache",
                        "operations": "data/local/operations",
                    },
                }
            )
    manifest = root / "package" / "tts-more-package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8-sig")
    return root


def test_discovers_v1_and_v2_packages_from_explicit_container_and_siblings(tmp_path: Path) -> None:
    app_root = tmp_path / "TTS More"
    app_root.mkdir()
    _write_package(tmp_path / "GPT portable", schema_version=1, component="gpt-sovits", port=9880)
    explicit = tmp_path / "另一个盘符" / "packages"
    _write_package(explicit / "Index portable", schema_version=2, component="indextts", port=9881)

    packages = discover_portable_packages(app_root, [explicit], include_siblings=True)

    assert [(item.component, item.schema_version) for item in packages] == [
        ("gpt-sovits", 1),
        ("indextts", 2),
    ]
    assert all(item.valid for item in packages)
    assert packages[1].package_profile == "bootstrap"
    assert packages[0].package_id == "gpt-sovits"
    assert packages[0].version == "0.1.0"


def test_completed_v2_descriptor_exposes_identity_protocol_and_operations_path(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path / "GPT package", schema_version=2, component="gpt-sovits", port=9880
    )

    descriptor = read_portable_package(package_root)

    assert descriptor.package_id == "gpt-sovits"
    assert descriptor.version == "0.2.1"
    assert descriptor.protocol_version == "1.0"
    assert descriptor.controller_range == ">=0.2.0,<0.3.0"
    assert descriptor.operations_path == "data/local/operations"


def test_reader_tolerates_older_rc_v2_identity_protocol_and_data_omissions(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path / "Index rc package",
        schema_version=2,
        component="indextts",
        port=9881,
        completed_v2=False,
    )

    descriptor = read_portable_package(package_root)

    assert descriptor.valid is True
    assert descriptor.package_id == "indextts"
    assert descriptor.version == "0.2.0"
    assert descriptor.protocol_version == "1.0"
    assert descriptor.controller_range == ">=0.2.0,<0.3.0"
    assert descriptor.operations_path == "data/local/operations"


def test_local_package_registration_is_managed_but_lan_registration_is_not(tmp_path: Path) -> None:
    package_root = _write_package(tmp_path / "Cosy package", schema_version=2, component="cosyvoice", port=9882)
    descriptor = discover_portable_packages(tmp_path / "app", [package_root], include_siblings=False)[0]

    local = endpoint_from_portable_package(
        descriptor,
        PortablePackageRegisterRequest(package_root=str(package_root), display_name="Cosy portable"),
    )
    lan = endpoint_from_portable_package(
        descriptor,
        PortablePackageRegisterRequest(
            package_root=str(package_root),
            base_url="http://192.168.50.10:9882",
            display_name="Cosy LAN",
        ),
    )

    assert local.mode == "local" and local.managed is True
    assert local.start_command[:2] == ["python.exe", "scripts/portable_package_runner.py"]
    assert local.repo_path == str(package_root.resolve())
    assert lan.mode == "external" and lan.managed is False
    assert lan.network_scope == "lan"
    assert lan.default_params["delivery"] == "artifact"


def test_public_endpoint_is_rejected_by_portable_package_registration(tmp_path: Path) -> None:
    package_root = _write_package(tmp_path / "GPT package", schema_version=2, component="gpt-sovits", port=9880)
    descriptor = discover_portable_packages(tmp_path / "app", [package_root], include_siblings=False)[0]

    try:
        endpoint_from_portable_package(
            descriptor,
            PortablePackageRegisterRequest(package_root=str(package_root), base_url="https://tts.example.com"),
        )
    except ValueError as exc:
        assert "trusted LAN" in str(exc)
    else:
        raise AssertionError("public portable worker registration must be rejected")


def test_portable_package_api_discovers_and_persists_registration(tmp_path: Path) -> None:
    package_root = _write_package(tmp_path / "workers" / "GPT package", schema_version=2, component="gpt-sovits", port=9880)
    data_root = tmp_path / "app-data"
    client = TestClient(create_app(data_root=data_root, env_path=tmp_path / ".env.local"))

    discovered = client.post(
        "/api/portable-packages/discover",
        json={"roots": [str(package_root)], "include_siblings": False},
    )
    registered = client.post(
        "/api/portable-packages/register",
        json={"package_root": str(package_root), "display_name": "GPT portable"},
    )

    assert discovered.status_code == 200
    assert discovered.json()["packages"][0]["schema_version"] == 2
    assert registered.status_code == 200
    assert registered.json()["service"]["managed"] is True
    saved = json.loads((data_root / "local" / "services.json").read_text(encoding="utf-8"))
    assert any(service["display_name"] == "GPT portable" for service in saved)


def test_managed_runner_uses_only_the_worker_packages_private_runtime(tmp_path: Path) -> None:
    package_root = _write_package(tmp_path / "GPT package", schema_version=2, component="gpt-sovits", port=9880)
    runtime_python = package_root / "runtime" / "live" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"private-python")
    (package_root / "tts_more" / "component.json").write_text(
        json.dumps({"component": "gpt-sovits", "module": "tts_more_worker.gpt_sovits:app", "port": 9880}),
        encoding="utf-8",
    )
    module_path = REPO_ROOT / "scripts" / "portable_package_runner.py"
    spec = importlib.util.spec_from_file_location("portable_package_runner", module_path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    command, cwd, environment = runner.build_worker_process(package_root)

    assert command == [
        str(runtime_python),
        "-m",
        "uvicorn",
        "tts_more_worker.gpt_sovits:app",
        "--app-dir",
        str(package_root / "tts_more"),
        "--host",
        "127.0.0.1",
        "--port",
        "9880",
    ]
    assert cwd == package_root
    assert environment["TTS_MORE_GPTSOVITS_REPO"] == str(package_root)
    assert environment["TTS_MORE_WORKER_ALLOW_PATH_DELIVERY"] == "1"
