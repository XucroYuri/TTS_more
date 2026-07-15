from __future__ import annotations

import json
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import portable_file_io
from app.portable_discovery import (
    PortablePackageRegisterRequest,
    discover_portable_packages,
    endpoint_from_portable_package,
    read_portable_package,
)
from app.portable_services import discover_bounded_portable_packages


REPO_ROOT = Path(__file__).resolve().parents[2]


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("directory junction verification is Windows-only")
    environment = os.environ.copy()
    environment["B2_DISCOVERY_JUNCTION_PATH"] = str(link)
    environment["B2_DISCOVERY_JUNCTION_TARGET"] = str(target)
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "New-Item -ItemType Junction -Path $env:B2_DISCOVERY_JUNCTION_PATH -Target $env:B2_DISCOVERY_JUNCTION_TARGET | Out-Null",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        pytest.fail(f"Windows junction command failed: {exc}")
    if completed.returncode != 0:
        pytest.fail(f"Windows junction creation failed: {completed.stderr}")


def _hardlink(link: Path, target: Path) -> None:
    try:
        os.link(target, link)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"hardlink creation is unavailable: {exc}")
        pytest.fail(f"Windows hardlink creation failed: {exc}")


def _write_package(
    root: Path,
    *,
    schema_version: int,
    component: str,
    port: int,
    completed_v2: bool = True,
    operations_path: str = "data/local/operations",
) -> Path:
    for launcher in ("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "Build-Package.ps1"):
        (root / launcher).parent.mkdir(parents=True, exist_ok=True)
        (root / launcher).write_text("@echo off\n", encoding="utf-8")
    (root / "tts_more" / "locks").mkdir(parents=True)
    (root / "tts_more" / "locks" / "runtime.lock.json").write_text("{}", encoding="utf-8")
    (root / "tts_more" / "locks" / "models.lock.json").write_text("{}", encoding="utf-8")
    (root / "THIRD_PARTY_NOTICES.json").write_text("{}", encoding="utf-8")
    (root / "SHA256SUMS.txt").write_text("portable checksum manifest\n", encoding="utf-8")
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
                        "operations": operations_path,
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


def test_bounded_sibling_discovery_handles_moved_chinese_and_space_paths(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "移动 硬盘" / "语音 四仓"
    controller = suite / "TTS More"
    controller.mkdir(parents=True)
    expected = {
        "gpt-sovits": _write_package(
            suite / "GPT-SoVITS",
            schema_version=2,
            component="gpt-sovits",
            port=9880,
        ),
        "indextts": _write_package(
            suite / "IndexTTS",
            schema_version=2,
            component="indextts",
            port=9881,
        ),
        "cosyvoice": _write_package(
            suite / "CosyVoice",
            schema_version=2,
            component="cosyvoice",
            port=9882,
        ),
    }
    _write_package(
        tmp_path / "不在扫描范围" / "Nested GPT",
        schema_version=2,
        component="gpt-sovits",
        port=9980,
    )

    packages = discover_bounded_portable_packages(controller, [])

    assert {item.component: Path(item.package_root) for item in packages} == {
        component: root.resolve() for component, root in expected.items()
    }
    for descriptor in packages:
        package_root = Path(descriptor.package_root)
        package_root.relative_to(suite)
        Path(descriptor.manifest_path).relative_to(package_root)
        assert descriptor.complete_v2 is True
        assert descriptor.manageable is True
        assert all(not Path(path).is_absolute() for path in descriptor.launchers.values())
        assert not Path(descriptor.operations_path).is_absolute()
        assert not Path(descriptor.state_path).is_absolute()


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


def test_reader_rejects_manifest_hardlink_before_parsing_content(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path / "GPT hardlink package", schema_version=2, component="gpt-sovits", port=9880
    )
    manifest = package_root / "package" / "tts-more-package.json"
    outside = tmp_path / "outside-manifest.json"
    manifest.replace(outside)
    _hardlink(manifest, outside)

    with pytest.raises(OSError, match="hard link"):
        read_portable_package(package_root)


def test_reader_never_accepts_manifest_after_parent_switches_to_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = _write_package(
        tmp_path / "GPT race package", schema_version=2, component="gpt-sovits", port=9880
    )
    manifest_directory = package_root / "package"
    moved = package_root / "package-old"
    outside = tmp_path / "outside-package"
    outside.mkdir()
    (outside / "tts-more-package.json").write_bytes(
        (manifest_directory / "tts-more-package.json").read_bytes()
    )
    original_open = portable_file_io._open_binary
    swapped = False

    def swap_parent_before_open(current: Path):
        nonlocal swapped
        if not swapped and current.name == "tts-more-package.json":
            swapped = True
            manifest_directory.rename(moved)
            _junction(manifest_directory, outside)
        return original_open(current)

    monkeypatch.setattr(portable_file_io, "_open_binary", swap_parent_before_open)

    with pytest.raises(OSError, match="reparse|changed|escape"):
        read_portable_package(package_root)


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


@pytest.mark.parametrize("operations_path", ("C:/outside/operations", "../outside/operations"))
def test_reader_rejects_supplied_unsafe_operations_path(tmp_path: Path, operations_path: str) -> None:
    package_root = _write_package(
        tmp_path / "unsafe operations package",
        schema_version=2,
        component="gpt-sovits",
        port=9880,
        operations_path=operations_path,
    )

    descriptor = read_portable_package(package_root)

    assert descriptor.valid is False
    assert "portable package operations path is unsafe" in descriptor.errors


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
    assert local.control_kind == "portable-package"
    assert local.portable_locator is not None
    assert local.portable_locator.component == "cosyvoice"
    assert local.portable_locator.package_id == descriptor.package_id
    assert local.start_command[:2] == ["python.exe", "scripts/portable_package_runner.py"]
    assert local.repo_path == str(package_root.resolve())
    assert lan.mode == "external" and lan.managed is False
    assert lan.control_kind == "generic"
    assert lan.portable_locator is None
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
