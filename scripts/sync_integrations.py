from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable


INTEGRATION_VERSION = "2.0.0"
COMPONENTS = {
    "gpt-sovits": {"module": "tts_more_worker.gpt_sovits:app", "port": 9880, "python": "3.11.9"},
    "indextts": {"module": "tts_more_worker.indextts:app", "port": 9881, "python": "3.11.9"},
    "cosyvoice": {"module": "tts_more_worker.cosyvoice:app", "port": 9882, "python": "3.10.11"},
}
GUIDE_NAME = "使用说明-先看这里.txt"
ROOT_ENTRIES = (
    "Initialize.cmd",
    "Start.cmd",
    "Stop.cmd",
    "Repair.cmd",
    "Build-Package.ps1",
    "Start-WebUI.cmd",
    GUIDE_NAME,
)


class FileState:
    __slots__ = ("device", "inode", "mode", "size", "modified_ns", "attributes", "digest")

    def __init__(
        self, device: int, inode: int, mode: int, size: int,
        modified_ns: int, attributes: int, digest: str,
    ) -> None:
        self.device = device
        self.inode = inode
        self.mode = mode
        self.size = size
        self.modified_ns = modified_ns
        self.attributes = attributes
        self.digest = digest

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileState):
            return NotImplemented
        return all(getattr(self, name) == getattr(other, name) for name in self.__slots__)


class FileBackup:
    __slots__ = ("payload", "state")

    def __init__(self, payload: bytes | None, state: FileState | None) -> None:
        self.payload = payload
        self.state = state


class PublicationJournal:
    __slots__ = ("published", "removed")

    def __init__(self) -> None:
        self.published: dict[str, FileState] = {}
        self.removed: set[str] = set()


def sha256_file(path: Path) -> str:
    # All controlled integration files are text. Hash their canonical LF form
    # so Git's Windows checkout conversion cannot create false mirror drift.
    canonical = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(canonical).hexdigest()


def sync_integration(source_root: Path, target_root: Path, component: str, source_revision: str) -> dict[str, object]:
    if component not in COMPONENTS:
        raise ValueError(f"unsupported integration component: {component}")
    source_root = source_root.resolve(strict=True)
    target_root = Path(os.path.abspath(target_root))
    previous_files, previous_manifest = _read_previous_manifest(target_root)

    with tempfile.TemporaryDirectory(prefix="tts-more-sync-") as temporary:
        stage_root = Path(temporary) / "stage"
        stage_root.mkdir()
        manifest = _build_staged_integration(source_root, stage_root, component, source_revision)
        new_files = _validate_manifest_files(manifest.get("files"))
        _preflight_target(target_root, previous_files, new_files, previous_manifest)
        _publish_transaction(
            stage_root,
            target_root,
            previous_files,
            new_files,
            previous_manifest,
        )
    return manifest


def _build_staged_integration(
    source_root: Path, stage_root: Path, component: str, source_revision: str
) -> dict[str, object]:
    controlled = stage_root / "tts_more"
    controlled.mkdir(parents=True)
    _copy_tree(source_root / "integrations" / "tts_more_worker", controlled / "tts_more_worker")
    _copy_tree(source_root / "integrations" / "contract_tests", controlled / "tests")
    _copy_tree(source_root / "integrations" / "build_tools", controlled / "build-tools")
    _copy_tree(source_root / "backend" / "app" / "workers", controlled / "app" / "workers")
    _copy_file(source_root / "backend" / "app" / "models.py", controlled / "app" / "models.py")
    _copy_file(source_root / "backend" / "app" / "subprocess_safety.py", controlled / "app" / "subprocess_safety.py")
    _copy_file(source_root / "backend" / "app" / "__init__.py", controlled / "app" / "__init__.py")
    _copy_file(source_root / "backend" / "app" / "adapters" / "base.py", controlled / "app" / "adapters" / "base.py")
    _copy_file(source_root / "backend" / "app" / "adapters" / "__init__.py", controlled / "app" / "adapters" / "__init__.py")
    for name in (
        "portable_install.py",
        "portable_launcher.py",
        "portable_operations.py",
        "portable_packages.py",
        "verify-release-asset-set.py",
        "import_portable_data.py",
        "import-portable-data.py",
    ):
        _copy_file(source_root / "scripts" / name, controlled / name)
    for name in (
        "bootstrap-conda.ps1",
        "Resolve-PortableBuildPython.ps1",
        "Invoke-PortableStart.ps1",
        "Show-PortableProgress.ps1",
        "Portable-Validation.ps1",
        "select-portable-folder.ps1",
    ):
        _copy_file(source_root / "scripts" / name, controlled / name)
    for name in (
        "Initialize.ps1",
        "portable-python.ps1",
        "Start-Worker.ps1",
        "Stop-Worker.ps1",
        "Repair.ps1",
        "Build-Package.ps1",
        "Portable-Paths.ps1",
        "Start-WebUI.ps1",
    ):
        _copy_file(source_root / "integrations" / "windows" / name, controlled / name)
    _copy_file(source_root / "packaging" / "portable" / "toolchain.lock.json", controlled / "locks" / "toolchain.lock.json")
    _copy_file(source_root / "packaging" / "portable" / "tts-more-package.schema.json", controlled / "tts-more-package.schema.json")
    _copy_file(source_root / "packaging" / "portable" / "error-catalog.zh-CN.json", controlled / "error-catalog.zh-CN.json")
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
        (stage_root / name).write_text(content, encoding="utf-8", newline="\r\n" if name.endswith(".cmd") else "\n")

    files = {}
    for path in _tracked_paths(stage_root):
        relative = path.relative_to(stage_root).as_posix()
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


