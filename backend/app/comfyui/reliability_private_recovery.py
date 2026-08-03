from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
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
