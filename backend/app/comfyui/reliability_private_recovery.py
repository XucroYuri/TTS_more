from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from . import reliability_evidence as evidence


PRIVATE_RECOVERY_DIRECTORY = ".private-recovery"
PRIVATE_ROLES: tuple[str, ...] = (".o", ".h", ".c")


class PrivateRecoveryError(evidence.EvidenceStoreError):
    """The private recovery namespace cannot be safely used."""


class PrivateRecoveryBoundary(evidence._StrictModel):
    status: Literal["prepared", "validated"]
    run_key: evidence.RunKey
    output_root: str
    root_identity: evidence.SHA256
    private_root: str
    private_root_identity: evidence.SHA256


def _fail(message: str) -> PrivateRecoveryError:
    return PrivateRecoveryError(message)


def _validated_run_key(run_key: str) -> str:
    try:
        return TypeAdapter(evidence.RunKey).validate_python(run_key, strict=True)
    except ValidationError as exc:
        raise _fail("private recovery run key is invalid") from exc


def _validated_identity(identity: str) -> str:
    try:
        return TypeAdapter(evidence.SHA256).validate_python(identity, strict=True)
    except ValidationError as exc:
        raise _fail("private recovery directory identity is invalid") from exc


def _portable_identity(result: os.stat_result) -> str:
    return hashlib.sha256(f"{result.st_dev:x}:{result.st_ino:x}".encode("ascii")).hexdigest()


