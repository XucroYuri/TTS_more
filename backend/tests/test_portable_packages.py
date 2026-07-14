from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _load_portable_packages():
    module_path = REPO_ROOT / "scripts" / "portable_packages.py"
    assert module_path.is_file(), "portable package validator script is missing"
    spec = importlib.util.spec_from_file_location("portable_packages", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_portable_launcher():
    module_path = REPO_ROOT / "scripts" / "portable_launcher.py"
    assert module_path.is_file(), "portable worker launcher script is missing"
    spec = importlib.util.spec_from_file_location("portable_launcher", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sync_integrations():
    module_path = REPO_ROOT / "scripts" / "sync_integrations.py"
    spec = importlib.util.spec_from_file_location("sync_integrations_for_portable_schema", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _official_manifest_validator() -> Draft202012Validator:
    schema = json.loads(
        (REPO_ROOT / "packaging" / "portable" / "tts-more-package.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _run_checked(command: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {' '.join(command)}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _initialize_git_repository(root: Path) -> None:
    _run_checked(["git", "init", "--quiet"], root)
    _run_checked(["git", "config", "user.name", "Portable Schema Test"], root)
    _run_checked(["git", "config", "user.email", "portable-schema-test@example.invalid"], root)
    _run_checked(["git", "add", "."], root)
    _run_checked(["git", "commit", "--quiet", "-m", "portable schema fixture"], root)


def _copy_controller_builder_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "Build-Package.ps1", root / "Build-Package.ps1")
    shutil.copytree(
        REPO_ROOT / "backend" / "app",
        root / "backend" / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        destination = root / "backend" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "backend" / name, destination)
    frontend_dist = root / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text("<!doctype html><title>schema fixture</title>\n", encoding="utf-8")
    for name in (
        "bootstrap-conda.ps1",
        "initialize-portable.ps1",
        "repair-portable.ps1",
        "start-production.ps1",
        "stop-production.ps1",
        "Invoke-PortableStart.ps1",
        "Show-PortableProgress.ps1",
        "Portable-Validation.ps1",
        "export-portable-diagnostics.py",
        "portable_install.py",
        "portable_launcher.py",
        "portable_operations.py",
        "portable_packages.py",
        "portable_package_runner.py",
    ):
        destination = root / "scripts" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "scripts" / name, destination)
    for name in (
        "toolchain.lock.json",
        "runtime.lock.json",
        "models.lock.json",
        "tts-more-package.schema.json",
        "error-catalog.zh-CN.json",
    ):
        destination = root / "packaging" / "portable" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "packaging" / "portable" / name, destination)
    for name in (
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "LICENSE",
        "NOTICE",
        "repo.lock.json",
    ):
        shutil.copy2(REPO_ROOT / name, root / name)


@pytest.fixture(scope="module")
def generated_v2_manifests(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    if POWERSHELL is None:
        pytest.skip("real portable builder contract test requires PowerShell")
    root = tmp_path_factory.mktemp("official-portable-schema")
    build_python = str(Path(sys.executable).resolve())
    environment = {**os.environ, "TTS_MORE_BUILD_PYTHON": build_python}
    version = "0.2.0-schema-contract"

    controller_root = root / "controller"
    _copy_controller_builder_fixture(controller_root)
    _initialize_git_repository(controller_root)
    _run_checked(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(controller_root / "Build-Package.ps1"),
            "-Profile",
            "Bootstrap",
            "-Device",
            "CPU",
            "-Version",
            version,
            "-OutputRoot",
            str(root / "controller-output"),
        ],
        controller_root,
        env=environment,
    )
    controller_manifest_path = (
        controller_root
        / "artifacts"
        / "portable"
        / ".work"
        / "tts-more-bootstrap"
        / f"TTS-More-{version}-windows-x64-bootstrap"
        / "package"
        / "tts-more-package.json"
    )

    worker_root = root / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "gpt-sovits", "a" * 40)
    _initialize_git_repository(worker_root)
    _run_checked(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(worker_root / "Build-Package.ps1"),
            "-Profile",
            "Bootstrap",
            "-Device",
            "CPU",
            "-Version",
            version,
            "-OutputRoot",
            str(root / "worker-output"),
        ],
        worker_root,
        env=environment,
    )
    worker_manifest_path = (
        worker_root
        / "artifacts"
        / "portable"
        / ".work"
        / "gpt-sovits-bootstrap"
        / f"gpt-sovits-{version}-windows-x64-bootstrap"
        / "package"
        / "tts-more-package.json"
    )
    assert controller_manifest_path.is_file()
    assert worker_manifest_path.is_file()
    return {
        "controller": json.loads(controller_manifest_path.read_text(encoding="utf-8-sig")),
        "worker": json.loads(worker_manifest_path.read_text(encoding="utf-8-sig")),
    }


