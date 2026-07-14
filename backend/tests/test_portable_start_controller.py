from __future__ import annotations

import json
import hashlib
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
    shutil.copy2(REPO_ROOT / "scripts" / "Portable-Validation.ps1", scripts)


def _source_package(package_root: Path, *, delay_seconds: int = 0) -> None:
    _copy_controller(package_root)
    _write_text(package_root / "packaging" / "portable" / "runtime.lock.json", "{}\n")
    _write_text(package_root / "packaging" / "portable" / "models.lock.json", "{}\n")
    _compile_fake_python(package_root / "runtime" / "live" / "python.exe")
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
$runtimeSha = (Get-FileHash -LiteralPath (Join-Path $PackageRoot "packaging\portable\runtime.lock.json") -Algorithm SHA256).Hash.ToLowerInvariant()
$modelSha = (Get-FileHash -LiteralPath (Join-Path $PackageRoot "packaging\portable\models.lock.json") -Algorithm SHA256).Hash.ToLowerInvariant()
$state = @{{ schema_version = 1; component = "tts-more"; build_id = "source-checkout"; profile = "cpu"; runtime_lock_sha256 = $runtimeSha; model_lock_sha256 = $modelSha; ready = $true }} | ConvertTo-Json
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
    for relative in (
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "Build-Package.ps1",
        "THIRD_PARTY_NOTICES.json",
    ):
        _write_text(package_root / relative, "placeholder\n")
    _write_sha256_manifest(package_root)
    return payload


