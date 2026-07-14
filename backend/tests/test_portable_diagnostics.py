from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_diagnostics():
    module_path = REPO_ROOT / "scripts" / "export-portable-diagnostics.py"
    spec = importlib.util.spec_from_file_location("portable_diagnostics", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package(root: Path) -> Path:
    package_root = root / "用户名字" / "TTS More"
    (package_root / "package").mkdir(parents=True)
    (package_root / "packaging" / "portable").mkdir(parents=True)
    (package_root / "data" / "local").mkdir(parents=True)
    runtime_lock = package_root / "packaging" / "portable" / "runtime.lock.json"
    model_lock = package_root / "packaging" / "portable" / "models.lock.json"
    runtime_lock.write_text('{"safe":"runtime"}\n', encoding="utf-8")
    model_lock.write_text('{"safe":"models"}\n', encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "component": "tts-more",
        "package_id": "tts-more",
        "release_version": "0.2.0",
        "package_profile": "bootstrap",
        "build_id": "tts-more-0.2.0-deadbeef",
        "runtime": {"lock": "packaging/portable/runtime.lock.json"},
        "models": {"lock": "packaging/portable/models.lock.json"},
    }
    (package_root / "package" / "tts-more-package.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (package_root / "data" / "local" / "install-state.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "selected_device": "cpu",
                "machine_id": "DESKTOP-PRIVATE",
                "model_path": r"C:\Users\用户名字\models\secret.bin",
            }
        ),
        encoding="utf-8",
    )
    return package_root


def _directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(f"junction creation is not available: {completed.stderr}")
        return
    link.symlink_to(target, target_is_directory=True)


def test_diagnostics_remove_machine_identity_and_embedded_secrets(tmp_path: Path) -> None:
    diagnostics = _load_diagnostics()
    package_root = _package(tmp_path)
    report = diagnostics.build_diagnostic_report(
        package_root=package_root,
        operation={
            "status": "repairable",
            "message": (
                r"failed at C:\Users\用户名字\private-reference.wav "
                r"and \\server\share\output.wav; Authorization: Bearer top-secret-token; "
                "identity DESKTOP-PRIVATE 用户名字 GPU-embedded-secret"
            ),
            "device_uuid": "GPU-secret",
            "command_args": ["--api-key", "secret-key"],
            "error_code": "CUDA_PROBE_FAILED",
            "events": [
                {
                    "seq": 1,
                    "phase": "blocked",
                    "message": "input private-reference.wav could not be opened",
                    "audio_path": r"C:\Users\用户名字\private-reference.wav",
                    "unknown_private_field": "must-not-be-exported",
                }
            ],
        },
        probe={
            "status": "failed",
            "detail": r"python at C:\Portable Root\runtime\python.exe",
            "proxy": "http://user:password@proxy.invalid:8080",
            "environment": {"PATH": r"C:\Users\用户名字\bin"},
        },
    )
    text = json.dumps(report, ensure_ascii=False, sort_keys=True)

    for forbidden in (
        "用户名字",
        "private-reference.wav",
        "must-not-be-exported",
        "output.wav",
        "GPU-secret",
        "GPU-embedded-secret",
        "top-secret-token",
        "secret-key",
        "DESKTOP-PRIVATE",
        "proxy.invalid",
        "Portable Root",
        str(tmp_path),
    ):
        assert forbidden not in text
    assert "CUDA_PROBE_FAILED" in text
    assert report["manifest"]["schema_version"] == 2
    assert set(report["lock_sha256"]) == {"models", "runtime"}


def test_diagnostics_project_operation_and_probe_to_typed_machine_fields_only(tmp_path: Path) -> None:
    diagnostics = _load_diagnostics()
    package_root = _package(tmp_path)
    report = diagnostics.build_diagnostic_report(
        package_root=package_root,
        operation={
            "status": "ready",
            "exit_code": 0,
            "error_code": "PORT_IN_USE",
            "message": "customer@example.invalid CUSTOMER-MARKER token=private voice.wav",
            "environment": {"CUSTOMER": "nested-private"},
            "events": [
                {
                    "seq": 1,
                    "phase": "ready",
                    "percent": 42.5,
                    "error_code": "PORT_IN_USE",
                    "message": "customer@example.invalid C:\\private\\voice.wav",
                    "unknown": "CUSTOMER-EVENT",
                },
                {"seq": -1, "phase": "made-up", "percent": 900, "error_code": "secret"},
            ],
        },
        probe={
            "status": "failed",
            "error_code": "CUDA_PROBE_FAILED",
            "detail": "customer@example.invalid CUSTOMER-PROBE",
            "environment": {"TOKEN": "nested-private"},
            "checks": [
                {
                    "name": "python",
                    "status": "passed",
                    "passed": True,
                    "version": "3.11.9",
                    "duration_ms": 12.5,
                    "email": "customer@example.invalid",
                },
                {"name": "CUSTOMER-CHECK", "status": "passed", "passed": "yes"},
            ],
        },
    )

    assert report["operation"] == {
        "status": "ready",
        "exit_code": 0,
        "error_code": "PORT_IN_USE",
        "events": [
            {"seq": 1, "phase": "ready", "percent": 42.5, "error_code": "PORT_IN_USE"},
        ],
    }
    assert report["probe"] == {
        "status": "failed",
        "error_code": "CUDA_PROBE_FAILED",
        "checks": [
            {
                "name": "python",
                "status": "passed",
                "passed": True,
                "version": "3.11.9",
                "duration_ms": 12.5,
            },
        ],
    }
    text = json.dumps(report, ensure_ascii=False)
    for forbidden in (
        "customer@example.invalid",
        "CUSTOMER",
        "nested-private",
        "private",
        "voice.wav",
        "token=",
    ):
        assert forbidden not in text


