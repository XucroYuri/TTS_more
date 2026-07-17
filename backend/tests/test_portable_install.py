from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import urllib.error
from pathlib import Path
from uuid import UUID

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLER = None


def _load_installer():
    global _INSTALLER
    if _INSTALLER is not None:
        return _INSTALLER
    module_path = REPO_ROOT / "scripts" / "portable_install.py"
    assert module_path.is_file(), "portable installer core is missing"
    spec = importlib.util.spec_from_file_location("portable_install", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _INSTALLER = module
    return _INSTALLER


installer = _load_installer()


def _runtime_lock() -> dict[str, object]:
    return {
        "schema_version": 1,
        "profiles": {
            "cu128": {"minimum_nvidia_driver": "570.0"},
            "cu126": {"minimum_nvidia_driver": "560.0"},
            "cpu": {},
        },
        "auto_order": ["cu128", "cu126", "cpu"],
    }


def _chunked_downloader(payload: bytes) -> installer.Downloader:
    def download(url: str, target: Path, resume_from: int, progress, cancelled) -> None:
        with target.open("ab") as handle:
            for byte in payload[resume_from:]:
                if cancelled and cancelled():
                    raise installer.PortableInstallCancelled("portable installation cancelled")
                handle.write(bytes((byte,)))
                if progress:
                    progress(handle.tell(), len(payload), url)

    return download


class _FakeHttpResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, status: int, headers: dict[str, str]) -> None:
        super().__init__(payload)
        self.status = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def test_select_device_profile_uses_cim_driver_without_nvidia_smi() -> None:
    installer = _load_installer()
    controllers = [{"name": "NVIDIA GeForce RTX 5090", "driver_version": "32.0.15.7652"}]

    selected = installer.select_device_profile(_runtime_lock(), "auto", controllers)

    assert selected == "cu128"
    assert installer.nvidia_marketing_driver("32.0.15.7652") == (576, 52)


def test_select_device_profile_falls_back_but_explicit_cuda_fails_closed() -> None:
    installer = _load_installer()
    controllers = [{"name": "NVIDIA GeForce", "driver_version": "32.0.15.6120"}]

    assert installer.select_device_profile(_runtime_lock(), "auto", controllers) == "cu126"
    with pytest.raises(RuntimeError, match="cu128 requires NVIDIA driver 570.0"):
        installer.select_device_profile(_runtime_lock(), "cu128", controllers)


def test_select_device_profile_uses_cpu_when_no_nvidia_controller_exists() -> None:
    installer = _load_installer()

    selected = installer.select_device_profile(
        _runtime_lock(), "auto", [{"name": "Microsoft Basic Display Adapter", "driver_version": "10.0"}]
    )

    assert selected == "cpu"


