# One-Click Portable Control Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Start.cmd` the single initialization-and-start entry and expose a stable operation protocol shared by all four packages.

**Architecture:** Extend schema v2 without changing its version, retaining tolerant reads for older rc v2 packages while making builders emit the completed contract. A dependency-free Python module owns operation JSON validation; PowerShell 5.1 owns the pre-runtime controller and emits the same schema. Existing transactional installers and process ownership helpers remain the implementation base.

**Tech Stack:** Python 3.11 standard library, PowerShell 5.1, pytest 8, JSON Lines, FastAPI package manifests, uv.

## Global Constraints

- Supported systems are Windows 10 22H2 and Windows 11 x64.
- `Start.cmd` must automatically initialize when install state is absent or invalid.
- Bootstrap starts online once and then works offline; Full never silently downloads during start.
- Package paths in manifests are relative and cannot contain drive letters or `..`.
- TTS More uses Python 3.11 and has no CUDA dependency.
- Unknown processes and ports are never terminated.
- Operation files live under `data/local/operations/<operation-id>/`.
- No implementation may require administrator rights or a preinstalled Python.

---

### Task 1: Complete schema v2 and builder output

**Files:**
- Modify: `packaging/portable/tts-more-package.schema.json`
- Modify: `scripts/portable_packages.py`
- Modify: `Build-Package.ps1`
- Modify: `integrations/windows/Build-Package.ps1`
- Modify: `backend/tests/test_portable_packages.py`
- Modify: `backend/tests/test_portable_discovery.py`

**Interfaces:**
- Consumes: current schema v1/v2 manifest validation.
- Produces: required v2 keys `package_id`, `release_version`, `protocol`, and `data`; descriptor fields `package_id`, `protocol_version`, `controller_range` and `operations_path`; tolerant discovery defaults for rc v2 manifests.

- [ ] **Step 1: Write failing strict-builder and tolerant-reader tests**

```python
def test_completed_v2_requires_identity_protocol_and_data_paths(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload.update({
        "package_id": "tts-more",
        "release_version": "0.2.0",
        "protocol": {"name": "tts-more-v1", "version": "1.0", "controller_range": ">=0.2.0,<0.3.0"},
        "data": {"user": "data/user", "local": "data/local", "cache": "data/cache", "operations": "data/local/operations"},
    })
    manifest = _write_v2_manifest(tmp_path, payload)
    report = packages.validate_manifest(manifest, tmp_path)
    assert report["valid"] is True
    del payload["package_id"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = packages.validate_manifest(manifest, tmp_path)
    assert "package_id is required" in report["errors"]
```

Add `_write_v2_manifest(root: Path, payload: dict[str, object]) -> Path` beside `_valid_v2_manifest()`. It creates every relative launcher/lock/license file referenced by the payload, writes `package/tts-more-package.json`, and returns that manifest path.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_packages.py backend/tests/test_portable_discovery.py -q`

Expected: FAIL because the completed v2 keys are not validated or generated.

- [ ] **Step 3: Implement strict build validation and tolerant discovery defaults**

```python
V2_REQUIRED_FIELDS = (*V2_REQUIRED_FIELDS, "package_id", "release_version", "protocol", "data")

def _validate_v2_data(payload: dict[str, Any], errors: list[str]) -> None:
    protocol = _mapping(payload.get("protocol"))
    if protocol.get("name") != "tts-more-v1":
        errors.append("protocol.name must be tts-more-v1")
    for key in ("version", "controller_range"):
        _require_text(protocol, key, f"protocol.{key}", errors)
    data = _mapping(payload.get("data"))
    for key in ("user", "local", "cache", "operations"):
        _validate_relative_path(data.get(key), f"data.{key}", errors)
