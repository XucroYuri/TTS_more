from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_controller(package_root: Path) -> None:
    scripts = package_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "Invoke-PortableStart.ps1", scripts)
    shutil.copy2(REPO_ROOT / "scripts" / "Show-PortableProgress.ps1", scripts)


def _source_package(package_root: Path, *, delay_seconds: int = 0) -> None:
    _copy_controller(package_root)
    _write_text(package_root / "packaging" / "portable" / "runtime.lock.json", "{}\n")
    _write_text(package_root / "packaging" / "portable" / "models.lock.json", "{}\n")
    _write_text(
        package_root / "scripts" / "initialize-portable.ps1",
        rf'''[CmdletBinding()]
param(
    [string]$PackageRoot = "",
    [string]$OperationRoot = "",
    [string]$CancelFile = ""
)
$ErrorActionPreference = "Stop"
Add-Content -LiteralPath (Join-Path $PackageRoot "order.log") -Value "initialize"
Add-Content -LiteralPath (Join-Path $PackageRoot "initialize-count.log") -Value "1"
if ({delay_seconds} -gt 0) {{ Start-Sleep -Seconds {delay_seconds} }}
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "runtime\live"), (Join-Path $PackageRoot "data\local") | Out-Null
[IO.File]::WriteAllBytes((Join-Path $PackageRoot "runtime\live\python.exe"), [byte[]](1))
$state = @{{ schema_version = 1; component = "tts-more"; build_id = "source-checkout"; profile = "cpu" }} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $PackageRoot "data\local\install-state.json"), $state, [Text.UTF8Encoding]::new($false))
exit 0
''',
    )
    _write_text(
        package_root / "scripts" / "start-production.ps1",
        '''[CmdletBinding()]
param(
    [string]$PackageRoot = "",
    [string]$OperationRoot = "",
    [Nullable[int]]$PortOverride = $null
)
Add-Content -LiteralPath (Join-Path $PackageRoot "order.log") -Value "start"
Add-Content -LiteralPath (Join-Path $PackageRoot "start-count.log") -Value "1"
if ($null -ne $PortOverride) { [IO.File]::WriteAllText((Join-Path $PackageRoot "port.log"), [string]$PortOverride) }
exit 0
''',
    )


def _manifest(package_root: Path, *, profile: str, operations: str = "data/local/operations") -> dict[str, object]:
    runtime_lock = package_root / "packaging" / "portable" / "runtime.lock.json"
    model_lock = package_root / "packaging" / "portable" / "models.lock.json"
    _write_text(runtime_lock, "{}\n")
    _write_text(model_lock, "{}\n")
    payload: dict[str, object] = {
        "schema_version": 2,
        "component": "tts-more",
        "package_id": "tts-more",
        "release_version": "0.2.0",
        "version": "0.2.0",
        "build_id": "build-test",
        "package_profile": profile,
        "platform": "windows-x64",
        "api_contract": "tts-more-v1",
        "protocol": {"name": "tts-more-v1", "version": "1.0", "controller_range": ">=0.2.0,<0.3.0"},
        "source": {"repository": "https://example.invalid/repo.git", "revision": "a" * 40},
        "integration": {"version": "2.0.0", "source_revision": "b" * 40, "bundle_sha256": "c" * 64},
        "runtime": {
            "python_version": "3.11",
            "device_profiles": ["cpu"],
            "lock": "packaging/portable/runtime.lock.json",
            "state_path": "data/local/install-state.json",
        },
        "models": {"lock": "packaging/portable/models.lock.json", "required": False},
        "data_root": "data/local",
        "data": {
            "user": "data/user",
            "local": "data/local",
            "cache": "data/cache",
            "operations": operations,
        },
        "launchers": {
            "initialize": "Initialize.cmd",
            "start": "Start.cmd",
            "stop": "Stop.cmd",
            "repair": "Repair.cmd",
            "build": "Build-Package.ps1",
        },
        "endpoint": {
            "default_url": "http://127.0.0.1:8000",
            "port": 8000,
            "health_path": "/api/health",
            "capabilities_path": "/capabilities",
            "bind_policy": "loopback",
        },
        "capabilities": ["orchestrator"],
        "sha256_manifest": "SHA256SUMS.txt",
        "licenses": "THIRD_PARTY_NOTICES.json",
    }
    _write_text(package_root / "package" / "tts-more-package.json", json.dumps(payload))
    return payload