def _compile_fake_python(path: Path, *, version: str = "3.11", imports_ok: bool = True) -> None:
    compiler_candidates = (
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    )
    compiler = next((candidate for candidate in compiler_candidates if candidate.is_file()), None)
    if compiler is None:
        pytest.skip("the .NET C# compiler is unavailable")
    source = path.with_suffix(".cs")
    _write_text(
        source,
        rf'''using System;
using System.IO;
class FakePython {{
  static string Value(string[] args, string name) {{
    for (int i = 0; i + 1 < args.Length; i++) if (args[i] == name) return args[i + 1];
    return "";
  }}
  static int Main(string[] args) {{
    string joined = String.Join(" ", args);
    string executed = Environment.GetEnvironmentVariable("A4_FAKE_EXEC_MARKER");
    if (!String.IsNullOrEmpty(executed)) File.AppendAllText(executed, joined + Environment.NewLine);
    if (joined.Contains("version_info")) {{ Console.WriteLine("{version}"); return 0; }}
    if (joined.Contains("definitely_missing") || {str(not imports_ok).lower()}) return 1;
    if (joined.Contains("write-state")) {{
      string build = Environment.GetEnvironmentVariable("A4_FAKE_WRITE_BAD_STATE") == "1" ? "bad-build" : Value(args,"--build-id");
      string json = "{{\"schema_version\":1,\"component\":\"" + Value(args,"--component") + "\",\"build_id\":\"" + build + "\",\"profile\":\"" + Value(args,"--profile") + "\",\"runtime_lock_sha256\":\"" + Value(args,"--runtime-lock-sha256") + "\",\"model_lock_sha256\":\"" + Value(args,"--model-lock-sha256") + "\",\"ready\":true}}";
      Directory.CreateDirectory(Path.GetDirectoryName(Value(args,"--path")));
      File.WriteAllText(Value(args,"--path"), json);
      return 0;
    }}
    string marker = Environment.GetEnvironmentVariable("A4_FAKE_SPAWN_MARKER");
    if (!String.IsNullOrEmpty(marker) && !joined.Contains("-c")) File.WriteAllText(marker, joined);
    return 0;
  }}
}}
''',
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(compiler), "/nologo", f"/out:{path}", str(source)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source.unlink()


def _write_sha256_manifest(package_root: Path) -> None:
    lines = []
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS.txt" or "operations" in path.parts:
            continue
        relative = path.relative_to(package_root).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    _write_text(package_root / "SHA256SUMS.txt", "\n".join(lines) + "\n")


def _prepare_full_package(
    package_root: Path,
    *,
    model_mode: str = "valid",
    runtime_version: str = "3.11",
    imports_ok: bool = True,
) -> None:
    _source_package(package_root)
    payload = _manifest(package_root, profile="full")
    payload["models"]["required"] = True
    _write_text(package_root / "package" / "tts-more-package.json", json.dumps(payload))
    runtime_lock = package_root / "packaging" / "portable" / "runtime.lock.json"
    model_lock = package_root / "packaging" / "portable" / "models.lock.json"
    _write_text(
        runtime_lock,
        json.dumps(
            {
                "schema_version": 1,
                "component": "tts-more",
                "python_version": "3.11",
                "import_probe": "import sys",
                "profiles": {"cpu": {"dependency_lock": "backend/uv.lock"}},
            }
        ),
    )
    expected_model = b"locked-model"
    model_target = package_root / "models" / "voice.bin"
    model_payload = {
        "schema_version": 1,
        "component": "tts-more",
        "component": "tts-more",
        "required": True,
        "required_paths": ["models/voice.bin"],
        "assets": [
            {
                "id": "voice",
                "target": "models/voice.bin",
                "size_bytes": len(expected_model),
                "sha256": hashlib.sha256(expected_model).hexdigest(),
            }
        ],
    }
    _write_text(model_lock, json.dumps(model_payload))
    if model_mode != "missing":
        model_target.parent.mkdir(parents=True)
        model_target.write_bytes(expected_model if model_mode == "valid" else b"locked-modem")
    python = package_root / "runtime" / "live" / "python.exe"
    _compile_fake_python(python, version=runtime_version, imports_ok=imports_ok)
    state = {
        "schema_version": 1,
        "component": "tts-more",
        "build_id": "build-test",
        "profile": "cpu",
        "runtime_lock_sha256": hashlib.sha256(runtime_lock.read_bytes()).hexdigest(),
        "model_lock_sha256": hashlib.sha256(model_lock.read_bytes()).hexdigest(),
        "ready": True,
    }
    _write_text(package_root / "data" / "local" / "install-state.json", json.dumps(state))
    for relative in (
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "Build-Package.ps1",
        "THIRD_PARTY_NOTICES.json",
    ):
        _write_text(package_root / relative, "placeholder\n")
    _write_sha256_manifest(package_root)


def _run_controller(
    package_root: Path,
    *arguments: str,
    timeout: int = 20,
    environment_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    environment = os.environ.copy()
    environment["PATH"] = ""
    environment.update(environment_updates or {})
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
    shutil.copy2(REPO_ROOT / "scripts" / "Portable-Validation.ps1", scripts)
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
    shutil.copy2(REPO_ROOT / "scripts" / "Portable-Validation.ps1", scripts)
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


def test_repeated_start_stays_attached_to_live_owner_beyond_twelve_seconds(tmp_path: Path) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    package_root = tmp_path / "long-live-owner"
    _source_package(package_root, delay_seconds=14)
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
        timeout=25,
        check=False,
    )
    first_stdout, first_stderr = first.communicate(timeout=25)

    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Attaching to active operation" in second.stdout
    assert (package_root / "initialize-count.log").read_text(encoding="utf-8").splitlines() == ["1"]
    assert (package_root / "start-count.log").read_text(encoding="utf-8").splitlines() == ["1"]


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
    for staged_name in ("Invoke-PortableStart.ps1", "Show-PortableProgress.ps1", "Portable-Validation.ps1", "portable_operations.py"):
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
    assert (target / "tts_more" / "Portable-Validation.ps1").is_file()
    assert (target / "tts_more" / "error-catalog.zh-CN.json").is_file()


def test_worker_initializer_records_staged_manifest_build_id() -> None:
    initializer = (REPO_ROOT / "integrations" / "windows" / "Initialize.ps1").read_text(encoding="utf-8")

    assert '$manifestPath = Join-Path $Root "package\\tts-more-package.json"' in initializer
    assert '--build-id $buildId' in initializer
    assert '--build-id source-checkout' not in initializer


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("launchers.initialize", "C:/outside/Initialize.cmd"),
        ("licenses", "../outside/licenses.json"),
        ("sha256_manifest", "D:/outside/SHA256SUMS.txt"),
        ("platform", "linux-x64"),
        ("runtime.python_version", "3.10"),
        ("schema_version", "2"),
        ("endpoint.port", "8000"),
        ("endpoint.health_path", ""),
        ("endpoint.health_path", "health"),
        ("endpoint.health_path", 42),
        ("endpoint.capabilities_path", ""),
        ("endpoint.capabilities_path", "capabilities"),
        ("endpoint.capabilities_path", 42),
        ("capabilities", ["orchestrator", 1]),
        ("runtime.device_profiles", ["cpu", 1]),
        ("source", []),
        ("protocol", "tts-more-v1"),
        ("launchers.start", 17),
        ("data.operations", []),
        ("unexpected", "not-allowed"),
    ),
)
def test_staged_manifest_is_completely_validated_before_operation_creation(
    tmp_path: Path, field: str, value: object
) -> None:
    package_root = tmp_path / field.replace(".", "-")
    _source_package(package_root)
    payload = _manifest(package_root, profile="bootstrap")
    target: dict[str, object] = payload
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]  # type: ignore[assignment,index]
    target[parts[-1]] = value
    _write_text(package_root / "package" / "tts-more-package.json", json.dumps(payload))

    result = _run_controller(package_root, "-NoUi")

    assert result.returncode == 22
    assert "PACKAGE_CORRUPT" in result.stdout + result.stderr
    assert not (package_root / "data" / "local" / "operations").exists()
    assert not (package_root / "initialize-count.log").exists()


