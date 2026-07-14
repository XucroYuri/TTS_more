from __future__ import annotations

import hashlib
import importlib.util
import json
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
    operations = tmp_path / "operations"
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
                "--operation-root",
                str(cancelled_operation),
                "--cancel-file",
                str(cancel_file),
            ]
        )
        == 20
    )


def test_initializers_forward_operation_and_cancel_contract_to_downloads() -> None:
    controller = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(encoding="utf-8")
    worker = (REPO_ROOT / "integrations" / "windows" / "Initialize.ps1").read_text(encoding="utf-8")
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap-conda.ps1").read_text(encoding="utf-8")

    for initializer in (controller, worker):
        assert '[string]$OperationRoot = ""' in initializer
        assert 'Join-Path $OperationRoot "cancel.requested"' in initializer
        assert "--operation-root" in initializer
        assert "--cancel-file" in initializer
        assert "-OperationRoot $OperationRoot" in initializer
        assert "-CancelFile $CancelFile" in initializer
    assert '[string]$OperationRoot = ""' in bootstrap
    assert '[string]$CancelFile = ""' in bootstrap
    assert "Portable initialization cancelled" in bootstrap
    assert 'FileMode]::Append' in bootstrap


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