```

Add the exact keys to both PowerShell builders and call `_validate_v2_data()` from `_validate_v2()`. In `read_portable_package()`, map absent rc fields to `package_id=component`, `release_version=version`, and descriptor `operations_path=payload.data.operations` or `data/local/operations` without allowing new builders to omit `data.operations`.

- [ ] **Step 4: Run manifest, discovery and package tests**

Run: `py -3.11 -m pytest backend/tests/test_portable_packages.py backend/tests/test_portable_discovery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the schema contract**

```powershell
git add packaging/portable/tts-more-package.schema.json scripts/portable_packages.py Build-Package.ps1 integrations/windows/Build-Package.ps1 backend/tests/test_portable_packages.py backend/tests/test_portable_discovery.py
git commit -m "feat: complete portable package v2 contract"
```

### Task 2: Add the operation state and event protocol

**Files:**
- Create: `scripts/portable_operations.py`
- Create: `backend/tests/test_portable_operations.py`
- Modify: `scripts/sync_integrations.py`
- Modify: `integrations/contract_tests/test_portable_integration.py`

**Interfaces:**
- Consumes: schema v2 `data.operations` path.
- Produces: `create_operation(root, operation_id, component, action, initiator)`, `append_event(root, operation_id, phase, message, percent=None, error_code=None)`, `finish_operation(root, operation_id, status, exit_code)`, and `read_operation(root, operation_id)`.

- [ ] **Step 1: Write failing atomic-operation tests**

```python
def test_operation_events_are_ordered_and_finish_atomically(tmp_path: Path) -> None:
    operation_id = "11111111-1111-4111-8111-111111111111"
    create_operation(tmp_path, operation_id, "gpt-sovits", "start", "direct")
    append_event(tmp_path, operation_id, "checking", "正在检查电脑")
    append_event(tmp_path, operation_id, "downloading", "正在下载模型", percent=25.0)
    finish_operation(tmp_path, operation_id, "repairable", 20)
    operation, events = read_operation(tmp_path, operation_id)
    assert [event["seq"] for event in events] == [1, 2]
    assert operation["status"] == "repairable"
    assert operation["exit_code"] == 20
```

- [ ] **Step 2: Run the new test and confirm import failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_operations.py -q`

Expected: FAIL because `scripts.portable_operations` does not exist.

- [ ] **Step 3: Implement dependency-free atomic JSON and JSONL helpers**

```python
PHASES = {"not_initialized", "checking", "downloading", "installing", "validating", "starting", "ready", "stopped", "repairable", "blocked"}