def test_export_is_deterministic_atomic_contained_and_non_overwriting(tmp_path: Path) -> None:
    diagnostics = _load_diagnostics()
    package_root = _package(tmp_path)
    first = package_root / "data" / "local" / "diagnostics" / "first.zip"
    second = package_root / "data" / "local" / "diagnostics" / "second.zip"

    diagnostics.export_diagnostic_zip(package_root=package_root, output=first)
    diagnostics.export_diagnostic_zip(package_root=package_root, output=second)
    assert first.read_bytes() == second.read_bytes()
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        diagnostics.export_diagnostic_zip(package_root=package_root, output=first)
    assert first.read_bytes() == before
    assert not list(first.parent.glob("*.partial"))

    with pytest.raises(ValueError, match="diagnostics directory"):
        diagnostics.export_diagnostic_zip(
            package_root=package_root,
            output=tmp_path / "escaped.zip",
        )


def test_export_zip_is_strict_whitelist_and_never_follows_private_symlinks(tmp_path: Path) -> None:
    diagnostics = _load_diagnostics()
    package_root = _package(tmp_path)
    for relative, payload in (
        ("runtime/live/private.txt", "runtime-private"),
        ("models/voice.bin", "model-private"),
        ("data/cache/token.txt", "cache-private"),
        ("data/user/audio.wav", "audio-private"),
    ):
        path = package_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    outside_private = tmp_path / "outside-private"
    outside_private.mkdir()
    private = outside_private / "outside-private.txt"
    private.write_text("outside-private", encoding="utf-8")
    linked_directory = package_root / "data" / "local" / "linked-private"
    _directory_link(linked_directory, outside_private)

    output = package_root / "data" / "local" / "diagnostics" / "report.zip"
    diagnostics.export_diagnostic_zip(package_root=package_root, output=output)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["diagnostics/report.json"]
        info = archive.getinfo("diagnostics/report.json")
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        combined = archive.read("diagnostics/report.json").decode("utf-8")
    for forbidden in (
        "runtime-private",
        "model-private",
        "cache-private",
        "audio-private",
        "outside-private",
    ):
        assert forbidden not in combined


def test_failed_export_cleans_temporary_file_without_touching_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diagnostics = _load_diagnostics()
    package_root = _package(tmp_path)
    output = package_root / "data" / "local" / "diagnostics" / "report.zip"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"existing")

    monkeypatch.setattr(diagnostics, "_publish_no_replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(FileExistsError):
        diagnostics.export_diagnostic_zip(package_root=package_root, output=output)
    assert output.read_bytes() == b"existing"

    failing_output = output.with_name("failing.zip")
    monkeypatch.setattr(
        diagnostics,
        "_publish_no_replace",
        lambda *_args: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(OSError, match="boom"):
        diagnostics.export_diagnostic_zip(package_root=package_root, output=failing_output)
    assert not failing_output.exists()
    assert not list(output.parent.glob("*.partial"))


def test_output_directory_reparse_point_is_rejected(tmp_path: Path) -> None:
    diagnostics = _load_diagnostics()
    package_root = _package(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    diagnostics_root = package_root / "data" / "local" / "diagnostics"
    _directory_link(diagnostics_root, outside)

    with pytest.raises(ValueError, match="reparse|symlink"):
        diagnostics.export_diagnostic_zip(
            package_root=package_root,
            output=diagnostics_root / "report.zip",
        )


def test_bootstrap_builder_and_ci_gate_diagnostics_and_machine_path_audit() -> None:
    builder = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(
        encoding="utf-8"
    )

    assert builder.count('"export-portable-diagnostics.py"') == 1
    for marker in ("USERPROFILE", "USERNAME", "COMPUTERNAME"):
        assert marker in builder
    assert "test_portable_diagnostics.py" in workflow
    assert "export-portable-diagnostics.py" in workflow
    assert "diagnostics/report.json" in workflow
