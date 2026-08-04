from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, TypeAdapter, ValidationError

from . import reliability_evidence as evidence


PRIVATE_RECOVERY_DIRECTORY = ".private-recovery"
PRIVATE_ROLES: tuple[str, ...] = (".o", ".h", ".c")
MAX_PRIVATE_RECOVERY_ENTRIES = 4096
MAX_PRIVATE_RECOVERY_OBSERVED_BYTES = 68_719_476_736
MAX_PRIVATE_RECOVERY_STABLE_MEMBER_BYTES = 67_108_864
MAX_PRIVATE_RECOVERY_SNAPSHOT_BYTES = 4_194_304


class PrivateRecoveryError(evidence.EvidenceStoreError):
    """The private recovery namespace cannot be safely used."""


class PrivateRecoveryBoundary(evidence._StrictModel):
    status: Literal["prepared", "validated"]
    run_key: evidence.RunKey
    output_root: str
    root_identity: evidence.SHA256
    private_root: str
    private_root_identity: evidence.SHA256


class PrivateRecoveryLimits(evidence._StrictModel):
    max_entries: StrictInt = Field(default=4096, ge=0, le=MAX_PRIVATE_RECOVERY_ENTRIES)
    max_total_observed_bytes: StrictInt = Field(
        default=68_719_476_736,
        ge=0,
        le=MAX_PRIVATE_RECOVERY_OBSERVED_BYTES,
    )
    max_stable_member_bytes: StrictInt = Field(
        default=67_108_864,
        ge=0,
        le=MAX_PRIVATE_RECOVERY_STABLE_MEMBER_BYTES,
    )
    max_snapshot_bytes: StrictInt = Field(
        default=4_194_304,
        ge=0,
        le=MAX_PRIVATE_RECOVERY_SNAPSHOT_BYTES,
    )


class PrivateStaticMember(evidence._StrictModel):
    role: Literal[".o", ".h", ".c"]
    present: StrictBool
    size_bytes: StrictInt | None
    sha256: evidence.SHA256 | None


class PrivateMutableEntry(evidence._StrictModel):
    relative_name_sha256: evidence.SHA256
    kind: Literal["file", "directory"]
    observed_size_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
    stable: StrictBool
    sha256: evidence.SHA256 | None


class PrivateMutableTree(evidence._StrictModel):
    present: StrictBool
    mutable: Literal[True]
    entry_count: StrictInt = Field(ge=0, le=4096)
    observed_total_bytes: StrictInt = Field(ge=0, le=68_719_476_736)
    entries: tuple[PrivateMutableEntry, ...]


class PrivateRecoverySnapshot(evidence._StrictModel):
    schema_version: Literal[1]
    kind: Literal["reliability-private-recovery-snapshot"]
    run_key: evidence.RunKey
    namespace_identity_sha256: evidence.SHA256
    retained: Literal[True]
    observation_complete: StrictBool
    overflow: StrictBool
    limits: PrivateRecoveryLimits
    static_members: tuple[PrivateStaticMember, PrivateStaticMember, PrivateStaticMember]
    mutable_tree: PrivateMutableTree


def _fail(message: str) -> PrivateRecoveryError:
    return PrivateRecoveryError(message)


def _require_valid_limits(limits: PrivateRecoveryLimits) -> None:
    values = (
        (limits.max_entries, MAX_PRIVATE_RECOVERY_ENTRIES),
        (limits.max_total_observed_bytes, MAX_PRIVATE_RECOVERY_OBSERVED_BYTES),
        (limits.max_stable_member_bytes, MAX_PRIVATE_RECOVERY_STABLE_MEMBER_BYTES),
        (limits.max_snapshot_bytes, MAX_PRIVATE_RECOVERY_SNAPSHOT_BYTES),
    )
    if any(type(value) is not int or value < 0 or value > maximum for value, maximum in values):
        raise _fail("private recovery limits are invalid")


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