def test_ensure_locked_asset_resumes_partial_and_promotes_only_after_hash(tmp_path: Path) -> None:
    installer = _load_installer()
    payload = b"portable-runtime-payload"
    destination = tmp_path / "cache" / "runtime.zip"
    partial = destination.with_name("runtime.zip.partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(payload[:9])
    calls: list[tuple[str, Path, int, object, object]] = []
    progress = lambda _done, _total, _url: None
    cancelled = lambda: False

    def download(url: str, target: Path, resume_from: int, on_progress, is_cancelled) -> None:
        calls.append((url, target, resume_from, on_progress, is_cancelled))
        with target.open("ab") as handle:
            handle.write(payload[resume_from:])

    report = installer.ensure_locked_asset(
        {
            "id": "runtime",
            "urls": ["https://example.invalid/runtime.zip"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        },
        destination,
        downloader=download,
        progress=progress,
        cancelled=cancelled,
    )

    assert report == {"path": str(destination), "reused": False, "source": "https://example.invalid/runtime.zip"}
    assert calls == [("https://example.invalid/runtime.zip", partial, 9, progress, cancelled)]
    assert destination.read_bytes() == payload
    assert not partial.exists()


def test_complete_valid_partial_is_promoted_without_network(tmp_path: Path) -> None:
    installer = _load_installer()
    payload = b"complete-valid-partial"
    destination = tmp_path / "model.bin"
    partial = destination.with_name("model.bin.partial")
    partial.write_bytes(payload)

    def fail_download(*_args) -> None:
        raise AssertionError("network must not be used for a complete valid partial")

    report = installer.ensure_locked_asset(
        {
            "id": "model",
            "urls": ["https://example.invalid/model.bin"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        },
        destination,
        downloader=fail_download,
    )

    assert report == {"path": str(destination), "reused": False, "source": ""}
    assert destination.read_bytes() == payload
    assert not partial.exists()


def test_complete_invalid_partial_restarts_from_zero(tmp_path: Path) -> None:
    installer = _load_installer()
    payload = b"expected-full-payload"
    destination = tmp_path / "model.bin"
    partial = destination.with_name("model.bin.partial")
    partial.write_bytes(b"x" * len(payload))
    resume_offsets: list[int] = []

    def download(_url: str, target: Path, resume_from: int, _progress, _cancelled) -> None:
        resume_offsets.append(resume_from)
        target.write_bytes(payload)

    installer.ensure_locked_asset(
        {
            "id": "model",
            "urls": ["https://example.invalid/model.bin"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        },
        destination,
        downloader=download,
    )

    assert resume_offsets == [0]
    assert destination.read_bytes() == payload


def test_http_resume_appends_only_matching_206(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _load_installer()
    payload = b"0123456789"
    destination = tmp_path / "payload.partial"
    destination.write_bytes(payload[:4])
    requests: list[str | None] = []
    progress: list[tuple[int, int]] = []

    def urlopen(request, timeout: int):
        assert timeout == 120
        requests.append(request.headers.get("Range"))
        return _FakeHttpResponse(
            payload[4:],
            status=206,
            headers={"Content-Range": "bytes 4-9/10", "Content-Length": "6"},
        )

    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)
    installer._download_http(
        "https://example.invalid/payload.bin",
        destination,
        4,
        lambda done, total, _url: progress.append((done, total)),
        None,
    )

    assert requests == ["bytes=4-"]
    assert destination.read_bytes() == payload
    assert progress[-1] == (10, 10)


@pytest.mark.parametrize(
    "bad_headers",
    [
        {"Content-Length": "7"},
        {"Content-Range": "not-a-range", "Content-Length": "7"},
        {"Content-Range": "bytes 3-9/10", "Content-Length": "7"},
    ],
    ids=["missing-content-range", "malformed-content-range", "mismatched-start"],
)
def test_http_invalid_206_retries_clean_without_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_headers: dict[str, str]
) -> None:
    installer = _load_installer()
    payload = b"0123456789"
    destination = tmp_path / "payload.partial"
    destination.write_bytes(payload[:4])
    responses = iter(
        [
            _FakeHttpResponse(
                payload[3:],
                status=206,
                headers=bad_headers,
            ),
            _FakeHttpResponse(payload, status=200, headers={"Content-Length": "10"}),
        ]
    )
    requests: list[str | None] = []

    def urlopen(request, timeout: int):
        assert timeout == 120
        requests.append(request.headers.get("Range"))
        return next(responses)

    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)
    installer._download_http("https://example.invalid/payload.bin", destination, 4, None, None)

    assert requests == ["bytes=4-", None]
    assert destination.read_bytes() == payload


def test_http_ignored_range_200_replaces_partial_from_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer()
    payload = b"0123456789"
    destination = tmp_path / "payload.partial"
    destination.write_bytes(payload[:4])
    requests: list[str | None] = []

    def urlopen(request, timeout: int):
        assert timeout == 120
        requests.append(request.headers.get("Range"))
        return _FakeHttpResponse(payload, status=200, headers={"Content-Length": "10"})

    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)
    installer._download_http("https://example.invalid/payload.bin", destination, 4, None, None)

    assert requests == ["bytes=4-"]
    assert destination.read_bytes() == payload


def test_http_range_416_retries_clean_without_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _load_installer()
    payload = b"0123456789"
    destination = tmp_path / "payload.partial"
    destination.write_bytes(payload[:4])
    requests: list[str | None] = []

    def urlopen(request, timeout: int):
        assert timeout == 120
        range_header = request.headers.get("Range")
        requests.append(range_header)
        if range_header is not None:
            raise urllib.error.HTTPError(request.full_url, 416, "range not satisfiable", {}, io.BytesIO())
        return _FakeHttpResponse(payload, status=200, headers={"Content-Length": "10"})

    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)
    installer._download_http("https://example.invalid/payload.bin", destination, 4, None, None)

    assert requests == ["bytes=4-", None]
    assert destination.read_bytes() == payload


