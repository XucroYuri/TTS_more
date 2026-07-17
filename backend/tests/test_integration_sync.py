from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE_NAME = "使用说明-先看这里.txt"


def _load_sync():
    path = REPO_ROOT / "scripts" / "sync_integrations.py"
    spec = importlib.util.spec_from_file_location("sync_integrations", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_windows_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run_copied_contract_after_mutation(sync, target: Path) -> subprocess.CompletedProcess[str]:
    manifest_path = target / "tts_more" / "integration.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = {
        relative: sync.sha256_file(target / relative)
        for relative in manifest["files"]
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(target), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(target), "add", "--all"], check=True)
    return subprocess.run(
        [sys.executable, str(target / "tts_more" / "tests" / "test_portable_integration.py"), "-v"],
        capture_output=True,
        text=True,
        check=False,
    )


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
    for relative in (
        "import_portable_data.py",
        "import-portable-data.py",
        "verify-release-asset-set.py",
        "select-portable-folder.ps1",
        "Resolve-PortableBuildPython.ps1",
    ):
        assert (target / "tts_more" / relative).is_file()
    for relative in ("build-tools/pyproject.toml", "build-tools/uv.lock"):
        assert (target / "tts_more" / relative).is_file()
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
    for relative in (
        "tts_more/import_portable_data.py",
        "tts_more/import-portable-data.py",
        "tts_more/verify-release-asset-set.py",
        "tts_more/select-portable-folder.ps1",
        "tts_more/Resolve-PortableBuildPython.ps1",
        "tts_more/build-tools/pyproject.toml",
        "tts_more/build-tools/uv.lock",
        "tts_more/portable-python.ps1",
    ):
        assert manifest["files"][relative] == sync.sha256_file(target / relative)
    assert manifest["files"][GUIDE_NAME] == sync.sha256_file(target / GUIDE_NAME)
    assert sync.check_integration(target) == []


@pytest.mark.parametrize(
    "relative",
    (
        "tts_more/import_portable_data.py",
        "tts_more/import-portable-data.py",
        "tts_more/verify-release-asset-set.py",
        "tts_more/select-portable-folder.ps1",
        "tts_more/Resolve-PortableBuildPython.ps1",
        "tts_more/build-tools/pyproject.toml",
        "tts_more/build-tools/uv.lock",
    ),
)
def test_check_rejects_missing_or_drifted_controlled_import_tools(
    tmp_path: Path, relative: str
) -> None:
    sync = _load_sync()
    target = tmp_path / relative.replace("/", "-")
    sync.sync_integration(REPO_ROOT, target, "cosyvoice", "d" * 40)
    controlled = target / relative
    controlled.write_text("drift", encoding="utf-8")
    assert f"hash mismatch: {relative}" in sync.check_integration(target)
    sync.sync_integration(REPO_ROOT, target, "cosyvoice", "d" * 40)
    controlled.unlink()
    assert f"missing controlled file: {relative}" in sync.check_integration(target)


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
        assert "旧版便携包" in guide and "不会自动扫描" in guide
        assert "原包保持不变" in guide and "启动服务之前" in guide
        assert "包内 CPython" in guide and "锁定 uv" in guide
        assert "data/cache/portable/conda" not in guide
        assert "runtime/live、models、data/user" in guide


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


def test_check_detects_manual_drift_but_allows_target_owned_extras(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "Index fork"
    sync.sync_integration(REPO_ROOT, target, "indextts", "b" * 40)
    (target / "Start.cmd").write_text("manual edit", encoding="utf-8")
    (target / "tts_more" / "unexpected.txt").write_text("drift", encoding="utf-8")

    errors = sync.check_integration(target)

    assert any("hash mismatch: Start.cmd" in error for error in errors)
    assert not any("unexpected controlled file" in error for error in errors)


def test_sync_preserves_nested_target_owned_assets_byte_for_byte(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "asset fork"
    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "1" * 40)
    sentinels = {
        "tts_more/__pycache__/worker.cpython-311.pyc": b"\x00pyc\xff",
        "tts_more/models/custom/model.bin": b"model-bytes\x00\xff",
        "tts_more/data/user/settings.json": b'{"user":true}\n',
        "tts_more/data/cache/download.partial": b"partial-cache",
    }
    for relative, payload in sentinels.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "2" * 40)

    assert {relative: (target / relative).read_bytes() for relative in sentinels} == sentinels
    assert sync.check_integration(target) == []