def _valid_gpt_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "component": "gpt-sovits-dev",
        "version": "0.1.0",
        "build_id": "test-build",
        "api_contract": "tts-more-v1",
        "default_endpoint": "http://127.0.0.1:9883",
        "port": 9883,
        "launcher": "Start.cmd",
        "health_path": "/health",
        "capabilities": ["tts", "artifact-transfer"],
        "model_profile": "full-quality-default",
        "runtime": "runtime/runtime.zip",
        "sha256_manifest": "SHA256SUMS.txt",
    }


def _valid_v2_manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "component": "gpt-sovits",
        "package_id": "gpt-sovits",
        "release_version": "0.2.0",
        "version": "0.2.0",
        "build_id": "gpt-main-test",
        "package_profile": "bootstrap",
        "platform": "windows-x64",
        "api_contract": "tts-more-v1",
        "source": {
            "repository": "https://github.com/XucroYuri/GPT-SoVITS.git",
            "revision": "f8a5865000000000000000000000000000000000",
        },
        "integration": {
            "version": "2.0.0",
            "source_revision": "d" * 40,
            "bundle_sha256": "a" * 64,
        },
        "runtime": {
            "python_version": "3.11",
            "device_profiles": ["auto", "cu128", "cu126", "cpu"],
            "lock": "locks/runtime.json",
            "state_path": "data/local/install-state.json",
        },
        "models": {"lock": "locks/models.json", "required": True},
        "data_root": "data/local",
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
        "launchers": {
            "initialize": "Initialize.cmd",
            "start": "Start.cmd",
            "stop": "Stop.cmd",
            "repair": "Repair.cmd",
            "build": "Build-Package.ps1",
        },
        "endpoint": {
            "default_url": "http://127.0.0.1:9880",
            "port": 9880,
            "health_path": "/health",
            "capabilities_path": "/capabilities",
            "bind_policy": "loopback",
        },
        "capabilities": ["tts", "artifact-transfer"],
        "sha256_manifest": "SHA256SUMS.txt",
        "licenses": "licenses/THIRD-PARTY-NOTICES.json",
    }


def _write_v2_manifest(root: Path, payload: dict[str, object]) -> Path:
    for relative_path in (
        *payload["launchers"].values(),
        payload["runtime"]["lock"],
        payload["models"]["lock"],
        payload["licenses"],
    ):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    manifest = root / "package" / "tts-more-package.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_validate_manifest_rejects_absolute_paths_and_missing_worker_fields(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    manifest = tmp_path / "tts-more-package.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "component": "gpt-sovits-dev", "launcher": "C:/bad/Start.cmd"}),
        encoding="utf-8",
    )

    report = packages.validate_manifest(manifest, tmp_path)

    assert report["valid"] is False
    assert "launcher must be a relative path" in report["errors"]
    assert "default_endpoint is required" in report["errors"]


