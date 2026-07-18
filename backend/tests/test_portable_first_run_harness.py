from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = REPO_ROOT / "scripts" / "serve-portable-fixtures.py"
HARNESS_SCRIPT = REPO_ROOT / "scripts" / "test-portable-first-run.ps1"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "portable-release.yml"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _load_script(path: Path, name: str):
    assert path.is_file(), f"{path.name} is missing"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_server_module():
    return _load_script(SERVER_SCRIPT, "serve_portable_fixtures_for_test")


def _portable_install_module():
    return _load_script(REPO_ROOT / "scripts" / "portable_install.py", "portable_install_for_fixture_test")


def _local_first_run_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env["TTS_MORE_FIRST_RUN_PYTHON"] = sys.executable
    env["TTS_MORE_FIRST_RUN_DEBUG"] = "1"
    return env


def _urlopen_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def test_fixture_server_binds_random_loopback_port_and_rejects_non_loopback_root(tmp_path: Path) -> None:
    module = _fixture_server_module()
    (tmp_path / "runtime.bin").write_bytes(b"0123456789abcdef")
    with module.PortableFixtureServer(tmp_path) as server:
        assert server.host == "127.0.0.1"
        assert server.port > 0
        assert server.url("runtime.bin").startswith(f"http://127.0.0.1:{server.port}/")


def test_fixture_server_preserves_partial_then_resumes_with_strict_range(tmp_path: Path) -> None:
    module = _fixture_server_module()
    installer = _portable_install_module()
    payload = b"0123456789abcdef"
    (tmp_path / "runtime.bin").write_bytes(payload)
    destination = tmp_path / "download" / "runtime.bin"
    asset = {
        "id": "runtime-fixture",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "urls": [],
    }
    with module.PortableFixtureServer(tmp_path, interrupt_after=8) as server:
        asset["urls"] = [server.url("runtime.bin")]
        with pytest.raises(RuntimeError, match="asset download failed"):
            installer.ensure_locked_asset(asset, destination)
        assert destination.with_name("runtime.bin.partial").read_bytes() == payload[:8]
        report = installer.ensure_locked_asset(asset, destination)
        assert report["reused"] is False
        assert destination.read_bytes() == payload
        range_requests = [entry for entry in server.requests if entry.get("range")]
        assert range_requests[-1]["range"] == "bytes=8-"
        assert range_requests[-1]["status"] == 206
        assert range_requests[-1]["content_range"] == "bytes 8-15/16"


