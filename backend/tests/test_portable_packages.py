from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_worker_launch_scripts_use_package_relative_runtime_paths() -> None:
    start_path = REPO_ROOT / "deployment" / "portable" / "common" / "Start-Worker.cmd"
    stop_path = REPO_ROOT / "deployment" / "portable" / "common" / "Stop-Worker.cmd"
    assert start_path.is_file(), "portable Start-Worker.cmd is missing"
    assert stop_path.is_file(), "portable Stop-Worker.cmd is missing"
    start = start_path.read_text(encoding="utf-8")
    stop = stop_path.read_text(encoding="utf-8")

    assert "%~dp0" in start
    assert "portable_launcher.py" in start
    assert "TTS_MORE_ARTIFACT_ROOT=%~dp0data\\local\\artifacts" in start
    assert "Expand-Archive" in start
    assert "runtime.zip" in start
    assert "%~dp0" in stop
    assert "worker.pid.json" in stop