def test_ensure_locked_asset_keeps_corrupt_partial_and_never_promotes_it(tmp_path: Path) -> None:
    installer = _load_installer()
    destination = tmp_path / "model.bin"

    def download(_url: str, target: Path, _resume_from: int, _progress, _cancelled) -> None:
        target.write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="failed SHA-256 verification"):
        installer.ensure_locked_asset(
            {
                "id": "model",
                "urls": ["https://example.invalid/model.bin"],
                "sha256": hashlib.sha256(b"expected").hexdigest(),
                "size_bytes": 8,
            },
            destination,
            downloader=download,
        )

    assert not destination.exists()
    assert destination.with_name("model.bin.partial").read_bytes() == b"corrupt"


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
    assert (tmp_path / "model.bin.partial").read_bytes() == payload[:1]


def test_cancel_is_checked_before_returning_cached_destination(tmp_path: Path) -> None:
    installer = _load_installer()
    payload = b"cached-destination"
    destination = tmp_path / "model.bin"
    destination.write_bytes(payload)

    with pytest.raises(installer.PortableInstallCancelled):
        installer.ensure_locked_asset(
            {
                "id": "model",
                "urls": ["https://example.invalid/model.bin"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            },
            destination,
            cancelled=lambda: True,
        )


def test_cancellation_is_not_swallowed_as_a_url_fallback_failure(tmp_path: Path) -> None:
    installer = _load_installer()
    payload = b"fallback-must-not-run"
    calls: list[str] = []

    def download(url: str, target: Path, _resume_from: int, progress, _cancelled) -> None:
        calls.append(url)
        target.write_bytes(payload[:1])
        if progress:
            progress(1, len(payload), url)
        raise installer.PortableInstallCancelled("portable installation cancelled")

    with pytest.raises(installer.PortableInstallCancelled):
        installer.ensure_locked_asset(
            {
                "id": "model",
                "urls": ["https://primary.invalid/model.bin", "https://fallback.invalid/model.bin"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            },
            tmp_path / "model.bin",
            downloader=download,
            progress=lambda _done, _total, _url: None,
            cancelled=lambda: False,
        )

    assert calls == ["https://primary.invalid/model.bin"]


def test_ensure_asset_cli_throttles_operation_events_and_returns_20_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer()
    from scripts.portable_operations import create_operation, read_operation

    operation_id = str(UUID("11111111-1111-4111-8111-111111111111"))
    package_root = tmp_path / "package"
    operations = package_root / "data" / "local" / "operations"
    create_operation(operations, operation_id, "gpt-sovits", "start", "direct")
    operation = operations / operation_id
    payload = b"event-progress"
    asset_path = tmp_path / "asset.json"
    asset_path.write_text(
        json.dumps(
            {
                "id": "model",
                "urls": ["https://example.invalid/model.bin"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        ),
        encoding="utf-8",
    )

    def download(url: str, target: Path, _resume_from: int, progress, _cancelled) -> None:
        target.write_bytes(payload)
        assert progress is not None
        progress(1, len(payload), url)
        progress(2, len(payload), url)
        progress(len(payload), len(payload), url)

    monkeypatch.setattr(installer, "_download_http", download)
    assert (
        installer.main(
            [
                "ensure-asset",
                "--asset",
                str(asset_path),
                "--path",
                str(tmp_path / "model.bin"),
                "--package-root",
                str(package_root),
                "--operation-root",
                str(operation),
                "--cancel-file",
                str(operation / "cancel.requested"),
            ]
        )
        == 0
    )
    _, events = read_operation(operations, operation_id)
    download_events = [event for event in events if event["phase"] == "downloading"]
    assert 1 <= len(download_events) <= 2
    assert download_events[-1]["percent"] == 100.0

    cancelled_operation_id = str(UUID("22222222-2222-4222-8222-222222222222"))
    create_operation(operations, cancelled_operation_id, "gpt-sovits", "start", "direct")
    cancelled_operation = operations / cancelled_operation_id
    cancel_file = cancelled_operation / "cancel.requested"
    cancel_file.touch()
    assert (
        installer.main(
            [
                "ensure-asset",
                "--asset",
                str(asset_path),
                "--path",
                str(tmp_path / "cancelled-model.bin"),
                "--package-root",
                str(package_root),
                "--operation-root",
                str(cancelled_operation),
                "--cancel-file",
                str(cancel_file),
            ]
        )
        == 20
    )


def test_operation_contract_rejects_unpaired_and_uncontained_paths(tmp_path: Path) -> None:
    installer = _load_installer()
    package_root = tmp_path / "package"
    operations = package_root / "data" / "local" / "operations"
    valid_id = "11111111-1111-4111-8111-111111111111"
    valid_operation = operations / valid_id
    valid_cancel = valid_operation / "cancel.requested"
    invalid_cases = [
        (valid_operation, None, "provided together"),
        (None, valid_cancel, "provided together"),
        (tmp_path / "outside" / valid_id, tmp_path / "outside" / valid_id / "cancel.requested", "direct child"),
        (
            operations / ".." / valid_id,
            operations / ".." / valid_id / "cancel.requested",
            "direct child",
        ),
        (operations / "not-a-uuid", operations / "not-a-uuid" / "cancel.requested", "valid UUID"),
        (valid_operation, operations / "cancel.requested", "cancel.requested"),
    ]
    if os.name == "nt":
        drive = "Z:" if package_root.drive.upper() != "Z:" else "Y:"
        other_drive_operation = Path(f"{drive}/{valid_id}")
        invalid_cases.append(
            (other_drive_operation, other_drive_operation / "cancel.requested", "direct child")
        )

    for operation_root, cancel_file, message in invalid_cases:
        with pytest.raises(ValueError, match=message):
            installer.validate_operation_paths(package_root, operation_root, cancel_file)


def test_cli_rejects_invalid_operation_contract_before_asset_io(tmp_path: Path) -> None:
    installer = _load_installer()
    package_root = tmp_path / "package"
    invalid_operation = tmp_path / "outside" / "11111111-1111-4111-8111-111111111111"

    with pytest.raises(ValueError, match="direct child"):
        installer.main(
            [
                "ensure-asset",
                "--asset",
                str(tmp_path / "missing-asset.json"),
                "--path",
                str(tmp_path / "model.bin"),
                "--package-root",
                str(package_root),
                "--operation-root",
                str(invalid_operation),
                "--cancel-file",
                str(invalid_operation / "cancel.requested"),
            ]
        )


def test_initializers_forward_operation_and_cancel_contract_to_downloads() -> None:
    controller = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(encoding="utf-8")
    worker = (REPO_ROOT / "integrations" / "windows" / "Initialize.ps1").read_text(encoding="utf-8")
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap-conda.ps1").read_text(encoding="utf-8")

    for initializer in (controller, worker):
        assert '[string]$OperationRoot = ""' in initializer
        assert '[string]$CancelFile = ""' in initializer
        assert "Resolve-OperationContract" in initializer
        assert '"data\\local\\operations"' in initializer
        assert "[guid]::TryParse" in initializer
        assert "--package-root" in initializer
        assert "--operation-root" in initializer
        assert "--cancel-file" in initializer
        assert "-PackageRoot $Root" in initializer
        assert "-OperationRoot $OperationRoot" in initializer
        assert "-CancelFile $CancelFile" in initializer
    assert '[string]$PackageRoot = ""' in bootstrap
    assert '[string]$OperationRoot = ""' in bootstrap
    assert '[string]$CancelFile = ""' in bootstrap
    assert "Resolve-OperationContract" in bootstrap
    assert "Portable initialization cancelled" in bootstrap
    assert 'FileMode]::Append' in bootstrap


def test_initializers_execute_runtime_lock_import_probe() -> None:
    controller = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(encoding="utf-8")
    worker = (REPO_ROOT / "integrations" / "windows" / "Initialize.ps1").read_text(encoding="utf-8")

    assert '$ImportProbe = if ($RuntimePayload.PSObject.Properties["import_probe"]' in controller
    assert "& $PortableRuntime.Python -c $ImportProbe" in controller
    assert 'import fastapi,pydantic,uvicorn; print(' not in controller
    assert "foreach ($asset in @($modelLockPayload.assets))" in controller
    assert "required model asset is missing after locked initialization" in controller
    assert '$importProbe = if ($runtimeLock.PSObject.Properties["import_probe"]' in worker
    assert "& $PortableRuntime.Python -c $importProbe" in worker
    assert "& $PortableRuntime.Uv pip check --python $PortableRuntime.Python" in worker
    assert "--target $PortableRuntime.SitePackages --link-mode copy" in worker
    assert "& $StagePython -c ([string]$config.import_probe)" not in worker


def test_controller_initializer_uses_only_embedded_python_runtime() -> None:
    controller = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(
        encoding="utf-8"
    )

    assert '. (Join-Path $Root "scripts\\portable-python.ps1")' in controller
    assert "$PortableRuntime = Install-PortablePythonRuntime" in controller
    assert "-Destination $Staging" in controller
    assert "& $PortableRuntime.Python -c" in controller
    assert "& $PortableRuntime.Uv lock --check" in controller
    assert "& $PortableRuntime.Uv export --frozen --no-dev --no-emit-project" in controller
    assert "--python $PortableRuntime.Python" in controller
    assert "--target $PortableRuntime.SitePackages" in controller
    assert "--link-mode copy" in controller
    assert "& $PortableRuntime.Uv pip check --python $PortableRuntime.Python" in controller
    forbidden = (
        "bootstrap-conda.ps1",
        "$Conda",
        "conda create",
        "-m pip",
        "Scripts\\uv.exe",
        "$BootstrapPython",
    )
    assert not any(token.casefold() in controller.casefold() for token in forbidden)

    python_install = controller.index("Install-PortablePythonRuntime")
    patch_probe = controller.index("sys.version_info[:3]")
    device_selection = controller.index("select-device")
    model_assets = controller.index("foreach ($asset in @($modelLockPayload.assets))")
    dependency_install = controller.index("pip install")
    import_probe = controller.index("-c $ImportProbe")
    atomic_publish = controller.rindex("Publish-PortableRuntimeTransaction -Staging $Staging")
    assert (
        python_install
        < patch_probe
        < device_selection
        < model_assets
        < dependency_install
        < import_probe
        < atomic_publish
    )


def test_controller_preserves_typed_cancellation_from_python_and_uv_downloads() -> None:
    helper = (REPO_ROOT / "scripts" / "portable-python.ps1").read_text(encoding="utf-8")
    mirror = (REPO_ROOT / "integrations" / "windows" / "portable-python.ps1").read_text(
        encoding="utf-8"
    )
    controller = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(
        encoding="utf-8"
    )

    assert helper == mirror
    assert "[System.OperationCanceledException]" in helper
    assert "if ($LASTEXITCODE -eq 20)" in helper
    assert "throw [System.OperationCanceledException]::new" in helper
    assert "catch [System.OperationCanceledException]" in controller
    assert "exit 20" in controller


def test_controller_runtime_publish_restores_previous_after_staging_move_failure() -> None:
    controller = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(
        encoding="utf-8"
    )

    assert "function Publish-PortableRuntimeTransaction" in controller
    assert "catch" in controller
    assert "Remove-Item -LiteralPath $Live -Recurse -Force" in controller
    assert "Move-Item -LiteralPath $Backup -Destination $Live" in controller
    assert "throw" in controller


def test_controller_runtime_publish_keeps_previous_until_state_commit() -> None:
    controller = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(
        encoding="utf-8"
    )

    assert "function Publish-PortableRuntimeTransaction" in controller
    assert controller.index("& $CommitState") < controller.rindex(
        "Remove-Item -LiteralPath $Backup -Recurse -Force"
    )
    assert "TTS_MORE_TEST_FAIL" not in controller


@pytest.mark.skipif(os.name != "nt", reason="runtime transaction uses Windows PowerShell filesystem semantics")
@pytest.mark.parametrize("failure", ("move", "state"))
def test_controller_runtime_publish_rolls_back_real_move_and_state_failures(
    tmp_path: Path, failure: str
) -> None:
    root = tmp_path / failure
    live = root / "live"
    staging = root / "staging"
    backup = root / "previous"
    state = root / "install-state.json"
    live.mkdir(parents=True)
    (live / "sentinel.txt").write_text("previous", encoding="utf-8")
    state.write_text("previous-state", encoding="utf-8")
    if failure == "state":
        staging.mkdir()
        (staging / "sentinel.txt").write_text("candidate", encoding="utf-8")

    initializer = REPO_ROOT / "scripts" / "initialize-portable.ps1"
    commit = "{ throw 'state write failed' }" if failure == "state" else "{ throw 'must not run' }"
    command = f"""
$tokens=$null; $errors=$null
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile('{initializer}',[ref]$tokens,[ref]$errors)
$fn=$ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Publish-PortableRuntimeTransaction'}},$true)
. ([scriptblock]::Create($fn.Extent.Text))
try {{ Publish-PortableRuntimeTransaction -Staging '{staging}' -Live '{live}' -Backup '{backup}' -CommitState {commit}; exit 91 }} catch {{ $message=$_.Exception.Message }}
if (!(Test-Path -LiteralPath '{live / 'sentinel.txt'}') -or (Get-Content -Raw '{live / 'sentinel.txt'}') -ne 'previous') {{ exit 92 }}
if (Test-Path -LiteralPath '{backup}') {{ exit 93 }}
if ((Get-Content -Raw '{state}') -ne 'previous-state') {{ exit 94 }}
    if ($message -notmatch '{'state write failed' if failure == 'state' else 'Cannot move item'}') {{ exit 95 }}
exit 0
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_portable_installer_is_natively_compatible_with_python_310() -> None:
    installer = (REPO_ROOT / "scripts" / "portable_install.py").read_text(encoding="utf-8")

    assert "from datetime import datetime, timezone" in installer
    assert "datetime.now(timezone.utc)" in installer
    assert "from datetime import UTC" not in installer


def test_write_install_state_is_atomic_and_records_lock_digests(tmp_path: Path) -> None:
    installer = _load_installer()
    state = tmp_path / "data" / "local" / "install-state.json"

    installer.write_install_state(
        state,
        component="gpt-sovits",
        build_id="build-1",
        profile="cu128",
        runtime_lock_sha256="a" * 64,
        model_lock_sha256="b" * 64,
    )

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["component"] == "gpt-sovits"
    assert payload["profile"] == "cu128"
    assert payload["runtime_lock_sha256"] == "a" * 64
    assert payload["model_lock_sha256"] == "b" * 64
    assert payload["ready"] is True
    assert not state.with_suffix(".json.tmp").exists()


def test_windows_powershell_utf8_bom_json_is_accepted(tmp_path: Path) -> None:
    installer = _load_installer()
    path = tmp_path / "asset.json"
    path.write_text(json.dumps({"id": "locked-asset"}), encoding="utf-8-sig")

    assert installer.load_json(path) == {"id": "locked-asset"}
