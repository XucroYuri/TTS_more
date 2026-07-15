from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


STORE_SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.025


@dataclass(frozen=True)
class ServiceDocument:
    schema_version: int | None
    services: list[dict[str, object]]


class ServicePostCommitError(RuntimeError):
    """The document is durable, but its in-process publication failed."""


def read_service_document(path: Path) -> ServiceDocument:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"services store is unreadable: {exc}") from exc
    if isinstance(payload, list):
        services = payload
        schema_version = None
    elif isinstance(payload, dict):
        if set(payload) != {"schema_version", "services"}:
            raise ValueError("services store has unknown or missing document fields")
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != STORE_SCHEMA_VERSION:
            raise ValueError("services store schema_version is unsupported")
        services = payload.get("services")
    else:
        raise ValueError("services store must be a legacy list or versioned object")
    if not isinstance(services, list) or any(not isinstance(item, dict) for item in services):
        raise ValueError("services store services must be a list of objects")
    return ServiceDocument(schema_version=schema_version, services=services)


def update_service_document(
    path: Path,
    updater: Callable[[ServiceDocument], ServiceDocument],
    *,
    default_schema_version: int | None,
    after_write: Callable[[ServiceDocument], None] | None = None,
) -> ServiceDocument:
    """Lock, replace, then publish under the same lock.

    ``after_write`` is only for non-blocking in-memory publication. It must not
    re-enter this store. A callback failure is reported explicitly after the
    durable replace so callers never describe the committed mutation as absent.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with services_store_lock(path.parent):
        current = (
            read_service_document(path)
            if path.exists()
            else ServiceDocument(default_schema_version, [])
        )
        updated = updater(current)
        write_service_document_atomic(path, updated)
        if after_write is not None:
            try:
                after_write(updated)
            except Exception as exc:
                raise ServicePostCommitError(
                    "service document was committed but in-memory publication failed"
                ) from exc
        return updated


def write_service_document_atomic(path: Path, document: ServiceDocument) -> None:
    payload: object = (
        {"schema_version": document.schema_version, "services": document.services}
        if document.schema_version is not None
        else document.services
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def services_store_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / ".services.lock"
    with lock_path.open("a+b", buffering=0) as handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _acquire_os_lock(handle)
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out acquiring services store lock") from exc
                time.sleep(min(LOCK_POLL_SECONDS, remaining))
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            _release_os_lock(handle)


def _acquire_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