@pytest.mark.parametrize("required_field", ("package_id", "protocol", "runtime", "capabilities"))
def test_staged_manifest_missing_required_field_creates_no_operation(
    tmp_path: Path, required_field: str
) -> None:
    package_root = tmp_path / f"missing-{required_field}"
    _source_package(package_root)
    payload = _manifest(package_root, profile="bootstrap")
    del payload[required_field]
    _write_text(package_root / "package" / "tts-more-package.json", json.dumps(payload))

    result = _run_controller(package_root, "-NoUi")

    assert result.returncode == 22
    assert not (package_root / "data" / "local" / "operations").exists()


@pytest.mark.parametrize(
    ("model_mode", "runtime_version", "imports_ok"),
    (
        ("missing", "3.11", True),
        ("corrupt", "3.11", True),
        ("valid", "3.10", True),
        ("valid", "3.11", False),
    ),
)
def test_full_package_validates_models_runtime_version_and_imports_before_service(
    tmp_path: Path, model_mode: str, runtime_version: str, imports_ok: bool
) -> None:
    package_root = tmp_path / f"full-{model_mode}-{runtime_version}-{imports_ok}"
    _prepare_full_package(
        package_root,
        model_mode=model_mode,
        runtime_version=runtime_version,
        imports_ok=imports_ok,
    )

    result = _run_controller(package_root, "-NoUi", timeout=30)

    assert result.returncode == 22, result.stdout + result.stderr
    assert "PACKAGE_CORRUPT" in result.stdout + result.stderr
    assert not (package_root / "initialize-count.log").exists()
    assert not (package_root / "start-count.log").exists()


@pytest.mark.parametrize("integrity_case", ("python-uncovered", "python-hash", "model-hash"))
def test_full_package_never_executes_runtime_before_all_integrity_checks_pass(
    tmp_path: Path, integrity_case: str
) -> None:
    package_root = tmp_path / integrity_case
    _prepare_full_package(package_root)
    sums = package_root / "SHA256SUMS.txt"
    lines = sums.read_text(encoding="utf-8").splitlines()
    python_suffix = "runtime/live/python.exe"
    if integrity_case == "python-uncovered":
        lines = [line for line in lines if not line.endswith(python_suffix)]
        _write_text(sums, "\n".join(lines) + "\n")
    elif integrity_case == "python-hash":
        lines = [f"{'0' * 64}  {python_suffix}" if line.endswith(python_suffix) else line for line in lines]
        _write_text(sums, "\n".join(lines) + "\n")
    else:
        (package_root / "models" / "voice.bin").write_bytes(b"tampered-model")
    marker = package_root / "runtime-executed.marker"

    result = _run_controller(
        package_root,
        "-NoUi",
        timeout=30,
        environment_updates={"A4_FAKE_EXEC_MARKER": str(marker)},
    )

    assert result.returncode == 22, result.stdout + result.stderr
    assert not marker.exists(), "untrusted package runtime executed before integrity validation"
    assert not (package_root / "start-count.log").exists()


