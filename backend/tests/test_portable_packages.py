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
    for dependency in ("numpy==1.26.4", "MarkupSafe==2.0.1", "websockets==12.0", "starlette==0.46.2", "setuptools==80.9.0"):
        assert dependency in requirements
    assert manifest["component"] == "gpt-sovits-dev"
    assert manifest["port"] == 9883


def test_every_local_tts_component_has_a_path_relative_start_and_stop_launcher() -> None:
    launchers = (
        (REPO_ROOT, "8000", "scripts\\start-dev.ps1"),
        (REPO_ROOT / "deployment" / "portable" / "gpt-sovits-dev", "9883", "runtime\\runtime.zip"),
        (REPO_ROOT / "repo" / "index-tts", "7860", ".venv\\Scripts\\python.exe"),
        (REPO_ROOT / "repo" / "CosyVoice", "9882", ".venv\\Scripts\\python.exe"),
    )

    for root, port, entrypoint in launchers:
        start = root / "Start.cmd"
        stop = root / "Stop.cmd"
        assert start.is_file(), f"missing Start.cmd: {start}"
        assert stop.is_file(), f"missing Stop.cmd: {stop}"
        contents = start.read_text(encoding="utf-8")
        assert "%~dp0" in contents
        assert port in contents
        assert entrypoint in contents
        assert "pid.json" in stop.read_text(encoding="utf-8")
