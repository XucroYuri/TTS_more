from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE_NAME = "使用说明-先看这里.txt"


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
    for entry in (
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "Build-Package.ps1",
        "Start-WebUI.cmd",
        GUIDE_NAME,
    ):
        assert (target / entry).is_file()
    assert exclude.read_text(encoding="utf-8") == "user-owned\n"

    manifest = json.loads((target / "tts_more" / "integration.manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_revision"] == "a" * 40
    assert manifest["integration_version"] == "2.0.0"
    assert manifest["files"]
    validation_relative = "tts_more/Portable-Validation.ps1"
    assert manifest["files"][validation_relative] == sync.sha256_file(target / validation_relative)
    assert manifest["files"][GUIDE_NAME] == sync.sha256_file(target / GUIDE_NAME)
    assert sync.check_integration(target) == []


def test_generated_guide_is_component_aware_and_package_relative(tmp_path: Path) -> None:
    sync = _load_sync()
    expected = {
        "gpt-sovits": ("GPT-SoVITS", "9880"),
        "indextts": ("IndexTTS", "9881"),
        "cosyvoice": ("CosyVoice", "9882"),
    }

    for component, (display_name, port) in expected.items():
        target = tmp_path / component
        sync.sync_integration(REPO_ROOT, target, component, "e" * 40)
        guide = (target / GUIDE_NAME).read_text(encoding="utf-8")

        assert display_name in guide
        assert f"默认端口：{port}" in guide
        assert "Start.cmd：启动 tts-more-v1 worker" in guide
        assert "Start-WebUI.cmd：启动上游原生 WebUI" in guide
        assert "Initialize.cmd" in guide and "Stop.cmd" in guide and "Repair.cmd" in guide
        assert "Bootstrap" in guide and "首次运行需要联网" in guide and "之后可离线运行" in guide
        assert "Full" in guide and "断网直接运行" in guide and "禁止上传 GitHub" in guide
        assert "无需安装系统 Python、Conda 或 Node" in guide
        assert "路径可能因电脑而异" in guide and "包内相对路径" in guide


def test_check_detects_generated_guide_drift_and_removal(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "Guide drift fork"
    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "f" * 40)
    guide = target / GUIDE_NAME

    guide.write_text("manual edit", encoding="utf-8")
    assert f"hash mismatch: {GUIDE_NAME}" in sync.check_integration(target)

    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "f" * 40)
    guide.unlink()
    assert f"missing controlled file: {GUIDE_NAME}" in sync.check_integration(target)


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
        assert "tts_more\\Invoke-PortableStart.ps1" in start
        assert (target / "tts_more" / "Start-Worker.ps1").is_file()
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