def test_sync_fails_before_mutation_when_unknown_file_collides(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "collision fork"
    collision = target / "tts_more" / "component.json"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"target-owned")
    sentinel = target / "tts_more" / "models" / "keep.bin"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"keep")

    with pytest.raises(FileExistsError, match="target-owned file collides"):
        sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "3" * 40)

    assert collision.read_bytes() == b"target-owned"
    assert sentinel.read_bytes() == b"keep"
    assert not (target / "Start.cmd").exists()
    assert not (target / "tts_more" / "integration.manifest.json").exists()


def test_sync_removes_files_owned_only_by_previous_manifest(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "removed file fork"
    sync.sync_integration(REPO_ROOT, target, "indextts", "4" * 40)
    obsolete = target / "tts_more" / "obsolete" / "old.txt"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_bytes(b"old controlled bytes")
    manifest_path = target / "tts_more" / "integration.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["tts_more/obsolete/old.txt"] = sync.sha256_file(obsolete)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    sync.sync_integration(REPO_ROOT, target, "indextts", "5" * 40)

    assert not obsolete.exists()
    assert not obsolete.parent.exists()


def test_sync_rolls_back_controlled_files_and_manifest_after_publication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = _load_sync()
    target = tmp_path / "rollback fork"
    sync.sync_integration(REPO_ROOT, target, "cosyvoice", "6" * 40)
    start = target / "Start.cmd"
    manifest_path = target / "tts_more" / "integration.manifest.json"
    old_start = start.read_bytes()
    old_manifest = manifest_path.read_bytes()
    unknown = target / "tts_more" / "models" / "keep.bin"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"unknown")
    original_payloads = sync._root_entry_payloads
    original_publish = sync._publish_file

    def changed_payloads(component: str) -> dict[str, str]:
        payloads = original_payloads(component)
        payloads["Start.cmd"] += "rem changed\n"
        return payloads

    def fail_manifest_publish(source: Path, destination: Path, target_root: Path) -> None:
        if destination == manifest_path:
            raise OSError("injected manifest publication failure")
        original_publish(source, destination, target_root)

    monkeypatch.setattr(sync, "_root_entry_payloads", changed_payloads)
    monkeypatch.setattr(sync, "_publish_file", fail_manifest_publish)

    with pytest.raises(OSError, match="injected manifest publication failure"):
        sync.sync_integration(REPO_ROOT, target, "cosyvoice", "7" * 40)

    assert start.read_bytes() == old_start
    assert manifest_path.read_bytes() == old_manifest
    assert unknown.read_bytes() == b"unknown"
    assert sync.check_integration(target) == []


