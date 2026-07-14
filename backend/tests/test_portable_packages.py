from __future__ import annotations

import importlib.util
import json
import os
import types
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


def _valid_v2_manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "component": "gpt-sovits",
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
    for relative_path in (
        *payload["launchers"].values(),
        payload["runtime"]["lock"],
        payload["models"]["lock"],
        payload["licenses"],
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    manifest = tmp_path / "package" / "tts-more-package.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    v2_report = packages.validate_manifest(manifest, tmp_path)

    assert v2_report == {
        "valid": True,
        "errors": [],
        "component": "gpt-sovits",
        "default_endpoint": "http://127.0.0.1:9880",
        "launcher": "Start.cmd",
    }


def test_validate_manifest_accepts_windows_powershell_utf8_bom(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    (tmp_path / "Start.cmd").write_text("@echo off\r\n", encoding="utf-8")
    manifest = tmp_path / "tts-more-package.json"
    manifest.write_text(json.dumps(_valid_gpt_manifest()), encoding="utf-8-sig")

    assert packages.validate_manifest(manifest, tmp_path)["valid"] is True


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
    assert schema["oneOf"] == [{"$ref": "#/$defs/v1"}, {"$ref": "#/$defs/v2"}]


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


def test_stop_worker_accepts_windows_powershell_utf8_bom_pid_record(tmp_path: Path, monkeypatch) -> None:
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
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)

    monkeypatch.setattr(launcher, "os", types.SimpleNamespace(name="nt"))
    assert launcher.os is not os
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher.stop_worker(package_root) == 0
    assert calls == [["taskkill", "/PID", "1234", "/T", "/F"]]
    assert not record.exists()


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
        (REPO_ROOT, None, "scripts\\start-production.ps1"),
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