def append_event(root: Path, operation_id: str, phase: str, message: str, *, percent: float | None = None, error_code: str | None = None) -> dict[str, object]:
    if phase not in PHASES:
        raise ValueError(f"unsupported operation phase: {phase}")
    directory = _operation_dir(root, operation_id)
    events_path = directory / "events.jsonl"
    seq = sum(1 for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1 if events_path.exists() else 1
    event = {"seq": seq, "timestamp": datetime.now(UTC).isoformat(), "phase": phase, "message": message}
    if percent is not None:
        event["percent"] = max(0.0, min(100.0, float(percent)))
    if error_code:
        event["error_code"] = error_code
    with events_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return event
```

Implement `create_operation`, `finish_operation`, `read_operation`, UUID validation and `os.replace()` writes. Add `portable_operations.py` to the controlled mirror list.

- [ ] **Step 4: Run operation and mirror tests**

Run: `py -3.11 -m pytest backend/tests/test_portable_operations.py backend/tests/test_integration_sync.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the operation protocol**

```powershell
git add scripts/portable_operations.py scripts/sync_integrations.py integrations/contract_tests/test_portable_integration.py backend/tests/test_portable_operations.py
git commit -m "feat: add portable operation event protocol"
```

### Task 3: Emit download progress and support safe cancellation

**Files:**
- Modify: `scripts/portable_install.py`
- Modify: `backend/tests/test_portable_install.py`
- Modify: `scripts/initialize-portable.ps1`
- Modify: `integrations/windows/Initialize.ps1`
- Modify: `scripts/bootstrap-conda.ps1`

**Interfaces:**
- Consumes: Task 2 operation directory and cancel marker `cancel.requested`.
- Produces: `Downloader = Callable[[str, Path, int, ProgressCallback | None, CancelCheck | None], None]`; progress callback `(downloaded_bytes, total_bytes, source_url) -> None`; exit code 20 for resumable cancellation.

- [ ] **Step 1: Write failing progress and cancellation tests**

```python
def test_download_reports_progress_and_keeps_partial_on_cancel(tmp_path: Path) -> None:
    installer = _load_installer()
    payload = b"0123456789"
    progress: list[tuple[int, int]] = []
    with pytest.raises(installer.PortableInstallCancelled):
        installer.ensure_locked_asset(
            {
                "id": "model",
                "urls": ["https://example.invalid/model.bin"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            },
            tmp_path / "model.bin",
            downloader=_chunked_downloader(payload),
            progress=lambda done, total, _url: progress.append((done, total)),
            cancelled=lambda: bool(progress),
        )
    assert progress[0][1] == 10
    assert (tmp_path / "model.bin.partial").exists()
```

Define `_chunked_downloader(payload: bytes) -> installer.Downloader` in the same test module. Its returned callable accepts `(url, target, resume_from, progress, cancelled)`, appends one byte at a time from `resume_from`, calls `progress(written, len(payload), url)` after each byte, and raises `PortableInstallCancelled` before the next write when `cancelled()` is true. Update the existing downloader test doubles to this five-argument signature.

- [ ] **Step 2: Run installer tests and confirm failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_install.py -q`

Expected: FAIL because cancellation and progress callbacks do not exist.

- [ ] **Step 3: Add callback-aware download and CLI event arguments**

```python
class PortableInstallCancelled(RuntimeError):
    pass

ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]
Downloader = Callable[[str, Path, int, ProgressCallback | None, CancelCheck | None], None]

def _copy_response(response: Any, output: BinaryIO, *, start: int, total: int, url: str, progress: ProgressCallback | None, cancelled: CancelCheck | None) -> None:
    written = start
    while chunk := response.read(1024 * 1024):
        if cancelled and cancelled():
            raise PortableInstallCancelled("portable installation cancelled")
        output.write(chunk)
        written += len(chunk)
        if progress:
            progress(written, total, url)
```

Add `--operation-root` and `--cancel-file` to `ensure-asset`; append throttled `downloading` events. Pass those arguments from both initializers and make Miniforge bootstrap retain its `.partial` file when cancellation is requested.

- [ ] **Step 4: Run installer and package tests**

Run: `py -3.11 -m pytest backend/tests/test_portable_install.py backend/tests/test_portable_packages.py -q`

Expected: PASS.

- [ ] **Step 5: Commit resumable progress**

```powershell
git add scripts/portable_install.py backend/tests/test_portable_install.py scripts/initialize-portable.ps1 integrations/windows/Initialize.ps1 scripts/bootstrap-conda.ps1
git commit -m "feat: report and cancel portable downloads safely"
```

### Task 4: Make Start.cmd initialize and start through one controller

**Files:**
- Create: `scripts/Invoke-PortableStart.ps1`
- Create: `scripts/Show-PortableProgress.ps1`
- Create: `backend/tests/test_portable_start_controller.py`
- Create: `packaging/portable/error-catalog.zh-CN.json`
- Modify: `Start.cmd`
- Modify: `scripts/start-production.ps1`
- Modify: `scripts/initialize-portable.ps1`
- Modify: `Build-Package.ps1`
- Modify: `scripts/sync_integrations.py`
- Modify: `integrations/windows/Start-Worker.ps1`

**Interfaces:**
- Consumes: Tasks 1-3 manifest, install state and operations.
- Produces: `Start.cmd [-OperationId UUID] [-ManagedBy tts-more] [-NoUi] [-PortOverride 1..65535]`; one package-scoped lock; existing-operation attachment.
- Produces PowerShell helpers with exact contracts: `Get-PackageContext(Root: string) -> PSCustomObject`, `Assert-PackageWritable(Root: string)`, `Open-PackageOperationLock(Root: string) -> IDisposable`, `Test-InstallState(Root: string) -> bool`, `Invoke-Initialize(Root: string, Operation: string)`, `Invoke-ServiceStart(Root: string, Operation: string, PortOverride: int?)`, and `Resolve-PortableExitCode(ErrorRecord: ErrorRecord) -> int`.

- [ ] **Step 1: Write failing controller contract tests**

```python
def test_start_cmd_uses_controller_that_calls_initialize_before_service() -> None:
    start = (REPO_ROOT / "Start.cmd").read_text(encoding="utf-8")
    controller = (REPO_ROOT / "scripts" / "Invoke-PortableStart.ps1").read_text(encoding="utf-8")
    assert "Invoke-PortableStart.ps1" in start
    assert "Test-InstallState" in controller
    assert "Assert-PackageWritable" in controller
    assert "Invoke-Initialize" in controller
    assert "Invoke-ServiceStart" in controller
    assert controller.index("Invoke-Initialize") < controller.index("Invoke-ServiceStart")
    assert "PACKAGE_CORRUPT" in controller
