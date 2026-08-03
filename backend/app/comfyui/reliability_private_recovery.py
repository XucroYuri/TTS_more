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


def _fail(message: str, exc: BaseException | None = None) -> PrivateRecoveryError:
    if exc is None:
        return PrivateRecoveryError(message)
    return PrivateRecoveryError(message)


def _validated_run_key(run_key: str) -> str:
    try:
        return TypeAdapter(evidence.RunKey).validate_python(run_key, strict=True)
    except ValidationError as exc:
        raise _fail("private recovery run key is invalid", exc) from exc


def _validated_identity(identity: str) -> str:
    try:
        return TypeAdapter(evidence.SHA256).validate_python(identity, strict=True)
    except ValidationError as exc:
        raise _fail("private recovery directory identity is invalid", exc) from exc


def _portable_identity(result: os.stat_result) -> str:
    return hashlib.sha256(f"{result.st_dev:x}:{result.st_ino:x}".encode("ascii")).hexdigest()


def _require_directory(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise _fail("private recovery directory is unavailable", exc) from exc
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
        raise _fail("private recovery directory is unsafe")
    return result


def _require_within(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise _fail("private recovery directory escaped the output root", exc) from exc


def _private_root_path(output_root: Path, run_key: str) -> tuple[Path, str, Path]:
    safe_key = _validated_run_key(run_key)
    try:
        root, _ = evidence._validated_root(output_root)
    except evidence.EvidenceStoreError as exc:
        raise _fail("private recovery output root is unsafe", exc) from exc
    return root, safe_key, root / PRIVATE_RECOVERY_DIRECTORY


def private_recovery_root(output_root: Path, run_key: str) -> Path:
    root, safe_key, private_root = _private_root_path(output_root, run_key)
    return root / PRIVATE_RECOVERY_DIRECTORY / safe_key


def _portable_prepare(
    output_root: Path,
    run_key: str,
    expected_root_identity: str,
) -> tuple[Path, str, Path, str]:
    root, safe_key, private_root = _private_root_path(output_root, run_key)
    expected = _validated_identity(expected_root_identity)
    root_stat = _require_directory(root)
    root_identity = _portable_identity(root_stat)
    if root_identity != expected:
        raise _fail("private recovery output root identity changed")
    try:
        os.mkdir(private_root)
    except FileExistsError:
        pass
    except OSError as exc:
        raise _fail("private recovery directory could not be created", exc) from exc
    private_stat = _require_directory(private_root)
    _require_within(private_root, root)
    run_root = private_root / safe_key
    try:
        os.mkdir(run_root)
    except FileExistsError as exc:
        raise _fail("private recovery run collision", exc) from exc
    except OSError as exc:
        raise _fail("private recovery run could not be created", exc) from exc
    _require_directory(run_root)
    _require_within(run_root, root)
    root_after = _require_directory(root)
    if _portable_identity(root_after) != root_identity:
        raise _fail("private recovery output root identity changed")
    return root, root_identity, private_root, _portable_identity(private_stat)


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
        raise _fail("private recovery run collision", exc) from exc
    except evidence.EvidenceStoreError as exc:
        raise _fail("private recovery directory is unsafe", exc) from exc
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
        raise _fail("private recovery directory is unsafe", exc) from exc


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
    try:
        actual_root, root_identity = evidence.validate_directory_identity(root, expected_root)
        actual_private, private_identity = evidence.validate_directory_identity(
            private_root, expected_private
        )
    except evidence.EvidenceStoreError as exc:
        raise _fail("private recovery directory identity changed", exc) from exc
    run_root = actual_private / safe_key
    _require_directory(run_root)
    _require_within(actual_private, actual_root)
    _require_within(run_root, actual_root)
    return PrivateRecoveryBoundary(
        status="validated",
        run_key=safe_key,
        output_root=str(actual_root),
        root_identity=root_identity,
        private_root=str(actual_private),
        private_root_identity=private_identity,
    )