def test_controller_rejects_uuid_operation_junction_before_writing(tmp_path: Path) -> None:
    package_root = tmp_path / "uuid-junction-package"
    _source_package(package_root)
    operation_id = str(uuid4())
    operations = package_root / "data" / "local" / "operations"
    operations.mkdir(parents=True)
    outside = tmp_path / "uuid-operation-outside"
    outside.mkdir()
    junction_environment = os.environ.copy()
    junction_environment["A4_JUNCTION_PATH"] = str(operations / operation_id)
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

    result = _run_controller(package_root, "-OperationId", operation_id, "-NoUi")

    assert result.returncode == 22
    assert "reparse" in (result.stdout + result.stderr).lower()
    assert not (outside / "operation.json").exists()
    assert not (outside / "events.jsonl").exists()


def test_initializer_rejects_uuid_operation_junction(tmp_path: Path) -> None:
    package_root = tmp_path / "initializer-uuid-junction"
    _manifest(package_root, profile="bootstrap")
    scripts = package_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "initialize-portable.ps1", scripts)
    shutil.copy2(REPO_ROOT / "scripts" / "Portable-Validation.ps1", scripts)
    operation_id = str(uuid4())
    operations = package_root / "data" / "local" / "operations"
    operations.mkdir(parents=True)
    outside = tmp_path / "initializer-uuid-outside"
    outside.mkdir()
    junction_environment = os.environ.copy()
    junction_environment["A4_JUNCTION_PATH"] = str(operations / operation_id)
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
    cancel = operations / operation_id / "cancel.requested"
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
            str(operations / operation_id),
            "-CancelFile",
            str(cancel),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 20
    assert "reparse" in (result.stdout + result.stderr).lower()


def test_stale_active_pointer_without_lock_is_reclaimed(tmp_path: Path) -> None:
    package_root = tmp_path / "stale-pointer-package"
    _source_package(package_root)
    operations = package_root / "data" / "local" / "operations"
    stale_id = str(uuid4())
    stale_operation = operations / stale_id
    stale_operation.mkdir(parents=True)
    _write_text(
        stale_operation / "operation.json",
        json.dumps(
            {
                "operation_id": stale_id,
                "component": "tts-more",
                "action": "start",
                "initiator": "direct",
                "started_at": "2026-07-14T00:00:00Z",
                "status": "installing",
                "exit_code": None,
            }
        ),
    )
    _write_text(
        operations / "active-start.json",
        json.dumps(
            {
                "operation_id": stale_id,
                "owner_pid": 999999,
                "owner_started_at": "2026-07-14T00:00:00Z",
                "published_at": "2026-07-14T00:00:00Z",
            }
        ),
    )

    result = _run_controller(package_root, "-NoUi")

    assert result.returncode == 0, result.stdout + result.stderr
    stale = json.loads((stale_operation / "operation.json").read_text(encoding="utf-8"))
    assert stale["status"] == "blocked"
    assert stale["exit_code"] == 22
    assert not (operations / "active-start.json").exists()
    assert (package_root / "initialize-count.log").read_text(encoding="utf-8").splitlines() == ["1"]
    assert (package_root / "start-count.log").read_text(encoding="utf-8").splitlines() == ["1"]