```

- [ ] **Step 2: Run the new test and confirm missing controller failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_start_controller.py -q`

Expected: FAIL because the controller files do not exist.

- [ ] **Step 3: Implement the controller and UI wrapper**

```powershell
param(
    [string]$OperationId = "",
    [string]$ManagedBy = "direct",
    [switch]$NoUi,
    [ValidateRange(1, 65535)][Nullable[int]]$PortOverride = $null
)

class PortableStartException : System.Exception {
    [string]$Code
    PortableStartException([string]$code, [string]$message) : base($message) { $this.Code = $code }
}
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (!$OperationId) { $OperationId = [guid]::NewGuid().ToString() }
$operation = Join-Path $Root "data\local\operations\$OperationId"
New-Item -ItemType Directory -Force -Path $operation | Out-Null
$lock = Open-PackageOperationLock -Root $Root
try {
    Assert-PackageWritable -Root $Root
    $context = Get-PackageContext -Root $Root
    Initialize-Operation -Root $Root -OperationId $OperationId -Initiator $ManagedBy
    if (!(Test-InstallState -Root $Root)) {
        if ($context.Profile -eq "full") { throw [PortableStartException]::new("PACKAGE_CORRUPT", "Full package assets are missing or invalid") }
        Invoke-Initialize -Root $Root -Operation $operation
    }
    Invoke-ServiceStart -Root $Root -Operation $operation -PortOverride $PortOverride
    Complete-Operation -Root $Root -OperationId $OperationId -Status ready -ExitCode 0
} catch {
    Fail-Operation -Root $Root -OperationId $OperationId -ErrorRecord $_
    exit (Resolve-PortableExitCode $_)
} finally { $lock.Dispose() }
```

Implement package-lock attachment when another operation is active. `Get-PackageContext` reads the staged manifest, or constructs a `source-checkout` context from TTS More packaging inputs or a fork’s `tts_more/component.json`; only staged manifests may claim `full`. `Assert-PackageWritable` rejects ZIP preview, Program Files and read-only roots with `PACKAGE_NOT_WRITABLE` instead of requesting elevation. `Show-PortableProgress.ps1` reads `events.jsonl`, offers minimize/background/cancel, and writes `cancel.requested`; it falls back to console when WinForms cannot initialize. Stage a Chinese error catalog containing `DOWNLOAD_NETWORK_INTERRUPTED`, `DISK_SPACE_INSUFFICIENT`, `CUDA_PROBE_FAILED`, `PORT_IN_USE`, `PACKAGE_NOT_WRITABLE` and `PACKAGE_CORRUPT`, with fields for event, cause, unchanged data and next action. After TTS More becomes ready, open its loopback URL; worker packages only display and copy their URL. Update `Start.cmd` to invoke only this controller.

