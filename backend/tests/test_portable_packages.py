from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


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


def _load_portable_package_runner():
    module_path = REPO_ROOT / "scripts" / "portable_package_runner.py"
    assert module_path.is_file(), "portable package runner script is missing"
    spec = importlib.util.spec_from_file_location("portable_package_runner_for_layout", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_release_asset_gate():
    module_path = REPO_ROOT / "scripts" / "verify-release-asset-set.py"
    assert module_path.is_file(), "executable release asset gate is missing"
    spec = importlib.util.spec_from_file_location("verify_release_asset_set", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sync_integrations():
    module_path = REPO_ROOT / "scripts" / "sync_integrations.py"
    spec = importlib.util.spec_from_file_location("sync_integrations_for_portable_schema", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _official_manifest_validator() -> Draft202012Validator:
    schema = json.loads(
        (REPO_ROOT / "packaging" / "portable" / "tts-more-package.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _run_checked(command: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {' '.join(command)}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _initialize_git_repository(root: Path) -> None:
    _run_checked(["git", "init", "--quiet"], root)
    _run_checked(["git", "config", "user.name", "Portable Schema Test"], root)
    _run_checked(["git", "config", "user.email", "portable-schema-test@example.invalid"], root)
    _run_checked(["git", "add", "."], root)
    _run_checked(["git", "commit", "--quiet", "-m", "portable schema fixture"], root)


def test_no_dev_runtime_can_import_packaged_portable_cli_dependencies() -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("isolated no-dev runtime smoke requires uv")
    cli = REPO_ROOT / "scripts" / "import-portable-data.py"
    command = (
        "import jsonschema, runpy, sys; "
        f"sys.path.insert(0, {str(cli.parent)!r}); "
        f"sys.argv = [{str(cli)!r}, '--help']; "
        f"runpy.run_path({str(cli)!r}, run_name='__main__')"
    )
    completed = subprocess.run(
        [
            uv,
            "run",
            "--isolated",
            "--frozen",
            "--no-dev",
            "--project",
            str(REPO_ROOT / "backend"),
            "python",
            "-c",
            command,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_controller_guide_explains_explicit_prestart_previous_version_import() -> None:
    guide = (REPO_ROOT / "packaging" / "portable" / "使用说明-先看这里.txt").read_text(
        encoding="utf-8"
    )

    assert "旧版便携包" in guide and "不会自动扫描" in guide
    assert "原包保持不变" in guide and "启动服务之前" in guide
    assert "data/cache/portable/conda" in guide
    assert "runtime/live、models、data/user" in guide


def _copy_controller_builder_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "Build-Package.ps1", root / "Build-Package.ps1")
    shutil.copytree(
        REPO_ROOT / "backend" / "app",
        root / "backend" / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        destination = root / "backend" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "backend" / name, destination)
    frontend_dist = root / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text("<!doctype html><title>schema fixture</title>\n", encoding="utf-8")
    for name in (
        "bootstrap-conda.ps1",
        "initialize-portable.ps1",
        "repair-portable.ps1",
        "start-production.ps1",
        "stop-production.ps1",
        "Invoke-PortableStart.ps1",
        "Show-PortableProgress.ps1",
        "Portable-Validation.ps1",
        "select-portable-folder.ps1",
        "export-portable-diagnostics.py",
        "import-portable-data.py",
        "import_portable_data.py",
        "portable_install.py",
        "portable_launcher.py",
        "portable_operations.py",
        "portable_packages.py",
        "portable_package_runner.py",
    ):
        destination = root / "scripts" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "scripts" / name, destination)
    for name in (
        "toolchain.lock.json",
        "runtime.lock.json",
        "models.lock.json",
        "tts-more-package.schema.json",
        "error-catalog.zh-CN.json",
    ):
        destination = root / "packaging" / "portable" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "packaging" / "portable" / name, destination)
    for name in (
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "LICENSE",
        "NOTICE",
        "repo.lock.json",
    ):
        shutil.copy2(REPO_ROOT / name, root / name)
    guide = REPO_ROOT / "packaging" / "portable" / "使用说明-先看这里.txt"
    assert guide.is_file(), "portable package root guide is missing"
    destination = root / "packaging" / "portable" / guide.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(guide, destination)


def _build_controller_bootstrap(tmp_path: Path, version: str) -> tuple[Path, Path]:
    assert POWERSHELL is not None
    controller_root = tmp_path / "controller"
    _copy_controller_builder_fixture(controller_root)
    _initialize_git_repository(controller_root)
    output_root = tmp_path / "controller-output"
    staging_before = {path.resolve() for path in Path(tempfile.gettempdir()).glob("tts-more-controller-*")}
    _run_checked(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(controller_root / "Build-Package.ps1"),
            "-Profile",
            "Bootstrap",
            "-Device",
            "CPU",
            "-Version",
            version,
            "-OutputRoot",
            str(output_root),
        ],
        controller_root,
        env={**os.environ, "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve())},
    )
    archives = list(output_root.glob("*.zip"))
    assert len(archives) == 1
    stage = _extract_zip_package_root(archives[0], tmp_path / "controller-extracted")
    assert {path.resolve() for path in Path(tempfile.gettempdir()).glob("tts-more-controller-*")} == staging_before
    return stage, archives[0]


def _build_worker_bootstrap(
    tmp_path: Path,
    version: str,
    component: str = "gpt-sovits",
    *,
    add_plain_locked_paths: bool = False,
) -> tuple[Path, Path, Path, dict[str, bytes]]:
    assert POWERSHELL is not None
    worker_root = tmp_path / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, component, "a" * 40)
    (worker_root / "upstream-entry.py").write_text("UPSTREAM_FIXTURE = True\n", encoding="utf-8")
    (worker_root / "README.md").write_text("upstream fixture\n", encoding="utf-8")
    (worker_root / "nested" / "artifacts").mkdir(parents=True)
    (worker_root / "nested" / "artifacts" / "machine.log").write_text(
        "private build artifact\n", encoding="utf-8"
    )
    (worker_root / "nested" / ".env.local").write_text("PRIVATE_TOKEN=secret\n", encoding="utf-8")
    source_component = worker_root / "tts_more" / "component.json"
    source_model_lock = worker_root / "tts_more" / "locks" / "models.lock.json"
    if add_plain_locked_paths:
        model_lock = json.loads(source_model_lock.read_text(encoding="utf-8-sig"))
        locked_asset = dict(model_lock["assets"][0])
        locked_asset["id"] = "plain-extension-audit-fixture"
        locked_asset["target"] = "locked_assets/metadata.json"
        model_lock["assets"].append(locked_asset)
        model_lock["required_paths"].append(r"locked_required\payload.txt")
        source_model_lock.write_text(json.dumps(model_lock, indent=2) + "\n", encoding="utf-8")
    source_before = {
        "component.json": source_component.read_bytes(),
        "locks/models.lock.json": source_model_lock.read_bytes(),
        "locks/runtime.lock.json": (worker_root / "tts_more" / "locks" / "runtime.lock.json").read_bytes(),
        "integration.manifest.json": (worker_root / "tts_more" / "integration.manifest.json").read_bytes(),
    }
    _initialize_git_repository(worker_root)
    output_root = tmp_path / "worker-output"
    work_root = _external_worker_test_root(tmp_path, "fixture-work")
    try:
        _run_checked(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                version,
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            worker_root,
            env={**os.environ, "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve())},
        )
        archives = list(output_root.glob("*.zip"))
        assert len(archives) == 1
        stage = _extract_zip_package_root(archives[0], tmp_path / "worker-extracted")
        assert not [path for path in work_root.rglob("*") if path.is_file()]
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
    return worker_root, stage, archives[0], source_before


def _inject_release_zip_entry(source: Path, destination: Path, relative: str) -> None:
    shutil.copy2(source, destination)
    with zipfile.ZipFile(destination, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        package_root = archive.namelist()[0].replace("\\", "/").split("/", 1)[0]
        archive.writestr(f"{package_root}/{relative}", b"ordinary extension payload")


def _extract_zip_package_root(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        roots = {name.replace("\\", "/").split("/", 1)[0] for name in archive.namelist()}
        assert len(roots) == 1
        archive.extractall(destination)
    return destination / roots.pop()


def _external_worker_test_root(tmp_path: Path, label: str) -> Path:
    identity = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"tm-{label}-{identity}"


def _padded_checkout_root(tmp_path: Path, target_length: int = 145) -> Path:
    prefix = tmp_path / "deep-checkout"
    suffix_length = max(16, target_length - len(str(prefix)) - len(str(Path("worker"))) - 2)
    return prefix / ("x" * suffix_length) / "worker"


def _create_windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert link.is_dir()


def _remove_windows_junction(link: Path) -> None:
    if os.path.lexists(link):
        os.rmdir(link)


def _remove_test_tree(root: Path) -> None:
    if not os.path.lexists(root):
        return

    def remove_readonly(function, path: str, _error) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(root, onerror=remove_readonly)


def test_worker_bootstrap_uses_short_external_unique_staging_from_deep_checkout(
    tmp_path: Path,
) -> None:
    if POWERSHELL is None:
        pytest.skip("deep Windows worker package build requires PowerShell")
    worker_root = _padded_checkout_root(tmp_path)
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "gpt-sovits", "a" * 40)
    workflow = worker_root / ".github" / "workflows" / "build_windows_packages.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: deep checkout fixture\n", encoding="utf-8")
    _initialize_git_repository(worker_root)
    work_root = _external_worker_test_root(tmp_path, "deep-work")
    output_root = _external_worker_test_root(tmp_path, "deep-output")
    sentinel = work_root / "other-build" / "sentinel.txt"
    try:
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("preserve sibling build\n", encoding="utf-8")
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "d1-long-path",
                "-OutputRoot",
                str(output_root),
            ],
            cwd=worker_root,
            env={
                **os.environ,
                "TEMP": str(work_root),
                "TMP": str(work_root),
                "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        archives = list(output_root.glob("*.zip"))
        assert len(archives) == 1
        assert _load_portable_packages().audit_release_zip(archives[0])["valid"] is True
        assert sentinel.read_text(encoding="utf-8") == "preserve sibling build\n"
        assert [path for path in work_root.rglob("*") if path.is_file()] == [sentinel]
        assert not (worker_root / "artifacts" / "portable" / ".work").exists()
        assert subprocess.check_output(
            ["git", "status", "--short"], cwd=worker_root, text=True
        ).strip() == ""
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        shutil.rmtree(output_root, ignore_errors=True)


@pytest.mark.parametrize("mode", ("root", "child", "default-child"))
def test_worker_rejects_work_root_at_or_below_source_before_side_effects(
    tmp_path: Path, mode: str
) -> None:
    if POWERSHELL is None:
        pytest.skip("Windows worker WorkRoot overlap contract requires PowerShell")
    fixture_root = _external_worker_test_root(tmp_path, f"overlap-{mode}")
    worker_root = fixture_root / "source"
    output_root = fixture_root / "output"
    _load_sync_integrations().sync_integration(
        REPO_ROOT, worker_root, "gpt-sovits", "a" * 40
    )
    source_sentinel = worker_root / "source-sentinel.txt"
    sibling_sentinel = worker_root / "source-sibling" / "keep.txt"
    source_sentinel.write_text("source sentinel\n", encoding="utf-8")
    sibling_sentinel.parent.mkdir(parents=True)
    sibling_sentinel.write_text("source sibling\n", encoding="utf-8")
    _initialize_git_repository(worker_root)
    before_status = subprocess.check_output(
        ["git", "status", "--short"], cwd=worker_root, text=True
    )
    before_sentinels = (source_sentinel.read_bytes(), sibling_sentinel.read_bytes())
    environment = {
        **os.environ,
        "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
    }
    arguments = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(worker_root / "Build-Package.ps1"),
        "-Profile",
        "Bootstrap",
        "-Device",
        "CPU",
        "-Version",
        "v" * 128 if mode == "root" else f"d2-overlap-{mode}",
        "-OutputRoot",
        str(output_root),
    ]
    if mode == "root":
        work_root = Path(str(worker_root).upper())
        arguments.extend(["-WorkRoot", str(work_root)])
    else:
        work_root = worker_root / "artifacts" / mode
        if mode == "child":
            arguments.extend(["-WorkRoot", str(work_root)])
        else:
            environment.update({"TEMP": str(work_root), "TMP": str(work_root)})
    try:
        completed = subprocess.run(
            arguments,
            cwd=worker_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=45,
        )
        message = (completed.stdout + completed.stderr).lower()
        assert completed.returncode != 0
        assert "workroot must be outside source checkout" in message
        assert "-workroot" in message
        assert not output_root.exists()
        assert (source_sentinel.read_bytes(), sibling_sentinel.read_bytes()) == before_sentinels
        assert (
            subprocess.check_output(
                ["git", "status", "--short"], cwd=worker_root, text=True
            )
            == before_status
        )
        assert not list(worker_root.rglob("tts-more-worker-*"))
        if mode == "child":
            assert not work_root.exists()
        elif mode == "default-child":
            assert not list(work_root.glob("tts-more-worker-*"))
    finally:
        _remove_test_tree(fixture_root)


def test_worker_allows_similar_prefix_sibling_work_root(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("Windows worker WorkRoot boundary contract requires PowerShell")
    fixture_root = _external_worker_test_root(tmp_path, "overlap-prefix")
    worker_root = fixture_root / "source"
    work_root = fixture_root / "source-work"
    output_root = fixture_root / "output"
    _load_sync_integrations().sync_integration(
        REPO_ROOT, worker_root, "gpt-sovits", "a" * 40
    )
    _initialize_git_repository(worker_root)
    sentinel = work_root / "sibling-build.txt"
    try:
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("preserve prefix sibling\n", encoding="utf-8")
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "d2-prefix-sibling",
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            cwd=worker_root,
            env={
                **os.environ,
                "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        archives = list(output_root.glob("*.zip"))
        assert len(archives) == 1
        assert _load_portable_packages().audit_release_zip(archives[0])["valid"] is True
        assert sentinel.read_text(encoding="utf-8") == "preserve prefix sibling\n"
        assert [path for path in work_root.rglob("*") if path.is_file()] == [sentinel]
        assert not list(work_root.glob("tts-more-worker-*"))
        assert subprocess.check_output(
            ["git", "status", "--short"], cwd=worker_root, text=True
        ).strip() == ""
    finally:
        _remove_test_tree(fixture_root)


@pytest.mark.parametrize("mode", ("junction", "junction-ancestor"))
def test_worker_rejects_reparse_work_root_before_staging(
    tmp_path: Path, mode: str
) -> None:
    if os.name != "nt" or POWERSHELL is None:
        pytest.skip("Windows junction WorkRoot contract requires Windows PowerShell")
    fixture_root = _external_worker_test_root(tmp_path, f"reparse-{mode}")
    worker_root = fixture_root / "source"
    output_root = fixture_root / "output"
    junction = fixture_root / "external-work-link"
    target = worker_root / "work-target"
    _load_sync_integrations().sync_integration(
        REPO_ROOT, worker_root, "gpt-sovits", "a" * 40
    )
    source_sentinel = target / "source-sentinel.txt"
    sibling_sentinel = worker_root / "source-sibling.txt"
    source_sentinel.parent.mkdir(parents=True)
    source_sentinel.write_text("junction target sentinel\n", encoding="utf-8")
    sibling_sentinel.write_text("source sibling\n", encoding="utf-8")
    _initialize_git_repository(worker_root)
    before_status = subprocess.check_output(
        ["git", "status", "--short"], cwd=worker_root, text=True
    )
    before_sentinels = (source_sentinel.read_bytes(), sibling_sentinel.read_bytes())
    try:
        _create_windows_junction(junction, target)
        work_root = (
            junction
            if mode == "junction"
            else junction / "future" / "package-work"
        )
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "j" * 128,
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            cwd=worker_root,
            env={
                **os.environ,
                "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=45,
        )
        message = (completed.stdout + completed.stderr).lower()
        assert completed.returncode != 0
        assert "workroot path must not traverse a reparse point" in message
        assert "-workroot" in message
        assert not output_root.exists()
        assert (source_sentinel.read_bytes(), sibling_sentinel.read_bytes()) == before_sentinels
        assert (
            subprocess.check_output(
                ["git", "status", "--short"], cwd=worker_root, text=True
            )
            == before_status
        )
        assert not list(target.glob("tts-more-worker-*"))
        assert not (target / "future").exists()
    finally:
        _remove_windows_junction(junction)
        _remove_test_tree(fixture_root)


def test_worker_rejects_existing_file_in_work_root_path_before_staging(
    tmp_path: Path,
) -> None:
    if POWERSHELL is None:
        pytest.skip("worker WorkRoot fail-closed contract requires PowerShell")
    fixture_root = _external_worker_test_root(tmp_path, "reparse-file")
    worker_root = fixture_root / "source"
    output_root = fixture_root / "output"
    file_ancestor = fixture_root / "not-a-directory"
    work_root = file_ancestor / "package-work"
    _load_sync_integrations().sync_integration(
        REPO_ROOT, worker_root, "gpt-sovits", "a" * 40
    )
    _initialize_git_repository(worker_root)
    try:
        file_ancestor.write_text("existing file ancestor\n", encoding="utf-8")
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "d3-file-ancestor",
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            cwd=worker_root,
            env={
                **os.environ,
                "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        message = (completed.stdout + completed.stderr).lower()
        assert completed.returncode != 0
        assert "workroot path contains an existing non-directory segment" in message
        assert "-workroot" in message
        assert file_ancestor.read_text(encoding="utf-8") == "existing file ancestor\n"
        assert not output_root.exists()
        assert subprocess.check_output(
            ["git", "status", "--short"], cwd=worker_root, text=True
        ).strip() == ""
    finally:
        _remove_test_tree(fixture_root)


def test_worker_staging_path_budget_fails_before_copy_without_touching_siblings(
    tmp_path: Path,
) -> None:
    if POWERSHELL is None:
        pytest.skip("Windows worker path budget contract requires PowerShell")
    worker_root = tmp_path / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "gpt-sovits", "a" * 40)
    _initialize_git_repository(worker_root)
    work_root = _padded_checkout_root(tmp_path, target_length=205).parent
    output_root = _external_worker_test_root(tmp_path, "budget-output")
    sentinel = work_root / "existing-build.txt"
    try:
        work_root.mkdir(parents=True)
        sentinel.write_text("preserve me\n", encoding="utf-8")
        before = {path.name: path.read_bytes() for path in work_root.iterdir() if path.is_file()}
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "d1-budget",
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            cwd=worker_root,
            env={**os.environ, "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve())},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        message = (completed.stdout + completed.stderr).lower()
        assert completed.returncode != 0
        assert "path budget" in message
        assert "-workroot" in message
        assert {path.name: path.read_bytes() for path in work_root.iterdir() if path.is_file()} == before
        assert list(path for path in work_root.iterdir() if path.is_dir()) == []
        assert not output_root.exists()
        assert not (worker_root / "artifacts" / "portable" / ".work").exists()
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        shutil.rmtree(output_root, ignore_errors=True)


def test_worker_build_failure_cleans_only_its_unique_staging(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("Windows worker staging cleanup requires PowerShell")
    worker_root = tmp_path / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "gpt-sovits", "a" * 40)
    _initialize_git_repository(worker_root)
    portable_packages = worker_root / "tts_more" / "portable_packages.py"
    portable_packages.write_text(
        'raise RuntimeError("forced post-copy package failure")\n', encoding="utf-8"
    )
    work_root = _external_worker_test_root(tmp_path, "failure-work")
    output_root = _external_worker_test_root(tmp_path, "failure-output")
    sentinel = work_root / "sibling-build.txt"
    try:
        work_root.mkdir(parents=True)
        sentinel.write_text("preserve sibling\n", encoding="utf-8")
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "d1-cleanup",
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            cwd=worker_root,
            env={**os.environ, "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve())},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        message = (completed.stdout + completed.stderr).lower()
        assert completed.returncode != 0
        assert "forced post-copy package failure" in message
        assert sentinel.read_text(encoding="utf-8") == "preserve sibling\n"
        assert [path for path in work_root.rglob("*") if path.is_file()] == [sentinel]
        assert not [path for path in work_root.iterdir() if path.is_dir()]
        assert not output_root.exists()
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        shutil.rmtree(output_root, ignore_errors=True)


def test_worker_cleanup_never_recursively_deletes_replacement_work_directory(
    tmp_path: Path,
) -> None:
    if os.name != "nt" or POWERSHELL is None:
        pytest.skip("Windows worker cleanup identity contract requires PowerShell")
    worker_root = tmp_path / "worker"
    _load_sync_integrations().sync_integration(
        REPO_ROOT, worker_root, "gpt-sovits", "a" * 40
    )
    portable_packages = worker_root / "tts_more" / "portable_packages.py"
    original = portable_packages.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    assert original.startswith(future)
    portable_packages.write_text(
        future
        + """from pathlib import Path
import os

work = Path(__file__).resolve().parents[3]
moved = Path(os.environ["TTS_MORE_CLEANUP_ATTACK_MOVED"])
denied = Path(os.environ["TTS_MORE_CLEANUP_ATTACK_DENIED"])
try:
    work.rename(moved)
except OSError as exc:
    denied.write_text(f"{type(exc).__name__}: {exc}\\n", encoding="utf-8")
else:
    work.mkdir()
    (work / "replacement-sentinel.txt").write_text("replacement received build path\\n", encoding="utf-8")

"""
        + original[len(future) :],
        encoding="utf-8",
    )
    _initialize_git_repository(worker_root)
    work_root = _external_worker_test_root(tmp_path, "worker-cleanup")
    output_root = _external_worker_test_root(tmp_path, "worker-cleanup-output")
    moved = work_root.parent / f"{work_root.name}-moved-original"
    denied = work_root.parent / f"{work_root.name}-rename-denied.txt"
    try:
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "d2-worker-cleanup-replacement",
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            cwd=worker_root,
            env={
                **os.environ,
                "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
                "TTS_MORE_CLEANUP_ATTACK_MOVED": str(moved),
                "TTS_MORE_CLEANUP_ATTACK_DENIED": str(denied),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "permissionerror" in denied.read_text(encoding="utf-8").lower()
        assert not moved.exists()
        assert not list(work_root.glob("tts-more-worker-*"))
        archives = list(output_root.glob("*.zip"))
        assert len(archives) == 1
        assert _load_portable_packages().audit_release_zip(archives[0])["valid"] is True
        assert not list(work_root.rglob("replacement-sentinel.txt"))
    finally:
        _remove_test_tree(work_root)
        _remove_test_tree(output_root)
        _remove_test_tree(moved)
        denied.unlink(missing_ok=True)


def test_controller_cleanup_never_recursively_deletes_replacement_work_directory(
    tmp_path: Path,
) -> None:
    if os.name != "nt" or POWERSHELL is None:
        pytest.skip("Windows controller cleanup identity contract requires PowerShell")
    controller_root = tmp_path / "controller"
    _copy_controller_builder_fixture(controller_root)
    portable_packages = controller_root / "scripts" / "portable_packages.py"
    original = portable_packages.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    assert original.startswith(future)
    portable_packages.write_text(
        future
        + """from pathlib import Path
import os

work = Path(__file__).resolve().parents[2]
moved = Path(os.environ["TTS_MORE_CLEANUP_ATTACK_MOVED"])
denied = Path(os.environ["TTS_MORE_CLEANUP_ATTACK_DENIED"])
try:
    work.rename(moved)
except OSError as exc:
    denied.write_text(f"{type(exc).__name__}: {exc}\\n", encoding="utf-8")
else:
    work.mkdir()
    (work / "replacement-sentinel.txt").write_text("replacement received build path\\n", encoding="utf-8")

"""
        + original[len(future) :],
        encoding="utf-8",
    )
    _initialize_git_repository(controller_root)
    work_root = _external_worker_test_root(tmp_path, "controller-cleanup")
    output_root = _external_worker_test_root(tmp_path, "controller-cleanup-output")
    moved = work_root.parent / f"{work_root.name}-moved-original"
    denied = work_root.parent / f"{work_root.name}-rename-denied.txt"
    try:
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(controller_root / "Build-Package.ps1"),
                "-Profile",
                "Bootstrap",
                "-Device",
                "CPU",
                "-Version",
                "d2-cleanup-replacement",
                "-OutputRoot",
                str(output_root),
                "-WorkRoot",
                str(work_root),
            ],
            cwd=controller_root,
            env={
                **os.environ,
                "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
                "TTS_MORE_CLEANUP_ATTACK_MOVED": str(moved),
                "TTS_MORE_CLEANUP_ATTACK_DENIED": str(denied),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "permissionerror" in denied.read_text(encoding="utf-8").lower()
        assert not moved.exists()
        assert not list(work_root.glob("tts-more-controller-*"))
        archives = list(output_root.glob("*.zip"))
        assert len(archives) == 1
        assert _load_portable_packages().audit_release_zip(archives[0])["valid"] is True
        assert not list(work_root.rglob("replacement-sentinel.txt"))
    finally:
        _remove_test_tree(work_root)
        _remove_test_tree(output_root)
        _remove_test_tree(moved)
        denied.unlink(missing_ok=True)


def test_worker_full_github_gate_precedes_work_root_side_effects(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("Windows Full package gate requires PowerShell")
    worker_root = tmp_path / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "gpt-sovits", "a" * 40)
    _initialize_git_repository(worker_root)
    work_root = _external_worker_test_root(tmp_path, "full-work")
    output_root = _external_worker_test_root(tmp_path, "full-output")
    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(worker_root / "Build-Package.ps1"),
            "-Profile",
            "Full",
            "-Device",
            "CPU",
            "-OutputRoot",
            str(output_root),
            "-WorkRoot",
            str(work_root),
        ],
        cwd=worker_root,
        env={
            **os.environ,
            "GITHUB_ACTIONS": "true",
            "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode != 0
    assert "profile=full is local-only" in (completed.stdout + completed.stderr).lower()
    assert not work_root.exists()
    assert not output_root.exists()


def test_controller_full_github_gate_precedes_work_root_side_effects(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("Windows Full package gate requires PowerShell")
    controller_root = tmp_path / "controller"
    _copy_controller_builder_fixture(controller_root)
    work_root = _external_worker_test_root(tmp_path, "controller-full-work")
    output_root = _external_worker_test_root(tmp_path, "controller-full-output")
    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(controller_root / "Build-Package.ps1"),
            "-Profile",
            "Full",
            "-Device",
            "CPU",
            "-OutputRoot",
            str(output_root),
            "-WorkRoot",
            str(work_root),
        ],
        cwd=controller_root,
        env={
            **os.environ,
            "GITHUB_ACTIONS": "true",
            "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode != 0
    assert "profile=full is local-only" in (completed.stdout + completed.stderr).lower()
    assert not work_root.exists()
    assert not output_root.exists()


@pytest.fixture(scope="module")
def clean_portable_builds(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    if POWERSHELL is None:
        pytest.skip("clean portable package contract requires PowerShell")
    root = tmp_path_factory.mktemp("clean-portable-layout")
    controller_stage, controller_zip = _build_controller_bootstrap(
        root / "controller-build", "0.2.0-clean-controller"
    )
    worker_root, worker_stage, worker_zip, source_before = (
        _build_worker_bootstrap(root / "worker-build", "0.2.0-clean-worker")
    )
    return {
        "controller_stage": controller_stage,
        "controller_zip": controller_zip,
        "worker_root": worker_root,
        "worker_stage": worker_stage,
        "worker_zip": worker_zip,
        "source_before": source_before,
    }


def test_controller_bootstrap_has_clean_normal_user_root(
    clean_portable_builds: dict[str, object],
) -> None:
    stage = Path(clean_portable_builds["controller_stage"])
    archive_path = Path(clean_portable_builds["controller_zip"])
    required_root = {
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "Build-Package.ps1",
        "使用说明-先看这里.txt",
    }
    assert required_root <= {path.name for path in stage.iterdir() if path.is_file()}
    assert (stage / "app" / "backend" / "app").is_dir()
    assert (stage / "app" / "frontend" / "index.html").is_file()
    assert (stage / "scripts" / "import-portable-data.py").is_file()
    assert (stage / "scripts" / "import_portable_data.py").is_file()
    assert (stage / "scripts" / "select-portable-folder.ps1").is_file()
    assert (stage / "licenses" / "LICENSE").is_file()
    assert (stage / "licenses" / "NOTICE").is_file()
    assert (stage / "licenses" / "THIRD_PARTY_NOTICES.json").is_file()
    assert (stage / "package" / "repo.lock.json").is_file()
    for clutter in (".git", "artifacts", "backend", "frontend", "LICENSE", "NOTICE", "repo.lock.json"):
        assert not (stage / clutter).exists(), f"checkout clutter leaked to package root: {clutter}"
    manifest = json.loads(
        (stage / "package" / "tts-more-package.json").read_text(encoding="utf-8-sig")
    )
    assert manifest["licenses"] == "licenses/THIRD_PARTY_NOTICES.json"
    packages = _load_portable_packages()
    assert packages.audit_release_zip(archive_path)["valid"] is True
    assert packages.verify_sha256_manifest(stage)["valid"] is True


def test_worker_bootstrap_stages_all_upstream_source_under_app_without_mutating_checkout(
    clean_portable_builds: dict[str, object],
) -> None:
    worker_root = Path(clean_portable_builds["worker_root"])
    stage = Path(clean_portable_builds["worker_stage"])
    archive_path = Path(clean_portable_builds["worker_zip"])
    required_root = {
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "Build-Package.ps1",
        "Start-WebUI.cmd",
        "使用说明-先看这里.txt",
    }
    assert required_root <= {path.name for path in stage.iterdir() if path.is_file()}
    assert (stage / "app" / "upstream-entry.py").is_file()
    assert (stage / "app" / "README.md").is_file()
    assert (stage / "app" / "tts_more" / "component.json").is_file()
    for relative in (
        "import_portable_data.py",
        "import-portable-data.py",
        "select-portable-folder.ps1",
    ):
        assert (stage / "app" / "tts_more" / relative).is_file()
    assert not (stage / "app" / "nested" / "artifacts").exists()
    assert not (stage / "app" / "nested" / ".env.local").exists()
    for clutter in (
        ".git",
        ".venv",
        "artifacts",
        "runtime",
        "data",
        "tts_more",
        "README.md",
        "upstream-entry.py",
    ):
        assert not (stage / clutter).exists(), f"worker checkout clutter leaked to package root: {clutter}"

    component = json.loads(
        (stage / "app" / "tts_more" / "component.json").read_text(encoding="utf-8-sig")
    )
    manifest = json.loads(
        (stage / "package" / "tts-more-package.json").read_text(encoding="utf-8-sig")
    )
    model_lock = json.loads(
        (stage / "app" / "tts_more" / "locks" / "models.lock.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert component["source_root"] == "app"
    assert manifest["runtime"]["lock"] == "app/tts_more/locks/runtime.lock.json"
    assert manifest["models"]["lock"] == "app/tts_more/locks/models.lock.json"
    targets = [str(asset["target"]).replace("\\", "/") for asset in model_lock["assets"]]
    required_paths = [str(path).replace("\\", "/") for path in model_lock["required_paths"]]
    assert targets and required_paths
    assert all(path.startswith("app/") and not path.startswith("app/app/") for path in targets)
    assert all(path.startswith("app/") and not path.startswith("app/app/") for path in required_paths)
    for relative, before in dict(clean_portable_builds["source_before"]).items():
        assert (worker_root / "tts_more" / relative).read_bytes() == before
    assert _load_sync_integrations().check_integration(stage / "app") == []
    packages = _load_portable_packages()
    assert packages.audit_release_zip(archive_path)["valid"] is True
    assert packages.verify_sha256_manifest(stage)["valid"] is True


@pytest.mark.parametrize("archive_key", ("controller_zip", "worker_zip"))
@pytest.mark.parametrize(
    "model_directory",
    ("pretrained_models", "checkpoints", "SoVITS_weights", "GPT_weights"),
)
def test_release_audit_rejects_plain_files_in_model_data_directories(
    clean_portable_builds: dict[str, object],
    tmp_path: Path,
    archive_key: str,
    model_directory: str,
) -> None:
    packages = _load_portable_packages()
    clean_archive = Path(clean_portable_builds[archive_key])
    assert packages.audit_release_zip(clean_archive)["valid"] is True
    polluted_archive = tmp_path / f"{archive_key}-{model_directory}.zip"
    _inject_release_zip_entry(
        clean_archive,
        polluted_archive,
        f"app/nested/{model_directory}/ordinary-payload.txt",
    )

    report = packages.audit_release_zip(polluted_archive)

    assert report["valid"] is False
    assert any("forbidden release asset" in error for error in report["errors"])


def test_release_audit_canonicalizes_model_directories_without_rejecting_models_packages(
    clean_portable_builds: dict[str, object], tmp_path: Path
) -> None:
    packages = _load_portable_packages()
    clean_archive = Path(clean_portable_builds["controller_zip"])
    canonical_attack = tmp_path / "canonical-model-directory.zip"
    _inject_release_zip_entry(
        clean_archive,
        canonical_attack,
        "app/nested/ＰＲＥＴＲＡＩＮＥＤ＿ＭＯＤＥＬＳ/ordinary.json",
    )
    legal_models_package = tmp_path / "legal-models-package.zip"
    _inject_release_zip_entry(
        clean_archive,
        legal_models_package,
        "app/python_package/models/__init__.py",
    )
    legal_file_boundary = tmp_path / "legal-file-boundary.zip"
    _inject_release_zip_entry(
        clean_archive,
        legal_file_boundary,
        "app/python_package/checkpoints",
    )

    rejected = packages.audit_release_zip(canonical_attack)

    assert rejected["valid"] is False
    assert any("forbidden release asset" in error for error in rejected["errors"])
    assert packages.audit_release_zip(legal_models_package)["valid"] is True
    assert packages.audit_release_zip(legal_file_boundary)["valid"] is True


def test_worker_release_audit_rejects_exact_staged_model_lock_paths(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("real worker Bootstrap model-lock audit test requires PowerShell")
    _, stage, clean_archive, _ = _build_worker_bootstrap(
        tmp_path / "w",
        "d1-lock",
        add_plain_locked_paths=True,
    )
    packages = _load_portable_packages()
    assert packages.audit_release_zip(clean_archive)["valid"] is True
    assert packages.verify_sha256_manifest(stage)["valid"] is True
    staged_lock = json.loads(
        (stage / "app" / "tts_more" / "locks" / "models.lock.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert any(
        asset.get("target") == "app/locked_assets/metadata.json"
        for asset in staged_lock["assets"]
    )
    assert "app/locked_required/payload.txt" in staged_lock["required_paths"]

    for index, relative in enumerate(
        ("APP/LOCKED_ASSETS/METADATA.JSON", "app/locked_REQUIRED/PAYLOAD.TXT")
    ):
        polluted_archive = tmp_path / f"locked-path-{index}.zip"
        _inject_release_zip_entry(clean_archive, polluted_archive, relative)

        report = packages.audit_release_zip(polluted_archive)

        assert report["valid"] is False
        assert any("locked model" in error for error in report["errors"])


def test_worker_package_resolves_package_source_and_bundle_roots_independently(
    clean_portable_builds: dict[str, object], tmp_path: Path
) -> None:
    stage = Path(clean_portable_builds["worker_stage"])
    package_root = tmp_path / "移动盘 worker 包"
    shutil.copytree(stage, package_root)
    runtime_python = package_root / "runtime" / "live" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"fixture runtime")

    runner = _load_portable_package_runner()
    command, cwd, environment = runner.build_worker_process(package_root)
    source_root = package_root / "app"
    bundle_root = source_root / "tts_more"
    assert cwd == source_root
    assert command[0] == str(runtime_python)
    assert command[command.index("--app-dir") + 1] == str(bundle_root)
    assert environment["TTS_MORE_PACKAGE_ROOT"] == str(package_root)
    assert environment["TTS_MORE_GPTSOVITS_REPO"] == str(source_root)
    assert environment["PYTHONPATH"] == str(source_root)
    assert environment["TTS_MORE_ARTIFACT_ROOT"] == str(
        package_root / "data" / "local" / "artifacts"
    )

    paths_script = bundle_root / "Portable-Paths.ps1"
    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f". '{paths_script}'; Get-PortableWorkerPaths -BundleRoot '{bundle_root}' | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    resolved = json.loads(completed.stdout.strip())
    assert Path(resolved["PackageRoot"]) == package_root
    assert Path(resolved["SourceRoot"]) == source_root
    assert Path(resolved["BundleRoot"]) == bundle_root

    start_controller = (bundle_root / "Invoke-PortableStart.ps1").read_text(encoding="utf-8")
    initializer = (bundle_root / "Initialize.ps1").read_text(encoding="utf-8")
    worker_start = (bundle_root / "Start-Worker.ps1").read_text(encoding="utf-8")
    worker_stop = (bundle_root / "Stop-Worker.ps1").read_text(encoding="utf-8")
    assert "Get-PortableWorkerPaths" in start_controller
    assert "SourceRoot = $sourceRoot" in start_controller
    assert 'lock --check --project $SourceRoot' in initializer
    assert 'export --frozen --no-dev --no-emit-project --project $SourceRoot' in initializer
    assert "ToolchainLockRelative" in initializer
    assert "-WorkingDirectory $SourceRoot" in worker_start
    assert 'TTS_MORE_GPTSOVITS_REPO = $SourceRoot' in worker_start
    assert "Get-PortableWorkerPaths" in worker_stop
    assert 'RelativePath "tts_more\\locks\\runtime.lock.json"' not in worker_stop


def test_worker_source_checkout_keeps_checkout_root_as_package_and_source_root(
    clean_portable_builds: dict[str, object],
) -> None:
    worker_root = Path(clean_portable_builds["worker_root"])
    bundle_root = worker_root / "tts_more"
    paths_script = bundle_root / "Portable-Paths.ps1"
    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f". '{paths_script}'; Get-PortableWorkerPaths -BundleRoot '{bundle_root}' | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    resolved = json.loads(completed.stdout.strip())
    assert Path(resolved["PackageRoot"]) == worker_root
    assert Path(resolved["SourceRoot"]) == worker_root
    assert Path(resolved["BundleRoot"]) == bundle_root
    assert json.loads((bundle_root / "component.json").read_text(encoding="utf-8"))["component"] == (
        "gpt-sovits"
    )
    assert "source_root" not in json.loads(
        (bundle_root / "component.json").read_text(encoding="utf-8")
    )
    assert "tts_more\\Start-WebUI.ps1" in (worker_root / "Start-WebUI.cmd").read_text(
        encoding="utf-8"
    )


def test_worker_source_root_rejects_a_junction_in_the_package_path(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("junction source_root contract is Windows-specific")
    package_root = tmp_path / "package"
    external_app = tmp_path / "external-app"
    bundle_root = external_app / "tts_more"
    bundle_root.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "integrations" / "windows" / "Portable-Paths.ps1", bundle_root)
    (bundle_root / "component.json").write_text(
        json.dumps({"component": "gpt-sovits", "source_root": "app"}), encoding="utf-8"
    )
    (package_root / "package").mkdir(parents=True)
    (package_root / "package" / "tts-more-package.json").write_text("{}\n", encoding="utf-8")
    junction = package_root / "app"
    create = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"New-Item -ItemType Junction -Path '{junction}' -Target '{external_app}' | Out-Null",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert create.returncode == 0, create.stderr

    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f". '{junction / 'tts_more' / 'Portable-Paths.ps1'}'; Get-PortableWorkerPaths -BundleRoot '{junction / 'tts_more'}' -PackageRoot '{package_root}'",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode != 0
    assert "reparse point" in (completed.stdout + completed.stderr).lower()


def test_staged_gpt_webui_uses_only_package_private_python_and_requires_initialize(
    clean_portable_builds: dict[str, object],
) -> None:
    stage = Path(clean_portable_builds["worker_stage"])
    webui_script = stage / "app" / "tts_more" / "Start-WebUI.ps1"
    script = webui_script.read_text(encoding="utf-8")
    assert 'Join-Path $Root "runtime\\live\\python.exe"' in script
    assert '@("-I", (Join-Path $SourceRoot "webui.py"), "zh_CN")' in script
    assert "go-webui.bat" in script
    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(webui_script),
            "-PackageRoot",
            str(stage),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode != 0
    assert "initialize.cmd first" in (completed.stdout + completed.stderr).lower()


def test_packaged_build_entry_fails_with_an_intentional_user_facing_message(
    clean_portable_builds: dict[str, object],
) -> None:
    for stage_key in ("controller_stage", "worker_stage"):
        script = Path(clean_portable_builds[stage_key]) / "Build-Package.ps1"
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode != 0
        assert "source checkout" in (completed.stdout + completed.stderr).lower()


@pytest.fixture(scope="module")
def generated_v2_manifests(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    if POWERSHELL is None:
        pytest.skip("real portable builder contract test requires PowerShell")
    root = tmp_path_factory.mktemp("official-portable-schema")
    build_python = str(Path(sys.executable).resolve())
    environment = {**os.environ, "TTS_MORE_BUILD_PYTHON": build_python}
    version = "0.2.0-schema-contract"

    controller_root = root / "controller"
    _copy_controller_builder_fixture(controller_root)
    _initialize_git_repository(controller_root)
    _run_checked(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(controller_root / "Build-Package.ps1"),
            "-Profile",
            "Bootstrap",
            "-Device",
            "CPU",
            "-Version",
            version,
            "-OutputRoot",
            str(root / "controller-output"),
        ],
        controller_root,
        env=environment,
    )
    controller_archive = next((root / "controller-output").glob("*.zip"))
    controller_package_root = _extract_zip_package_root(
        controller_archive, root / "controller-extracted"
    )
    controller_manifest_path = controller_package_root / "package" / "tts-more-package.json"

    worker_root = root / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "gpt-sovits", "a" * 40)
    _initialize_git_repository(worker_root)
    _run_checked(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(worker_root / "Build-Package.ps1"),
            "-Profile",
            "Bootstrap",
            "-Device",
            "CPU",
            "-Version",
            version,
            "-OutputRoot",
            str(root / "worker-output"),
        ],
        worker_root,
        env=environment,
    )
    worker_archive = next((root / "worker-output").glob("*.zip"))
    worker_package_root = _extract_zip_package_root(worker_archive, root / "worker-extracted")
    worker_manifest_path = worker_package_root / "package" / "tts-more-package.json"
    assert controller_manifest_path.is_file()
    assert worker_manifest_path.is_file()
    return {
        "controller": json.loads(controller_manifest_path.read_text(encoding="utf-8-sig")),
        "worker": json.loads(worker_manifest_path.read_text(encoding="utf-8-sig")),
    }


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
        "package_id": "gpt-sovits",
        "release_version": "0.2.0",
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
        "protocol": {
            "name": "tts-more-v1",
            "version": "1.0",
            "controller_range": ">=0.2.0,<0.3.0",
        },
        "data": {
            "user": "data/user",
            "local": "data/local",
            "cache": "data/cache",
            "operations": "data/local/operations",
        },
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


def _write_v2_manifest(root: Path, payload: dict[str, object]) -> Path:
    for relative_path in (
        *payload["launchers"].values(),
        payload["runtime"]["lock"],
        payload["models"]["lock"],
        payload["licenses"],
    ):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    manifest = root / "package" / "tts-more-package.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


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
    manifest = _write_v2_manifest(tmp_path, payload)

    v2_report = packages.validate_manifest(manifest, tmp_path)

    assert v2_report == {
        "valid": True,
        "errors": [],
        "component": "gpt-sovits",
        "default_endpoint": "http://127.0.0.1:9880",
        "launcher": "Start.cmd",
    }


def test_completed_v2_requires_identity_protocol_and_data_paths(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload.update(
        {
            "package_id": "tts-more",
            "release_version": "0.2.0",
            "protocol": {
                "name": "tts-more-v1",
                "version": "1.0",
                "controller_range": ">=0.2.0,<0.3.0",
            },
            "data": {
                "user": "data/user",
                "local": "data/local",
                "cache": "data/cache",
                "operations": "data/local/operations",
            },
        }
    )
    manifest = _write_v2_manifest(tmp_path, payload)
    report = packages.validate_manifest(manifest, tmp_path)
    assert report["valid"] is True
    del payload["package_id"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = packages.validate_manifest(manifest, tmp_path)
    assert "package_id is required" in report["errors"]


def test_completed_v2_validates_protocol_and_every_data_path(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload["protocol"] = {"name": "wrong", "version": "", "controller_range": ""}
    payload["data"]["operations"] = "C:/machine/operations"
    manifest = _write_v2_manifest(tmp_path, payload)

    report = packages.validate_manifest(manifest, tmp_path)

    assert "protocol.name must be tts-more-v1" in report["errors"]
    assert "protocol.version is required" in report["errors"]
    assert "protocol.controller_range is required" in report["errors"]
    assert "data.operations must be a relative path" in report["errors"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("package_id", 123),
        ("package_id", ""),
        ("release_version", 123),
        ("release_version", ""),
    ),
)
def test_completed_v2_requires_non_empty_string_identity_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload[field] = value
    manifest = _write_v2_manifest(tmp_path, payload)

    report = packages.validate_manifest(manifest, tmp_path)

    assert report["valid"] is False
    assert f"{field} is required" in report["errors"]


def test_validate_manifest_accepts_windows_powershell_utf8_bom(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    (tmp_path / "Start.cmd").write_text("@echo off\r\n", encoding="utf-8")
    manifest = tmp_path / "tts-more-package.json"
    manifest.write_text(json.dumps(_valid_gpt_manifest()), encoding="utf-8-sig")

    assert packages.validate_manifest(manifest, tmp_path)["valid"] is True


def test_create_zip_uses_a_single_package_root_and_zip64(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    stage = tmp_path / "Component 目录"
    (stage / "nested").mkdir(parents=True)
    (stage / "Start.cmd").write_text("@echo off\n", encoding="utf-8")
    (stage / "nested" / "asset.txt").write_text("locked", encoding="utf-8")
    output = tmp_path / "component.zip"

    packages.create_zip(stage, output)

    with zipfile.ZipFile(output) as archive:
        assert archive._allowZip64 is True
        assert sorted(archive.namelist()) == ["Component 目录/Start.cmd", "Component 目录/nested/asset.txt"]


def test_package_sha256_gate_requires_exact_coverage_and_rejects_tampering(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    root = tmp_path / "package"
    first = root / "Start.cmd"
    second = root / "scripts" / "portable_packages.py"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"start")
    second.write_bytes(b"gate")

    def write_sums(*, include_second: bool = True) -> None:
        entries = [(first, "Start.cmd")]
        if include_second:
            entries.append((second, "scripts/portable_packages.py"))
        (root / "SHA256SUMS.txt").write_text(
            "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n" for path, relative in entries),
            encoding="utf-8",
        )

    write_sums()
    assert packages.verify_sha256_manifest(root)["valid"] is True

    second.write_bytes(b"tampered")
    tampered = packages.verify_sha256_manifest(root)
    assert tampered["valid"] is False
    assert any("hash mismatch" in error for error in tampered["errors"])

    write_sums(include_second=False)
    uncovered = packages.verify_sha256_manifest(root)
    assert uncovered["valid"] is False
    assert any("exact coverage" in error for error in uncovered["errors"])


def test_release_audit_accepts_bootstrap_and_rejects_full_or_runtime_assets(tmp_path: Path) -> None:
    packages = _load_portable_packages()

    def make_zip(name: str, profile: str, extra: dict[str, bytes] | None = None) -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "Component/package/tts-more-package.json",
                json.dumps({"schema_version": 2, "component": "gpt-sovits", "package_profile": profile}),
            )
            for relative, payload in (extra or {}).items():
                archive.writestr(f"Component/{relative}", payload)
        return path

    bootstrap = make_zip("bootstrap.zip", "bootstrap")
    full = make_zip("full.zip", "full")
    contaminated = make_zip("bad.zip", "bootstrap", {"runtime/live/python.exe": b"runtime"})

    assert packages.audit_release_zip(bootstrap)["valid"] is True
    assert "profile=full" in packages.audit_release_zip(full)["errors"][0]
    assert "forbidden release asset" in packages.audit_release_zip(contaminated)["errors"][0]


def test_release_audit_rejects_nested_t7_model_weight(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / "nested-t7.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "Component/package/tts-more-package.json",
            json.dumps(
                {"schema_version": 2, "component": "indextts", "package_profile": "bootstrap"}
            ),
        )
        archive.writestr("Component/indextts/indextts/utils/JDC/bst.t7", b"tracked model")

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("forbidden release asset" in error for error in report["errors"])


@pytest.mark.parametrize(
    "git_metadata_path",
    (
        "third_party/Matcha-TTS/.git",
        "third_party/Matcha-TTS/.git/config",
    ),
)
def test_release_audit_rejects_nested_git_metadata(
    tmp_path: Path, git_metadata_path: str
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / f"nested-git-{len(list(tmp_path.iterdir()))}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "Component/package/tts-more-package.json",
            json.dumps(
                {"schema_version": 2, "component": "cosyvoice", "package_profile": "bootstrap"}
            ),
        )
        archive.writestr(f"Component/{git_metadata_path}", b"git metadata")

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("forbidden release asset" in error for error in report["errors"])


@pytest.mark.parametrize(
    "private_path",
    (
        "app/artifacts/build.log",
        "app/nested/cache/download.part",
        "app/.env",
        "app/nested/.env.local",
        "app/nested/.env.production",
    ),
)
def test_release_audit_rejects_checkout_artifacts_caches_and_environment_files(
    tmp_path: Path, private_path: str
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / f"private-{len(list(tmp_path.iterdir()))}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "Component/package/tts-more-package.json",
            json.dumps(
                {"schema_version": 2, "component": "gpt-sovits", "package_profile": "bootstrap"}
            ),
        )
        archive.writestr(f"Component/{private_path}", b"private")

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("forbidden release asset" in error for error in report["errors"])


def test_worker_bootstrap_recursively_excludes_nested_submodule_gitdir(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("real worker Bootstrap gitdir cleanup test requires PowerShell")
    worker_root = tmp_path / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "cosyvoice", "a" * 40)
    _initialize_git_repository(worker_root)

    submodule_root = worker_root / "third_party" / "Matcha-TTS"
    submodule_git_dir = tmp_path / "submodule-git"
    submodule_root.mkdir(parents=True)
    _run_checked(
        [
            "git",
            "init",
            "--quiet",
            f"--separate-git-dir={submodule_git_dir}",
            str(submodule_root),
        ],
        worker_root,
    )
    _run_checked(["git", "config", "user.name", "Portable Submodule Test"], submodule_root)
    _run_checked(
        ["git", "config", "user.email", "portable-submodule-test@example.invalid"],
        submodule_root,
    )
    (submodule_root / "matcha.py").write_text("MATCHA_FIXTURE = True\n", encoding="utf-8")
    _run_checked(["git", "add", "matcha.py"], submodule_root)
    _run_checked(["git", "commit", "--quiet", "-m", "submodule fixture"], submodule_root)
    submodule_revision = subprocess.check_output(
        ["git", "-C", str(submodule_root), "rev-parse", "HEAD"],
        text=True,
    ).strip()

    gitdir_file = submodule_root / ".git"
    assert gitdir_file.is_file()
    assert (
        subprocess.check_output(
            ["git", "-C", str(submodule_root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        == submodule_revision
    )
    component_path = worker_root / "tts_more" / "component.json"
    component = json.loads(component_path.read_text(encoding="utf-8"))
    component["submodules"] = {"third_party/Matcha-TTS": submodule_revision}
    component_path.write_text(json.dumps(component, indent=2) + "\n", encoding="utf-8")
    _run_checked(["git", "add", "tts_more/component.json", "third_party/Matcha-TTS"], worker_root)
    _run_checked(["git", "commit", "--quiet", "-m", "wire submodule fixture"], worker_root)
    source_gitdir = gitdir_file.read_bytes()

    version = "0.2.0-gitdir-contract"
    output_root = tmp_path / "output"
    _run_checked(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(worker_root / "Build-Package.ps1"),
            "-Profile",
            "Bootstrap",
            "-Device",
            "CPU",
            "-Version",
            version,
            "-OutputRoot",
            str(output_root),
        ],
        worker_root,
        env={**os.environ, "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve())},
    )

    archives = list(output_root.glob("*.zip"))
    assert len(archives) == 1
    packages = _load_portable_packages()
    assert packages.audit_release_zip(archives[0])["valid"] is True
    with zipfile.ZipFile(archives[0]) as archive:
        git_entries = [
            name
            for name in archive.namelist()
            if ".git" in {part.casefold() for part in name.replace("\\", "/").split("/")}
        ]
        assert not git_entries
        archive.extractall(tmp_path / "extracted")
        package_root = tmp_path / "extracted" / archive.namelist()[0].split("/", 1)[0]
    assert packages.verify_sha256_manifest(package_root)["valid"] is True

    assert not (worker_root / "artifacts" / "portable" / ".work").exists()
    assert gitdir_file.read_bytes() == source_gitdir


def test_worker_bootstrap_removes_tracked_nested_t7_before_packaging(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("real worker Bootstrap cleanup test requires PowerShell")
    worker_root = tmp_path / "worker"
    _load_sync_integrations().sync_integration(REPO_ROOT, worker_root, "indextts", "a" * 40)
    tracked_weight = worker_root / "indextts" / "indextts" / "utils" / "JDC" / "bst.t7"
    tracked_weight.parent.mkdir(parents=True)
    tracked_weight.write_bytes(b"tracked model weight fixture")
    _initialize_git_repository(worker_root)

    version = "0.2.0-t7-contract"
    output_root = tmp_path / "output"
    _run_checked(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(worker_root / "Build-Package.ps1"),
            "-Profile",
            "Bootstrap",
            "-Device",
            "CPU",
            "-Version",
            version,
            "-OutputRoot",
            str(output_root),
        ],
        worker_root,
        env={**os.environ, "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve())},
    )

    archives = list(output_root.glob("*.zip"))
    assert len(archives) == 1
    packages = _load_portable_packages()
    assert packages.audit_release_zip(archives[0])["valid"] is True
    with zipfile.ZipFile(archives[0]) as archive:
        assert not [name for name in archive.namelist() if name.casefold().endswith(".t7")]
    assert not (worker_root / "artifacts" / "portable" / ".work").exists()
    assert tracked_weight.read_bytes() == b"tracked model weight fixture"


@pytest.mark.parametrize(
    "polluted_path",
    (
        "runtime/python.exe",
        "Runtime/tools/ffmpeg.exe",
        r"runtime\python.exe",
        "ｒｕｎｔｉｍｅ/python.exe",
        "runtime./python.exe",
        "data/user/reference.wav",
        "DATA/LOCAL/install-state.json",
        r"data\cache\asset.bin",
        "data/models/voice.onnx",
    ),
)
def test_release_audit_rejects_all_runtime_and_private_data_path_variants(
    tmp_path: Path, polluted_path: str
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / f"polluted-{len(list(tmp_path.iterdir()))}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "Component/package/tts-more-package.json",
            json.dumps({"schema_version": 2, "component": "tts-more", "package_profile": "bootstrap"}),
        )
        archive.writestr(f"Component/{polluted_path}", b"private")

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("forbidden release asset" in error for error in report["errors"])


@pytest.mark.parametrize(
    "entries",
    (
        ("package/tts-more-package.json", "runtime/runtime.zip"),
        ("package/tts-more-package.json", "data/user/customer.wav"),
        ("Component/package/tts-more-package.json", "Other/Start.cmd"),
        ("Component", "Component/package/tts-more-package.json"),
        (r"Component\package\tts-more-package.json",),
        ("Component/package/tts-more-package.json", "component/Start.cmd"),
        ("Component/package/tts-more-package.json", "Component./Start.cmd"),
        ("Component/package/tts-more-package.json", "Ｃomponent/Start.cmd"),
    ),
)
def test_release_audit_requires_one_safe_unambiguous_top_level_package_directory(
    tmp_path: Path, entries: tuple[str, ...]
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / f"unsafe-root-{len(list(tmp_path.iterdir()))}.zip"
    manifest = json.dumps(
        {"schema_version": 2, "component": "tts-more", "package_profile": "bootstrap"}
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name in entries:
            archive.writestr(name, manifest if name.replace("\\", "/").endswith("tts-more-package.json") else b"payload")
    for name in entries:
        if "\\" in name:
            normalized = name.replace("\\", "/").encode("utf-8")
            archive_path.write_bytes(archive_path.read_bytes().replace(normalized, name.encode("utf-8")))

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("top-level package directory" in error for error in report["errors"])


@pytest.mark.parametrize(
    "extra_directory",
    ("Other/", "component/", "Component. /", "Ｃomponent/"),
)
def test_release_audit_counts_raw_directory_entries_when_enforcing_one_package_root(
    tmp_path: Path, extra_directory: str
) -> None:
    packages = _load_portable_packages()
    archive_path = tmp_path / "extra-directory-root.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Component/", b"")
        archive.writestr("Component/package/", b"")
        archive.writestr(
            "Component/package/tts-more-package.json",
            json.dumps(
                {"schema_version": 2, "component": "tts-more", "package_profile": "bootstrap"}
            ),
        )
        archive.writestr(extra_directory, b"")

    report = packages.audit_release_zip(archive_path)

    assert report["valid"] is False
    assert any("top-level package directory" in error for error in report["errors"])


def test_tts_more_builder_uses_the_shared_zip64_writer() -> None:
    builder = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")

    assert "create-zip --package-root" in builder
    assert "Compress-Archive" not in builder
    assert '"$zip.spdx.json"' in builder
    assert '"$zip.licenses.json"' in builder
    assert '"$zip.acceptance.json"' in builder
    assert "^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$" in builder
    assert '"import-portable-data.py"' in builder
    assert '"import_portable_data.py"' in builder


def test_builders_pin_staging_without_delete_share_and_delete_root_by_handle() -> None:
    controller = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")
    worker = (REPO_ROOT / "integrations" / "windows" / "Build-Package.ps1").read_text(
        encoding="utf-8"
    )

    for builder in (controller, worker):
        assert "0x00010000" in builder  # DELETE access on the retained work handle
        assert "CreateDirectoryRelative($workBaseHandle, $workIdentity, $false)" in builder
        disposition = builder.index("MarkDirectoryForDeletion($createdWorkHandle)")
        close = builder.index("$createdWorkHandle.Dispose()", disposition)
        assert disposition < close
        assert "Remove-Item -LiteralPath $resolvedWork -Force" not in builder


def test_v2_builders_emit_completed_identity_protocol_and_data_contract() -> None:
    controller_builder = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")
    worker_builder = (REPO_ROOT / "integrations" / "windows" / "Build-Package.ps1").read_text(encoding="utf-8")

    assert 'package_id = "tts-more"; release_version = $Version' in controller_builder
    assert 'package_id = [string]$config.component; release_version = $Version' in worker_builder
    for builder in (controller_builder, worker_builder):
        assert (
            'protocol = @{ name = "tts-more-v1"; version = "1.0"; '
            'controller_range = ">=0.2.0,<0.3.0" }'
        ) in builder
        assert (
            'data = @{ user = "data/user"; local = "data/local"; cache = "data/cache"; '
            'operations = "data/local/operations" }'
        ) in builder


def test_portable_release_workflow_sanitizes_pr_ref_names() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(encoding="utf-8")

    assert "[^0-9A-Za-z._-]" in workflow
    assert "audit_release_zip" in workflow
    assert "EXPECTED_FULL_REJECTION" in workflow
    assert "verify-sha256 --package-root" in workflow
    assert workflow.index("audit-release --zip") < workflow.index("verify-sha256 --package-root")
    assert workflow.index("verify-sha256 --package-root") < workflow.index("actions/upload-artifact")


def test_tts_more_initializer_serializes_an_empty_controller_list_as_json() -> None:
    initializer = (REPO_ROOT / "scripts" / "initialize-portable.ps1").read_text(encoding="utf-8")

    assert "ConvertTo-Json -InputObject $videoControllers" in initializer


def test_four_pack_builder_is_full_only_and_refuses_github_actions() -> None:
    builder = (REPO_ROOT / "build-four-pack.ps1").read_text(encoding="utf-8")

    assert '$env:GITHUB_ACTIONS -eq "true"' in builder
    assert '-Profile Full' in builder
    assert '"tts-more", "gpt-sovits", "indextts", "cosyvoice"' in builder
    assert "source revision drift" in builder
    assert "compatibility-matrix.json" in builder
    assert "four-pack.provenance.json" in builder


@pytest.mark.parametrize(
    ("component", "profile", "expected"),
    [
        ("tts-more", "cpu", "TTS-More-0.2.0-windows-x64-full.zip"),
        ("gpt-sovits", "cu128", "gpt-sovits-0.2.0-windows-x64-full-cu128.zip"),
        ("indextts", "cu126", "indextts-0.2.0-windows-x64-full-cu126.zip"),
        ("cosyvoice", "cpu", "cosyvoice-0.2.0-windows-x64-full-cpu.zip"),
    ],
)
def test_full_package_name_uses_resolved_profile_only_for_workers(
    component: str, profile: str, expected: str
) -> None:
    packages = _load_portable_packages()

    assert packages.full_package_name(component, "0.2.0", profile) == expected


@pytest.mark.parametrize(
    ("component", "version", "profile"),
    [
        ("gpt-sovits", "0.2.0", "auto"),
        ("gpt-sovits", "0.2.0", ""),
        ("gpt-sovits", "0.2.0", "cuda"),
        ("unknown", "0.2.0", "cpu"),
        ("gpt-sovits", "", "cpu"),
        ("gpt-sovits", "../escape", "cpu"),
        ("gpt-sovits", r"0.2.0\\escape", "cpu"),
        ("gpt-sovits", "x" * 128, "cpu"),
    ],
)
def test_full_package_name_rejects_auto_unknown_and_unsafe_inputs(
    component: str, version: str, profile: str
) -> None:
    packages = _load_portable_packages()

    with pytest.raises(ValueError):
        packages.full_package_name(component, version, profile)


def _write_full_package_candidate(
    root: Path,
    *,
    component: str = "gpt-sovits",
    version: str = "0.2.0",
    package_profile: str = "full",
    resolved_profile: str = "cu128",
    provenance_sha: str | None = None,
    sidecar_sha: str | None = None,
    provenance_source: str | None = None,
    inner_sha_valid: bool = True,
) -> Path:
    packages = _load_portable_packages()
    filename = (
        packages.full_package_name(component, version, resolved_profile)
        if package_profile == "full"
        else f"{component}-{version}-windows-x64-bootstrap.zip"
    )
    archive_path = root / filename
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    package_root = filename.removesuffix(".zip")
    source_revision = "f8a5865000000000000000000000000000000000"
    build_id = f"{component}-{version}-{source_revision[:12]}"
    manifest = _valid_v2_manifest()
    manifest.update(
        {
            "component": component,
            "package_id": component,
            "version": version,
            "release_version": version,
            "package_profile": package_profile,
            "build_id": build_id,
        }
    )
    manifest["source"]["revision"] = source_revision
    manifest["runtime"]["device_profiles"] = [resolved_profile]
    install_state = {
        "schema_version": 1,
        "component": component,
        "build_id": build_id,
        "profile": resolved_profile,
        "runtime_lock_sha256": hashlib.sha256(b"fixture\n").hexdigest(),
        "model_lock_sha256": hashlib.sha256(b"fixture\n").hexdigest(),
        "ready": True,
        "completed_at": "2026-07-16T00:00:00+00:00",
    }
    entries = {
        "package/tts-more-package.json": json.dumps(manifest).encode("utf-8"),
        "data/local/install-state.json": json.dumps(install_state).encode("utf-8"),
    }
    for relative in (
        *manifest["launchers"].values(),
        manifest["runtime"]["lock"],
        manifest["models"]["lock"],
        manifest["licenses"],
    ):
        entries[str(relative)] = b"fixture\n"
    sums = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {relative}\n"
        for relative, payload in sorted(entries.items())
    )
    if not inner_sha_valid:
        sums = sums.replace(hashlib.sha256(entries["data/local/install-state.json"]).hexdigest(), "f" * 64)
    with zipfile.ZipFile(archive_path, "w", allowZip64=True) as archive:
        for relative, payload in entries.items():
            archive.writestr(f"{package_root}/{relative}", payload)
        archive.writestr(f"{package_root}/SHA256SUMS.txt", sums.encode("utf-8"))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (root / f"{filename}.sha256").write_text(
        f"{sidecar_sha or digest}  {filename}\n", encoding="ascii"
    )
    (root / f"{filename}.provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component": component,
                "version": version,
                "profile": package_profile,
                "resolved_profile": resolved_profile,
                "sha256": provenance_sha or digest,
                "source_revision": provenance_source or source_revision,
            }
        ),
        encoding="utf-8",
    )
    resolved_binding = resolved_profile if package_profile == "full" else "none"
    delivery = {
        "component": component,
        "version": version,
        "profile": package_profile,
        "source_revision": source_revision,
        "sha256": digest,
    }
    if package_profile == "full":
        delivery["resolved_profile"] = resolved_profile
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": package_root,
        "documentNamespace": f"https://tts-more.local/spdx/{component}/{version}/{digest}",
        "comment": (
            "TTS-More delivery binding: "
            f"component={component};version={version};profile={package_profile};"
            f"resolved_profile={resolved_binding};source_revision={source_revision};sha256={digest}"
        ),
        "creationInfo": {"created": "2026-07-16T00:00:00Z", "creators": ["Tool: fixture"]},
        "packages": [],
    }
    (root / f"{filename}.spdx.json").write_text(json.dumps(spdx), encoding="utf-8")
    (root / f"{filename}.licenses.json").write_text(
        json.dumps({"schema_version": 1, "component": component, "packages": [], "delivery": delivery}),
        encoding="utf-8",
    )
    acceptance = {
        "schema_version": 1,
        **delivery,
        "manifest_valid": True,
        "schema_audit": True,
        "path_audit": True,
        "sha256_manifest_audit": True,
        "bootstrap_audit": package_profile == "bootstrap",
        "machine_path_scan": True,
    }
    (root / f"{filename}.acceptance.json").write_text(
        json.dumps(acceptance), encoding="utf-8"
    )
    return archive_path


def _rewrite_full_candidate_archive_root(archive_path: Path, archive_root: str) -> None:
    temporary = archive_path.with_suffix(".rewritten.zip")
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as destination:
        for entry in source.infolist():
            _old_root, relative = entry.filename.split("/", 1)
            destination.writestr(f"{archive_root}/{relative}", source.read(entry))
    temporary.replace(archive_path)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    Path(f"{archive_path}.sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="ascii"
    )
    provenance_path = Path(f"{archive_path}.provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["sha256"] = digest
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")


def _rewrite_full_candidate_entries(archive_path: Path, mutate) -> None:
    temporary = archive_path.with_suffix(".rewritten.zip")
    with zipfile.ZipFile(archive_path) as source:
        root = source.namelist()[0].split("/", 1)[0]
        entries = {
            entry.filename.split("/", 1)[1]: source.read(entry)
            for entry in source.infolist()
            if not entry.is_dir() and not entry.filename.endswith("/SHA256SUMS.txt")
        }
    mutate(entries)
    sums = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {relative}\n"
        for relative, payload in sorted(entries.items())
    )
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as destination:
        for relative, payload in entries.items():
            destination.writestr(f"{root}/{relative}", payload)
        destination.writestr(f"{root}/SHA256SUMS.txt", sums.encode("utf-8"))
    temporary.replace(archive_path)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    Path(f"{archive_path}.sha256").write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    provenance_path = Path(f"{archive_path}.provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["sha256"] = digest
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")


def test_select_full_package_reads_manifest_state_and_verifies_sha_sidecars(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    archive_path = _write_full_package_candidate(tmp_path)

    report = packages.select_full_package(
        [archive_path],
        expected_component="gpt-sovits",
        expected_version="0.2.0",
        requested_profile="auto",
        expected_source_revision="f8a5865000000000000000000000000000000000",
    )

    assert report["valid"] is True
    assert report["resolved_profile"] == "cu128"
    assert report["filename"] == archive_path.name


@pytest.mark.parametrize(
    "archive_root_variant",
    [
        lambda expected: expected + ".",
        lambda expected: expected + " ",
        lambda expected: expected.replace("g", "ｇ", 1),
        lambda expected: expected.replace("g", "G", 1),
    ],
    ids=("trailing-dot", "trailing-space", "nfkc-lookalike", "case-variant"),
)
def test_select_full_package_rejects_raw_archive_root_aliases(
    tmp_path: Path, archive_root_variant
) -> None:
    packages = _load_portable_packages()
    archive_path = _write_full_package_candidate(tmp_path)
    expected_root = archive_path.name.removesuffix(".zip")
    _rewrite_full_candidate_archive_root(
        archive_path, archive_root_variant(expected_root)
    )

    report = packages.select_full_package(
        [archive_path],
        expected_component="gpt-sovits",
        expected_version="0.2.0",
        requested_profile="auto",
        expected_source_revision="f8a5865000000000000000000000000000000000",
    )

    assert report["valid"] is False
    assert any("archive root" in error for error in report["errors"])


@pytest.mark.parametrize(
    "failure",
    ("schema", "launcher", "runtime-lock", "model-lock", "license", "lock-digest"),
)
def test_select_full_package_requires_complete_schema_files_and_lock_binding(
    tmp_path: Path, failure: str
) -> None:
    packages = _load_portable_packages()
    archive_path = _write_full_package_candidate(tmp_path)

    def mutate(entries: dict[str, bytes]) -> None:
        manifest = json.loads(entries["package/tts-more-package.json"])
        if failure == "schema":
            manifest.pop("endpoint")
        elif failure == "launcher":
            entries.pop(manifest["launchers"]["start"])
        elif failure == "runtime-lock":
            entries.pop(manifest["runtime"]["lock"])
        elif failure == "model-lock":
            entries.pop(manifest["models"]["lock"])
        elif failure == "license":
            entries.pop(manifest["licenses"])
        elif failure == "lock-digest":
            state = json.loads(entries[manifest["runtime"]["state_path"]])
            state["runtime_lock_sha256"] = "0" * 64
            entries[manifest["runtime"]["state_path"]] = json.dumps(state).encode()
        entries["package/tts-more-package.json"] = json.dumps(manifest).encode()

    _rewrite_full_candidate_entries(archive_path, mutate)
    report = packages.select_full_package(
        [archive_path],
        expected_component="gpt-sovits",
        expected_version="0.2.0",
        requested_profile="auto",
        expected_source_revision="f8a5865000000000000000000000000000000000",
    )

    assert report["valid"] is False
    assert report["errors"]


def test_select_full_package_binds_expected_source_revision(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    archive_path = _write_full_package_candidate(tmp_path)

    report = packages.select_full_package(
        [archive_path],
        expected_component="gpt-sovits",
        expected_version="0.2.0",
        requested_profile="auto",
        expected_source_revision="b" * 40,
    )

    assert report["valid"] is False
    assert any("source revision" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [
        (
            "spdx.json",
            {
                "spdxVersion": "SPDX-2.3",
                "dataLicense": "CC0-1.0",
                "SPDXID": "SPDXRef-DOCUMENT",
                "name": "foreign-package",
                "documentNamespace": "not-a-valid-namespace",
                "comment": "wrong delivery binding",
                "creationInfo": {"created": "2026-07-16T00:00:00Z", "creators": ["Tool: fixture"]},
                "packages": [],
            },
        ),
        (
            "licenses.json",
            {
                "schema_version": 1,
                "component": "gpt-sovits",
                "packages": [],
                "delivery": {
                    "component": "foreign",
                    "version": "9.9.9",
                    "profile": "bootstrap",
                    "source_revision": "b" * 40,
                    "sha256": "0" * 64,
                },
            },
        ),
        (
            "acceptance.json",
            {
                "schema_version": 1,
                "component": "gpt-sovits",
                "version": "0.2.0",
                "profile": "full",
                "source_revision": "f8a5865000000000000000000000000000000000",
                "sha256": "0" * 64,
                "manifest_valid": True,
                "schema_audit": False,
                "path_audit": True,
                "sha256_manifest_audit": True,
                "machine_path_scan": True,
            },
        ),
    ],
)
def test_select_full_package_semantically_binds_all_three_json_sidecars(
    tmp_path: Path, suffix: str, payload: dict[str, object]
) -> None:
    packages = _load_portable_packages()
    archive_path = _write_full_package_candidate(tmp_path)
    Path(f"{archive_path}.{suffix}").write_text(json.dumps(payload), encoding="utf-8")

    report = packages.select_full_package(
        [archive_path],
        expected_component="gpt-sovits",
        expected_version="0.2.0",
        requested_profile="auto",
        expected_source_revision="f8a5865000000000000000000000000000000000",
    )

    assert report["valid"] is False
    assert suffix.split(".", 1)[0] in " ".join(report["errors"]).lower()


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "foreign",
        "bootstrap",
        "requested",
        "sidecar",
        "provenance",
        "source",
        "inner",
    ],
)
def test_select_full_package_rejects_ambiguous_or_mislabelled_candidates(
    tmp_path: Path, failure: str
) -> None:
    packages = _load_portable_packages()
    archive_path = _write_full_package_candidate(
        tmp_path,
        package_profile="bootstrap" if failure == "bootstrap" else "full",
        sidecar_sha="0" * 64 if failure == "sidecar" else None,
        provenance_sha="1" * 64 if failure == "provenance" else None,
        provenance_source="b" * 40 if failure == "source" else None,
        inner_sha_valid=failure != "inner",
    )
    candidates = [archive_path]
    expected_component = "indextts" if failure == "foreign" else "gpt-sovits"
    requested_profile = "cpu" if failure == "requested" else "auto"
    if failure == "duplicate":
        duplicate_root = tmp_path / "duplicate"
        duplicate_root.mkdir()
        duplicate = duplicate_root / archive_path.name
        for source in (
            archive_path,
            Path(f"{archive_path}.sha256"),
            Path(f"{archive_path}.provenance.json"),
            Path(f"{archive_path}.spdx.json"),
            Path(f"{archive_path}.licenses.json"),
            Path(f"{archive_path}.acceptance.json"),
        ):
            shutil.copy2(source, duplicate_root / source.name)
        candidates.append(duplicate)

    report = packages.select_full_package(
        candidates,
        expected_component=expected_component,
        expected_version="0.2.0",
        requested_profile=requested_profile,
        expected_source_revision="f8a5865000000000000000000000000000000000",
    )

    assert report["valid"] is False
    assert report["errors"]


def test_builders_delegate_full_naming_to_python_after_profile_resolution() -> None:
    controller = (REPO_ROOT / "Build-Package.ps1").read_text(encoding="utf-8")
    worker = (REPO_ROOT / "integrations" / "windows" / "Build-Package.ps1").read_text(
        encoding="utf-8"
    )

    for builder in (controller, worker):
        assert "full-package-name" in builder
        assert "portable_packages.py" in builder
    assert "--resolved-profile cpu" in controller
    assert "Assert-TtsMoreFullPayloadBoundary" in controller
    assert worker.index("Initialize.ps1") < worker.index("full-package-name")
    assert 'install-state.json' in worker and "resolve-full-profile" in worker
    assert "requested device profile does not match resolved profile" in worker
    assert "--archive-root $packageName" in worker
    assert "Rename-Item" not in worker[worker.index("Initialize.ps1") : worker.index("create-zip")]


@pytest.mark.parametrize(
    ("mutation", "requested"),
    [
        ({"profile": "cu126"}, "auto"),
        ({"profile": "cu128"}, "cpu"),
        ({"drop": "file"}, "auto"),
        ({"raw": "{"}, "auto"),
        ({"ready": False}, "auto"),
        ({"profile": "auto"}, "auto"),
        ({"profile": "cuda"}, "auto"),
        ({"component": "indextts"}, "auto"),
        ({"build_id": "wrong-build"}, "auto"),
    ],
)
def test_resolve_full_profile_requires_completed_matching_install_state(
    tmp_path: Path, mutation: dict[str, object], requested: str
) -> None:
    packages = _load_portable_packages()
    state = {
        "schema_version": 1,
        "component": "gpt-sovits",
        "build_id": "gpt-sovits-0.2.0-aaaaaaaaaaaa",
        "profile": "cpu",
        "runtime_lock_sha256": "c" * 64,
        "model_lock_sha256": "d" * 64,
        "ready": True,
        "completed_at": "2026-07-16T00:00:00+00:00",
    }
    state_path = tmp_path / "install-state.json"
    if "raw" in mutation:
        state_path.write_text(str(mutation["raw"]), encoding="utf-8")
    elif mutation.get("drop") == "file":
        pass
    else:
        state.update(mutation)
        state_path.write_text(json.dumps(state), encoding="utf-8")

    if mutation == {"profile": "cu126"}:
        assert (
            packages.resolve_full_profile(
                state_path,
                expected_component="gpt-sovits",
                expected_build_id="gpt-sovits-0.2.0-aaaaaaaaaaaa",
                requested_profile=requested,
            )
            == "cu126"
        )
    else:
        with pytest.raises(ValueError):
            packages.resolve_full_profile(
                state_path,
                expected_component="gpt-sovits",
                expected_build_id="gpt-sovits-0.2.0-aaaaaaaaaaaa",
                requested_profile=requested,
            )


def _write_plan_only_four_pack_fixture(root: Path) -> tuple[Path, dict[str, Path]]:
    root.mkdir()
    shutil.copy2(REPO_ROOT / "build-four-pack.ps1", root / "build-four-pack.ps1")
    (root / "Build-Package.ps1").write_text(
        "throw 'PlanOnly invoked controller build'\n", encoding="utf-8"
    )
    workers: dict[str, Path] = {}
    repositories: list[dict[str, str]] = []
    for component, name in (
        ("gpt-sovits", "GPT-SoVITS-main"),
        ("indextts", "index-tts"),
        ("cosyvoice", "CosyVoice"),
    ):
        worker = root.parent / f"{root.name}-{component}"
        worker.mkdir()
        (worker / "Build-Package.ps1").write_text(
            "throw 'PlanOnly invoked worker build'\n", encoding="utf-8"
        )
        _initialize_git_repository(worker)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worker, text=True, encoding="utf-8"
        ).strip()
        repositories.append(
            {
                "name": name,
                "provider_type": component,
                "path": f"repo/{name}",
                "commit": revision,
            }
        )
        workers[component] = worker
    (root / "repo.lock.json").write_text(
        json.dumps({"repositories": repositories}), encoding="utf-8"
    )
    _initialize_git_repository(root)
    return root, workers


def test_four_pack_plan_only_records_device_intentions_without_machine_paths(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack PlanOnly contract requires PowerShell")
    fixture, workers = _write_plan_only_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "output"

    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(fixture / "build-four-pack.ps1"),
            "-Device",
            "CU126",
            "-Version",
            "0.2.0-plancheck",
            "-OutputRoot",
            str(output),
            "-GptRoot",
            str(workers["gpt-sovits"]),
            "-IndexRoot",
            str(workers["indextts"]),
            "-CosyVoiceRoot",
            str(workers["cosyvoice"]),
            "-PlanOnly",
        ],
        cwd=fixture,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    plan = json.loads(completed.stdout[completed.stdout.index("{") :])
    assert [(item["component"], item["device"]) for item in plan["components"]] == [
        ("tts-more", "cpu"),
        ("gpt-sovits", "cu126"),
        ("indextts", "cu126"),
        ("cosyvoice", "cu126"),
    ]
    controller_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=fixture, text=True, encoding="utf-8"
    ).strip()
    assert plan["components"][0]["expected_revision"] == controller_revision
    assert re.fullmatch(r"[0-9a-f]{40}", controller_revision)
    serialized = json.dumps(plan)
    assert str(fixture) not in serialized
    assert str(output) not in serialized
    assert not output.exists()
    assert not list(fixture.rglob("planonly-invoked"))


def test_four_pack_rejects_unsafe_version_before_resolving_or_writing(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack validation contract requires PowerShell")
    fixture = tmp_path / "four-pack"
    fixture.mkdir()
    shutil.copy2(REPO_ROOT / "build-four-pack.ps1", fixture / "build-four-pack.ps1")
    output = tmp_path / "must-not-exist"

    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(fixture / "build-four-pack.ps1"),
            "-Version",
            "../escape",
            "-OutputRoot",
            str(output),
            "-PlanOnly",
        ],
        cwd=fixture,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode != 0
    assert "version" in (completed.stdout + completed.stderr).lower()
    assert not output.exists()


def test_four_pack_discovers_changed_zips_by_manifest_not_filename_glob() -> None:
    builder = (REPO_ROOT / "build-four-pack.ps1").read_text(encoding="utf-8")

    assert 'device="CPU"' in builder
    assert 'device=$Device' in builder
    assert "select-full-package" in builder
    assert 'Get-ChildItem -LiteralPath $transactionRoot -Filter "*.zip"' in builder
    assert "Get-FileHash" in builder
    assert 'Filter "*-$Version-windows-x64-full.zip"' not in builder
    assert "did not produce exactly one changed full ZIP" in builder
    assert "transaction" in builder.lower()
    assert builder.index("compatibility-matrix.json") < builder.index("publish-directory")


def _write_fake_full_package_maker(root: Path) -> Path:
    maker = root / "make-full-package.py"
    maker.write_text(
        r'''from __future__ import annotations
import argparse, hashlib, json, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from portable_packages import create_zip, full_package_name

parser = argparse.ArgumentParser()
parser.add_argument("--component", required=True)
parser.add_argument("--device", required=True)
parser.add_argument("--version", required=True)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--source-revision", required=True)
parser.add_argument("--mode", default="ok")
args = parser.parse_args()
if args.mode == "stale":
    raise SystemExit(0)
source_revision = "b" * 40 if args.mode == "wrong-source" else args.source_revision
resolved = "cpu" if args.component == "tts-more" else ("cu126" if args.device.casefold() == "auto" else args.device.casefold())
filename = full_package_name(args.component, args.version, resolved)
stage = args.output / f".fixture-stage-{args.component}"
stage.mkdir(parents=True, exist_ok=False)
build_id = f"{args.component}-{args.version}-{source_revision[:12]}"
port = {"tts-more": 8000, "gpt-sovits": 9880, "indextts": 9881, "cosyvoice": 9882}[args.component]
manifest = {
    "schema_version": 2, "component": args.component, "package_id": args.component,
    "release_version": args.version, "version": args.version, "build_id": build_id,
    "package_profile": "full", "platform": "windows-x64", "api_contract": "tts-more-v1",
    "source": {"repository": "https://example.invalid/source", "revision": source_revision},
    "integration": {"version": "2.0.0", "source_revision": "d" * 40, "bundle_sha256": "a" * 64},
    "runtime": {"python_version": "3.11", "device_profiles": [resolved], "lock": "locks/runtime.json", "state_path": "data/local/install-state.json"},
    "models": {"lock": "locks/models.json", "required": args.component != "tts-more"},
    "data_root": "data/local", "protocol": {"name": "tts-more-v1", "version": "1.0", "controller_range": ">=0.2.0,<0.3.0"},
    "data": {"user": "data/user", "local": "data/local", "cache": "data/cache", "operations": "data/local/operations"},
    "launchers": {"initialize": "Initialize.cmd", "start": "Start.cmd", "stop": "Stop.cmd", "repair": "Repair.cmd", "build": "Build-Package.ps1"},
    "endpoint": {"default_url": f"http://127.0.0.1:{port}", "port": port, "health_path": "/health", "capabilities_path": "/capabilities", "bind_policy": "loopback"},
    "capabilities": ["tts"], "sha256_manifest": "SHA256SUMS.txt", "licenses": "licenses/THIRD-PARTY-NOTICES.json",
}
state = {"schema_version": 1, "component": args.component, "build_id": build_id, "profile": resolved, "runtime_lock_sha256": "", "model_lock_sha256": "", "ready": True, "completed_at": "2026-07-16T00:00:00+00:00"}
files = {"package/tts-more-package.json": json.dumps(manifest).encode()}
for relative in (*manifest["launchers"].values(), manifest["runtime"]["lock"], manifest["models"]["lock"], manifest["licenses"]):
    files[str(relative)] = b"fixture\n"
state["runtime_lock_sha256"] = hashlib.sha256(files[manifest["runtime"]["lock"]]).hexdigest()
state["model_lock_sha256"] = hashlib.sha256(files[manifest["models"]["lock"]]).hexdigest()
files[manifest["runtime"]["state_path"]] = json.dumps(state).encode()
for relative, payload in files.items():
    path = stage / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
sums = "".join(f"{hashlib.sha256(payload).hexdigest()}  {relative}\n" for relative, payload in sorted(files.items()))
(stage / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
archive = args.output / filename
create_zip(stage, archive, filename.removesuffix(".zip"))
shutil.rmtree(stage)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
Path(f"{archive}.sha256").write_text(f"{digest}  {filename}\n", encoding="ascii")
Path(f"{archive}.provenance.json").write_text(json.dumps({"schema_version": 1, "component": args.component, "version": args.version, "profile": "full", "resolved_profile": resolved, "source_revision": source_revision, "sha256": digest}), encoding="utf-8")
delivery = {"component": args.component, "version": args.version, "profile": "full", "resolved_profile": resolved, "source_revision": source_revision, "sha256": digest}
spdx = {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT", "name": filename.removesuffix(".zip"), "documentNamespace": f"https://tts-more.local/spdx/{args.component}/{args.version}/{digest}", "comment": f"TTS-More delivery binding: component={args.component};version={args.version};profile=full;resolved_profile={resolved};source_revision={source_revision};sha256={digest}", "creationInfo": {"created": "2026-07-16T00:00:00Z", "creators": ["Tool: fixture"]}, "packages": []}
Path(f"{archive}.spdx.json").write_text(json.dumps(spdx), encoding="utf-8")
Path(f"{archive}.licenses.json").write_text(json.dumps({"schema_version": 1, "component": args.component, "packages": [], "delivery": delivery}), encoding="utf-8")
Path(f"{archive}.acceptance.json").write_text(json.dumps({"schema_version": 1, **delivery, "manifest_valid": True, "schema_audit": True, "path_audit": True, "sha256_manifest_audit": True, "bootstrap_audit": False, "machine_path_scan": True}), encoding="utf-8")
if args.mode == "duplicate":
    duplicate = args.output / f"duplicate-{args.component}.zip"
    shutil.copy2(archive, duplicate)
    for suffix in ("sha256", "provenance.json", "spdx.json", "licenses.json", "acceptance.json"):
        shutil.copy2(Path(f"{archive}.{suffix}"), Path(f"{duplicate}.{suffix}"))
if args.mode in {"mutate-earlier", "delete-earlier"}:
    earlier = next(args.output.glob("TTS-More-*.zip"))
    if args.mode == "mutate-earlier":
        earlier.write_bytes(earlier.read_bytes() + b"late mutation")
    else:
        earlier.unlink()
if args.mode == "inject":
    (args.output / "unexpected-injected.txt").write_text("injected", encoding="utf-8")
if args.mode == "publish-collision":
    collision = Path(__import__("os").environ["TTS_MORE_FIXTURE_FINAL_OUTPUT"])
    collision.mkdir(parents=True)
    (collision / "sentinel.txt").write_text("collision", encoding="utf-8")
if args.mode == "replace-transaction":
    owned = args.output.with_name(args.output.name + "-owned")
    args.output.rename(owned)
    args.output.mkdir()
    (args.output / "replacement-sentinel.txt").write_text("replacement", encoding="utf-8")
''',
        encoding="utf-8",
    )
    return maker


def _write_fake_full_builder(root: Path, component: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Build-Package.ps1").write_text(
        f'''param([string]$Profile, [string]$Device, [string]$Version, [string]$OutputRoot)
"{component}:$Device" | Add-Content -LiteralPath $env:TTS_MORE_FIXTURE_LOG -Encoding UTF8
if ($env:TTS_MORE_FIXTURE_FAIL_COMPONENT -eq "{component}") {{ throw "fixture component failure" }}
$mode = if ($env:TTS_MORE_FIXTURE_MODE_COMPONENT -eq "{component}") {{ $env:TTS_MORE_FIXTURE_MODE }} else {{ "ok" }}
$revision = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".git")) {{ (& git -C $PSScriptRoot rev-parse HEAD).Trim() }} else {{ "e" * 40 }}
& $env:TTS_MORE_BUILD_PYTHON $env:TTS_MORE_FIXTURE_MAKER --component "{component}" --device $Device --version $Version --output $OutputRoot --source-revision $revision --mode $mode
if ($LASTEXITCODE -ne 0) {{ throw "fixture package maker failed" }}
''',
        encoding="utf-8",
    )


def _write_executable_four_pack_fixture(root: Path) -> tuple[Path, dict[str, Path]]:
    root.mkdir()
    shutil.copy2(REPO_ROOT / "build-four-pack.ps1", root / "build-four-pack.ps1")
    shutil.copytree(REPO_ROOT / "scripts", root / "scripts")
    schema_target = root / "packaging" / "portable" / "tts-more-package.schema.json"
    schema_target.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "packaging" / "portable" / "tts-more-package.schema.json", schema_target)
    _write_fake_full_builder(root, "tts-more")
    workers: dict[str, Path] = {}
    repositories: list[dict[str, str]] = []
    for component, name in (("gpt-sovits", "GPT-SoVITS-main"), ("indextts", "index-tts"), ("cosyvoice", "CosyVoice")):
        worker = root.parent / f"worker-{component}"
        _write_fake_full_builder(worker, component)
        _initialize_git_repository(worker)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worker, text=True, encoding="utf-8").strip()
        repositories.append({"name": name, "provider_type": component, "path": f"repo/{name}", "commit": revision})
        workers[component] = worker
    (root / "repo.lock.json").write_text(json.dumps({"repositories": repositories}), encoding="utf-8")
    _write_fake_full_package_maker(root)
    _initialize_git_repository(root)
    return root, workers


def _run_executable_four_pack_fixture(
    fixture: Path,
    workers: dict[str, Path],
    output: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    log = fixture / "routing.log"
    env = {
        **os.environ,
        "TTS_MORE_BUILD_PYTHON": str(Path(sys.executable).resolve()),
        "TTS_MORE_FIXTURE_MAKER": str(fixture / "make-full-package.py"),
        "TTS_MORE_FIXTURE_LOG": str(log),
        "TTS_MORE_FIXTURE_FINAL_OUTPUT": str(output),
        **(extra_env or {}),
    }
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(fixture / "build-four-pack.ps1"), "-Device", "CU126", "-Version", "0.2.0-test", "-OutputRoot", str(output), "-GptRoot", str(workers["gpt-sovits"]), "-IndexRoot", str(workers["indextts"]), "-CosyVoiceRoot", str(workers["cosyvoice"])],
        cwd=fixture,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_four_pack_fake_build_routes_cpu_and_publishes_only_after_all_verified(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "published"

    completed = _run_executable_four_pack_fixture(fixture, workers, output)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (fixture / "routing.log").read_text(encoding="utf-8-sig").splitlines() == [
        "tts-more:CPU", "gpt-sovits:CU126", "indextts:CU126", "cosyvoice:CU126"
    ]
    assert len(list(output.glob("*.zip"))) == 4
    assert (output / "TTS-More-0.2.0-test-windows-x64-full.zip").is_file()
    assert len(list(output.glob("*-full-cu126.zip"))) == 3
    assert (output / "compatibility-matrix.json").is_file()
    assert (output / "four-pack.provenance.json").is_file()


@pytest.mark.parametrize(("mode_component", "mode"), [("gpt-sovits", "duplicate"), ("gpt-sovits", "stale")])
def test_four_pack_fake_build_rejects_duplicate_or_stale_without_publishing(
    tmp_path: Path, mode_component: str, mode: str
) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "published"

    completed = _run_executable_four_pack_fixture(
        fixture,
        workers,
        output,
        extra_env={"TTS_MORE_FIXTURE_MODE_COMPONENT": mode_component, "TTS_MORE_FIXTURE_MODE": mode},
    )

    assert completed.returncode != 0
    assert not output.exists()
    assert not list(tmp_path.glob(".tts-more-four-pack-transaction-*"))


def test_four_pack_component_failure_leaves_final_output_untouched(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "published"
    previous = tmp_path / "previous-version"
    previous.mkdir()
    sentinel = previous / "delivery.zip"
    sentinel.write_bytes(b"previous delivery must remain byte-identical")
    sentinel_sha = hashlib.sha256(sentinel.read_bytes()).hexdigest()

    completed = _run_executable_four_pack_fixture(
        fixture,
        workers,
        output,
        extra_env={"TTS_MORE_FIXTURE_FAIL_COMPONENT": "gpt-sovits"},
    )

    assert completed.returncode != 0
    assert not output.exists()
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == sentinel_sha
    assert list(previous.iterdir()) == [sentinel]
    assert not list(tmp_path.glob(".tts-more-four-pack-transaction-*"))
    assert (fixture / "routing.log").read_text(encoding="utf-8-sig").splitlines() == [
        "tts-more:CPU", "gpt-sovits:CU126"
    ]


def test_four_pack_rejects_package_source_revision_not_bound_to_repo_lock(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "published"

    completed = _run_executable_four_pack_fixture(
        fixture,
        workers,
        output,
        extra_env={"TTS_MORE_FIXTURE_MODE_COMPONENT": "gpt-sovits", "TTS_MORE_FIXTURE_MODE": "wrong-source"},
    )

    assert completed.returncode != 0
    assert not output.exists()


def test_four_pack_rejects_untracked_worker_source_that_builder_would_copy(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    (workers["gpt-sovits"] / "copied-untracked.py").write_text("DIRTY = True\n", encoding="utf-8")
    output = tmp_path / "published"

    completed = _run_executable_four_pack_fixture(fixture, workers, output)

    assert completed.returncode != 0
    assert "dirty" in (completed.stdout + completed.stderr).lower()
    assert not output.exists()


@pytest.mark.parametrize(
    ("profile", "relative"),
    (("bootstrap", "models/private.txt"), ("full", "pretrained_models/extra.dat")),
)
def test_builder_source_audit_rejects_unlocked_files_in_model_named_paths(
    tmp_path: Path, profile: str, relative: str
) -> None:
    packages = _load_portable_packages()
    root = tmp_path / "worker"
    lock = root / "tts_more" / "locks" / "models.lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps({"schema_version": 1, "component": "gpt-sovits", "assets": []}),
        encoding="utf-8",
    )
    _initialize_git_repository(root)
    extra = root / relative
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("not a locked model asset\n", encoding="utf-8")

    report = packages.audit_builder_source(root, component="gpt-sovits", profile=profile)

    assert report["valid"] is False
    assert relative in " ".join(report["errors"])


def test_builder_source_audit_allows_only_exact_locked_full_model_payloads(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    root = tmp_path / "worker"
    payload = b"locked model payload\n"
    target = "pretrained_models/model.bin"
    lock = root / "tts_more" / "locks" / "models.lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component": "gpt-sovits",
                "assets": [
                    {"target": target, "sha256": hashlib.sha256(payload).hexdigest()}
                ],
            }
        ),
        encoding="utf-8",
    )
    _initialize_git_repository(root)
    model = root / target
    model.parent.mkdir(parents=True)
    model.write_bytes(payload)
    for relative in (
        "runtime/live/python.exe",
        "data/user/voice.wav",
        "cache/download.bin",
        "artifacts/old.zip",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ignored local asset")

    report = packages.audit_builder_source(root, component="gpt-sovits", profile="full")

    assert report["valid"] is True, report["errors"]


def test_builder_source_audit_applies_nested_cache_and_env_exclusions_per_file(
    tmp_path: Path,
) -> None:
    packages = _load_portable_packages()
    root = tmp_path / "worker"
    lock = root / "tts_more" / "locks" / "models.lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps({"schema_version": 1, "component": "gpt-sovits", "assets": []}),
        encoding="utf-8",
    )
    _initialize_git_repository(root)
    for relative in ("scratch/cache/only.tmp", "scratch/config/.env.local"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("builder excluded\n", encoding="utf-8")

    report = packages.audit_builder_source(root, component="gpt-sovits", profile="full")

    assert report["valid"] is True, report["errors"]

    ordinary = root / "scratch" / "ordinary.txt"
    ordinary.write_text("builder copies this\n", encoding="utf-8")
    report = packages.audit_builder_source(root, component="gpt-sovits", profile="full")

    assert report["valid"] is False
    assert "scratch/ordinary.txt" in " ".join(report["errors"])


@pytest.mark.parametrize("mode", ("mutate-earlier", "delete-earlier", "inject"))
def test_four_pack_revalidates_final_asset_set_before_publication(tmp_path: Path, mode: str) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "published"

    completed = _run_executable_four_pack_fixture(
        fixture,
        workers,
        output,
        extra_env={"TTS_MORE_FIXTURE_MODE_COMPONENT": "cosyvoice", "TTS_MORE_FIXTURE_MODE": mode},
    )

    assert completed.returncode != 0
    assert not output.exists()


def test_four_pack_rejects_worker_nested_output_root(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = workers["gpt-sovits"] / "nested-delivery"

    completed = _run_executable_four_pack_fixture(fixture, workers, output)

    assert completed.returncode != 0
    assert "source root" in (completed.stdout + completed.stderr).lower()
    assert not output.exists()


def test_four_pack_publication_collision_is_no_replace_and_preserves_destination(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "published"

    completed = _run_executable_four_pack_fixture(
        fixture,
        workers,
        output,
        extra_env={"TTS_MORE_FIXTURE_MODE_COMPONENT": "cosyvoice", "TTS_MORE_FIXTURE_MODE": "publish-collision"},
    )

    assert completed.returncode != 0
    assert (output / "sentinel.txt").read_text(encoding="utf-8") == "collision"
    assert list(output.iterdir()) == [output / "sentinel.txt"]


def test_four_pack_rejects_reparse_output_parent(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    real_parent = tmp_path / "real-output-parent"
    real_parent.mkdir()
    junction = tmp_path / "output-parent-junction"
    _create_windows_junction(junction, real_parent)
    output = junction / "published"

    completed = _run_executable_four_pack_fixture(fixture, workers, output)

    assert completed.returncode != 0
    assert "reparse" in (completed.stdout + completed.stderr).lower()
    assert not output.exists()


def test_four_pack_cleanup_refuses_replaced_transaction_identity(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("four-pack execution contract requires PowerShell")
    fixture, workers = _write_executable_four_pack_fixture(tmp_path / "four-pack")
    output = tmp_path / "published"

    completed = _run_executable_four_pack_fixture(
        fixture,
        workers,
        output,
        extra_env={"TTS_MORE_FIXTURE_MODE_COMPONENT": "cosyvoice", "TTS_MORE_FIXTURE_MODE": "replace-transaction"},
    )

    assert completed.returncode != 0
    replacements = list(tmp_path.glob(".tts-more-four-pack-transaction-*/replacement-sentinel.txt"))
    assert len(replacements) == 1
    assert replacements[0].read_text(encoding="utf-8") == "replacement"
    assert not output.exists()


def test_portable_release_workflow_audits_every_zip_and_uses_exact_asset_allowlist() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(
        encoding="utf-8"
    )

    assert "foreach ($candidateZip in $candidateZips)" in workflow
    assert "audit-release --zip $candidateZip.FullName" in workflow
    assert "package_profile" in workflow and "audit-release-assets --directory" in workflow
    assert "for candidate_zip in release-assets/*.zip" in workflow
    assert "audit-release --zip \"$candidate_zip\"" in workflow
    assert "release-assets/* --clobber" not in workflow
    upload_block = workflow.split("uses: actions/upload-artifact@v7", 1)[1].split(
        "github-release:", 1
    )[0]
    expected = {
        "artifacts/portable/bootstrap/*.zip",
        "artifacts/portable/bootstrap/*.sha256",
        "artifacts/portable/bootstrap/*.spdx.json",
        "artifacts/portable/bootstrap/*.licenses.json",
        "artifacts/portable/bootstrap/*.provenance.json",
        "artifacts/portable/bootstrap/*.acceptance.json",
    }
    actual = {
        line.strip()
        for line in upload_block.splitlines()
        if line.strip().startswith("artifacts/portable/bootstrap/")
    }
    assert actual == expected


def test_release_asset_set_audits_each_zip_and_rejects_filename_manifest_spoof(
    clean_portable_builds: dict[str, object], tmp_path: Path
) -> None:
    packages = _load_portable_packages()
    source_zip = Path(clean_portable_builds["controller_zip"])
    release = tmp_path / "release"
    release.mkdir()
    for source in (
        source_zip,
        Path(f"{source_zip}.sha256"),
        Path(f"{source_zip}.spdx.json"),
        Path(f"{source_zip}.licenses.json"),
        Path(f"{source_zip}.provenance.json"),
        Path(f"{source_zip}.acceptance.json"),
    ):
        shutil.copy2(source, release / source.name)

    audit = packages.audit_release_assets(
        release,
        expected_component="tts-more",
        expected_version="0.2.0-clean-controller",
    )
    assert audit["valid"] is True, audit["errors"]

    renamed = release / "TTS-More-9.9.9-windows-x64-bootstrap.zip"
    (release / source_zip.name).rename(renamed)
    for suffix in ("sha256", "spdx.json", "licenses.json", "provenance.json", "acceptance.json"):
        (release / f"{source_zip.name}.{suffix}").rename(release / f"{renamed.name}.{suffix}")
    digest = hashlib.sha256(renamed.read_bytes()).hexdigest()
    (release / f"{renamed.name}.sha256").write_text(
        f"{digest}  {renamed.name}\n", encoding="ascii"
    )
    provenance_path = release / f"{renamed.name}.provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
    provenance["sha256"] = digest
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    report = packages.audit_release_assets(
        release,
        expected_component="tts-more",
        expected_version="0.2.0-clean-controller",
    )

    assert report["valid"] is False
    assert any("filename" in error for error in report["errors"])


def test_release_asset_set_rejects_full_zip_even_beside_valid_bootstrap(
    clean_portable_builds: dict[str, object], tmp_path: Path
) -> None:
    packages = _load_portable_packages()
    source_zip = Path(clean_portable_builds["controller_zip"])
    release = tmp_path / "release"
    release.mkdir()
    for source in source_zip.parent.glob(f"{source_zip.name}*"):
        shutil.copy2(source, release / source.name)
    _write_full_package_candidate(release, component="tts-more", resolved_profile="cpu")

    report = packages.audit_release_assets(
        release,
        expected_component="tts-more",
        expected_version="0.2.0-clean-controller",
    )

    assert report["valid"] is False
    assert any("exactly one" in error or "profile=full" in error for error in report["errors"])


def test_portable_release_workflow_is_valid_yaml_and_has_no_broad_asset_glob() -> None:
    import yaml

    workflow_path = REPO_ROOT / ".github" / "workflows" / "portable-release.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)

    assert isinstance(parsed, dict) and "jobs" in parsed
    assert "artifacts/portable/bootstrap/*\n" not in workflow
    assert "release-assets/* --clobber" not in workflow
    assert "audit-release-assets --directory" in workflow


def test_release_asset_set_requires_one_expected_component_and_version(
    clean_portable_builds: dict[str, object], tmp_path: Path
) -> None:
    packages = _load_portable_packages()
    release = tmp_path / "release"
    release.mkdir()
    first = Path(clean_portable_builds["controller_zip"])
    _second_stage, second = _build_controller_bootstrap(
        tmp_path / "second-controller", "9.9.9-foreign"
    )
    for archive_path in (first, second):
        for source in archive_path.parent.glob(f"{archive_path.name}*"):
            shutil.copy2(source, release / source.name)

    report = packages.audit_release_assets(
        release,
        expected_component="tts-more",
        expected_version="0.2.0-clean-controller",
    )

    assert report["valid"] is False
    assert any("exactly one" in error for error in report["errors"])


def test_portable_release_workflow_passes_expected_component_and_version() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("audit-release-assets --directory") == 2
    assert workflow.count("--expected-component tts-more") == 2
    assert workflow.count("--expected-version") >= 2
    assert "$candidateZips[0]" not in workflow


def test_portable_release_workflow_uses_locked_backend_build_environment() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(
        encoding="utf-8"
    )

    assert 'python -m pip install -e "backend[dev]"' not in workflow
    assert "uv sync --locked --project backend --extra dev" in workflow
    assert "$buildPython = (Resolve-Path backend\\.venv\\Scripts\\python.exe).Path" in workflow
    assert "$env:TTS_MORE_BUILD_PYTHON = $buildPython" in workflow
    assert "& $buildPython scripts\\portable_packages.py audit-release" in workflow
    assert "& $buildPython scripts\\portable_packages.py audit-release-assets" in workflow
    release = workflow.split("github-release:", 1)[1]
    assert "uv sync --locked --project integrations/build_tools" in release
    assert 'build_python="$RUNNER_TEMP/tts-more-build-tools/bin/python"' in release
    assert '"$build_python" scripts/portable_packages.py audit-release' in release
    assert '"$build_python" scripts/portable_packages.py audit-release-assets' in release


def test_portable_release_workflow_fails_closed_on_existing_remote_extra_assets() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(
        encoding="utf-8"
    )
    publish = workflow.split("- name: Publish bootstrap assets only", 1)[1]

    remote_query = "gh release view \"$GITHUB_REF_NAME\" --json assets --jq '.assets[].name'"
    assert remote_query in publish
    assert "remote_asset_names" in publish and "local_asset_names" in publish
    assert "comm -23" in publish
    assert publish.index(remote_query) < publish.index("gh release upload")
    assert "release delete-asset" not in publish


def test_portable_release_workflow_rechecks_exact_remote_assets_after_upload() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").read_text(
        encoding="utf-8"
    )
    publish = workflow.split("- name: Publish bootstrap assets only", 1)[1]
    upload = 'gh release upload "$GITHUB_REF_NAME" "${assets[@]}" --clobber'
    gate_call = '"$build_python" scripts/verify-release-asset-set.py'

    assert upload in publish
    assert gate_call in publish
    assert publish.index(upload) < publish.index(gate_call)
    assert 'verify_asset_args+=(--expected-name "$asset_name")' in publish
    gate_block = publish[publish.index(gate_call) :]
    assert '--repository "$GITHUB_REPOSITORY"' in gate_block
    assert '--tag "$GITHUB_REF_NAME"' in gate_block
    assert '"${verify_asset_args[@]}"' in gate_block


def _release_gate_expected_names() -> list[str]:
    archive = "TTS-More-0.2.0-test-windows-x64-bootstrap.zip"
    return [
        archive,
        f"{archive}.sha256",
        f"{archive}.spdx.json",
        f"{archive}.licenses.json",
        f"{archive}.provenance.json",
        f"{archive}.acceptance.json",
    ]


def test_release_asset_gate_executes_tag_query_and_accepts_exact_six() -> None:
    gate = _load_release_asset_gate()
    expected = _release_gate_expected_names()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="\n".join(reversed(expected)) + "\n", stderr="")

    exit_code = gate.main(
        [
            "--repository",
            "XucroYuri/TTS_more",
            "--tag",
            "v0.2.0/rc1",
            *(argument for name in expected for argument in ("--expected-name", name)),
        ],
        run=fake_run,
    )

    assert exit_code == 0
    assert calls == [
        [
            "gh",
            "api",
            "repos/XucroYuri/TTS_more/releases/tags/v0.2.0%2Frc1",
            "--jq",
            ".assets[].name",
        ]
    ]


def test_release_asset_gate_returns_nonzero_for_concurrent_seventh_asset() -> None:
    gate = _load_release_asset_gate()
    expected = _release_gate_expected_names()

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        actual = [*expected, "concurrent-foreign-full.zip"]
        return subprocess.CompletedProcess(command, 0, stdout="\n".join(actual) + "\n", stderr="")

    exit_code = gate.main(
        [
            "--repository",
            "XucroYuri/TTS_more",
            "--tag",
            "v0.2.0-test",
            *(argument for name in expected for argument in ("--expected-name", name)),
        ],
        run=fake_run,
    )

    assert exit_code != 0


def test_release_asset_gate_rejects_foreign_asset_replacing_expected_asset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = _load_release_asset_gate()
    expected = _release_gate_expected_names()
    replaced = expected[-1]
    foreign = "foreign.zip"

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        actual = [*expected[:-1], foreign]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(actual) + "\n",
            stderr="",
        )

    exit_code = gate.main(
        [
            "--repository",
            "XucroYuri/TTS_more",
            "--tag",
            "v0.2.0-test",
            *(argument for name in expected for argument in ("--expected-name", name)),
        ],
        run=fake_run,
    )

    assert exit_code != 0
    error = capsys.readouterr().err
    assert "mismatch" in error
    assert f"missing=['{replaced}']" in error
    assert f"extra=['{foreign}']" in error


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
    assert {"package_id", "release_version", "protocol", "data"} <= set(schema["$defs"]["v2"]["required"])
    assert schema["$defs"]["v2"]["properties"]["protocol"]["properties"]["name"] == {"const": "tts-more-v1"}
    assert schema["$defs"]["v2"]["properties"]["data"]["required"] == ["user", "local", "cache", "operations"]
    for field in ("health_path", "capabilities_path"):
        assert schema["$defs"]["v2"]["properties"]["endpoint"]["properties"][field] == {
            "type": "string",
            "minLength": 1,
            "pattern": "^/",
        }
    assert schema["oneOf"] == [{"$ref": "#/$defs/v1"}, {"$ref": "#/$defs/v2"}]


@pytest.mark.parametrize("component", ("controller", "worker"))
def test_official_schema_accepts_real_builder_generated_v2_manifests(
    generated_v2_manifests: dict[str, dict[str, object]], component: str
) -> None:
    manifest = generated_v2_manifests[component]

    errors = sorted(_official_manifest_validator().iter_errors(manifest), key=lambda error: list(error.path))

    assert not errors, "\n".join(error.message for error in errors)


@pytest.mark.parametrize("field", ("health_path", "capabilities_path"))
@pytest.mark.parametrize("invalid_value", ("", "api/health", 42))
def test_official_schema_and_python_validator_reject_invalid_v2_endpoint_paths(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    packages = _load_portable_packages()
    payload = _valid_v2_manifest()
    payload["endpoint"][field] = invalid_value
    manifest = _write_v2_manifest(tmp_path, payload)

    schema_errors = list(_official_manifest_validator().iter_errors(payload))
    python_report = packages.validate_manifest(manifest, tmp_path)

    assert schema_errors, f"official schema accepted endpoint.{field}={invalid_value!r}"
    assert python_report["valid"] is False
    assert f"endpoint.{field} must start with /" in python_report["errors"]


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


def test_stop_worker_reads_bom_but_rejects_live_legacy_record_without_safe_identity(
    tmp_path: Path,
) -> None:
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
    with pytest.raises(RuntimeError, match="legacy PID record"):
        launcher.stop_worker(
            package_root,
            inspector=lambda _pid: {
                "pid": 1234,
                "created_at": "unknown",
                "executable_path": str(executable),
                "command_args": [],
            },
            terminator=lambda _pid: pytest.fail("legacy process must not be terminated"),
            port_owner_inspector=lambda _port: {1234},
        )
    assert record.exists()


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
        (REPO_ROOT, None, "scripts\\Invoke-PortableStart.ps1"),
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