def _read_previous_manifest(target_root: Path) -> tuple[set[str], bytes | None]:
    manifest_path = target_root / "tts_more" / "integration.manifest.json"
    _assert_safe_target_path(target_root, target_root / "tts_more", include_leaf=True)
    _assert_safe_target_path(target_root, manifest_path, include_leaf=True)
    if not _lexists(manifest_path):
        return set(), None
    if not stat.S_ISREG(os.lstat(manifest_path).st_mode):
        raise ValueError("previous integration manifest is not a file")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("previous integration manifest is invalid") from exc
    if manifest.get("schema_version") != 1:
        raise ValueError("previous integration manifest is invalid")
    return _validate_manifest_files(manifest.get("files")), raw


def _validate_manifest_files(files: object) -> set[str]:
    if not isinstance(files, dict):
        raise ValueError("integration manifest files are invalid")
    owned: set[str] = set()
    aliases: dict[str, str] = {}
    for relative, digest in files.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ValueError("integration manifest files are invalid")
        _validate_controlled_relative(relative)
        if len(digest) != 64 or any(character not in "0123456789abcdefABCDEF" for character in digest):
            raise ValueError("integration manifest files are invalid")
        alias = relative.casefold()
        if alias in aliases:
            raise ValueError(
                f"case-insensitive alias in integration manifest: {aliases[alias]} and {relative}"
            )
        aliases[alias] = relative
        owned.add(relative)
    return owned


def _validate_controlled_relative(relative: str) -> None:
    pure = PurePosixPath(relative)
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    if (
        not relative
        or relative != pure.as_posix()
        or "\\" in relative
        or ":" in relative
        or pure.is_absolute()
        or any(part in ("", ".", "..") for part in pure.parts)
        or any(part.endswith((".", " ")) for part in pure.parts)
        or any(part.split(".", 1)[0].casefold() in reserved for part in pure.parts)
        or (relative not in ROOT_ENTRIES and not relative.startswith("tts_more/"))
        or relative == "tts_more/integration.manifest.json"
    ):
        raise ValueError(f"invalid controlled path in integration manifest: {relative}")


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_safe_target_path(target_root: Path, path: Path, *, include_leaf: bool) -> None:
    root = Path(os.path.abspath(target_root))
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"integration target escapes target root: {candidate}") from exc

    parts = relative.parts if include_leaf else relative.parts[:-1]
    current = root
    paths = [root]
    for part in parts:
        current /= part
        paths.append(current)
    for current in paths:
        if _lexists(current) and _is_reparse(current):
            raise ValueError(f"integration target contains a reparse point: {current}")

    resolved_root = os.path.normcase(str(root.resolve(strict=False)))
    resolved_candidate = os.path.normcase(str(candidate.resolve(strict=False)))
    try:
        common = os.path.commonpath((resolved_root, resolved_candidate))
    except ValueError as exc:
        raise ValueError(f"integration target escapes resolved target root: {candidate}") from exc
    if common != resolved_root:
        raise ValueError(f"integration target escapes resolved target root: {candidate}")