_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _close_directory(handle: int) -> None:
    if os.name == "nt":
        evidence._windows_close_handle(handle)
    else:
        os.close(handle)


def _open_relative_directory(parent: int, name: str) -> int:
    try:
        if os.name == "nt":
            return evidence._windows_nt_create(parent, name, directory=True, create_new=False)
        descriptor = os.open(name, _PORTABLE_DIRECTORY_FLAGS, dir_fd=parent)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise _fail("private recovery directory is unsafe")
        return descriptor
    except PrivateRecoveryError:
        raise
    except (OSError, evidence.EvidenceStoreError) as exc:
        raise _fail("private recovery directory is unsafe") from exc


def _open_relative_file(parent: int, name: str) -> int:
    try:
        if os.name == "nt":
            handle = evidence._windows_nt_create(parent, name, directory=False, create_new=False)
            import msvcrt

            return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        return os.open(name, _FILE_OPEN_FLAGS, dir_fd=parent)
    except (OSError, evidence.EvidenceStoreError) as exc:
        raise _fail("private recovery file is unsafe") from exc


def _stat_identity(result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def _read_relative_file(
    parent: int,
    name: str,
    *,
    max_bytes: int,
    required_stable: bool,
) -> tuple[int, bool, str | None]:
    descriptor = _open_relative_file(parent, name)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _fail("private recovery file is unsafe")
        observed_size = opened.st_size
        if observed_size > max_bytes:
            if required_stable:
                raise _fail("private recovery static member exceeds size limit")
            return observed_size, False, None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after_read = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        replacement = _open_relative_file(parent, name)
    except PrivateRecoveryError:
        if required_stable:
            raise _fail("private recovery static member changed")
        return observed_size, False, None
    try:
        after_name = os.fstat(replacement)
    finally:
        os.close(replacement)
    stable = (
        _stat_identity(opened) == _stat_identity(after_read) == _stat_identity(after_name)
        and len(payload) == observed_size
        and len(payload) <= max_bytes
    )
    if not stable:
        if required_stable:
            raise _fail("private recovery static member changed")
        return observed_size, False, None
    return observed_size, True, hashlib.sha256(payload).hexdigest()


def _windows_directory_names(handle: int) -> tuple[tuple[str, bool], ...]:
    import ctypes

    buffer = ctypes.create_string_buffer(64 * 1024)
    status_block = evidence._WindowsIoStatusBlock()
    query = ctypes.WinDLL("ntdll").NtQueryDirectoryFile
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(evidence._WindowsIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ubyte,
        ctypes.c_void_p,
        ctypes.c_ubyte,
    ]
    query.restype = ctypes.c_long
    names: list[tuple[str, bool]] = []
    restart = True
    while True:
        status = query(
            handle,
            None,
            None,
            None,
            ctypes.byref(status_block),
            buffer,
            len(buffer),
            1,
            False,
            None,
            restart,
        )
        restart = False
        code = status & 0xFFFFFFFF
        if code == 0x80000006:
            return tuple(names)
        if status < 0:
            raise _fail("private recovery directory could not be enumerated")
        offset = 0
        while True:
            next_offset = int.from_bytes(buffer.raw[offset : offset + 4], "little")
            attributes = int.from_bytes(buffer.raw[offset + 56 : offset + 60], "little")
            name_length = int.from_bytes(buffer.raw[offset + 60 : offset + 64], "little")
            name = buffer.raw[offset + 64 : offset + 64 + name_length].decode("utf-16-le")
            if name not in {".", ".."}:
                if attributes & evidence._FILE_ATTRIBUTE_REPARSE_POINT:
                    raise _fail("private recovery directory contains a reparse member")
                names.append((name, bool(attributes & 0x10)))
            if next_offset == 0:
                break
            offset += next_offset


def _directory_names(handle: int) -> tuple[tuple[str, bool], ...]:
    if os.name == "nt":
        return _windows_directory_names(handle)
    try:
        names: list[tuple[str, bool]] = []
        for entry in os.scandir(handle):
            result = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(result.st_mode):
                raise _fail("private recovery directory contains a reparse member")
            if stat.S_ISDIR(result.st_mode):
                names.append((entry.name, True))
            elif stat.S_ISREG(result.st_mode):
                names.append((entry.name, False))
            else:
                raise _fail("private recovery directory contains an unsafe member")
        return tuple(names)
    except PrivateRecoveryError:
        raise
    except OSError as exc:
        raise _fail("private recovery directory could not be enumerated") from exc


def _normalized_relative(parent: str, name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise _fail("private recovery relative name is unsafe")
    return unicodedata.normalize("NFC", f"{parent}/{name}" if parent else name)


def _observe_mutable_tree(
    run_handle: int,
    limits: PrivateRecoveryLimits,
    *,
    present: bool,
) -> tuple[PrivateMutableTree, bool, bool]:
    if not present:
        return (
            PrivateMutableTree(
                present=False,
                mutable=True,
                entry_count=0,
                observed_total_bytes=0,
                entries=(),
            ),
            True,
            False,
        )
    mutable_handle = _open_relative_directory(run_handle, ".p")
    _close_directory(mutable_handle)
    entries: list[PrivateMutableEntry] = []
    seen_hashes: set[str] = set()
    seen_casefolded: set[str] = set()
    total = 0
    overflow = False
    complete = True
    pending: list[str] = [""]
    while pending and not overflow:
        prefix = pending.pop(0)
        directory = _open_mutable_parent(run_handle, prefix)
        try:
            names = _directory_names(directory)
        finally:
            _close_directory(directory)
        candidates = [
            (
                hashlib.sha256(relative.encode("utf-8")).hexdigest(),
                relative,
                name,
                is_directory,
            )
            for name, is_directory in names
            for relative in (_normalized_relative(prefix, name),)
        ]
        for name_hash, relative, name, is_directory in sorted(
            candidates,
            key=lambda item: (
                item[0],
                "directory" if item[3] else "file",
                0,
            ),
        ):
            casefolded = relative.casefold()
            if name_hash in seen_hashes or casefolded in seen_casefolded:
                raise _fail("private recovery mutable names collide")
            if is_directory:
                size = 0
                stable = True
                digest = None
            else:
                parent_handle = _open_mutable_parent(run_handle, prefix)
                try:
                    size, stable, digest = _read_relative_file(
                        parent_handle,
                        name,
                        max_bytes=limits.max_stable_member_bytes,
                        required_stable=False,
                    )
                finally:
                    _close_directory(parent_handle)
            if (
                len(entries) + 1 > limits.max_entries
                or total + size > limits.max_total_observed_bytes
            ):
                overflow = True
                complete = False
                break
            seen_hashes.add(name_hash)
            seen_casefolded.add(casefolded)
            entries.append(
                PrivateMutableEntry(
                    relative_name_sha256=name_hash,
                    kind="directory" if is_directory else "file",
                    observed_size_bytes=size,
                    stable=stable,
                    sha256=digest,
                )
            )
            total += size
            if is_directory:
                pending.append(relative)
    ordered = tuple(
        sorted(
            entries,
            key=lambda item: (item.relative_name_sha256, item.kind, item.observed_size_bytes),
        )
    )
    return (
        PrivateMutableTree(
            present=True,
            mutable=True,
            entry_count=len(ordered),
            observed_total_bytes=total,
            entries=ordered,
        ),
        complete,
        overflow,
    )


def _open_mutable_parent(run_handle: int, relative: str) -> int:
    current = _open_relative_directory(run_handle, ".p")
    try:
        for part in relative.split("/") if relative else ():
            child = _open_relative_directory(current, part)
            _close_directory(current)
            current = child
        return current
    except BaseException:
        _close_directory(current)
        raise


def _open_observation_run(boundary: PrivateRecoveryBoundary) -> tuple[int, str]:
    safe_key = _validated_run_key(boundary.run_key)
    expected_root = _validated_identity(boundary.root_identity)
    expected_private = _validated_identity(boundary.private_root_identity)
    root = Path(boundary.output_root)
    if os.name == "nt":
        root_handle: int | None = None
        private_handle: int | None = None
        run_handle: int | None = None
        try:
            root_handle = evidence._windows_open_root_directory(root)
            if evidence._windows_directory_identity(
                evidence._windows_handle_information(root_handle)
            ) != expected_root:
                raise _fail("private recovery output root identity changed")
            private_handle = evidence._windows_nt_create(
                root_handle,
                PRIVATE_RECOVERY_DIRECTORY,
                directory=True,
                create_new=False,
            )
            if evidence._windows_directory_identity(
                evidence._windows_handle_information(private_handle)
            ) != expected_private:
                raise _fail("private recovery directory identity changed")
            run_handle = evidence._windows_nt_create(
                private_handle,
                safe_key,
                directory=True,
                create_new=False,
            )
            return run_handle, safe_key
        except PrivateRecoveryError:
            raise
        except evidence.EvidenceStoreError as exc:
            raise _fail("private recovery directory is unsafe") from exc
        finally:
            if private_handle is not None:
                evidence._windows_close_handle(private_handle)
            if root_handle is not None:
                evidence._windows_close_handle(root_handle)
    root_handle = _portable_open_directory(root)
    private_handle: int | None = None
    run_handle: int | None = None
    try:
        if _portable_identity(os.fstat(root_handle)) != expected_root:
            raise _fail("private recovery output root identity changed")
        private_handle = _open_relative_directory(root_handle, PRIVATE_RECOVERY_DIRECTORY)
        if _portable_identity(os.fstat(private_handle)) != expected_private:
            raise _fail("private recovery directory identity changed")
        run_handle = _open_relative_directory(private_handle, safe_key)
        return run_handle, safe_key
    finally:
        if private_handle is not None:
            _close_directory(private_handle)
        _close_directory(root_handle)


def observe_private_recovery(
    boundary: PrivateRecoveryBoundary,
    *,
    limits: PrivateRecoveryLimits = PrivateRecoveryLimits(),
) -> PrivateRecoverySnapshot:
    _require_valid_limits(limits)
    run_handle, safe_key = _open_observation_run(boundary)
    try:
        top_level_names = {name for name, _ in _directory_names(run_handle)}
        static_members: list[PrivateStaticMember] = []
        for role in PRIVATE_ROLES:
            if role not in top_level_names:
                static_members.append(
                    PrivateStaticMember(role=role, present=False, size_bytes=None, sha256=None)
                )
                continue
            size, stable, digest = _read_relative_file(
                run_handle,
                role,
                max_bytes=limits.max_stable_member_bytes,
                required_stable=True,
            )
            assert stable and digest is not None
            static_members.append(
                PrivateStaticMember(role=role, present=True, size_bytes=size, sha256=digest)
            )
        mutable_tree, complete, overflow = _observe_mutable_tree(
            run_handle,
            limits,
            present=".p" in top_level_names,
        )
    finally:
        _close_directory(run_handle)
    return PrivateRecoverySnapshot(
        schema_version=1,
        kind="reliability-private-recovery-snapshot",
        run_key=safe_key,
        namespace_identity_sha256=_validated_identity(boundary.private_root_identity),
        retained=True,
        observation_complete=complete,
        overflow=overflow,
        limits=limits,
        static_members=tuple(static_members),
        mutable_tree=mutable_tree,
    )


def write_private_recovery_snapshot(
    output_root: Path,
    run_key: str,
    snapshot: PrivateRecoverySnapshot,
) -> evidence.ArtifactCommitment:
    safe_key = _validated_run_key(run_key)
    if snapshot.run_key != safe_key:
        raise _fail("private recovery snapshot run key does not match output run")
    _require_valid_limits(snapshot.limits)
    canonical = (
        json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(canonical) > snapshot.limits.max_snapshot_bytes:
        raise _fail("private recovery snapshot exceeds size limit")
    return evidence.write_artifact(
        output_root,
        safe_key,
        "log",
        canonical,
        name="private-recovery",
    )


@dataclass
class _DeleteNode:
    role: str
    relative: str
    name: str
    parent: int
    handle: int
    is_directory: bool
    size_bytes: int
    sha256: str | None
    depth: int


class PrivateRecoveryDeleteTransaction:
    """A fully prevalidated, no-follow deletion transaction.

    No deletion occurs while the transaction is built.  The caller may only
    delete the four fixed roles, in the fixed recovery order, and then the
    run-key leaf.  On Windows every object is held without delete sharing from
    validation through disposition; on POSIX every unlink/rmdir is relative to
    its already-open parent and rechecks the held inode immediately first.
    """

    def __init__(
        self,
        *,
        run_key: str,
        namespace_handle: int,
        leaf_handle: int,
        nodes: tuple[_DeleteNode, ...],
    ) -> None:
        self.run_key = run_key
        self.namespace_handle = namespace_handle
        self.leaf_handle = leaf_handle
        self.nodes = list(nodes)
        self.mutation_count = 0
        self._closed = False

    def role_payload(self, role: str) -> bytes:
        matches = [node for node in self.nodes if node.role == role and node.relative == role]
        if len(matches) != 1 or matches[0].is_directory:
            raise _fail("private recovery static member is unavailable")
        return _read_delete_node(matches[0])

    def delete_role(self, role: str) -> None:
        if role not in {".p", ".c", ".h", ".o"}:
            raise _fail("private recovery deletion role is invalid")
        selected = [node for node in self.nodes if node.role == role]
        for node in sorted(selected, key=lambda item: (item.depth, item.relative), reverse=True):
            _delete_held_node(node)
            self.nodes.remove(node)
            self.mutation_count += 1

    def delete_leaf(self) -> None:
        if self.nodes:
            raise _fail("private recovery run directory is not empty")
        if os.name == "nt":
            _windows_mark_delete(self.leaf_handle)
            evidence._windows_close_handle(self.leaf_handle)
        else:
            _portable_revalidate_name(self.namespace_handle, self.run_key, self.leaf_handle)
            os.rmdir(self.run_key, dir_fd=self.namespace_handle)
            os.close(self.leaf_handle)
        self.leaf_handle = -1
        self.mutation_count += 1

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for node in reversed(self.nodes):
            try:
                _close_delete_handle(node.handle)
            except OSError:
                pass
        self.nodes.clear()
        for handle in (self.leaf_handle, self.namespace_handle):
            if handle != -1:
                try:
                    _close_delete_handle(handle)
                except OSError:
                    pass
        self.leaf_handle = -1
        self.namespace_handle = -1

    def __enter__(self) -> "PrivateRecoveryDeleteTransaction":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _windows_open_delete_relative(parent: int, name: str, *, directory: bool) -> int:
    import ctypes

    _buffer, unicode_name = evidence._windows_relative_name(name)
    attributes = evidence._WindowsObjectAttributes(
        Length=ctypes.sizeof(evidence._WindowsObjectAttributes),
        RootDirectory=parent,
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=0x40 | 0x1000,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    status_block = evidence._WindowsIoStatusBlock()
    output = ctypes.c_void_p()
    nt_create_file = ctypes.WinDLL("ntdll").NtCreateFile
    nt_create_file.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(evidence._WindowsObjectAttributes),
        ctypes.POINTER(evidence._WindowsIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    nt_create_file.restype = ctypes.c_long
    status = nt_create_file(
        ctypes.byref(output),
        0x00010000 | 0x00100000 | 0x80 | 0x1,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        0x80,
        0x1,  # share read only: deny replacement, deletion, and new writers
        1,
        0x20 | 0x00200000 | (0x1 if directory else 0x40),
        None,
        0,
    )
    if status < 0 or output.value is None:
        raise _fail("private recovery delete lease is unavailable")
    handle = int(output.value)
    try:
        information = evidence._windows_handle_information(handle)
        if information.FileAttributes & evidence._FILE_ATTRIBUTE_REPARSE_POINT:
            raise _fail("private recovery delete lease rejected a reparse member")
        if directory != bool(information.FileAttributes & 0x10):
            raise _fail("private recovery delete lease has the wrong kind")
        return handle
    except BaseException:
        evidence._windows_close_handle(handle)
        raise


def _windows_mark_delete(handle: int) -> None:
    import ctypes

    class _Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_int)]

    disposition = _Disposition(1)
    setter = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    setter.restype = ctypes.c_int
    if not setter(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise OSError(ctypes.get_last_error(), "private recovery deletion failed")


def _windows_read_handle(handle: int, size: int) -> bytes:
    import ctypes

    if size < 0 or size > MAX_PRIVATE_RECOVERY_STABLE_MEMBER_BYTES:
        raise _fail("private recovery delete member exceeds size limit")
    reader = ctypes.WinDLL("kernel32", use_last_error=True).ReadFile
    reader.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    reader.restype = ctypes.c_int
    seeker = ctypes.WinDLL("kernel32", use_last_error=True).SetFilePointerEx
    seeker.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint32]
    seeker.restype = ctypes.c_int
    if not seeker(handle, 0, None, 0):
        raise _fail("private recovery delete member could not be read")
    payload = bytearray()
    while len(payload) < size:
        request = min(1024 * 1024, size - len(payload))
        buffer = ctypes.create_string_buffer(request)
        read = ctypes.c_uint32()
        if not reader(handle, buffer, request, ctypes.byref(read), None):
            raise _fail("private recovery delete member could not be read")
        if read.value == 0:
            break
        payload.extend(buffer.raw[: read.value])
    if len(payload) != size:
        raise _fail("private recovery delete member changed")
    return bytes(payload)


def _portable_open_delete_relative(parent: int, name: str, *, directory: bool) -> int:
    flags = _PORTABLE_DIRECTORY_FLAGS if directory else _FILE_OPEN_FLAGS
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
        opened = os.fstat(descriptor)
        if directory != stat.S_ISDIR(opened.st_mode) or (not directory and not stat.S_ISREG(opened.st_mode)):
            raise _fail("private recovery delete lease has the wrong kind")
        return descriptor
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise


def _open_delete_relative(parent: int, name: str, *, directory: bool) -> int:
    if os.name == "nt":
        return _windows_open_delete_relative(parent, name, directory=directory)
    return _portable_open_delete_relative(parent, name, directory=directory)


def _close_delete_handle(handle: int) -> None:
    if os.name == "nt":
        evidence._windows_close_handle(handle)
    else:
        os.close(handle)


def _delete_handle_size(handle: int) -> int:
    if os.name == "nt":
        information = evidence._windows_handle_information(handle)
        return (information.FileSizeHigh << 32) | information.FileSizeLow
    return os.fstat(handle).st_size


def _read_delete_handle(handle: int, size: int) -> bytes:
    if os.name == "nt":
        return _windows_read_handle(handle, size)
    os.lseek(handle, 0, os.SEEK_SET)
    payload = bytearray()
    while len(payload) <= MAX_PRIVATE_RECOVERY_STABLE_MEMBER_BYTES:
        chunk = os.read(handle, min(1024 * 1024, MAX_PRIVATE_RECOVERY_STABLE_MEMBER_BYTES + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    if len(payload) != size or len(payload) > MAX_PRIVATE_RECOVERY_STABLE_MEMBER_BYTES:
        raise _fail("private recovery delete member changed")
    return bytes(payload)


def _read_delete_node(node: _DeleteNode) -> bytes:
    if node.is_directory:
        raise _fail("private recovery delete member has the wrong kind")
    payload = _read_delete_handle(node.handle, node.size_bytes)
    if hashlib.sha256(payload).hexdigest() != node.sha256:
        raise _fail("private recovery delete member changed")
    return payload


def _portable_revalidate_name(parent: int, name: str, handle: int) -> None:
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise _fail("private recovery delete member changed") from exc
    held = os.fstat(handle)
    if stat.S_ISLNK(named.st_mode) or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
        raise _fail("private recovery delete member changed")


def _delete_held_node(node: _DeleteNode) -> None:
    if os.name == "nt":
        _windows_mark_delete(node.handle)
        evidence._windows_close_handle(node.handle)
    else:
        _portable_revalidate_name(node.parent, node.name, node.handle)
        if node.is_directory:
            os.rmdir(node.name, dir_fd=node.parent)
        else:
            os.unlink(node.name, dir_fd=node.parent)
        os.close(node.handle)
    node.handle = -1


def _open_delete_tree(
    leaf_handle: int,
    snapshot: PrivateRecoverySnapshot,
) -> tuple[_DeleteNode, ...]:
    expected_static = {member.role: member for member in snapshot.static_members}
    expected_mutable = {
        (entry.relative_name_sha256, entry.kind): entry
        for entry in snapshot.mutable_tree.entries
    }
    nodes: list[_DeleteNode] = []
    opened_directories: list[int] = []
    try:
        top = dict(_directory_names(leaf_handle))
        expected_top = {
            **{role: False for role, member in expected_static.items() if member.present},
            **({".p": True} if snapshot.mutable_tree.present else {}),
        }
        if top != expected_top:
            raise _fail("private recovery delete membership changed")
        for role in (".o", ".h", ".c"):
            member = expected_static[role]
            if not member.present:
                continue
            handle = _open_delete_relative(leaf_handle, role, directory=False)
            size = _delete_handle_size(handle)
            payload = _read_delete_handle(handle, size)
            digest = hashlib.sha256(payload).hexdigest()
            if size != member.size_bytes or digest != member.sha256:
                _close_delete_handle(handle)
                raise _fail("private recovery static commitment changed")
            nodes.append(_DeleteNode(role, role, role, leaf_handle, handle, False, size, digest, 0))
        if snapshot.mutable_tree.present:
            mutable = _open_delete_relative(leaf_handle, ".p", directory=True)
            opened_directories.append(mutable)
            nodes.append(_DeleteNode(".p", ".p", ".p", leaf_handle, mutable, True, 0, None, 0))
            pending: list[tuple[int, str, int]] = [(mutable, "", 1)]
            observed: dict[tuple[str, str], tuple[int, str | None]] = {}
            while pending:
                parent, prefix, depth = pending.pop(0)
                for name, is_directory in _directory_names(parent):
                    relative = _normalized_relative(prefix, name)
                    digest_name = hashlib.sha256(relative.encode("utf-8")).hexdigest()
                    handle = _open_delete_relative(parent, name, directory=is_directory)
                    if is_directory:
                        size = 0
                        digest = None
                        pending.append((handle, relative, depth + 1))
                        opened_directories.append(handle)
                    else:
                        size = _delete_handle_size(handle)
                        payload = _read_delete_handle(handle, size)
                        digest = hashlib.sha256(payload).hexdigest()
                    key = (digest_name, "directory" if is_directory else "file")
                    if key in observed:
                        _close_delete_handle(handle)
                        raise _fail("private recovery mutable commitment collides")
                    observed[key] = (size, digest)
                    nodes.append(_DeleteNode(".p", relative, name, parent, handle, is_directory, size, digest, depth))
            if set(observed) != set(expected_mutable):
                raise _fail("private recovery mutable membership changed")
            for key, (size, digest) in observed.items():
                expected = expected_mutable[key]
                if not expected.stable or expected.observed_size_bytes != size or expected.sha256 != digest:
                    raise _fail("private recovery mutable commitment changed")
        return tuple(nodes)
    except BaseException:
        for node in reversed(nodes):
            if node.handle != -1:
                try:
                    _close_delete_handle(node.handle)
                except OSError:
                    pass
        raise


def open_private_recovery_delete_transaction(
    output_root: Path,
    run_key: str,
    *,
    expected_root_identity: str,
    expected_namespace_identity: str,
    expected_leaf_identity: str,
    snapshot: PrivateRecoverySnapshot,
) -> PrivateRecoveryDeleteTransaction:
    """Lease and prevalidate every private member without deleting any byte."""
    safe_key = _validated_run_key(run_key)
    expected_root = _validated_identity(expected_root_identity)
    expected_namespace = _validated_identity(expected_namespace_identity)
    expected_leaf = _validated_identity(expected_leaf_identity)
    if (
        snapshot.run_key != safe_key
        or snapshot.namespace_identity_sha256 != expected_namespace
        or not snapshot.observation_complete
        or snapshot.overflow
        or any(not entry.stable for entry in snapshot.mutable_tree.entries)
    ):
        raise _fail("private recovery snapshot cannot authorize deletion")
    root = evidence.validate_directory_identity(Path(output_root), expected_root)[0]
    if os.name == "nt":
        root_handle = evidence._windows_open_root_directory(root)
        namespace_handle = -1
        leaf_handle = -1
        try:
            namespace_handle = _windows_open_delete_relative(
                root_handle, PRIVATE_RECOVERY_DIRECTORY, directory=True
            )
            namespace_information = evidence._windows_handle_information(namespace_handle)
            if evidence._windows_directory_identity(namespace_information) != expected_namespace:
                raise _fail("private recovery namespace identity changed")
            leaf_handle = _windows_open_delete_relative(namespace_handle, safe_key, directory=True)
            leaf_information = evidence._windows_handle_information(leaf_handle)
            if evidence._windows_directory_identity(leaf_information) != expected_leaf:
                raise _fail("private recovery leaf identity changed")
            nodes = _open_delete_tree(leaf_handle, snapshot)
            return PrivateRecoveryDeleteTransaction(
                run_key=safe_key,
                namespace_handle=namespace_handle,
                leaf_handle=leaf_handle,
                nodes=nodes,
            )
        except BaseException:
            for handle in (leaf_handle, namespace_handle):
                if handle != -1:
                    evidence._windows_close_handle(handle)
            raise
        finally:
            evidence._windows_close_handle(root_handle)
    root_handle = _portable_open_directory(root)
    namespace_handle = -1
    leaf_handle = -1
    try:
        namespace_handle = _open_delete_relative(
            root_handle, PRIVATE_RECOVERY_DIRECTORY, directory=True
        )
        if _portable_identity(os.fstat(namespace_handle)) != expected_namespace:
            raise _fail("private recovery namespace identity changed")
        leaf_handle = _open_delete_relative(namespace_handle, safe_key, directory=True)
        if _portable_identity(os.fstat(leaf_handle)) != expected_leaf:
            raise _fail("private recovery leaf identity changed")
        nodes = _open_delete_tree(leaf_handle, snapshot)
        return PrivateRecoveryDeleteTransaction(
            run_key=safe_key,
            namespace_handle=namespace_handle,
            leaf_handle=leaf_handle,
            nodes=nodes,
        )
    except BaseException:
        for handle in (leaf_handle, namespace_handle):
            if handle != -1:
                os.close(handle)
        raise
    finally:
        os.close(root_handle)