def test_sync_rolls_back_path_when_publisher_fails_after_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = _load_sync()
    target = tmp_path / "post replace failure fork"
    unknown = target / "tts_more" / "models" / "keep.bin"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"unknown")
    original_publish = sync._publish_file
    failed = False

    def fail_after_first_replace(source: Path, destination: Path, target_root: Path) -> None:
        nonlocal failed
        original_publish(source, destination, target_root)
        if not failed:
            failed = True
            raise OSError("injected post-replace failure")

    monkeypatch.setattr(sync, "_publish_file", fail_after_first_replace)

    with pytest.raises(OSError, match="injected post-replace failure"):
        sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "8" * 40)

    assert unknown.read_bytes() == b"unknown"
    assert not (target / "Build-Package.ps1").exists()
    assert not (target / "tts_more" / "integration.manifest.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_sync_and_check_reject_tts_more_junction_without_writing_outside(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "junction fork"
    target.mkdir()
    root_entry = target / "Start.cmd"
    root_entry.write_bytes(b"target-root-entry")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel.bin"
    outside_sentinel.write_bytes(b"outside")
    link = target / "tts_more"
    _make_windows_junction(link, outside)
    try:
        with pytest.raises(ValueError, match="reparse"):
            sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "9" * 40)
        errors = sync.check_integration(target)
        assert any("reparse" in error for error in errors)
        assert root_entry.read_bytes() == b"target-root-entry"
        assert outside_sentinel.read_bytes() == b"outside"
        assert sorted(path.name for path in outside.iterdir()) == ["sentinel.bin"]
    finally:
        os.rmdir(link)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_sync_and_check_reject_controlled_ancestor_junction(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "ancestor junction fork"
    sync.sync_integration(REPO_ROOT, target, "indextts", "a" * 40)
    root_entry = target / "Start.cmd"
    root_bytes = root_entry.read_bytes()
    app = target / "tts_more" / "app"
    shutil.rmtree(app)
    outside = tmp_path / "outside app"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"keep")
    _make_windows_junction(app, outside)
    try:
        with pytest.raises(ValueError, match="reparse"):
            sync.sync_integration(REPO_ROOT, target, "indextts", "b" * 40)
        assert any("reparse" in error for error in sync.check_integration(target))
        assert root_entry.read_bytes() == root_bytes
        assert sentinel.read_bytes() == b"keep"
    finally:
        os.rmdir(app)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_sync_and_check_reject_broken_junction(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "broken junction fork"
    target.mkdir()
    outside = tmp_path / "removed target"
    outside.mkdir()
    link = target / "tts_more"
    _make_windows_junction(link, outside)
    outside.rmdir()
    try:
        assert os.path.lexists(link) and not link.exists()
        with pytest.raises(ValueError, match="reparse"):
            sync.sync_integration(REPO_ROOT, target, "cosyvoice", "c" * 40)
        assert any("reparse" in error for error in sync.check_integration(target))
    finally:
        os.rmdir(link)


@pytest.mark.parametrize(
    "bad_relative",
    (
        "tts_more//evil.py",
        "tts_more/./evil.py",
        "tts_more/../evil.py",
        "tts_more\\evil.py",
        "/absolute.py",
        "tts_more/file:ads",
        "tts_more/trailing.",
        "tts_more/trailing ",
        "tts_more/CON",
        "tts_more/dir/NUL.txt",
    ),
)
def test_sync_and_check_reject_noncanonical_previous_manifest_paths(
    tmp_path: Path, bad_relative: str
) -> None:
    sync = _load_sync()
    target = tmp_path / "bad manifest fork"
    manifest_path = target / "tts_more" / "integration.manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "files": {bad_relative: "0" * 64}}),
        encoding="utf-8",
    )
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"unchanged")

    with pytest.raises(ValueError, match="invalid controlled path"):
        sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "d" * 40)

    assert any("invalid controlled path" in error for error in sync.check_integration(target))
    assert sentinel.read_bytes() == b"unchanged"


def test_sync_and_check_reject_casefold_aliases_in_previous_manifest(tmp_path: Path) -> None:
    sync = _load_sync()
    target = tmp_path / "case alias fork"
    manifest_path = target / "tts_more" / "integration.manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": {
                    "tts_more/A.py": "0" * 64,
                    "tts_more/a.py": "1" * 64,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="case-insensitive alias"):
        sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "e" * 40)

    assert any("case-insensitive alias" in error for error in sync.check_integration(target))


@pytest.mark.parametrize(
    "desired_files",
    (
        {"tts_more//bad.py": "0" * 64},
        {"tts_more/A.py": "0" * 64, "tts_more/a.py": "1" * 64},
    ),
)
def test_sync_rejects_noncanonical_or_aliased_desired_paths_before_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desired_files: dict[str, str]
) -> None:
    sync = _load_sync()
    target = tmp_path / "bad desired fork"
    sentinel = target / "sentinel.bin"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"unchanged")

    def bad_stage(*args: object, **kwargs: object) -> dict[str, object]:
        return {"files": desired_files}

    monkeypatch.setattr(sync, "_build_staged_integration", bad_stage)

    with pytest.raises(ValueError):
        sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "f" * 40)

    assert sentinel.read_bytes() == b"unchanged"


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
        native_controller = (target / "tts_more" / "Start-WebUI.ps1").read_text(
            encoding="utf-8"
        )
        assert "tts_more\\Invoke-PortableStart.ps1" in start
        assert (target / "tts_more" / "Start-Worker.ps1").is_file()
        assert "tts_more\\Start-WebUI.ps1" in native
        assert native_entry in native_controller