def _preflight_target(
    target_root: Path,
    previous_files: set[str],
    new_files: set[str],
    previous_manifest: bytes | None,
) -> None:
    _assert_safe_target_path(target_root, target_root, include_leaf=True)
    _assert_safe_target_path(target_root, target_root / "tts_more", include_leaf=True)
    if _lexists(target_root) and not stat.S_ISDIR(os.lstat(target_root).st_mode):
        raise FileExistsError(f"target-owned file collides with integration root: {target_root}")
    manifest_relative = "tts_more/integration.manifest.json"
    owned = previous_files | ({manifest_relative} if previous_manifest is not None else set())
    for relative in sorted(new_files | {manifest_relative}):
        destination = target_root / Path(PurePosixPath(relative))
        _assert_safe_target_path(target_root, destination, include_leaf=True)
        if _lexists(destination) and relative not in owned:
            raise FileExistsError(f"target-owned file collides with newly controlled path: {relative}")
        parent = destination.parent
        while parent != target_root:
            if _lexists(parent) and not stat.S_ISDIR(os.lstat(parent).st_mode):
                raise FileExistsError(f"target-owned file collides with integration directory: {parent}")
            parent = parent.parent
    for relative in previous_files:
        existing = target_root / Path(PurePosixPath(relative))
        _assert_safe_target_path(target_root, existing, include_leaf=True)
        if _lexists(existing) and not stat.S_ISREG(os.lstat(existing).st_mode):
            raise ValueError(f"previous controlled path is not a file: {relative}")


def _publish_transaction(
    stage_root: Path,
    target_root: Path,
    previous_files: set[str],
    new_files: set[str],
    previous_manifest: bytes | None,
) -> None:
    manifest_relative = "tts_more/integration.manifest.json"
    prior_paths = previous_files | ({manifest_relative} if previous_manifest is not None else set())
    backups: dict[str, FileBackup] = {}
    for relative in prior_paths:
        path = target_root / Path(PurePosixPath(relative))
        _assert_safe_target_path(target_root, path, include_leaf=True)
        backups[relative] = _capture_backup(path)
    target_root.mkdir(parents=True, exist_ok=True)
    journal = PublicationJournal()
    obsolete = previous_files - new_files
    try:
        for relative in sorted(new_files):
            _publish_file(
                stage_root / Path(PurePosixPath(relative)),
                target_root / Path(PurePosixPath(relative)),
                target_root,
                backups.get(relative),
                journal,
            )
        for relative in sorted(obsolete, reverse=True):
            _remove_owned_file(target_root, relative, backups[relative], journal)
        _publish_file(
            stage_root / Path(PurePosixPath(manifest_relative)),
            target_root / Path(PurePosixPath(manifest_relative)),
            target_root,
            backups.get(manifest_relative),
            journal,
        )
        for relative in sorted(new_files | {manifest_relative}):
            _assert_safe_target_path(
                target_root,
                target_root / Path(PurePosixPath(relative)),
                include_leaf=True,
            )
    except BaseException as original:
        rollback_errors = _rollback_publication(target_root, backups, journal, prior_paths)
        if rollback_errors:
            raise RuntimeError(
                f"rollback incomplete after {original}: " + "; ".join(rollback_errors)
            ) from original
        raise


def _rollback_publication(
    target_root: Path,
    backups: dict[str, FileBackup],
    journal: PublicationJournal,
    prior_paths: set[str],
) -> list[str]:
    errors: list[str] = []
    for relative in sorted(set(journal.published) - prior_paths, reverse=True):
        path = target_root / Path(PurePosixPath(relative))
        try:
            _assert_safe_target_path(target_root, path, include_leaf=True)
            if not _matches_state(path, journal.published[relative]):
                raise ValueError(f"published path identity changed before rollback: {relative}")
            path.unlink()
            _assert_safe_target_path(target_root, path, include_leaf=True)
            _remove_empty_parents(path.parent, target_root)
        except BaseException as exc:
            errors.append(f"{relative}: {exc}")
    for relative, backup in backups.items():
        path = target_root / Path(PurePosixPath(relative))
        try:
            _assert_safe_target_path(target_root, path, include_leaf=True)
            if relative in journal.published:
                if not _matches_state(path, journal.published[relative]):
                    raise ValueError(f"published path identity changed before rollback: {relative}")
                if backup.payload is None:
                    path.unlink()
                    _remove_empty_parents(path.parent, target_root)
                else:
                    _write_bytes_atomically(target_root, path, backup.payload)
            elif relative in journal.removed:
                if _lexists(path):
                    raise ValueError(f"removed path was externally recreated before rollback: {relative}")
                if backup.payload is not None:
                    _write_new_bytes_exclusively(target_root, path, backup.payload)
            else:
                _assert_backup_unchanged(path, backup, relative)
            _assert_safe_target_path(target_root, path, include_leaf=True)
        except BaseException as exc:
            errors.append(f"{relative}: {exc}")
    return errors