def test_validate_manifest_accepts_staged_gpt_worker_package(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    (tmp_path / "Start.cmd").write_text("@echo off\r\n", encoding="utf-8")
    manifest = tmp_path / "package" / "tts-more-package.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(_valid_gpt_manifest()), encoding="utf-8")

    report = packages.validate_manifest(manifest, tmp_path)

    assert report == {
        "valid": True,
        "errors": [],
        "component": "gpt-sovits-dev",
        "default_endpoint": "http://127.0.0.1:9883",
        "launcher": "Start.cmd",
    }


def test_validate_manifest_accepts_schema_v2_and_keeps_v1_compatibility(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    manifest = _write_v2_manifest(tmp_path, payload)

    v2_report = packages.validate_manifest(manifest, tmp_path)

    assert v2_report == {
        "valid": True,
        "errors": [],
        "component": "gpt-sovits",
        "default_endpoint": "http://127.0.0.1:9880",
        "launcher": "Start.cmd",
    }


def test_completed_v2_requires_identity_protocol_and_data_paths(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload.update(
        {
            "package_id": "tts-more",
            "release_version": "0.2.0",
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
    manifest = _write_v2_manifest(tmp_path, payload)
    report = packages.validate_manifest(manifest, tmp_path)
    assert report["valid"] is True
    del payload["package_id"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = packages.validate_manifest(manifest, tmp_path)
    assert "package_id is required" in report["errors"]


def test_completed_v2_validates_protocol_and_every_data_path(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload["protocol"] = {"name": "wrong", "version": "", "controller_range": ""}
    payload["data"]["operations"] = "C:/machine/operations"
    manifest = _write_v2_manifest(tmp_path, payload)

    report = packages.validate_manifest(manifest, tmp_path)

    assert "protocol.name must be tts-more-v1" in report["errors"]
    assert "protocol.version is required" in report["errors"]
    assert "protocol.controller_range is required" in report["errors"]
    assert "data.operations must be a relative path" in report["errors"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("package_id", 123),
        ("package_id", ""),
        ("release_version", 123),
        ("release_version", ""),
    ),
)
def test_completed_v2_requires_non_empty_string_identity_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload[field] = value
    manifest = _write_v2_manifest(tmp_path, payload)

    report = packages.validate_manifest(manifest, tmp_path)

    assert report["valid"] is False
    assert f"{field} is required" in report["errors"]


def test_validate_manifest_accepts_windows_powershell_utf8_bom(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    (tmp_path / "Start.cmd").write_text("@echo off\r\n", encoding="utf-8")
    manifest = tmp_path / "tts-more-package.json"
    manifest.write_text(json.dumps(_valid_gpt_manifest()), encoding="utf-8-sig")

    assert packages.validate_manifest(manifest, tmp_path)["valid"] is True


def test_create_zip_uses_a_single_package_root_and_zip64(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    stage = tmp_path / "Component 目录"
    (stage / "nested").mkdir(parents=True)
    (stage / "Start.cmd").write_text("@echo off\n", encoding="utf-8")
    (stage / "nested" / "asset.txt").write_text("locked", encoding="utf-8")
    output = tmp_path / "component.zip"

    packages.create_zip(stage, output)

    with zipfile.ZipFile(output) as archive:
        assert archive._allowZip64 is True
        assert sorted(archive.namelist()) == ["Component 目录/Start.cmd", "Component 目录/nested/asset.txt"]


def test_package_sha256_gate_requires_exact_coverage_and_rejects_tampering(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    root = tmp_path / "package"
    first = root / "Start.cmd"
    second = root / "scripts" / "portable_packages.py"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"start")
    second.write_bytes(b"gate")

    def write_sums(*, include_second: bool = True) -> None:
        entries = [(first, "Start.cmd")]
        if include_second:
            entries.append((second, "scripts/portable_packages.py"))
        (root / "SHA256SUMS.txt").write_text(
            "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n" for path, relative in entries),
            encoding="utf-8",
        )

    write_sums()
    assert packages.verify_sha256_manifest(root)["valid"] is True

    second.write_bytes(b"tampered")
    tampered = packages.verify_sha256_manifest(root)
    assert tampered["valid"] is False
    assert any("hash mismatch" in error for error in tampered["errors"])

    write_sums(include_second=False)
    uncovered = packages.verify_sha256_manifest(root)
    assert uncovered["valid"] is False
    assert any("exact coverage" in error for error in uncovered["errors"])


def test_release_audit_accepts_bootstrap_and_rejects_full_or_runtime_assets(tmp_path: Path) -> None:
    packages = _load_portable_packages()

    def make_zip(name: str, profile: str, extra: dict[str, bytes] | None = None) -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "Component/package/tts-more-package.json",
                json.dumps({"schema_version": 2, "component": "gpt-sovits", "package_profile": profile}),
            )
            for relative, payload in (extra or {}).items():
                archive.writestr(f"Component/{relative}", payload)
        return path

    bootstrap = make_zip("bootstrap.zip", "bootstrap")
    full = make_zip("full.zip", "full")
    contaminated = make_zip("bad.zip", "bootstrap", {"runtime/live/python.exe": b"runtime"})

    assert packages.audit_release_zip(bootstrap)["valid"] is True
    assert "profile=full" in packages.audit_release_zip(full)["errors"][0]
    assert "forbidden release asset" in packages.audit_release_zip(contaminated)["errors"][0]


@pytest.mark.parametrize(
    "polluted_path",
    (
        "runtime/python.exe",
        "Runtime/tools/ffmpeg.exe",
        r"runtime\python.exe",
        "ｒｕｎｔｉｍｅ/python.exe",
        "runtime./python.exe",
        "data/user/reference.wav",
        "DATA/LOCAL/install-state.json",
        r"data\cache\asset.bin",
        "data/models/voice.onnx",
    ),
)
def test_release_audit_rejects_all_runtime_and_private_data_path_variants(
    tmp_path: Path, polluted_path: str
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / f"polluted-{len(list(tmp_path.iterdir()))}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "Component/package/tts-more-package.json",
            json.dumps({"schema_version": 2, "component": "tts-more", "package_profile": "bootstrap"}),
        )
        archive.writestr(f"Component/{polluted_path}", b"private")

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("forbidden release asset" in error for error in report["errors"])


@pytest.mark.parametrize(
    "entries",
    (
        ("package/tts-more-package.json", "runtime/runtime.zip"),
        ("package/tts-more-package.json", "data/user/customer.wav"),
        ("Component/package/tts-more-package.json", "Other/Start.cmd"),
        ("Component", "Component/package/tts-more-package.json"),
        (r"Component\package\tts-more-package.json",),
        ("Component/package/tts-more-package.json", "component/Start.cmd"),
        ("Component/package/tts-more-package.json", "Component./Start.cmd"),
        ("Component/package/tts-more-package.json", "Ｃomponent/Start.cmd"),
    ),
)
def test_release_audit_requires_one_safe_unambiguous_top_level_package_directory(
    tmp_path: Path, entries: tuple[str, ...]
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / f"unsafe-root-{len(list(tmp_path.iterdir()))}.zip"
    manifest = json.dumps(
        {"schema_version": 2, "component": "tts-more", "package_profile": "bootstrap"}
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name in entries:
            archive.writestr(name, manifest if name.replace("\\", "/").endswith("tts-more-package.json") else b"payload")
    for name in entries:
        if "\\" in name:
            normalized = name.replace("\\", "/").encode("utf-8")
            archive_path.write_bytes(archive_path.read_bytes().replace(normalized, name.encode("utf-8")))

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("top-level package directory" in error for error in report["errors"])


@pytest.mark.parametrize(
    "extra_directory",
    ("Other/", "component/", "Component. /", "Ｃomponent/"),
)
def test_release_audit_counts_raw_directory_entries_when_enforcing_one_package_root(
    tmp_path: Path, extra_directory: str
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / "extra-directory-root.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Component/", b"")
        archive.writestr("Component/package/", b"")
        archive.writestr(
            "Component/package/tts-more-package.json",
            json.dumps(
                {"schema_version": 2, "component": "tts-more", "package_profile": "bootstrap"}
            ),
        )
        archive.writestr(extra_directory, b"")

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("top-level package directory" in error for error in report["errors"])


def test_tts_more_builder_uses_the_shared_zip64_writer() -> None:
    builder = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")

    assert "create-zip --package-root" in builder
    assert "Compress-Archive" not in builder
    assert '"$zip.spdx.json"' in builder
    assert '"$zip.licenses.json"' in builder
    assert '"$zip.acceptance.json"' in builder
    assert "^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$" in builder


def test_v2_builders_emit_completed_identity_protocol_and_data_contract() -> None:
    controller_builder = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")
    worker_builder = (REPO_ROOT / "integrations" / "windows" / "Build-Package.ps1").read_text(encoding="utf-8")

    assert 'package_id = "tts-more"; release_version = $Version' in controller_builder
    assert 'package_id = [string]$config.component; release_version = $Version' in worker_builder
    for builder in (controller_builder, worker_builder):
        assert (
            'protocol = @{ name = "tts-more-v1"; version = "1.0"; '
            'controller_range = ">=0.2.0,<0.3.0" }'
        ) in builder
        assert (
            'data = @{ user = "data/user"; local = "data/local"; cache = "data/cache"; '
            'operations = "data/local/operations" }'
        ) in builder


def test_portable_release_workflow_sanitizes_pr_ref_names() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(encoding="utf-8")

    assert "[^0-9A-Za-z._-]" in workflow
    assert "audit_release_zip" in workflow
    assert "EXPECTED_FULL_REJECTION" in workflow
    assert "verify-sha256 --package-root" in workflow
    assert workflow.index("audit-release --zip") < workflow.index("verify-sha256 --package-root")
    assert workflow.index("verify-sha256 --package-root") < workflow.index("actions/upload-artifact")


def test_tts_more_initializer_serializes_an_empty_controller_list_as_json() -> None:
    initializer = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(encoding="utf-8")

    assert "ConvertTo-Json -InputObject $videoControllers" in initializer


def test_four_pack_builder_is_full_only_and_refuses_github_actions() -> None:
    builder = (REPO_ROOT / "build-four-pack.ps1").read_text(encoding="utf-8")

    assert '$env:GITHUB_ACTIONS -eq "true"' in builder
    assert '-Profile Full' in builder
    assert '"tts-more", "gpt-sovits", "indextts", "cosyvoice"' in builder
    assert "source revision drift" in builder
    assert "compatibility-matrix.json" in builder
    assert "four-pack.provenance.json" in builder


def test_validate_manifest_rejects_invalid_v2_profile_and_nested_absolute_paths(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload["package_profile"] = "portable"
    payload["runtime"]["lock"] = "C:/machine/runtime.lock"
    manifest = tmp_path / "tts-more-package.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    report = packages.validate_manifest(manifest, tmp_path)

    assert report["valid"] is False
    assert "package_profile must be bootstrap or full" in report["errors"]
    assert "runtime.lock must be a relative path" in report["errors"]


def test_package_json_schema_defines_v1_compatibility_and_v2_output_contract() -> None:
    schema = json.loads(
        (REPO_ROOT / "packaging" / "portable" / "tts-more-package.schema.json").read_text(encoding="utf-8")
    )

    assert schema["$defs"]["v1"]["properties"]["schema_version"] == {"const": 1}
    assert schema["$defs"]["v2"]["properties"]["schema_version"] == {"const": 2}
    assert schema["$defs"]["v2"]["properties"]["package_profile"]["enum"] == ["bootstrap", "full"]
    assert {"package_id", "release_version", "protocol", "data"} <= set(schema["$defs"]["v2"]["required"])
    assert schema["$defs"]["v2"]["properties"]["protocol"]["properties"]["name"] == {"const": "tts-more-v1"}
    assert schema["$defs"]["v2"]["properties"]["data"]["required"] == ["user", "local", "cache", "operations"]
    for field in ("health_path", "capabilities_path"):
        assert schema["$defs"]["v2"]["properties"]["endpoint"]["properties"][field] == {
            "type": "string",
            "minLength": 1,
            "pattern": "^/",
        }
    assert schema["oneOf"] == [{"$ref": "#/$defs/v1"}, {"$ref": "#/$defs/v2"}]


@pytest.mark.parametrize("component", ("controller", "worker"))
def test_official_schema_accepts_real_builder_generated_v2_manifests(
    generated_v2_manifests: dict[str, dict[str, object]], component: str
) -> None:
    manifest = generated_v2_manifests[component]

    errors = sorted(_official_manifest_validator().iter_errors(manifest), key=lambda error: list(error.path))

    assert not errors, "\n".join(error.message for error in errors)


@pytest.mark.parametrize("field", ("health_path", "capabilities_path"))
@pytest.mark.parametrize("invalid_value", ("", "api/health", 42))
def test_official_schema_and_python_validator_reject_invalid_v2_endpoint_paths(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload["endpoint"][field] = invalid_value
    manifest = _write_v2_manifest(tmp_path, payload)

    schema_errors = list(_official_manifest_validator().iter_errors(payload))
    python_report = packages.validate_manifest(manifest, tmp_path)

    assert schema_errors, f"official schema accepted endpoint.{field}={invalid_value!r}"
    assert python_report["valid"] is False
    assert f"endpoint.{field} must start with /" in python_report["errors"]


def test_gpt_dev_sample_manifest_uses_builtin_windows_zip_runtime() -> None:
    manifest = json.loads(
        (REPO_ROOT / "deployment" / "portable" / "gpt-sovits-dev" / "package" / "tts-more-package.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["runtime"] == "runtime/runtime.zip"


def test_prepare_runtime_extracts_once_and_reuses_matching_build(tmp_path: Path, monkeypatch) -> None:
    launcher = _load_portable_launcher()
    package_root = tmp_path / "GPT-SoVITS-dev"
    runtime_live = package_root / "runtime" / "live"
    manifest = package_root / "package" / "tts-more-package.json"
    manifest.parent.mkdir(parents=True)
    payload = _valid_gpt_manifest()
    payload["build_id"] = "build-001"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (package_root / "runtime").mkdir(exist_ok=True)
    (package_root / "runtime" / "runtime.zip").write_bytes(b"portable-runtime")

    calls: list[tuple[Path, Path]] = []

    def fake_extract(archive: Path, destination: Path) -> None:
        calls.append((archive, destination))
        runtime_live.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(launcher, "extract_archive", fake_extract)

    assert launcher.prepare_runtime(package_root) == runtime_live
    assert launcher.prepare_runtime(package_root) == runtime_live
    assert calls == [(package_root / "runtime" / "runtime.zip", runtime_live)]


def test_prepare_runtime_finalizes_start_cmd_extraction_without_extracting_twice(tmp_path: Path, monkeypatch) -> None:
    launcher = _load_portable_launcher()
    package_root = tmp_path / "GPT-SoVITS-dev"
    runtime_live = package_root / "runtime" / "live"
    manifest = package_root / "package" / "tts-more-package.json"
    manifest.parent.mkdir(parents=True)
    payload = _valid_gpt_manifest()
    payload["build_id"] = "build-001"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (package_root / "runtime").mkdir(exist_ok=True)
    (package_root / "runtime" / "runtime.zip").write_bytes(b"portable-runtime")
    runtime_live.mkdir(parents=True)
    (runtime_live / "python.exe").write_bytes(b"package-python")

    def fail_extract(_archive: Path, _destination: Path) -> None:
        raise AssertionError("the Start.cmd extraction must be reused")

    monkeypatch.setattr(launcher, "extract_archive", fail_extract)

    assert launcher.prepare_runtime(package_root) == runtime_live
    assert json.loads((runtime_live / ".portable-build.json").read_text(encoding="utf-8")) == {
        "build_id": "build-001"
    }


def test_stop_worker_reads_bom_but_rejects_live_legacy_record_without_safe_identity(
    tmp_path: Path,
) -> None:
    launcher = _load_portable_launcher()
    package_root = tmp_path / "GPT-SoVITS-dev"
    executable = package_root / "runtime" / "live" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"portable-python")
    record = package_root / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"pid": 1234, "executable_path": str(executable), "port": 9883}),
        encoding="utf-8-sig",
    )
    with pytest.raises(RuntimeError, match="legacy PID record"):
        launcher.stop_worker(
            package_root,
            inspector=lambda _pid: {
                "pid": 1234,
                "created_at": "unknown",
                "executable_path": str(executable),
                "command_args": [],
            },
            terminator=lambda _pid: pytest.fail("legacy process must not be terminated"),
            port_owner_inspector=lambda _port: {1234},
        )
    assert record.exists()


def test_worker_launch_scripts_use_package_relative_runtime_paths() -> None:
    launcher_roots = (
        REPO_ROOT / "deployment" / "portable" / "common",
        REPO_ROOT / "deployment" / "portable" / "gpt-sovits-dev",
    )

    for launcher_root in launcher_roots:
        start_path = launcher_root / ("Start-Worker.cmd" if launcher_root.name == "common" else "Start.cmd")
        stop_path = launcher_root / ("Stop-Worker.cmd" if launcher_root.name == "common" else "Stop.cmd")
        assert start_path.is_file(), f"portable start launcher is missing: {start_path}"
        assert stop_path.is_file(), f"portable stop launcher is missing: {stop_path}"
        start = start_path.read_text(encoding="utf-8")
        stop = stop_path.read_text(encoding="utf-8")

        normalization = 'for %%I in ("%~dp0.") do set "PACKAGE_ROOT=%%~fI"'
        assert normalization in start
        assert normalization in stop
        assert "portable_launcher.py" in start
        assert "TTS_MORE_ARTIFACT_ROOT=%PACKAGE_ROOT%\\data\\local\\artifacts" in start
        assert "Expand-Archive" in start
        assert "runtime.zip" in start
        assert 'if not exist "%RUNTIME_ROOT%\\python.exe" (' in start
        assert '.portable-build.json" (' not in start
        assert '"%PACKAGE_ROOT%\\app\\scripts\\portable_launcher.py"' in start
        assert "worker.pid.json" in stop
        assert '"%PACKAGE_ROOT%\\app\\scripts\\portable_launcher.py"' in stop


def test_gpt_portable_builder_requires_dev_lock_and_offline_payloads() -> None:
    builder_path = REPO_ROOT / "scripts" / "build-portable-gpt-dev.ps1"
    assert builder_path.is_file(), "GPT portable builder script is missing"
    script = builder_path.read_text(encoding="utf-8")
    requirements = (REPO_ROOT / "packaging" / "portable" / "gpt-dev-requirements.lock.txt").read_text(encoding="utf-8")
    manifest = json.loads(
        (REPO_ROOT / "deployment" / "portable" / "gpt-sovits-dev" / "package" / "tts-more-package.json").read_text(
            encoding="utf-8"
        )
    )

    assert '"variant": "dev"' in script
    assert "bootstrap-conda.ps1" in script
    assert "conda-pack" in script
    assert "onnxruntime-gpu==1.26.0" in script
    assert "runtime.zip" in script
    assert "TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)" in script
    assert "Install-GptModelPayloads" in script
    assert "Invoke-GptDownloadWithFallback" in script
    assert "Test-ZipArchive" in script
    assert "curl.exe" in script
    assert "--range" in script
    assert "--speed-time" in script
    assert "--speed-limit" in script
    assert "--proxy" in script
    assert "reuse validated GPT payload" in script
    assert "PIP_CACHE_DIR" in script
    assert "install.ps1" not in script
    assert "[switch]$ReuseRuntime" in script
    assert "reuse existing private GPT runtime" in script
    assert "setuptools=80.9.0" in script
    assert "gpt-dev-requirements.pip.txt" in script
    assert "& $Command | Out-Host" in script
    assert "TrimStart([char]'\\', [char]'/')" in script
    assert '"bin\\7z.exe"' in script
    assert 'add worker launcher to runtime.zip' in script
    assert 'for %%I in ("%~dp0..\\..") do set "PACKAGE_ROOT=%%~fI"' in script
    assert 'set "TTS_MORE_PACKAGE_ROOT=%PACKAGE_ROOT%"' in script
    assert "$root = $env:TTS_MORE_PACKAGE_ROOT" in script
    assert "$root = $args[0]" not in script
    for dependency in ("numpy==1.26.4", "MarkupSafe==2.0.1", "websockets==12.0", "starlette==0.46.2", "setuptools==80.9.0"):
        assert dependency in requirements
    assert manifest["component"] == "gpt-sovits-dev"
    assert manifest["port"] == 9883


def test_every_local_tts_component_has_a_path_relative_start_and_stop_launcher() -> None:
    launchers = (
        (REPO_ROOT, None, "scripts\\Invoke-PortableStart.ps1"),
        (REPO_ROOT / "deployment" / "portable" / "gpt-sovits-dev", "9883", "runtime\\runtime.zip"),
        (REPO_ROOT / "deployment" / "tts-repos" / "indextts" / "launchers", "7860", ".venv\\Scripts\\python.exe"),
        (REPO_ROOT / "deployment" / "tts-repos" / "cosyvoice" / "launchers", "9882", ".venv\\Scripts\\python.exe"),
    )

    for root, port, entrypoint in launchers:
        start = root / "Start.cmd"
        stop = root / "Stop.cmd"
        assert start.is_file(), f"missing Start.cmd: {start}"
        assert stop.is_file(), f"missing Stop.cmd: {stop}"
        contents = start.read_text(encoding="utf-8")
        assert "%~dp0" in contents
        if port is not None:
            assert port in contents
        assert entrypoint in contents
        assert "stop-production.ps1" in stop.read_text(encoding="utf-8") if root == REPO_ROOT else "pid.json" in stop.read_text(encoding="utf-8")
    assert "8000" in (REPO_ROOT / "scripts" / "start-production.ps1").read_text(encoding="utf-8")


def test_local_start_launchers_allow_non_destructive_port_overrides() -> None:
    app_launcher = (REPO_ROOT / "scripts" / "start-dev.ps1").read_text(encoding="utf-8")
    app_stop = (REPO_ROOT / "Stop-Dev.cmd").read_text(encoding="utf-8")
    assert "$env:TTS_MORE_BACKEND_PORT" in app_launcher
    assert "$env:TTS_MORE_FRONTEND_PORT" in app_launcher
    assert 'backend_port = $BackendPort' in app_launcher
    assert 'frontend_port = $FrontendPort' in app_launcher
    assert '-ArgumentList "dev", "--host", "127.0.0.1", "--port", ([string]$FrontendPort)' in app_launcher
    assert '-ArgumentList "dev", "--", "--host"' not in app_launcher
    assert "$processId = $payload.$name" in app_stop
    assert "$pid = $payload.$name" not in app_stop.lower()

    worker_launchers = (
        (REPO_ROOT / "deployment" / "tts-repos" / "indextts" / "launchers" / "Start.cmd", "7860"),
        (REPO_ROOT / "deployment" / "tts-repos" / "cosyvoice" / "launchers" / "Start.cmd", "9882"),
    )
    for launcher_path, default_port in worker_launchers:
        launcher = launcher_path.read_text(encoding="utf-8")
        assert f'if not defined TTS_MORE_PORT set "TTS_MORE_PORT={default_port}"' in launcher
        assert 'set "NO_PROXY=127.0.0.1,localhost,%NO_PROXY%"' in launcher
        assert 'set "no_proxy=%NO_PROXY%"' in launcher
        assert "port {0} is already in use" in launcher
        assert "-f $env:TTS_MORE_PORT" in launcher

    cosy_launcher = (
        REPO_ROOT / "deployment" / "tts-repos" / "cosyvoice" / "launchers" / "Start.cmd"
    ).read_text(encoding="utf-8")
    assert "$model = Join-Path $root 'pretrained_models\\CosyVoice-300M'" in cosy_launcher
    assert "CosyVoice-300M model directory is missing" in cosy_launcher
    assert "'--model_dir', $model" in cosy_launcher