@pytest.mark.parametrize(
    ("relative", "active", "replacement"),
    (
        (
            "Start.cmd",
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Noop.ps1" %*\n'
            'rem powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data.operations) -Label "data.operations"',
            '$operationsRoot = Join-Path $resolvedRoot "data\\local\\operations"\n'
            '                # $operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data.operations) -Label "data.operations"',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }',
            '$serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Noop.ps1" }\n'
            '    # $serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$result = Invoke-ChildPowerShell -Script $context.ServiceScript -Arguments $arguments',
            '$result = Invoke-ChildPowerShell -Script $context.InitializeScript -Arguments $arguments\n'
            '    # $result = Invoke-ChildPowerShell -Script $context.ServiceScript -Arguments $arguments',
        ),
        (
            "tts_more/Start-Worker.ps1",
            '$Python = Join-Path $Root "runtime\\live\\python.exe"',
            '$Python = "python.exe"\n# $Python = Join-Path $Root "runtime\\live\\python.exe"',
        ),
        (
            "tts_more/Start-Worker.ps1",
            '$process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $SourceRoot -WindowStyle Hidden -PassThru',
            '$process = Start-Process -FilePath "python.exe" -ArgumentList $arguments -WorkingDirectory $SourceRoot -WindowStyle Hidden -PassThru\n'
            '# $process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $SourceRoot -WindowStyle Hidden -PassThru',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }',
            '$serviceScript = if ($component -ne "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }\n'
            '    # $serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'OperationsRoot = [IO.Path]::GetFullPath($operationsRoot)',
            'OperationsRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot "data\\local\\operations"))\n'
            '        # OperationsRoot = [IO.Path]::GetFullPath($operationsRoot)',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$lockPath = Join-Path $context.OperationsRoot ".start.lock"',
            '$lockPath = Join-Path $Root ".start.lock"\n'
            '    # $lockPath = Join-Path $context.OperationsRoot ".start.lock"',
        ),
        (
            "tts_more/Start-Worker.ps1",
            '$Python = Join-Path $Root "runtime\\live\\python.exe"',
            'if ($false) { $Python = Join-Path $Root "runtime\\live\\python.exe" }',
        ),
        (
            "tts_more/Start-Worker.ps1",
            '$process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $SourceRoot -WindowStyle Hidden -PassThru',
            'if ($false) { $process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $SourceRoot -WindowStyle Hidden -PassThru }',
        ),
        (
            "Start.cmd",
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
            'exit /b 0\n'
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
        ),
        (
            "Start.cmd",
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
            'goto :end\n'
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*\n'
            ':end',
        ),
        (
            "Start.cmd",
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
            'cmd.exe /c ver\n'
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data.operations) -Label "data.operations"',
            'if ($false) { $operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data.operations) -Label "data.operations" }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$result = Invoke-ChildPowerShell -Script $context.ServiceScript -Arguments $arguments',
            'if ($false) { $result = Invoke-ChildPowerShell -Script $context.ServiceScript -Arguments $arguments }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'Invoke-ServiceStart -Root $root -Operation $operation -PortOverride $PortOverride',
            'if ($false) { Invoke-ServiceStart -Root $root -Operation $operation -PortOverride $PortOverride }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$operationRoot = [IO.Path]::GetFullPath((Join-Path $context.OperationsRoot $canonicalId))',
            '$operationRoot = [IO.Path]::GetFullPath((Join-Path $Root $canonicalId))\n'
            '    # $operationRoot = [IO.Path]::GetFullPath((Join-Path $context.OperationsRoot $canonicalId))',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'Test-PathWithinRoot -Root $context.OperationsRoot -Path $operationRoot',
            'Test-PathWithinRoot -Root $Root -Path $operationRoot',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '[IO.Path]::GetFullPath($context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase',
            '[IO.Path]::GetFullPath($Root), [StringComparison]::OrdinalIgnoreCase',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'Assert-PortableExactOperationContract -OperationsRoot $context.OperationsRoot -OperationRoot $operationRoot',
            'Assert-PortableExactOperationContract -OperationsRoot $Root -OperationRoot $operationRoot',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$activePath = Join-Path $script:Context.OperationsRoot "active-start.json"',
            '$activePath = Join-Path $root "active-start.json"\n'
            '    # $activePath = Join-Path $script:Context.OperationsRoot "active-start.json"',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$operationRoot = [IO.Path]::GetFullPath((Join-Path $context.OperationsRoot $canonicalId))',
            '$operationRoot = if ($false) { [IO.Path]::GetFullPath((Join-Path $context.OperationsRoot $canonicalId)) } '
            'else { [IO.Path]::GetFullPath((Join-Path $Root $canonicalId)) }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'Test-PathWithinRoot -Root $context.OperationsRoot -Path $operationRoot',
            '$false -and (Test-PathWithinRoot -Root $context.OperationsRoot -Path $operationRoot)',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '[string]::Equals((Split-Path -Parent $operationRoot), [IO.Path]::GetFullPath($context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase)',
            '$false -and [string]::Equals((Split-Path -Parent $operationRoot), [IO.Path]::GetFullPath($context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase)',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'OperationsRoot = [IO.Path]::GetFullPath($operationsRoot)',
            'OperationsRoot = if ($false) { [IO.Path]::GetFullPath($operationsRoot) } '
            'else { [IO.Path]::GetFullPath((Join-Path $resolvedRoot "data\\local\\operations")) }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }',
            '$serviceScript = if ($component -eq "tts-more") { '
            'if ($false) { Join-Path $resolvedRoot "scripts\\start-production.ps1" } '
            'else { Join-Path $resolvedRoot "scripts\\Noop.ps1" } '
            '} else { Join-Path $bundle "Start-Worker.ps1" }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }',
            '$serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\\start-production.ps1" } '
            'else { if ($false) { Join-Path $bundle "Start-Worker.ps1" } '
            'else { Join-Path $bundle "Noop.ps1" } }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$activePath = Join-Path $Context.OperationsRoot "active-start.json"',
            '$activePath = Join-Path $Root "active-start.json"',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$pointer = Join-Path $Context.OperationsRoot "active-start.json"',
            '$pointer = Join-Path $Root "active-start.json"',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$operation = [IO.Path]::GetFullPath((Join-Path $Context.OperationsRoot $parsed.ToString()))',
            '$operation = [IO.Path]::GetFullPath((Join-Path $Root $parsed.ToString()))',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '[string]::Equals((Split-Path -Parent $operation), [IO.Path]::GetFullPath($Context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase)',
            '[string]::Equals((Split-Path -Parent $operation), [IO.Path]::GetFullPath($Root), [StringComparison]::OrdinalIgnoreCase)',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$staleOperation = Join-Path $Context.OperationsRoot $parsed.ToString()',
            '$staleOperation = Join-Path $Root $parsed.ToString()',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '[void](Assert-PortableExactOperationContract -OperationsRoot $Context.OperationsRoot -OperationRoot $staleOperation -CancelFile (Join-Path $staleOperation "cancel.requested") -RequireOperation)',
            '[void](Assert-PortableExactOperationContract -OperationsRoot $Root -OperationRoot $staleOperation '
            '-CancelFile (Join-Path $staleOperation "cancel.requested") -RequireOperation)',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'ServiceScript = $serviceScript',
            'ServiceScript = $initializeScript',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'if ($profile -notin @("bootstrap", "full")) { Throw-PortableStartError "PACKAGE_CORRUPT" "The package profile is invalid" }\n'
            '        if ([int]$manifest.schema_version -eq 2) {\n'
            '            try {\n'
            '                $requiredText = @("component", "package_id", "release_version", "version", "build_id", "api_contract")',
            'if ($profile -notin @("bootstrap", "full")) { Throw-PortableStartError "PACKAGE_CORRUPT" "The package profile is invalid" }\n'
            '        if ($false -and ([int]$manifest.schema_version -eq 2)) {\n'
            '            try {\n'
            '                $requiredText = @("component", "package_id", "release_version", "version", "build_id", "api_contract")',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data.operations) -Label "data.operations"',
            '$operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot '
            '-RelativePath ([string]$(if ($false) { $manifest.data.operations } '
            'else { Join-Path "data\\local" "operations" })) -Label "data.operations"',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            '$lockPath = Join-Path $context.OperationsRoot ".start.lock"',
            '$lockPath = if ($false) { Join-Path $context.OperationsRoot ".start.lock" } '
            'else { Join-Path $Root ".start.lock" }',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'Throw-PortableStartError "PACKAGE_CORRUPT" "OperationRoot must be a UUID direct child of data.operations"',
            '$null = "boundary check disabled"',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'Throw-PortableStartError "OPERATION_ACTIVE" "The active operation is outside data.operations"',
            '$null = "parent check disabled"',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'throw [PortableStartException]::new($Code, $Message)',
            '$null = [PortableStartException]::new($Code, $Message)',
        ),
        (
            "tts_more/Invoke-PortableStart.ps1",
            'throw [PortableStartException]::new($Code, $Message)',
            'begin { exit 0 }\n'
            '    end { throw [PortableStartException]::new($Code, $Message) }',
        ),
    ),
)
def test_copied_contract_rejects_commented_decoys_and_mutated_active_control_flow(
    tmp_path: Path,
    relative: str,
    active: str,
    replacement: str,
) -> None:
    if os.name != "nt" and shutil.which("pwsh") is None:
        pytest.skip("source mutation harness requires PowerShell AST support")
    sync = _load_sync()
    target = tmp_path / "mutated fork"
    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "9" * 40)
    path = target / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(active) == 1
    path.write_text(source.replace(active, replacement), encoding="utf-8")

    result = _run_copied_contract_after_mutation(sync, target)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"active mutation escaped the copied contract: {relative}\n{combined}"
    )
    hash_lines = [
        line
        for line in combined.splitlines()
        if line.startswith("test_controlled_mirror_has_no_hash_drift ")
    ]
    assert len(hash_lines) == 1 and hash_lines[0].endswith("... ok"), combined
    semantic_test = (
        "test_package_entrypoints_and_native_webui_are_separate"
        if relative == "Start.cmd"
        else "test_controller_uses_manifest_operations_worker_delegate_and_private_runtime"
    )
    semantic_lines = [
        line
        for line in combined.splitlines()
        if line.startswith(f"{semantic_test} ")
    ]
    assert len(semantic_lines) == 1 and semantic_lines[0].endswith("... FAIL"), combined