def test_stale_recovery_appends_terminal_event_before_committing_exit_state(tmp_path: Path) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    package = tmp_path / "stale-order-package"
    _copy_controller(package)
    operations = package / "data" / "local" / "operations"
    operation_id = str(uuid4())
    operation = operations / operation_id
    operation.mkdir(parents=True)
    _write_text(operation / "operation.json", json.dumps({"exit_code": None, "status": "installing"}))
    _write_text(
        operations / "active-start.json",
        json.dumps({"operation_id": operation_id, "owner_pid": 999999}),
    )
    probe = package / "stale-order-probe.ps1"
    _write_text(
        probe,
        r'''$ErrorActionPreference = "Stop"
. $env:A4_CONTROLLER
$script:trace = @()
function Add-OperationEvent { param($Operation,$Phase,$Message,$ErrorCode,$Percent) $script:trace += "event" }
function Complete-Operation { param($Operation,$Status,$ExitCode) $script:trace += "complete" }
$context = [pscustomobject]@{ Root = $env:A4_ROOT; OperationsRoot = $env:A4_OPERATIONS }
Clear-StaleActivePointer -Context $context
Write-Host ($script:trace -join ",")
''',
    )
    environment = os.environ.copy()
    environment["A4_CONTROLLER"] = str(package / "scripts" / "Invoke-PortableStart.ps1")
    environment["A4_ROOT"] = str(package)
    environment["A4_OPERATIONS"] = str(operations)

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(probe)],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("event,complete")


def test_atomic_json_replacement_never_deletes_existing_destination() -> None:
    controller = (REPO_ROOT / "scripts" / "Invoke-PortableStart.ps1").read_text(encoding="utf-8")

    assert "[IO.File]::Replace($temporary, $Path, $backup)" in controller
    assert "[IO.File]::Delete($Path)" not in controller


def test_progress_disables_cancel_at_starting_and_port_override_precedes_ui() -> None:
    controller = (REPO_ROOT / "scripts" / "Invoke-PortableStart.ps1").read_text(encoding="utf-8")
    progress = (REPO_ROOT / "scripts" / "Show-PortableProgress.ps1").read_text(encoding="utf-8")

    assert "Test-PortableCancellationAvailable" in progress
    assert '$cancel.Enabled = Test-PortableCancellationAvailable -Phase ([string]$event.phase)' in progress
    assert '"starting"' in progress and '"ready"' in progress
    assert "Read-PortableEventDelta" in progress
    assert "[IO.File]::ReadAllLines($eventsPath)" not in progress
    assert "[IO.FileStream]" in progress and "Seek(" in progress
    assert controller.index("$urlPort =") < controller.index("Start-ProgressWindow -Operation")


def test_progress_event_reader_consumes_only_complete_incremental_jsonl_records(tmp_path: Path) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    operation = tmp_path / "incremental-events"
    operation.mkdir()
    events = operation / "events.jsonl"
    _write_text(events, '{"seq":1,"phase":"checking","message":"one"}\n')
    progress_source = (REPO_ROOT / "scripts" / "Show-PortableProgress.ps1").read_text(encoding="utf-8")
    # The injected dot-source guard only isolates the production helper for this executable probe.
    progress_source = progress_source.replace(
        "if ($RequestCancel)",
        'if ($MyInvocation.InvocationName -eq ".") { return }\n\nif ($RequestCancel)',
        1,
    )
    progress = tmp_path / "Show-PortableProgress.ps1"
    _write_text(progress, progress_source)
    probe = tmp_path / "incremental-reader-probe.ps1"
    _write_text(
        probe,
        r'''$ErrorActionPreference = "Stop"
. $env:A4_PROGRESS -OperationRoot $env:A4_OPERATION
[long]$offset = 0
$carry = ""
$first = @(Read-PortableEventDelta -Path $env:A4_EVENTS -Offset ([ref]$offset) -Carry ([ref]$carry))
if ($first.Count -ne 1 -or [int]$first[0].seq -ne 1) { throw "first delta mismatch" }
[IO.File]::AppendAllText($env:A4_EVENTS, '{"seq":2,"phase":"installing"', [Text.UTF8Encoding]::new($false))
$partial = @(Read-PortableEventDelta -Path $env:A4_EVENTS -Offset ([ref]$offset) -Carry ([ref]$carry))
if ($partial.Count -ne 0) { throw "partial JSONL record was emitted" }
[IO.File]::AppendAllText($env:A4_EVENTS, ',"message":"two"}' + "`n", [Text.UTF8Encoding]::new($false))
$second = @(Read-PortableEventDelta -Path $env:A4_EVENTS -Offset ([ref]$offset) -Carry ([ref]$carry))
if ($second.Count -ne 1 -or [int]$second[0].seq -ne 2) { throw "second delta mismatch" }
$again = @(Read-PortableEventDelta -Path $env:A4_EVENTS -Offset ([ref]$offset) -Carry ([ref]$carry))
if ($again.Count -ne 0) { throw "already consumed records were reread" }
Write-Host "INCREMENTAL_JSONL_OK offset=$offset"
''',
    )
    environment = os.environ.copy()
    environment.update(
        {
            "A4_PROGRESS": str(progress),
            "A4_OPERATION": str(operation),
            "A4_EVENTS": str(events),
        }
    )

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(probe)],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "INCREMENTAL_JSONL_OK" in result.stdout