- [ ] **Step 4: Run controller, launcher and package tests**

Run: `py -3.11 -m pytest backend/tests/test_portable_start_controller.py backend/tests/test_portable_launcher.py backend/tests/test_portable_packages.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the one-click entry**

```powershell
git add Start.cmd scripts/Invoke-PortableStart.ps1 scripts/Show-PortableProgress.ps1 scripts/start-production.ps1 scripts/initialize-portable.ps1 Build-Package.ps1 scripts/sync_integrations.py integrations/windows/Start-Worker.ps1 packaging/portable/error-catalog.zh-CN.json backend/tests/test_portable_start_controller.py
git commit -m "feat: initialize from the portable Start entry"
```

### Task 5: Verify process ownership, diagnostics and TTS More package behavior

**Files:**
- Modify: `scripts/portable_launcher.py`
- Modify: `backend/tests/test_portable_launcher.py`
- Create: `scripts/export-portable-diagnostics.py`
- Create: `backend/tests/test_portable_diagnostics.py`
- Modify: `Build-Package.ps1`
- Modify: `.github/workflows/portable-release.yml`

**Interfaces:**
- Consumes: operation and PID records.
- Produces: redacted diagnostic ZIP and reliable repeated-start/stop behavior.

- [ ] **Step 1: Write failing redaction and active-operation tests**

```python
def test_diagnostics_remove_machine_identity(tmp_path: Path) -> None:
    report = build_diagnostic_report(
        package_root=tmp_path / "用户名字" / "TTS More",
        operation={"message": "C:\\Users\\用户名字\\secret.wav", "device_uuid": "GPU-secret"},
    )
    text = json.dumps(report, ensure_ascii=False)
    assert "用户名字" not in text
    assert "secret.wav" not in text
    assert "GPU-secret" not in text
```

- [ ] **Step 2: Run launcher and diagnostics tests and confirm failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_launcher.py backend/tests/test_portable_diagnostics.py -q`

Expected: FAIL because the diagnostic exporter does not exist.

- [ ] **Step 3: Implement fail-closed diagnostics and ownership checks**

```python
SENSITIVE_KEYS = {"device_uuid", "audio_path", "output_path", "proxy", "authorization"}

def redact(value: object, *, package_root: Path) -> object:
    if isinstance(value, dict):
        return {key: redact(item, package_root=package_root) for key, item in value.items() if key.lower() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [redact(item, package_root=package_root) for item in value]
    if isinstance(value, str):
        return value.replace(str(package_root), "<PACKAGE_ROOT>") if value.startswith(str(package_root)) else "<REDACTED_PATH>" if ":\\" in value else value
    return value
```

Keep PID records until both the owned process and port are gone. Include only manifest version, lock hashes, status, error codes and redacted probe results in the diagnostic ZIP.

- [ ] **Step 4: Run the entire portable test slice and build a CPU Bootstrap package**

Run: `py -3.11 -m pytest backend/tests/test_portable_*.py backend/tests/test_integration_sync.py -q`

Run: `.\Build-Package.ps1 -Profile Bootstrap -Device CPU -Version 0.2.0-plancheck`

Expected: all tests PASS; one audited Bootstrap ZIP is created; the ZIP contains no runtime, models, cache or machine paths.

- [ ] **Step 5: Commit the Phase A verification gate**

```powershell
git add scripts/portable_launcher.py scripts/export-portable-diagnostics.py Build-Package.ps1 .github/workflows/portable-release.yml backend/tests/test_portable_launcher.py backend/tests/test_portable_diagnostics.py
git commit -m "test: gate one-click portable control core"
```