def test_copied_contract_rejects_commented_decoys_in_nested_context_return(tmp_path: Path) -> None:
    if os.name != "nt" and shutil.which("pwsh") is None:
        pytest.skip("source mutation harness requires PowerShell AST support")
    sync = _load_sync()
    target = tmp_path / "nested return decoy fork"
    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "6" * 40)
    controller = target / "tts_more" / "Invoke-PortableStart.ps1"
    source = controller.read_text(encoding="utf-8")
    operations_field = "OperationsRoot = [IO.Path]::GetFullPath($operationsRoot)"
    return_marker = "    return [pscustomobject]@{\n"
    context_start = source.index("function Get-PackageContext")
    context_end = source.index("function Resolve-PortableStartRoot")
    context = source[context_start:context_end]
    assert context.count(operations_field) == 1
    assert context.count(return_marker) == 1
    context = context.replace(
        operations_field,
        "OperationsRoot = [IO.Path]::GetFullPath($resolvedRoot)",
        1,
    )
    context = context.replace(
        return_marker,
        "    if ($false) { return [pscustomobject]@{ "
        "OperationsRoot = [IO.Path]::GetFullPath($operationsRoot) } }\n"
        + return_marker,
        1,
    )
    controller.write_text(source[:context_start] + context + source[context_end:], encoding="utf-8")

    result = _run_copied_contract_after_mutation(sync, target)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"nested return decoy escaped contract\n{combined}"
    assert any(
        line.startswith("test_controlled_mirror_has_no_hash_drift ") and line.endswith("... ok")
        for line in combined.splitlines()
    ), combined
    assert any(
        line.startswith("test_controller_uses_manifest_operations_worker_delegate_and_private_runtime ")
        and line.endswith("... FAIL")
        for line in combined.splitlines()
    ), combined