def _run_controller(package_root: Path, *arguments: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    environment = os.environ.copy()
    environment["PATH"] = ""
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(package_root / "scripts" / "Invoke-PortableStart.ps1"),
            *arguments,
        ],
        cwd=package_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _operation_directories(operations_root: Path) -> list[Path]:
    return sorted(
        path
        for path in operations_root.iterdir()
        if path.is_dir() and path.name != "run" and _is_uuid(path.name)
    )


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def test_source_start_initializes_before_service_without_system_python(tmp_path: Path) -> None:
    package_root = tmp_path / "source checkout"
    _source_package(package_root)
    operation_id = str(uuid4())

    result = _run_controller(
        package_root,
        "-OperationId",
        operation_id,
        "-ManagedBy",
        "tts-more",
        "-NoUi",
        "-PortOverride",
        "8123",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (package_root / "order.log").read_text(encoding="utf-8").splitlines() == ["initialize", "start"]
    assert (package_root / "port.log").read_text(encoding="utf-8") == "8123"
    operation_root = package_root / "data" / "local" / "operations" / operation_id
    operation = json.loads((operation_root / "operation.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (operation_root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert operation["operation_id"] == operation_id
    assert operation["component"] == "tts-more"
    assert operation["action"] == "start"
    assert operation["initiator"] == "tts-more"
    assert operation["status"] == "ready"
    assert operation["exit_code"] == 0
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert {"checking", "installing", "starting", "ready"} <= {event["phase"] for event in events}


def test_full_package_fails_closed_in_manifest_operation_directory_without_initializing(tmp_path: Path) -> None:
    package_root = tmp_path / "full package"
    _source_package(package_root)
    _manifest(package_root, profile="full", operations="state/start-operations")
    operation_id = str(uuid4())

    result = _run_controller(package_root, "-OperationId", operation_id, "-NoUi")

    assert result.returncode == 22
    assert "PACKAGE_CORRUPT" in result.stdout + result.stderr
    assert not (package_root / "initialize-count.log").exists()
    operation_root = package_root / "state" / "start-operations" / operation_id
    operation = json.loads((operation_root / "operation.json").read_text(encoding="utf-8"))
    assert operation["status"] == "blocked"
    events = [json.loads(line) for line in (operation_root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["error_code"] == "PACKAGE_CORRUPT"


def test_manifest_operation_directory_cannot_escape_package(tmp_path: Path) -> None:
    package_root = tmp_path / "malicious package"
    _source_package(package_root)
    _manifest(package_root, profile="bootstrap", operations="../outside/operations")

    result = _run_controller(package_root, "-NoUi")

    assert result.returncode == 22
    assert "PACKAGE_CORRUPT" in result.stdout + result.stderr
    assert not (tmp_path / "outside").exists()
    assert not (package_root / "initialize-count.log").exists()


def test_manifest_operation_directory_cannot_escape_through_junction(tmp_path: Path) -> None:
    package_root = tmp_path / "junction package"
    _source_package(package_root)
    _manifest(package_root, profile="bootstrap", operations="redirected/start-operations")
    outside = tmp_path / "outside-junction-target"
    outside.mkdir()
    try:
        (package_root / "redirected").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if not POWERSHELL:
            pytest.skip(f"directory symlinks are unavailable: {error}")
        junction_environment = os.environ.copy()
        junction_environment["A4_JUNCTION_PATH"] = str(package_root / "redirected")
        junction_environment["A4_JUNCTION_TARGET"] = str(outside)
        junction = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path $env:A4_JUNCTION_PATH -Target $env:A4_JUNCTION_TARGET | Out-Null",
            ],
            env=junction_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"directory junctions are unavailable: {junction.stderr}")

    result = _run_controller(package_root, "-NoUi")

    assert result.returncode == 22
    assert "PACKAGE_CORRUPT" in result.stdout + result.stderr
    assert not (outside / "start-operations").exists()


def test_initializer_accepts_manifest_scoped_operation_contract_before_bootstrap(tmp_path: Path) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    package_root = tmp_path / "manifest operations package"
    _manifest(package_root, profile="bootstrap", operations="state/start-operations")
    scripts = package_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "initialize-portable.ps1", scripts)
    operation_id = str(uuid4())
    operation = package_root / "state" / "start-operations" / operation_id
    operation.mkdir(parents=True)
    cancel = operation / "cancel.requested"
    cancel.touch()

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "initialize-portable.ps1"),
            "-PackageRoot",
            str(package_root),
            "-OperationRoot",
            str(operation),
            "-CancelFile",
            str(cancel),
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 20, result.stdout + result.stderr


def test_initializer_rejects_manifest_operation_junction(tmp_path: Path) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    package_root = tmp_path / "initializer junction package"
    _manifest(package_root, profile="bootstrap", operations="redirected/start-operations")
    scripts = package_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "initialize-portable.ps1", scripts)
    outside = tmp_path / "initializer-outside"
    outside.mkdir()
    junction_environment = os.environ.copy()
    junction_environment["A4_JUNCTION_PATH"] = str(package_root / "redirected")
    junction_environment["A4_JUNCTION_TARGET"] = str(outside)
    junction = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "New-Item -ItemType Junction -Path $env:A4_JUNCTION_PATH -Target $env:A4_JUNCTION_TARGET | Out-Null",
        ],
        env=junction_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"directory junctions are unavailable: {junction.stderr}")
    operation = package_root / "redirected" / "start-operations" / str(uuid4())
    operation.mkdir(parents=True)
    cancel = operation / "cancel.requested"
    cancel.touch()

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "initialize-portable.ps1"),
            "-PackageRoot",
            str(package_root),
            "-OperationRoot",
            str(operation),
            "-CancelFile",
            str(cancel),
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 20
    assert "reparse" in (result.stdout + result.stderr).lower()


