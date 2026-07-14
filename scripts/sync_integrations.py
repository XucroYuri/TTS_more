from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


INTEGRATION_VERSION = "2.0.0"
COMPONENTS = {
    "gpt-sovits": {"module": "tts_more_worker.gpt_sovits:app", "port": 9880, "python": "3.11"},
    "indextts": {"module": "tts_more_worker.indextts:app", "port": 9881, "python": "3.11"},
    "cosyvoice": {"module": "tts_more_worker.cosyvoice:app", "port": 9882, "python": "3.10"},
}
ROOT_ENTRIES = ("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "Build-Package.ps1", "Start-WebUI.cmd")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_integration(source_root: Path, target_root: Path, component: str, source_revision: str) -> dict[str, object]:
    if component not in COMPONENTS:
        raise ValueError(f"unsupported integration component: {component}")
    source_root = source_root.resolve(strict=True)
    target_root.mkdir(parents=True, exist_ok=True)
    controlled = target_root / "tts_more"
    if controlled.exists():
        shutil.rmtree(controlled)
    controlled.mkdir(parents=True)

    _copy_tree(source_root / "integrations" / "tts_more_worker", controlled / "tts_more_worker")
    _copy_tree(source_root / "integrations" / "contract_tests", controlled / "tests")
    _copy_tree(source_root / "backend" / "app" / "workers", controlled / "app" / "workers")
    _copy_file(source_root / "backend" / "app" / "models.py", controlled / "app" / "models.py")
    _copy_file(source_root / "backend" / "app" / "subprocess_safety.py", controlled / "app" / "subprocess_safety.py")
    _copy_file(source_root / "backend" / "app" / "__init__.py", controlled / "app" / "__init__.py")
    _copy_file(source_root / "backend" / "app" / "adapters" / "base.py", controlled / "app" / "adapters" / "base.py")
    _copy_file(source_root / "backend" / "app" / "adapters" / "__init__.py", controlled / "app" / "adapters" / "__init__.py")
    for name in ("portable_install.py", "portable_launcher.py", "portable_packages.py"):
        _copy_file(source_root / "scripts" / name, controlled / name)
    _copy_file(source_root / "scripts" / "bootstrap-conda.ps1", controlled / "bootstrap-conda.ps1")
    for name in ("Initialize.ps1", "Start-Worker.ps1", "Stop-Worker.ps1", "Repair.ps1", "Build-Package.ps1"):
        _copy_file(source_root / "integrations" / "windows" / name, controlled / name)
    _copy_file(source_root / "packaging" / "portable" / "toolchain.lock.json", controlled / "locks" / "toolchain.lock.json")
    _copy_file(source_root / "packaging" / "portable" / "tts-more-package.schema.json", controlled / "tts-more-package.schema.json")
    _copy_file(source_root / "LICENSE", controlled / "LICENSE.integration")
    _copy_file(source_root / "NOTICE", controlled / "NOTICE.integration")
    _copy_tree(source_root / "integrations" / "components" / component, controlled / "locks")

    component_source = json.loads(
        (source_root / "integrations" / "components" / component / "component-source.json").read_text(encoding="utf-8")
    )
    config = {"schema_version": 1, "component": component, **COMPONENTS[component], **component_source}
    (controlled / "component.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    root_payloads = _root_entry_payloads(component)
    for name, content in root_payloads.items():
        (target_root / name).write_text(content, encoding="utf-8", newline="\r\n" if name.endswith(".cmd") else "\n")

    files = {}
    for path in _tracked_paths(target_root):
        relative = path.relative_to(target_root).as_posix()
        files[relative] = sha256_file(path)
    manifest = {
        "schema_version": 1,
        "component": component,
        "integration_version": INTEGRATION_VERSION,
        "source_revision": source_revision,
        "files": dict(sorted(files.items())),
    }
    (controlled / "integration.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def check_integration(target_root: Path) -> list[str]:
    target_root = target_root.resolve(strict=True)
    manifest_path = target_root / "tts_more" / "integration.manifest.json"
    if not manifest_path.is_file():
        return ["integration manifest is missing"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {str(path): str(digest) for path, digest in manifest.get("files", {}).items()}
    errors: list[str] = []
    for relative, digest in expected.items():
        path = target_root / relative
        if not path.is_file():
            errors.append(f"missing controlled file: {relative}")
        elif sha256_file(path) != digest:
            errors.append(f"hash mismatch: {relative}")
    expected_controlled = {name for name in expected if name.startswith("tts_more/")}
    for path in (target_root / "tts_more").rglob("*"):
        if not path.is_file() or path == manifest_path or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(target_root).as_posix()
        if relative not in expected_controlled:
            errors.append(f"unexpected controlled file: {relative}")
    return errors


def _root_entry_payloads(component: str) -> dict[str, str]:
    webui = {
        "gpt-sovits": '@echo off\ncall "%~dp0go-webui.bat" %*\nexit /b %errorlevel%\n',
        "indextts": '@echo off\nsetlocal\nset "PYTHON=%~dp0.venv\\Scripts\\python.exe"\nif not exist "%PYTHON%" set "PYTHON=%~dp0runtime\\live\\python.exe"\n"%PYTHON%" "%~dp0webui.py" %*\nexit /b %errorlevel%\n',
        "cosyvoice": '@echo off\nsetlocal\nset "PYTHON=%~dp0.venv\\Scripts\\python.exe"\nif not exist "%PYTHON%" set "PYTHON=%~dp0runtime\\live\\python.exe"\n"%PYTHON%" "%~dp0webui.py" %*\nexit /b %errorlevel%\n',
    }[component]
    return {
        "Initialize.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Initialize.ps1" %*\nexit /b %errorlevel%\n',
        "Start.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Start-Worker.ps1" %*\nexit /b %errorlevel%\n',
        "Stop.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Stop-Worker.ps1" %*\nexit /b %errorlevel%\n',
        "Repair.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Repair.ps1" %*\nexit /b %errorlevel%\n',
        "Build-Package.ps1": '& "$PSScriptRoot\\tts_more\\Build-Package.ps1" @args\nexit $LASTEXITCODE\n',
        "Start-WebUI.cmd": webui,
    }


def _tracked_paths(target_root: Path) -> Iterable[Path]:
    for name in ROOT_ENTRIES:
        yield target_root / name
    yield from sorted(path for path in (target_root / "tts_more").rglob("*") if path.is_file())


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"integration source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"integration source directory is missing: {source}")
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize or verify controlled TTS More fork integrations")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--component", choices=sorted(COMPONENTS))
    parser.add_argument("--source-revision")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        errors = check_integration(args.target)
        for error in errors:
            print(error)
        return 1 if errors else 0
    if not args.component:
        parser.error("--component is required unless --check is used")
    revision = args.source_revision or subprocess.check_output(
        ["git", "-C", str(args.source_root), "rev-parse", "HEAD"], text=True
    ).strip()
    manifest = sync_integration(args.source_root, args.target, args.component, revision)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