def test_copied_contract_requires_operations_assignment_in_schema_v2_branch(tmp_path: Path) -> None:
    if os.name != "nt" and shutil.which("pwsh") is None:
        pytest.skip("source mutation harness requires PowerShell AST support")
    sync = _load_sync()
    target = tmp_path / "wrong schema branch fork"
    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "8" * 40)
    controller = target / "tts_more" / "Invoke-PortableStart.ps1"
    source = controller.read_text(encoding="utf-8")
    correct = (
        '$operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot '
        '-RelativePath ([string]$manifest.data.operations) -Label "data.operations"'
    )
    fallback = '$operationsRoot = Join-Path $resolvedRoot "data\\local\\operations"'
    schema_else = "        } else {\n            " + fallback
    assert source.count(correct) == 1
    assert source.count(schema_else) == 1
    source = source.replace(correct, fallback)
    source = source.replace(schema_else, "        } else {\n            " + correct)
    controller.write_text(source, encoding="utf-8")

    result = _run_copied_contract_after_mutation(sync, target)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"schema-v2 branch mutation escaped the copied contract\n{combined}"
    assert any(
        line.startswith("test_controlled_mirror_has_no_hash_drift ") and line.endswith("... ok")
        for line in combined.splitlines()
    ), combined
    assert any(
        line.startswith("test_controller_uses_manifest_operations_worker_delegate_and_private_runtime ")
        and line.endswith("... FAIL")
        for line in combined.splitlines()
    ), combined