def _require_directory(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise _fail("private recovery directory is unavailable") from exc
    if (
        stat.S_ISLNK(result.st_mode)
        or getattr(result, "st_file_attributes", 0) & evidence._FILE_ATTRIBUTE_REPARSE_POINT
        or not stat.S_ISDIR(result.st_mode)
    ):
        raise _fail("private recovery directory is unsafe")
    return result


def _private_root_path(output_root: Path, run_key: str) -> tuple[Path, str, Path]:
    safe_key = _validated_run_key(run_key)
    try:
        root, _ = evidence._validated_root(output_root)
    except evidence.EvidenceStoreError as exc:
        raise _fail("private recovery output root is unsafe") from exc
    return root, safe_key, root / PRIVATE_RECOVERY_DIRECTORY


def private_recovery_root(output_root: Path, run_key: str) -> Path:
    root, safe_key, private_root = _private_root_path(output_root, run_key)
    return root / PRIVATE_RECOVERY_DIRECTORY / safe_key


_PORTABLE_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def _portable_open_directory(path: Path) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise _fail("private recovery no-follow directory opens are unavailable")
    before = _require_directory(path)
    try:
        descriptor = os.open(path, _PORTABLE_DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fail("private recovery directory is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        after = _require_directory(path)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (
            opened.st_dev,
            opened.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise _fail("private recovery directory identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _portable_open_relative_directory(parent: int, name: str) -> int:
    try:
        return os.open(name, _PORTABLE_DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise _fail("private recovery directory is unsafe") from exc


def _portable_create_relative_directory(parent: int, name: str, *, create_new: bool) -> int:
    try:
        os.mkdir(name, dir_fd=parent)
    except FileExistsError as exc:
        if create_new:
            raise _fail("private recovery run collision") from exc
    except OSError as exc:
        raise _fail("private recovery directory could not be created") from exc
    return _portable_open_relative_directory(parent, name)


def _portable_prepare(
    output_root: Path,
    run_key: str,
    expected_root_identity: str,
) -> tuple[Path, str, Path, str]:
    root, safe_key, private_root = _private_root_path(output_root, run_key)
    expected = _validated_identity(expected_root_identity)
    root_handle = _portable_open_directory(root)
    handles = [root_handle]
    try:
        root_identity = _portable_identity(os.fstat(root_handle))
        if root_identity != expected:
            raise _fail("private recovery output root identity changed")
        private_handle = _portable_create_relative_directory(
            root_handle, PRIVATE_RECOVERY_DIRECTORY, create_new=False
        )
        handles.append(private_handle)
        private_identity = _portable_identity(os.fstat(private_handle))
        run_handle = _portable_create_relative_directory(
            private_handle, safe_key, create_new=True
        )
        handles.append(run_handle)
        root_after = _require_directory(root)
        private_after = _require_directory(private_root)
        run_after = _require_directory(private_root / safe_key)
        if (
            _portable_identity(root_after) != root_identity
            or _portable_identity(private_after) != private_identity
            or _portable_identity(run_after) != _portable_identity(os.fstat(run_handle))
        ):
            raise _fail("private recovery directory identity changed")
        return root, root_identity, private_root, private_identity
    finally:
        for handle in reversed(handles):
            os.close(handle)


def _windows_prepare(
    output_root: Path,
    run_key: str,
    expected_root_identity: str,
) -> tuple[Path, str, Path, str]:
    root, safe_key, private_root = _private_root_path(output_root, run_key)
    expected = _validated_identity(expected_root_identity)
    absolute = evidence._absolute_directory_path(root)
    root_handle = evidence._windows_open_root_directory(Path(absolute.anchor))
    handles = [root_handle]
    try:
        parent = root_handle
        for component in absolute.parts[1:]:
            parent = evidence._windows_nt_create(
                parent, component, directory=True, create_new=False
            )
            handles.append(parent)
        root_information = evidence._windows_handle_information(parent)
        root_identity = evidence._windows_directory_identity(root_information)
        if root_identity != expected:
            raise _fail("private recovery output root identity changed")
        private_handle = evidence._windows_nt_create(
            parent,
            PRIVATE_RECOVERY_DIRECTORY,
            directory=True,
            create_new=False,
            create_if_missing=True,
        )
        handles.append(private_handle)
        private_information = evidence._windows_handle_information(private_handle)
        run_handle = evidence._windows_nt_create(
            private_handle, safe_key, directory=True, create_new=True
        )
        handles.append(run_handle)
        run_information = evidence._windows_handle_information(run_handle)
        root_after = _require_directory(absolute)
        private_after = _require_directory(private_root)
        run_after = _require_directory(private_root / safe_key)
        root_index = (root_information.FileIndexHigh << 32) | root_information.FileIndexLow
        private_index = (private_information.FileIndexHigh << 32) | private_information.FileIndexLow
        run_index = (run_information.FileIndexHigh << 32) | run_information.FileIndexLow
        if (
            root_after.st_ino != root_index
            or private_after.st_ino != private_index
            or run_after.st_ino != run_index
        ):
            raise _fail("private recovery directory identity changed")
        return (
            absolute,
            root_identity,
            private_root,
            evidence._windows_directory_identity(private_information),
        )
    except PrivateRecoveryError:
        raise
    except FileExistsError as exc:
        raise _fail("private recovery run collision") from exc
    except evidence.EvidenceStoreError as exc:
        raise _fail("private recovery directory is unsafe") from exc
    finally:
        for handle in reversed(handles):
            evidence._windows_close_handle(handle)


def prepare_private_recovery(
    output_root: Path,
    run_key: str,
    *,
    expected_root_identity: str,
) -> PrivateRecoveryBoundary:
    try:
        if os.name == "nt":
            root, root_identity, private_root, private_identity = _windows_prepare(
                output_root, run_key, expected_root_identity
            )
        else:
            root, root_identity, private_root, private_identity = _portable_prepare(
                output_root, run_key, expected_root_identity
            )
        return PrivateRecoveryBoundary(
            status="prepared",
            run_key=_validated_run_key(run_key),
            output_root=str(root),
            root_identity=root_identity,
            private_root=str(private_root),
            private_root_identity=private_identity,
        )
    except PrivateRecoveryError:
        raise
    except evidence.EvidenceStoreError as exc:
        raise _fail("private recovery directory is unsafe") from exc


def _windows_validate(
    root: Path,
    run_key: str,
    expected_root_identity: str,
    expected_private_root_identity: str,
) -> tuple[Path, str, Path, str]:
    absolute = evidence._absolute_directory_path(root)
    root_handle = evidence._windows_open_root_directory(Path(absolute.anchor))
    handles = [root_handle]
    try:
        parent = root_handle
        for component in absolute.parts[1:]:
            parent = evidence._windows_nt_create(
                parent, component, directory=True, create_new=False
            )
            handles.append(parent)
        root_information = evidence._windows_handle_information(parent)
        root_identity = evidence._windows_directory_identity(root_information)
        if root_identity != expected_root_identity:
            raise _fail("private recovery output root identity changed")
        private_handle = evidence._windows_nt_create(
            parent, PRIVATE_RECOVERY_DIRECTORY, directory=True, create_new=False
        )
        handles.append(private_handle)
        private_information = evidence._windows_handle_information(private_handle)
        private_identity = evidence._windows_directory_identity(private_information)
        if private_identity != expected_private_root_identity:
            raise _fail("private recovery directory identity changed")
        run_handle = evidence._windows_nt_create(
            private_handle, run_key, directory=True, create_new=False
        )
        handles.append(run_handle)
        return absolute, root_identity, absolute / PRIVATE_RECOVERY_DIRECTORY, private_identity
    except PrivateRecoveryError:
        raise
    except evidence.EvidenceStoreError as exc:
        raise _fail("private recovery directory is unsafe") from exc
    finally:
        for handle in reversed(handles):
            evidence._windows_close_handle(handle)


def _portable_validate(
    root: Path,
    run_key: str,
    expected_root_identity: str,
    expected_private_root_identity: str,
) -> tuple[Path, str, Path, str]:
    root_handle = _portable_open_directory(root)
    handles = [root_handle]
    try:
        root_identity = _portable_identity(os.fstat(root_handle))
        if root_identity != expected_root_identity:
            raise _fail("private recovery output root identity changed")
        private_handle = _portable_open_relative_directory(
            root_handle, PRIVATE_RECOVERY_DIRECTORY
        )
        handles.append(private_handle)
        private_identity = _portable_identity(os.fstat(private_handle))
        if private_identity != expected_private_root_identity:
            raise _fail("private recovery directory identity changed")
        run_handle = _portable_open_relative_directory(private_handle, run_key)
        handles.append(run_handle)
        return root, root_identity, root / PRIVATE_RECOVERY_DIRECTORY, private_identity
    finally:
        for handle in reversed(handles):
            os.close(handle)


def validate_private_recovery(
    output_root: Path,
    run_key: str,
    *,
    expected_root_identity: str,
    expected_private_root_identity: str,
) -> PrivateRecoveryBoundary:
    root, safe_key, private_root = _private_root_path(output_root, run_key)
    expected_root = _validated_identity(expected_root_identity)
    expected_private = _validated_identity(expected_private_root_identity)
    if os.name == "nt":
        actual_root, root_identity, actual_private, private_identity = _windows_validate(
            root, safe_key, expected_root, expected_private
        )
    else:
        actual_root, root_identity, actual_private, private_identity = _portable_validate(
            root, safe_key, expected_root, expected_private
        )
    return PrivateRecoveryBoundary(
        status="validated",
        run_key=safe_key,
        output_root=str(actual_root),
        root_identity=root_identity,
        private_root=str(actual_private),
        private_root_identity=private_identity,
    )
