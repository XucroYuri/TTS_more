"""Durable GPU ownership fences; no runtime recovery or HTTP reset API.

The sidecar lock is retained on disk to distinguish a first deployment from a
deleted journal. A maintenance tool may replace a journal only after separately
proving that all former GPU holders have finished cleanup.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .protocol import CoordinationUnavailable


class JournalError(CoordinationUnavailable):
    pass


def _epoch(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _regular(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise JournalError("coordination journal is not a regular private file")


class GPUJournal:
    MAX_BYTES = 65536

    def __init__(self, path: str | Path, groups: Mapping[str, Iterable[str]], epoch: str) -> None:
        self.path = Path(path).absolute()
        self.epoch = epoch
        self._registrations = {group: sorted(participants) for group, participants in groups.items()}
        self._lock_file = None
        self._state: dict[str, dict[str, Any]] = {}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Journals belong in an operator-owned local directory. Reparse
            # ancestors would make the lock and atomic replace target ambiguous.
            for directory in (self.path.parent, *self.path.parent.parents):
                metadata = directory.lstat()
                if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise JournalError("coordination journal directory is indirect")
            lock_path = self.path.with_name(self.path.name + ".lock")
            lock_existed = lock_path.exists() or lock_path.is_symlink()
            if lock_existed:
                _regular(lock_path)
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self._lock_file = os.fdopen(descriptor, "r+b", buffering=0)
            if os.fstat(self._lock_file.fileno()).st_size == 0:
                self._lock_file.write(b"\0")
            self._lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                _regular(self.path)
                with self.path.open("rb") as stream:
                    raw = stream.read(self.MAX_BYTES + 1)
                self._state = self._read(raw)
            else:
                if lock_existed:
                    raise JournalError("coordination journal is missing after prior deployment")
                self._state = {group: {"dirty": False, "lease_hash": None, "lease_epoch": None} for group in groups}
                self._write(self._state)
        except Exception as exc:
            self.close()
            if isinstance(exc, JournalError):
                raise
            raise JournalError("coordination journal is unavailable") from None

    def _read(self, raw: bytes) -> dict[str, dict[str, Any]]:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate journal field")
                result[key] = value
            return result

        try:
            if len(raw) > self.MAX_BYTES:
                raise ValueError("journal too large")
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
            if not isinstance(document, dict) or set(document) != {"version", "epoch", "registrations", "groups"}:
                raise ValueError("invalid journal document")
            if type(document["version"]) is not int or document["version"] != 1 or not _epoch(document["epoch"]):
                raise ValueError("invalid journal version or epoch")
            if document["registrations"] != self._registrations or not isinstance(document["groups"], dict) or set(document["groups"]) != set(self._registrations):
                raise ValueError("journal registrations changed")
            for entry in document["groups"].values():
                if not isinstance(entry, dict) or set(entry) != {"dirty", "lease_hash", "lease_epoch"} or not isinstance(entry["dirty"], bool):
                    raise ValueError("invalid journal fence")
                if entry["dirty"]:
                    digest = entry["lease_hash"]
                    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest) or not _epoch(entry["lease_epoch"]):
                        raise ValueError("invalid journal holder identity")
                elif entry["lease_hash"] is not None or entry["lease_epoch"] is not None:
                    raise ValueError("clean journal contains an ownership claim")
            return document["groups"]
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise JournalError("coordination journal is invalid") from None

    @property
    def dirty_groups(self) -> set[str]:
        return {group for group, entry in self._state.items() if entry["dirty"]}

    def commit(self, group: str, *, lease_hash: str | None) -> None:
        if self._lock_file is None:
            raise JournalError("coordination journal is closed")
        if group not in self._state:
            raise JournalError("unknown journal resource group")
        if lease_hash is not None and (len(lease_hash) != 64 or any(char not in "0123456789abcdef" for char in lease_hash)):
            raise JournalError("invalid journal lease identity")
        candidate = {key: dict(value) for key, value in self._state.items()}
        candidate[group] = {"dirty": lease_hash is not None, "lease_hash": lease_hash, "lease_epoch": self.epoch if lease_hash else None}
        self._write(candidate)
        self._state = candidate

    def _write(self, state: Mapping[str, Any]) -> None:
        document = {"version": 1, "epoch": self.epoch, "registrations": self._registrations, "groups": state}
        temporary: str | None = None
        try:
            content = json.dumps(document, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if len(content) > self.MAX_BYTES:
                raise JournalError("coordination journal exceeds its size limit")
            if self.path.exists() or self.path.is_symlink():
                _regular(self.path)
            descriptor, temporary = tempfile.mkstemp(prefix=".gpu-fence-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            with self.path.open("r+b") as stream:
                os.fsync(stream.fileno())
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception:
            raise JournalError("coordination journal commit failed") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def close(self) -> None:
        stream, self._lock_file = self._lock_file, None
        if stream is not None:
            try:
                stream.close()  # Closing the descriptor releases either OS lock.
            except OSError:
                pass
