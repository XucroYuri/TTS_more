from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class PortableFileError(OSError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _PortableFileChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class _DirectoryIdentity:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


def safe_read_bytes(
    root: Path,
    path: Path,
    *,
    max_bytes: int,
    label: str,
    retries: int = 2,
    allow_missing: bool = False,
) -> bytes | None:
    """Read one contained regular file without trusting path lookup races.

    Every attempt snapshots the root/ancestor/file identities, opens a handle,
    validates the handle with fstat, performs a bounded read, and proves that
    both the path and ancestor chain still name the same objects afterwards.
    Atomic replacement races are retried a finite number of times.
    """

    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if type(retries) is not int or not 0 <= retries <= 8:
        raise ValueError("retries must be between 0 and 8")
    lexical_root = Path(os.path.abspath(Path(root).expanduser()))
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    try:
        candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise PortableFileError("PORTABLE_PATH_ESCAPE", f"{label} escapes package root") from exc

    last_change: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            ancestors = _snapshot_ancestors(lexical_root, candidate.parent, label)
            try:
                before = _snapshot_file(candidate, max_bytes=max_bytes, label=label)
            except FileNotFoundError:
                if allow_missing:
                    _verify_ancestors(ancestors, lexical_root, candidate.parent, label)
                    if not candidate.exists():
                        return None
                    raise _PortableFileChanged(f"{label} appeared during read")
                raise

            with _open_binary(candidate) as handle:
                opened_before = _snapshot_handle(handle, max_bytes=max_bytes, label=label)
                if opened_before != before:
                    raise _PortableFileChanged(f"{label} changed before open")
                content = handle.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise PortableFileError("PORTABLE_FILE_TOO_LARGE", f"{label} is too large")
                opened_after = _snapshot_handle(handle, max_bytes=max_bytes, label=label)
                if opened_after != opened_before:
                    raise _PortableFileChanged(f"{label} changed while reading")

            after = _snapshot_file(candidate, max_bytes=max_bytes, label=label)
            if after != opened_after:
                raise _PortableFileChanged(f"{label} path changed after read")
            _verify_ancestors(ancestors, lexical_root, candidate.parent, label)
            physical = candidate.resolve(strict=True)
            try:
                physical.relative_to(lexical_root.resolve(strict=True))
            except ValueError as exc:
                raise PortableFileError(
                    "PORTABLE_PATH_ESCAPE", f"{label} resolves outside package root"
                ) from exc
            return content
        except PortableFileError:
            raise
        except (FileNotFoundError, PermissionError, _PortableFileChanged) as exc:
            last_change = exc
            if attempt < retries:
                continue
        except OSError as exc:
            last_change = exc
            if attempt < retries:
                continue
        break
    raise PortableFileError(
        "PORTABLE_FILE_CHANGED", f"{label} changed during a bounded read"
    ) from last_change


def _snapshot_ancestors(root: Path, parent: Path, label: str) -> tuple[_DirectoryIdentity, ...]:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise PortableFileError("PORTABLE_PATH_ESCAPE", f"{label} parent escapes package root") from exc
    current = root
    identities: list[_DirectoryIdentity] = []
    for part in (Path("."), *relative.parts):
        if part != Path("."):
            current = current / part
        metadata = current.lstat()
        if _is_reparse_point(current, metadata):
            raise PortableFileError("PORTABLE_PATH_REPARSE", f"{label} parent traverses a reparse point")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PortableFileError("PORTABLE_PATH_INVALID", f"{label} parent is not a directory")
        identities.append(
            _DirectoryIdentity(current, int(metadata.st_dev), int(metadata.st_ino))
        )
    if _path_key(root.resolve(strict=True)) != _path_key(root):
        raise PortableFileError("PORTABLE_PATH_REPARSE", "package root is not a stable physical path")
    return tuple(identities)


def _verify_ancestors(
    before: tuple[_DirectoryIdentity, ...], root: Path, parent: Path, label: str
) -> None:
    after = _snapshot_ancestors(root, parent, label)
    if after != before:
        raise _PortableFileChanged(f"{label} parent changed during read")


def _snapshot_file(path: Path, *, max_bytes: int, label: str) -> _FileIdentity:
    metadata = path.lstat()
    return _validated_file_identity(metadata, path, max_bytes=max_bytes, label=label)


def _snapshot_handle(handle: BinaryIO, *, max_bytes: int, label: str) -> _FileIdentity:
    metadata = os.fstat(handle.fileno())
    return _validated_file_identity(metadata, None, max_bytes=max_bytes, label=label)


def _validated_file_identity(
    metadata: os.stat_result,
    path: Path | None,
    *,
    max_bytes: int,
    label: str,
) -> _FileIdentity:
    if not stat.S_ISREG(metadata.st_mode):
        raise PortableFileError("PORTABLE_FILE_INVALID", f"{label} is not a regular file")
    if path is not None and _is_reparse_point(path, metadata):
        raise PortableFileError("PORTABLE_PATH_REPARSE", f"{label} is a reparse point")
    if int(metadata.st_nlink) != 1:
        raise PortableFileError("PORTABLE_PATH_HARDLINK", f"{label} is a hard link")
    if int(metadata.st_size) > max_bytes:
        raise PortableFileError("PORTABLE_FILE_TOO_LARGE", f"{label} is too large")
    return _FileIdentity(
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _is_reparse_point(path: Path, metadata: os.stat_result | None = None) -> bool:
    current = metadata or path.lstat()
    attributes = int(getattr(current, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & flag)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path)).casefold()


def _open_binary(path: Path) -> BinaryIO:
    return path.open("rb")


__all__ = ["PortableFileError", "safe_read_bytes"]