def test_package_private_installer_uses_manifest_scoped_operations_root(tmp_path: Path) -> None:
    from scripts.portable_install import validate_operation_paths

    package_root = tmp_path / "installer package"
    _manifest(package_root, profile="bootstrap", operations="state/start-operations")
    operation = package_root / "state" / "start-operations" / str(uuid4())
    cancel = operation / "cancel.requested"

    assert validate_operation_paths(package_root, operation, cancel) == (operation.resolve(), cancel.resolve())


def test_controller_preserves_initializer_cancellation_exit_code(tmp_path: Path) -> None:
    package_root = tmp_path / "cancelled package"
    _source_package(package_root)
    _write_text(
        package_root / "scripts" / "initialize-portable.ps1",
        '''[CmdletBinding()]
param([string]$PackageRoot = "", [string]$OperationRoot = "", [string]$CancelFile = "")
exit 20
''',
    )
    operation_id = str(uuid4())

    result = _run_controller(package_root, "-OperationId", operation_id, "-NoUi")

    assert result.returncode == 20, result.stdout + result.stderr
    operation = json.loads(
        (
            package_root
            / "data"
            / "local"
            / "operations"
            / operation_id
            / "operation.json"
        ).read_text(encoding="utf-8")
    )
    assert operation["status"] == "stopped"
    assert operation["exit_code"] == 20