def test_copied_contract_rejects_hardcoded_initialize_operation_consumers(tmp_path: Path) -> None:
    if os.name != "nt" and shutil.which("pwsh") is None:
        pytest.skip("source mutation harness requires PowerShell AST support")
    sync = _load_sync()
    target = tmp_path / "hardcoded operation consumers fork"
    sync.sync_integration(REPO_ROOT, target, "gpt-sovits", "7" * 40)
    controller = target / "tts_more" / "Invoke-PortableStart.ps1"
    source = controller.read_text(encoding="utf-8")
    start = source.index("function Initialize-Operation")
    end = source.index("function Add-OperationEvent")
    block = source[start:end]
    assert block.count("$context.OperationsRoot") == 4
    block = block.replace(
        "$context.OperationsRoot",
        '(Join-Path $Root "data\\local\\operations")',
    )
    controller.write_text(source[:start] + block + source[end:], encoding="utf-8")

    result = _run_copied_contract_after_mutation(sync, target)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"hardcoded operation consumers escaped contract\n{combined}"
    assert any(
        line.startswith("test_controlled_mirror_has_no_hash_drift ") and line.endswith("... ok")
        for line in combined.splitlines()
    ), combined
    assert any(
        line.startswith("test_controller_uses_manifest_operations_worker_delegate_and_private_runtime ")
        and line.endswith("... FAIL")
        for line in combined.splitlines()
    ), combined


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


def test_worker_builder_uses_bounded_external_unique_staging_and_fails_closed() -> None:
    builder = (REPO_ROOT / "integrations" / "windows" / "Build-Package.ps1").read_text(
        encoding="utf-8"
    )
    root_wrapper = _load_sync()._root_entry_payloads("gpt-sovits")["Build-Package.ps1"]

    assert '[string]$WorkRoot = ""' in builder
    assert "[IO.Path]::GetTempPath()" in builder
    assert "[Guid]::NewGuid()" in builder
    assert "WorkRoot must be outside source checkout" in builder
    assert "[StringComparison]::OrdinalIgnoreCase" in builder
    assert "[IO.Path]::DirectorySeparatorChar" in builder
    assert builder.index("WorkRoot must be outside source checkout") < builder.index(
        "$workIdentity"
    )
    assert "WorkRoot path must not traverse a reparse point" in builder
    assert "WorkRoot path contains an existing non-directory segment" in builder
    assert "Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop" in builder
    assert builder.index("WorkRoot path must not traverse a reparse point") < builder.index(
        "$workIdentity"
    )
    stage_creation = builder.index("New-Item -ItemType Directory -Force -Path $stage")
    post_create_check = builder.index(
        "[void](Assert-PortableWorkPath -CandidatePath $stage)", stage_creation
    )
    source_copy = builder.index(
        "foreach ($entry in Get-ChildItem -LiteralPath $Root -Force", post_create_check
    )
    assert stage_creation < post_create_check < source_copy
    assert "$workPathExists = Assert-PortableWorkPath -CandidatePath $work" in builder
    assert "worker package staging path budget exceeded before copy" in builder
    assert builder.index("Assert-PortableTreePathBudget") < builder.index(
        "Copy-PortableTree -Source $entry.FullName"
    )
    assert "finally {" in builder
    assert "try {" in root_wrapper
    assert "catch {" in root_wrapper
    assert "exit $LASTEXITCODE" not in root_wrapper