def test_portable_start_scripts_reject_external_python_fallbacks_before_spawn() -> None:
    for path in (
        REPO_ROOT / "scripts" / "start-production.ps1",
        REPO_ROOT / "integrations" / "windows" / "Start-Worker.ps1",
    ):
        script = path.read_text(encoding="utf-8")
        assert "TTS_MORE_PYTHON_EXE" not in script
        assert '.venv\\Scripts\\python.exe' not in script
        assert "Assert-PortableRuntime" in script
        assert "listener_is_owned" in script or "verify-owned-listener" in script


@pytest.mark.parametrize(
    ("component", "profile_case", "bad_write"),
    (
        ("tts-more", "cpu", False),
        ("gpt-sovits", "cpu", False),
        ("tts-more", "ordered", False),
        ("gpt-sovits", "ordered", False),
        ("tts-more", "cpu", True),
        ("gpt-sovits", "cpu", True),
    ),
)
def test_initializers_repair_stale_state_from_verified_private_assets_without_bootstrap(
    tmp_path: Path, component: str, profile_case: str, bad_write: bool
) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    root = tmp_path / component
    bundle = root / "scripts" if component == "tts-more" else root / "tts_more"
    bundle.mkdir(parents=True)
    initializer = (
        REPO_ROOT / "scripts" / "initialize-portable.ps1"
        if component == "tts-more"
        else REPO_ROOT / "integrations" / "windows" / "Initialize.ps1"
    )
    shutil.copy2(initializer, bundle)
    shutil.copy2(REPO_ROOT / "scripts" / "Portable-Validation.ps1", bundle)
    shutil.copy2(REPO_ROOT / "scripts" / "portable_install.py", bundle)
    expected_python = "3.11"
    runtime_lock = (
        root / "packaging" / "portable" / "runtime.lock.json"
        if component == "tts-more"
        else bundle / "locks" / "runtime.lock.json"
    )
    model_lock = (
        root / "packaging" / "portable" / "models.lock.json"
        if component == "tts-more"
        else bundle / "locks" / "models.lock.json"
    )
    _write_text(
        runtime_lock,
        json.dumps(
            {
                "component": component,
                "python_version": expected_python,
                "import_probe": "import sys",
                "required_free_bytes": 0,
                "profiles": (
                    {"cpu": {"dependency_lock": "cpu.lock"}, "cu128": {"dependency_lock": "cu.lock"}}
                    if profile_case == "cpu"
                    else {"cu126": {"dependency_lock": "cu126.lock"}, "cu128": {"dependency_lock": "cu128.lock"}}
                ),
                "auto_order": ["cu128", "cpu"] if profile_case == "cpu" else ["cu128", "cu126"],
            }
        ),
    )
    _write_text(
        model_lock,
        json.dumps(
            {
                "component": component,
                "complete": True,
                "required_free_bytes": 0,
                "required_paths": [],
                "assets": [],
            }
        ),
    )
    if component == "tts-more":
        _write_text(root / "backend" / "uv.lock", "locked\n")
    else:
        _write_text(
            bundle / "component.json",
            json.dumps(
                {
                    "component": component,
                    "python": expected_python,
                    "import_probe": "import sys",
                }
            ),
        )
    _compile_fake_python(root / "runtime" / "live" / "python.exe")
    _write_text(root / "package" / "tts-more-package.json", json.dumps({"build_id": "current-build"}))
    state_path = root / "data" / "local" / "install-state.json"
    _write_text(
        state_path,
        json.dumps(
            {
                "schema_version": 1,
                "component": component,
                "build_id": "stale-build",
                "profile": "unsupported-profile",
                "runtime_lock_sha256": "0" * 64,
                "model_lock_sha256": "0" * 64,
                "ready": True,
            }
        ),
    )
    environment = os.environ.copy()
    if bad_write:
        environment["A4_FAKE_WRITE_BAD_STATE"] = "1"
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bundle / initializer.name),
            "-PackageRoot",
            str(root),
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if bad_write:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "repaired" not in result.stdout.lower()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    repaired = json.loads(state_path.read_text(encoding="utf-8"))
    assert repaired["build_id"] == "current-build"
    assert repaired["profile"] == ("cpu" if profile_case == "cpu" else "cu128")
    assert repaired["runtime_lock_sha256"] == hashlib.sha256(runtime_lock.read_bytes()).hexdigest()
    assert repaired["model_lock_sha256"] == hashlib.sha256(model_lock.read_bytes()).hexdigest()
    assert "repaired" in result.stdout.lower()
    assert not (root / "data" / "cache" / "portable" / "video-controllers.json").exists()