def test_repeated_start_attaches_to_active_package_operation(tmp_path: Path) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    package_root = tmp_path / "concurrent package"
    _source_package(package_root, delay_seconds=2)
    environment = os.environ.copy()
    environment["PATH"] = ""
    command = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(package_root / "scripts" / "Invoke-PortableStart.ps1"),
        "-NoUi",
    ]
    first = subprocess.Popen(
        command,
        cwd=package_root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    active = package_root / "data" / "local" / "operations" / "active-start.json"
    deadline = time.monotonic() + 10
    while not active.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert active.is_file(), first.communicate(timeout=5)

    second = subprocess.run(
        command,
        cwd=package_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    first_stdout, first_stderr = first.communicate(timeout=15)

    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Attaching to active operation" in second.stdout
    assert (package_root / "initialize-count.log").read_text(encoding="utf-8").splitlines() == ["1"]
    assert (package_root / "start-count.log").read_text(encoding="utf-8").splitlines() == ["1"]
    assert len(_operation_directories(package_root / "data" / "local" / "operations")) == 1


def test_launcher_staging_progress_and_error_catalog_contracts(tmp_path: Path) -> None:
    start = (REPO_ROOT / "Start.cmd").read_text(encoding="utf-8")
    controller = (REPO_ROOT / "scripts" / "Invoke-PortableStart.ps1").read_text(encoding="utf-8")
    progress = (REPO_ROOT / "scripts" / "Show-PortableProgress.ps1").read_text(encoding="utf-8")
    builder = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")
    sync_source = (REPO_ROOT / "scripts" / "sync_integrations.py").read_text(encoding="utf-8")
    catalog = json.loads(
        (REPO_ROOT / "packaging" / "portable" / "error-catalog.zh-CN.json").read_text(encoding="utf-8")
    )

    assert "Invoke-PortableStart.ps1" in start
    assert "start-production.ps1" not in start
    for function in (
        "Get-PackageContext",
        "Assert-PackageWritable",
        "Open-PackageOperationLock",
        "Test-InstallState",
        "Invoke-Initialize",
        "Invoke-ServiceStart",
        "Resolve-PortableExitCode",
    ):
        assert f"function {function}" in controller
    assert controller.index("Invoke-Initialize -Root") < controller.index("Invoke-ServiceStart -Root")
    assert "System.Windows.Forms" in progress
    assert "cancel.requested" in progress
    assert "console" in progress.lower()
    for staged_name in ("Invoke-PortableStart.ps1", "Show-PortableProgress.ps1", "portable_operations.py"):
        assert staged_name in builder
        assert staged_name in sync_source

    expected_codes = {
        "DOWNLOAD_NETWORK_INTERRUPTED",
        "DISK_SPACE_INSUFFICIENT",
        "CUDA_PROBE_FAILED",
        "PORT_IN_USE",
        "PACKAGE_NOT_WRITABLE",
        "PACKAGE_CORRUPT",
    }
    assert expected_codes <= set(catalog["errors"])
    for code in expected_codes:
        assert {"event", "cause", "unchanged_data", "next_action"} <= set(catalog["errors"][code])

    from scripts import sync_integrations

    target = tmp_path / "fork"
    sync_integrations.sync_integration(REPO_ROOT, target, "cosyvoice", "d" * 40)
    synced_start = (target / "Start.cmd").read_text(encoding="utf-8")
    assert "Invoke-PortableStart.ps1" in synced_start
    assert (target / "tts_more" / "Invoke-PortableStart.ps1").is_file()
    assert (target / "tts_more" / "Show-PortableProgress.ps1").is_file()
    assert (target / "tts_more" / "error-catalog.zh-CN.json").is_file()


def test_worker_initializer_records_staged_manifest_build_id() -> None:
    initializer = (REPO_ROOT / "integrations" / "windows" / "Initialize.ps1").read_text(encoding="utf-8")

    assert '$manifestPath = Join-Path $Root "package\\tts-more-package.json"' in initializer
    assert '--build-id $buildId' in initializer
    assert '--build-id source-checkout' not in initializer