def test_fixture_server_range_proxy_failure_and_same_length_corruption(tmp_path: Path) -> None:
    module = _fixture_server_module()
    payload = b"abcdefghijklmnop"
    (tmp_path / "asset.dat").write_bytes(payload)
    with module.PortableFixtureServer(tmp_path) as server:
        request = urllib.request.Request(server.url("asset.dat"), headers={"Range": "bytes=4-7"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 4-7/16"
            assert response.read() == payload[4:8]
        assert _urlopen_status(server.url("asset.dat", mode="proxy-failure")) == 503
        with urllib.request.urlopen(server.url("asset.dat", mode="corrupt"), timeout=5) as response:
            corrupt = response.read()
        assert len(corrupt) == len(payload)
        assert corrupt != payload


def test_fixture_server_rejects_traversal_and_malformed_ranges(tmp_path: Path) -> None:
    module = _fixture_server_module()
    (tmp_path / "asset.dat").write_bytes(b"fixture")
    with module.PortableFixtureServer(tmp_path) as server:
        assert _urlopen_status(server.url("..%2Fasset.dat")) == 404
        request = urllib.request.Request(server.url("asset.dat"), headers={"Range": "bytes=1-2,4-5"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        assert caught.value.code == 400


def test_harness_contract_limits_path_audits_before_expand_and_covers_exact_components() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")
    compact = " ".join(script.split())
    assert "[Parameter(Mandatory = $true)][string[]]$Packages" in script
    assert "[Parameter(Mandatory = $true)][string]$Output" in script
    assert all(component in script for component in ("tts-more", "gpt-sovits", "indextts", "cosyvoice"))
    assert "audit-release" in script and "verify-sha256" in script
    assert compact.index("audit-release") < compact.index("Expand-Archive")
    assert "System32\\WindowsPowerShell\\v1.0" in script
    assert all(command in script for command in ("python", "conda", "node", "git"))
    assert "Get-Command" in script and "COMMAND_LEAK" in script


def test_harness_runs_real_packages_below_spaced_unicode_temp_root() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")

    assert 'Join-Path ([IO.Path]::GetTempPath()) "TTS More 中文"' in script
    assert 'Join-Path ([IO.Path]::GetTempPath()) "tts-fr"' not in script
    assert '. (Join-Path $PSScriptRoot "Portable-Validation.ps1")' in script
    assert "$serverArgumentLine = ConvertTo-PortableWindowsArgumentLine -Arguments $serverArguments" in script
    assert "-ArgumentList $serverArgumentLine" in script


def test_harness_accepts_json_integer_width_differences_from_powershell_hosts() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")

    assert "schema_version -isnot [int]" not in script
    assert "TryParse([string]$manifest.schema_version" in script


def test_harness_does_not_copy_host_python_dlls_into_package_runtime() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")

    assert "Copy-FixtureDirectoryFiltered" not in script
    assert 'Get-ChildItem -LiteralPath $FixtureBasePrefix -Filter "python*.dll"' not in script
    assert "Get-OrDownloadLockedAsset" in script


def test_harness_uses_locked_official_embeddable_python_instead_of_host_runtime_seed() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")

    assert "function Get-LockedEmbeddedPythonAsset" in script
    assert "function Get-OrDownloadLockedAsset" in script
    assert "Copy-FixturePythonRuntime" not in script
    assert "Copy-FixturePythonStdlib" not in script
    assert "Copy-FixturePythonExtensions" not in script
    assert "New-FixtureEmbeddedPythonZip" not in script


def test_harness_initialize_contract_uses_embedded_python_and_uv_fixtures() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")
    assert all(name in script for name in ("Start.cmd", "Stop.cmd", "Repair.cmd", "Initialize.cmd"))
    assert "Get-LockedEmbeddedPythonAsset" in script
    assert "Get-OrDownloadLockedAsset" in script
    assert "New-FixtureUvWheel" in script
    assert "uv-0.11.28.data/scripts/uv.exe" in script
    assert "Install-FixtureCondaAdapter" not in script
    assert "runtime\\Scripts\\uv.exe" not in script
    assert "data\\cache\\portable\\conda" not in script
    assert "SHA256SUMS.txt" in script and "Write-FixtureSha256Manifest" in script
    assert "fixture-only" in script.lower()
    assert 'Invoke-RootCommand -Root $Package.Root -Name "Initialize.cmd"' in script
    assert "controller_real_initialize" in script
    assert "worker_real_initialize" in script
    assert all(
        scenario in script
        for scenario in (
            "controller_real_initialize",
            "worker_real_initialize",
            "interruption",
            "resume",
            "proxy_fallback",
            "corruption_repair",
            "duplicate_start",
            "stale_pid",
            "clean_stop",
        )
    )
    assert "taskkill" not in script.lower()
    assert "Stop-Process -Id $initialPid -Force" in script
    assert "stale PID crash injection refused a non-package process" in script
    assert "Register-PackageOwnedProcess" in script
    assert "Assert-OwnedFixtureProcessesStopped" in script
    assert 'TTS_MORE_FIRST_RUN_DEBUG -ne "1"' not in script
    assert 'TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("DEBUG_WORK_ROOT=' not in script
    assert "Assert-FixtureNetworkEvidence" in script
    assert '"bytes=8-"' in script
    assert "status -eq 503" in script and "status -in @(200, 206)" in script


def test_harness_contract_forbids_overwriting_production_control_scripts() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")
    forbidden_targets = (
        '"initialize-portable.ps1"',
        '"start-production.ps1"',
        '"stop-production.ps1"',
        '"repair-portable.ps1"',
        '"Initialize.ps1"',
        '"Start-Worker.ps1"',
        '"Stop-Worker.ps1"',
        '"Repair.ps1"',
    )
    for target in forbidden_targets:
        assert target not in script, f"harness must not write fixture content to {target}"
    assert "Install-FixtureProtocol" in script
    assert "Install-FixtureControlScripts" not in script


def test_harness_evidence_schema_is_allowlisted_and_identity_fields_are_forbidden() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")
    compact = " ".join(script.split())
    assert "Assert-SanitizedEvidence" in script
    assert "worker_real_initialization" in compact
    forbidden = ("absolute_path", "username", "hostname", "ip_address", "pid", "command", "secret", "token")
    assert all(f'"{name}"' in script for name in forbidden)
    assert "junit" in script.lower() and "acceptance.json" in script


def test_harness_truthfully_reports_real_controller_and_worker_initialization() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")
    compact = " ".join(script.split())

    assert "worker_real_initialization" in script
    assert "controller_real_initialization" in script
    assert "fixture_runtime_preseeded" in script
    assert "direct_downloader" in script
    assert '"controller_real_initialize"' in compact
    assert '"worker_real_initialize"' in compact
    assert '$script:WorkerLifecycleSucceeded' in script
    assert 'worker_real_initialization = $false' in script
    assert '$script:WorkerLifecycleSucceeded[$component]' in script
    assert 'Finalize-WorkerInitializationEvidence' in script
    assert compact.index('Finalize-WorkerInitializationEvidence') < compact.rindex('Write-AcceptanceEvidence')
    assert 'fixture_runtime_preseeded = $false' in script
    assert 'Copy-FixturePythonRuntime -Root $Root -Destination (Join-Path $Root "runtime\\live")' not in script


def test_harness_preserves_component_exact_python_locks_and_official_archives() -> None:
    script = HARNESS_SCRIPT.read_text(encoding="utf-8-sig")
    compact = " ".join(script.split())

    assert '$manifest.runtime.python_version = "3.11.9"' not in script
    assert 'python_version = "3.11.9"' not in script
    assert 'New-FixtureEmbeddedPythonZip' not in script
    assert 'Get-LockedEmbeddedPythonAsset' in script
    assert 'Get-OrDownloadLockedAsset' in script
    assert 'python310.zip' in script and 'python310._pth' in script
    assert 'python311.zip' in script and 'python311._pth' in script
    assert "608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d" in script
    assert "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b" in script
    assert compact.rindex("Get-OrDownloadLockedAsset -Asset") < compact.rindex("Assert-RestrictedChildPath")
    assert 'OperationRoot' in script
    assert 'operation_progress_python' in script


@pytest.mark.skipif(os.name != "nt" or POWERSHELL is None, reason="PowerShell evidence contract")
def test_harness_failed_run_keeps_worker_initialization_false_until_finalization(tmp_path: Path) -> None:
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        f"""
$content = Get-Content -LiteralPath '{str(HARNESS_SCRIPT).replace("'", "''")}' -Raw
$start = $content.IndexOf('Set-StrictMode')
$end = $content.IndexOf('$caught = $null')
$defs = $content.Substring($start, $end - $start).Replace('$PSScriptRoot', "'{str(HARNESS_SCRIPT.parent).replace("'", "''")}'")
Invoke-Expression $defs
Add-Evidence -Component 'cosyvoice' -Scenario 'worker_real_initialize' -Result pass -Duration 0
$before = @($Evidence)[0].worker_real_initialization
$script:WorkerLifecycleSucceeded['cosyvoice'] = [pscustomobject]@{{ OperationProgressPython='3.10.11' }}
$script:WorkerLifecycleSucceeded['gpt-sovits'] = [pscustomobject]@{{ OperationProgressPython='3.11.9' }}
$script:WorkerLifecycleSucceeded['indextts'] = [pscustomobject]@{{ OperationProgressPython='3.11.9' }}
Finalize-WorkerInitializationEvidence
$after = @($Evidence)[0].worker_real_initialization
$version = @($Evidence)[0].operation_progress_python
[pscustomobject]@{{ Before=$before; After=$after; Version=$version }} | ConvertTo-Json -Compress
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload == {"Before": False, "After": True, "Version": "3.10.11"}


def test_harness_requires_windows_and_rejects_incomplete_package_set(tmp_path: Path) -> None:
    if os.name != "nt" or POWERSHELL is None:
        pytest.skip("PowerShell harness contract executes only on Windows")
    output = tmp_path / "evidence"
    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HARNESS_SCRIPT),
            "-Packages",
            str(tmp_path / "one.zip"),
            "-Output",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "TTS_MORE_FIRST_RUN_PYTHON": sys.executable},
    )
    assert completed.returncode != 0
    assert "exactly four" in (completed.stdout + completed.stderr).lower()
    junit_bytes = (output / "acceptance.junit.xml").read_bytes()
    junit = junit_bytes.decode("utf-8", errors="strict")
    assert 'encoding="utf-8"' in junit.splitlines()[0].lower()
    assert ET.fromstring(junit).tag == "testsuites"


def test_portable_release_workflow_runs_harness_unit_tests_and_single_package_smoke() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "backend/tests/test_portable_first_run_harness.py" in workflow
    assert "test-portable-first-run.ps1" in workflow
    assert "TTS_MORE_FIRST_RUN_PYTHON" in workflow
    single_smoke = workflow[workflow.index("Run TTS More clean-Windows single-package smoke") :]
    single_smoke = single_smoke[: single_smoke.index("Upload sanitized clean-Windows smoke evidence")]
    assert "shell: powershell" in single_smoke
    assert "-Packages $zip.FullName" in workflow
    assert "single-package-smoke" in workflow
    assert "portable-first-run-smoke-evidence" in workflow
    assert "single-package-smoke/acceptance.json" in workflow
    assert "single-package-smoke/acceptance.junit.xml" in workflow
    assert workflow.index("test-portable-first-run.ps1") < workflow.index(
        "portable-first-run-smoke-evidence"
    )


def test_first_run_harness_uses_embedded_sha256_helper_instead_of_get_file_hash() -> None:
    harness = HARNESS_SCRIPT.read_text(encoding="utf-8")

    assert "function Get-PortableFileSha256" in harness
    assert "Get-FileHash" not in harness


def _portable_package_test_helpers():
    return _load_script(
        REPO_ROOT / "backend" / "tests" / "test_portable_packages.py",
        "portable_package_test_helpers_for_first_run",
    )


def _fixture_runtime_processes() -> set[int]:
    if os.name != "nt" or POWERSHELL is None:
        return set()
    command = (
        "$items=@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue|"
        "Where-Object {$_.ExecutablePath -match '\\\\tts-fr\\\\[^\\\\]+\\\\.*\\\\runtime\\\\live\\\\python\\.exe$'}|"
        "Select-Object -ExpandProperty ProcessId); @($items)|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip() or "[]")
    if isinstance(payload, int):
        return {payload}
    return {int(pid) for pid in payload}


def _first_run_directories() -> set[Path]:
    root = Path(os.environ["TEMP"]) / "tts-fr"
    if not root.exists():
        return set()
    return {path.resolve() for path in root.glob("*") if path.is_dir()}


def _wait_for_first_run_cleanup(baseline: set[Path], timeout: float = 35.0) -> set[Path]:
    deadline = time.monotonic() + timeout
    latest: set[Path] = set()
    while True:
        latest = _first_run_directories()
        if latest <= baseline:
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.2)


def _build_micro_worker_bootstrap(
    helpers: object, tmp_path: Path, version: str, component: str
) -> Path:
    worker_root = tmp_path / "worker"
    sync = helpers._load_sync_integrations()
    sync.sync_integration(REPO_ROOT, worker_root, component, "a" * 40)
    (worker_root / "upstream-entry.py").write_text("UPSTREAM_FIXTURE = True\n", encoding="utf-8")
    component_path = worker_root / "tts_more" / "component.json"
    component_payload = json.loads(component_path.read_text(encoding="utf-8"))
    component_payload.pop("submodules", None)
    component_path.write_text(json.dumps(component_payload, indent=2) + "\n", encoding="utf-8")
    integration_path = worker_root / "tts_more" / "integration.manifest.json"
    integration = json.loads(integration_path.read_text(encoding="utf-8"))
    integration["files"]["tts_more/component.json"] = sync.sha256_file(component_path)
    integration_path.write_text(json.dumps(integration, indent=2) + "\n", encoding="utf-8")
    helpers._initialize_git_repository(worker_root)
    output_root = tmp_path / "output"
    work_root = helpers._external_worker_test_root(tmp_path, f"first-run-{component}")
    try:
        helpers._run_checked(
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
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            worker_root,
            env={**os.environ, "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve())},
        )
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
    archives = list(output_root.glob("*.zip"))
    assert len(archives) == 1
    return archives[0]


def _four_micro_bootstrap_zips(tmp_path: Path, version: str) -> tuple[Path, Path, Path, Path]:
    helpers = _portable_package_test_helpers()
    _controller_stage, controller_zip = helpers._build_controller_bootstrap(
        tmp_path / "controller-package", version
    )
    worker_zips = [
        _build_micro_worker_bootstrap(helpers, tmp_path / component, version, component)
        for component in ("gpt-sovits", "indextts", "cosyvoice")
    ]
    return (controller_zip, *worker_zips)


def _harness_command(packages: tuple[Path, ...], output: Path) -> str:
    package_literals = ",".join("'" + str(path).replace("'", "''") + "'" for path in packages)
    return (
        f"$packages=@({package_literals}); "
        f"& '{str(HARNESS_SCRIPT).replace(chr(39), chr(39) * 2)}' "
        f"-Packages $packages -Output '{str(output).replace(chr(39), chr(39) * 2)}'; "
        "exit $LASTEXITCODE"
    )


@pytest.mark.skipif(os.name != "nt" or POWERSHELL is None, reason="Windows acceptance harness")
def test_real_micro_four_package_fixture_harness_is_sanitized_and_cleans_up(tmp_path: Path) -> None:
    version = "0.2.0-first-run"
    packages = _four_micro_bootstrap_zips(tmp_path, version)
    output = tmp_path / "sanitized-evidence"
    processes_before = _fixture_runtime_processes()
    run_dirs_before = _first_run_directories()
    command = _harness_command(packages, output)
    try:
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=240,
            env=_local_first_run_env(),
        )
    finally:
        leftovers = _wait_for_first_run_cleanup(run_dirs_before) - run_dirs_before
        assert leftovers == set()
        assert _fixture_runtime_processes() - processes_before == set()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
    assert records
    assert {record["component"] for record in records} == {
        "tts-more",
        "gpt-sovits",
        "indextts",
        "cosyvoice",
    }
    shared_scenarios = {
        "package_audit",
        "path_isolation",
        "interruption",
        "resume",
        "proxy_fallback",
        "corruption_repair",
        "duplicate_start",
        "stale_pid",
        "clean_stop",
        "unknown_port",
    }
    for component in {record["component"] for record in records}:
        initialization = "controller_real_initialize" if component == "tts-more" else "worker_real_initialize"
        assert {record["scenario"] for record in records if record["component"] == component} == shared_scenarios | {initialization}
    assert len(records) == 44
    evidence_fields = {
        "component", "scenario", "result", "duration", "error_code",
        "worker_real_initialization", "controller_real_initialization",
        "fixture_runtime_preseeded", "direct_downloader", "operation_progress_python",
    }
    assert all(set(record) == evidence_fields for record in records)
    workers = [record for record in records if record["component"] != "tts-more"]
    assert workers and all(record["worker_real_initialization"] is True for record in workers)
    cosy = [record for record in workers if record["component"] == "cosyvoice"]
    assert cosy and all(record["operation_progress_python"] == "3.10.11" for record in cosy)
    assert all(record["fixture_runtime_preseeded"] is False for record in records)
    assert all(record["result"] == "pass" for record in records)
    serialized = json.dumps(records, ensure_ascii=False)
    assert not any(
        value and len(value) >= 3 and value.casefold() in serialized.casefold()
        for value in (os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME"), os.environ.get("USERPROFILE"))
    )
    assert (output / "acceptance.junit.xml").is_file()


@pytest.mark.skipif(os.name != "nt" or POWERSHELL is None, reason="Windows acceptance harness")
def test_real_micro_four_package_fixture_harness_allows_two_concurrent_runs(tmp_path: Path) -> None:
    packages = _four_micro_bootstrap_zips(tmp_path, "0.2.0-first-run-concurrent")
    outputs = (tmp_path / "evidence-a", tmp_path / "evidence-b")
    processes_before = _fixture_runtime_processes()
    run_dirs_before = _first_run_directories()
    commands = [_harness_command(packages, output) for output in outputs]
    running = [
        subprocess.Popen(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_local_first_run_env(),
        )
        for command in commands
    ]
    results = []
    try:
        for process in running:
            stdout, stderr = process.communicate(timeout=360)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in running:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        leftovers = _wait_for_first_run_cleanup(run_dirs_before, timeout=45.0) - run_dirs_before
        assert leftovers == set()
        assert _fixture_runtime_processes() - processes_before == set()

    for returncode, stdout, stderr in results:
        assert returncode == 0, stdout + stderr
    for output in outputs:
        records = json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
        assert len(records) == 44
        assert all(record["result"] == "pass" for record in records)