def _publish_file(
    source: Path,
    destination: Path,
    target_root: Path,
    backup: FileBackup | None,
    journal: PublicationJournal,
) -> None:
    relative = destination.relative_to(target_root).as_posix()
    _assert_safe_target_path(target_root, destination, include_leaf=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_target_path(target_root, destination, include_leaf=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".tts-more-sync-", dir=destination.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        _assert_safe_target_path(target_root, destination, include_leaf=True)
        if backup is None:
            if _lexists(destination):
                raise FileExistsError(f"target path appeared during publication: {relative}")
            _publish_temp_exclusively(temporary, destination)
        else:
            _assert_backup_unchanged(destination, backup, relative)
            os.replace(temporary, destination)
        state = _capture_regular_state(destination)
        journal.published[relative] = state
        _assert_safe_target_path(target_root, destination, include_leaf=True)
    finally:
        temporary.unlink(missing_ok=True)


def _write_bytes_atomically(target_root: Path, destination: Path, payload: bytes) -> None:
    _assert_safe_target_path(target_root, destination, include_leaf=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_target_path(target_root, destination, include_leaf=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".tts-more-rollback-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        _assert_safe_target_path(target_root, destination, include_leaf=True)
        os.replace(temporary, destination)
        _assert_safe_target_path(target_root, destination, include_leaf=True)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new_bytes_exclusively(target_root: Path, destination: Path, payload: bytes) -> None:
    _assert_safe_target_path(target_root, destination, include_leaf=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_target_path(target_root, destination, include_leaf=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".tts-more-rollback-new-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        if _lexists(destination):
            raise FileExistsError(f"target path appeared during rollback: {destination}")
        _publish_temp_exclusively(temporary, destination)
        _assert_safe_target_path(target_root, destination, include_leaf=True)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_temp_exclusively(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination, follow_symlinks=False)
        return
    except FileExistsError:
        raise
    except OSError:
        if _lexists(destination):
            raise FileExistsError(f"target path appeared during publication: {destination}")
        if os.name != "nt":
            raise
    # On Windows os.rename maps to MoveFileW without replace-existing flags.
    # It is atomic on the same volume and fails if a racing destination exists.
    os.rename(temporary, destination)


def _raw_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _capture_regular_state(path: Path) -> FileState:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse(path):
        raise ValueError(f"controlled path is not a regular non-reparse file: {path}")
    payload = path.read_bytes()
    return FileState(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        attributes=getattr(metadata, "st_file_attributes", 0),
        digest=_raw_digest(payload),
    )


def _capture_backup(path: Path) -> FileBackup:
    if not _lexists(path):
        return FileBackup(payload=None, state=None)
    payload = path.read_bytes()
    state = _capture_regular_state(path)
    if state.digest != _raw_digest(payload):
        raise ValueError(f"controlled path changed while creating backup: {path}")
    return FileBackup(payload=payload, state=state)


def _matches_state(path: Path, expected: FileState) -> bool:
    if not _lexists(path):
        return False
    try:
        actual = _capture_regular_state(path)
    except (OSError, ValueError):
        return False
    return actual == expected


def _assert_backup_unchanged(path: Path, backup: FileBackup, relative: str) -> None:
    if backup.state is None:
        if _lexists(path):
            raise FileExistsError(f"target path appeared during publication: {relative}")
        return
    if not _matches_state(path, backup.state):
        raise RuntimeError(f"prior controlled file changed during publication: {relative}")


def _remove_owned_file(
    target_root: Path,
    relative: str,
    backup: FileBackup,
    journal: PublicationJournal,
) -> None:
    path = target_root / Path(PurePosixPath(relative))
    _assert_safe_target_path(target_root, path, include_leaf=True)
    _assert_backup_unchanged(path, backup, relative)
    if backup.state is not None:
        path.unlink()
        journal.removed.add(relative)
    _assert_safe_target_path(target_root, path, include_leaf=True)
    _remove_empty_parents(path.parent, target_root)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    while path != stop and _lexists(path):
        _assert_safe_target_path(stop, path, include_leaf=True)
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            break
        try:
            path.rmdir()
        except OSError:
            break
        _assert_safe_target_path(stop, path, include_leaf=True)
        path = path.parent


def check_integration(target_root: Path) -> list[str]:
    target_root = Path(os.path.abspath(target_root))
    manifest_path = target_root / "tts_more" / "integration.manifest.json"
    try:
        _assert_safe_target_path(target_root, target_root, include_leaf=True)
        _assert_safe_target_path(target_root, target_root / "tts_more", include_leaf=True)
        _assert_safe_target_path(target_root, manifest_path, include_leaf=True)
    except ValueError as exc:
        return [str(exc)]
    if not _lexists(manifest_path):
        return ["integration manifest is missing"]
    if not stat.S_ISREG(os.lstat(manifest_path).st_mode):
        return ["integration manifest is not a regular file"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_names = _validate_manifest_files(manifest.get("files"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return [str(exc)]
    expected = {relative: str(manifest["files"][relative]) for relative in expected_names}
    errors: list[str] = []
    for relative, digest in expected.items():
        path = target_root / relative
        try:
            _assert_safe_target_path(target_root, path, include_leaf=True)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not _lexists(path) or not stat.S_ISREG(os.lstat(path).st_mode):
            errors.append(f"missing controlled file: {relative}")
        elif sha256_file(path) != digest:
            errors.append(f"hash mismatch: {relative}")
    return errors


def _root_entry_payloads(component: str) -> dict[str, str]:
    return {
        "Initialize.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Initialize.ps1" %*\nexit /b %errorlevel%\n',
        "Start.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" %*\nexit /b %errorlevel%\n',
        "Stop.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Stop-Worker.ps1" %*\nexit /b %errorlevel%\n',
        "Repair.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Repair.ps1" %*\nexit /b %errorlevel%\n',
        "Build-Package.ps1": (
            '$ErrorActionPreference = "Stop"\n'
            "try {\n"
            '    & "$PSScriptRoot\\tts_more\\Build-Package.ps1" @args\n'
            "}\n"
            "catch {\n"
            "    [Console]::Error.WriteLine($_.Exception.Message)\n"
            "    exit 1\n"
            "}\n"
            "exit 0\n"
        ),
        "Start-WebUI.cmd": '@echo off\npowershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\\Start-WebUI.ps1" %*\nexit /b %errorlevel%\n',
        GUIDE_NAME: _guide_payload(component),
    }


def _guide_payload(component: str) -> str:
    display_name = {
        "gpt-sovits": "GPT-SoVITS",
        "indextts": "IndexTTS",
        "cosyvoice": "CosyVoice",
    }[component]
    port = COMPONENTS[component]["port"]
    return f"""{display_name} Windows 便携版使用说明

常用入口
- Start.cmd：启动 tts-more-v1 worker（默认端口：{port}）。
- Start-WebUI.cmd：启动上游原生 WebUI；它与 worker 是两个独立入口。
- Initialize.cmd：检查并补齐当前包的运行时、依赖和默认模型。
- Stop.cmd：仅停止由当前便携包启动的 worker。
- Repair.cmd：校验资产，并只重新获取缺失或损坏的内容，不删除用户数据。

两种交付形态
- Bootstrap：首次运行需要联网完成初始化；初始化成功之后可离线运行。
- Full：仅在本地生成，包含已验证的运行资产，可断网直接运行；禁止上传 GitHub。

运行说明
- 运行时无需安装系统 Python、Conda 或 Node，也不要把这些系统路径写入配置。
- 路径可能因电脑而异；请整体移动或解压文件夹，所有运行路径必须保持包内相对路径。
- 直接运行当前包的 Start.cmd 时，启动器会在启动服务之前询问是否从旧版便携包导入；工作台管理或自动化启动不会询问。
- 启动器不会自动扫描旧包，只使用你在固定选择器中明确选择的文件夹；再次确认后才复制数据，旧版原包保持不变。
- 选择旧目录后、确认摘要前，Bootstrap 包可能只在 data/cache/portable 下载或复用受锁定的包内 CPython 和锁定 uv 来生成计划；此步骤不会写入 runtime/live、models、data/user。
"""


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
