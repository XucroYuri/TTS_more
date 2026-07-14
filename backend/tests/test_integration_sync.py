from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_sync():
    path = REPO_ROOT / "scripts" / "sync_integrations.py"
    spec = importlib.util.spec_from_file_location("sync_integrations", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_writes_controlled_bundle_root_entries_and_hash_manifest(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "GPT fork"
    (target / ".git" / "info").mkdir(parents=True)
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text("user-owned\n", encoding="utf-8")

    result = sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "a" * 40)

    assert result["component"] == "gpt-sovits"
    assert (target / "tts_more" / "tts_more_worker" / "gpt_sovits.py").is_file()
    assert (target / "tts_more" / "tests" / "test_portable_integration.py").is_file()
    assert "tts_more_worker.gpt_sovits:app" in (target / "tts_more" / "component.json").read_text(encoding="utf-8")
    for entry in ("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "Build-Package.ps1", "Start-WebUI.cmd"):
        assert (target / entry).is_file()
    assert exclude.read_text(encoding="utf-8") == "user-owned\n"

    manifest = json.loads((target / "tts_more" / "integration.manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_revision"] == "a" * 40
    assert manifest["integration_version"] == "2.0.0"
    assert manifest["files"]
    assert sync.check_integration(target) == []


def test_check_detects_manual_drift_and_unexpected_controlled_files(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "Index fork"
    sync.sync_integration(REPO_ROOT, target, "indextts", "b" * 40)
    (target / "Start.cmd").write_text("manual edit", encoding="utf-8")
    (target / "tts_more" / "unexpected.txt").write_text("drift", encoding="utf-8")

    errors = sync.check_integration(target)

    assert any("hash mismatch: Start.cmd" in error for error in errors)
    assert any("unexpected controlled file: tts_more/unexpected.txt" in error for error in errors)


def test_check_treats_crlf_and_lf_as_the_same_controlled_text(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "Cosy fork"
    sync.sync_integration(REPO_ROOT, target, "cosyvoice", "d" * 40)
    launcher = target / "Start.cmd"
    launcher.write_bytes(launcher.read_bytes().replace(b"\r\n", b"\n"))

    assert sync.check_integration(target) == []


def test_component_templates_preserve_native_webui_separately(tmp_path: Path) -> None:
    sync = _load_sync()
    expected = {
        "gpt-sovits": "go-webui.bat",
        "indextts": "webui.py",
        "cosyvoice": "webui.py",
    }
    for component, native_entry in expected.items():
        target = tmp_path / component
        sync.sync_integration(REPO_ROOT, target, component, "c" * 40)
        start = (target / "Start.cmd").read_text(encoding="utf-8")
        native = (target / "Start-WebUI.cmd").read_text(encoding="utf-8")
        assert "tts_more\\Start-Worker.ps1" in start
        assert native_entry in native


def test_windows_templates_are_safe_for_cpu_only_hosts_and_optional_lock_fields() -> None:
    initializer = (REPO_ROOT / "integrations" / "windows" / "Initialize.ps1").read_text(encoding="utf-8")
    builder = (REPO_ROOT / "integrations" / "windows" / "Build-Package.ps1").read_text(encoding="utf-8")

    assert "ConvertTo-Json -InputObject $videoControllers" in initializer
    assert "$runtimeLock.PSObject.Properties['payloads']" in initializer
    assert "$config.PSObject.Properties['submodules']" in builder
    assert "create-zip --package-root" in builder
    assert '$env:GITHUB_ACTIONS -eq "true"' in builder
    assert "audit-release --zip" in builder
    assert "device_profiles = @($deviceProfiles)" in builder
    assert "^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$" in builder
    assert "Copy-PortableTree" in builder
    assert "[IO.FileAttributes]::ReparsePoint" in builder
    assert "SoVITS_weights" in builder and "pretrained_models" in builder and "checkpoints" in builder