def test_atomic_json_locked_destination_preserves_old_readable_document(tmp_path: Path) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    package = tmp_path / "atomic-package"
    _copy_controller(package)
    target = package / "state.json"
    _write_text(target, '{"generation":"old"}\n')
    probe = package / "atomic-probe.ps1"
    _write_text(
        probe,
        r'''$ErrorActionPreference = "Stop"
. $env:A4_CONTROLLER
$stream = [IO.File]::Open($env:A4_TARGET, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
$threw = $false
try { Write-JsonAtomic -Path $env:A4_TARGET -Payload ([ordered]@{ generation = "new" }) } catch { $threw = $true }
finally { $stream.Dispose() }
if (!$threw) { throw "locked replacement unexpectedly succeeded" }
$payload = Get-Content -LiteralPath $env:A4_TARGET -Raw | ConvertFrom-Json
if ([string]$payload.generation -ne "old") { throw "old destination was not preserved" }
Write-Host "ATOMIC_OLD_PRESERVED"
''',
    )
    environment = os.environ.copy()
    environment["A4_CONTROLLER"] = str(package / "scripts" / "Invoke-PortableStart.ps1")
    environment["A4_TARGET"] = str(target)
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(probe)],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ATOMIC_OLD_PRESERVED" in result.stdout


@pytest.mark.parametrize("runtime_case", ("environment", "venv", "reparse", "wrong-version"))
def test_production_start_rejects_non_private_or_invalid_runtime_before_spawn(
    tmp_path: Path, runtime_case: str
) -> None:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is unavailable")
    root = tmp_path / f"start-{runtime_case}"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "start-production.ps1", scripts)
    shutil.copy2(REPO_ROOT / "scripts" / "Portable-Validation.ps1", scripts)
    (root / "backend").mkdir()
    (root / "frontend" / "dist").mkdir(parents=True)
    _write_text(
        root / "packaging" / "portable" / "runtime.lock.json",
        json.dumps({"python_version": "3.11", "import_probe": "import sys"}),
    )
    environment = os.environ.copy()
    marker = root / "spawned.marker"
    environment["A4_FAKE_SPAWN_MARKER"] = str(marker)
    if runtime_case == "environment":
        external = tmp_path / "external" / "python.exe"
        _compile_fake_python(external)
        environment["TTS_MORE_PYTHON_EXE"] = str(external)
    elif runtime_case == "venv":
        _compile_fake_python(root / ".venv" / "Scripts" / "python.exe")
    elif runtime_case == "wrong-version":
        _compile_fake_python(root / "runtime" / "live" / "python.exe", version="3.10")
    else:
        outside_live = tmp_path / "outside-live"
        _compile_fake_python(outside_live / "python.exe")
        (root / "runtime").mkdir()
        junction_environment = environment.copy()
        junction_environment["A4_JUNCTION_PATH"] = str(root / "runtime" / "live")
        junction_environment["A4_JUNCTION_TARGET"] = str(outside_live)
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
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "start-production.ps1"),
            "-PackageRoot",
            str(root),
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert not marker.exists()
    combined = (result.stdout + result.stderr).lower()
    assert "runtime" in combined or "python" in combined or "reparse" in combined
