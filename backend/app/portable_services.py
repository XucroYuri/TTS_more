from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

from pydantic import ValidationError

from app.models import PortableServiceLocator, TTSServiceEndpoint
from app.portable_discovery import PortablePackageDescriptor, inspect_locator_candidate

if os.name == "nt":
    import msvcrt
else:
    import fcntl


STORE_SCHEMA_VERSION = 1
MAX_SEARCH_CHILDREN = 256
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.025

__all__ = ["PortableServiceLocator", "PortableServiceStore", "resolve_locator"]


def resolve_locator(
    controller_root: Path,
    locator: PortableServiceLocator,
    search_roots: Sequence[Path],
) -> PortablePackageDescriptor | None:
    """Resolve a trusted, controller-compatible package by stable package identity."""

    return _inspect_locator(controller_root, locator, search_roots, require_manageable=True)


def _inspect_locator(
    controller_root: Path,
    locator: PortableServiceLocator,
    search_roots: Sequence[Path],
    *,
    require_manageable: bool = False,
) -> PortablePackageDescriptor | None:
    for candidate in _ordered_candidates(controller_root, locator, search_roots):
        descriptor = inspect_locator_candidate(candidate)
        if descriptor is None:
            continue
        if descriptor.component != locator.component or descriptor.package_id != locator.package_id:
            continue
        if require_manageable and not descriptor.manageable:
            continue
        return _with_port_override(descriptor, locator.port_override)
    return None


def _ordered_candidates(
    controller_root: Path,
    locator: PortableServiceLocator,
    search_roots: Sequence[Path],
) -> list[Path]:
    controller = _lexical_absolute(controller_root)
    raw_candidates: list[Path] = []
    if locator.relative_to_tts_more:
        sibling_name = locator.relative_to_tts_more.replace("\\", "/").split("/", 1)[1]
        raw_candidates.append(controller.parent / sibling_name)
    if locator.absolute_path_last_seen:
        raw_candidates.append(_lexical_absolute(Path(locator.absolute_path_last_seen)))

    # The controller's own parent is the first bounded identity-search root.
    raw_candidates.extend(_bounded_root_candidates(controller.parent))
    for search_root in search_roots:
        raw_candidates.extend(_bounded_root_candidates(_lexical_absolute(Path(search_root))))

    ordered: list[Path] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        absolute = _lexical_absolute(candidate)
        identity = _path_identity(absolute)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(absolute)
    return ordered


def _bounded_root_candidates(root: Path) -> list[Path]:
    try:
        if _manifest_path(root).is_file():
            return [root]
        if _is_broad_search_root(root) or not root.is_dir() or _is_reparse_point(root):
            return []
        children = sorted(
            islice(root.iterdir(), MAX_SEARCH_CHILDREN),
            key=lambda path: _path_identity(path),
        )
    except OSError:
        return []
    output: list[Path] = []
    for child in children[:MAX_SEARCH_CHILDREN]:
        try:
            if child.is_dir() and not _is_reparse_point(child) and _manifest_path(child).is_file():
                output.append(child)
        except OSError:
            continue
    return output


def _with_port_override(
    descriptor: PortablePackageDescriptor, port_override: int | None
) -> PortablePackageDescriptor:
    if port_override is None:
        return descriptor
    host = "localhost" if descriptor.default_url.startswith("http://localhost:") else "127.0.0.1"
    return descriptor.model_copy(
        update={
            "port": port_override,
            "default_url": f"http://{host}:{port_override}",
        }
    )


class PortableServiceStore:
    """Versioned, atomic storage for local endpoint records and locator hints."""

    def __init__(self, controller_root: Path) -> None:
        self.controller_root = _lexical_absolute(controller_root)
        self.path = self.controller_root / "data" / "local" / "services.json"

    def load(self) -> list[TTSServiceEndpoint]:
        path = self._safe_path(create_parent=False)
        if not path.exists():
            return []
        raw_services = self._read_raw(path)
        return [self._sanitize_loaded(endpoint) for endpoint in self._validate_services(raw_services)]

    def save(self, services: Sequence[TTSServiceEndpoint | dict[str, object]]) -> list[TTSServiceEndpoint]:
        validated = self._validate_services(list(services))
        path = self._safe_path(create_parent=True)
        with _store_lock(path.parent):
            if path.exists():
                self._validate_services(self._read_raw(path))
            self._write_atomic(path, [endpoint.model_dump(mode="json") for endpoint in validated])
        return validated

    def upsert(self, endpoint: TTSServiceEndpoint) -> list[TTSServiceEndpoint]:
        validated = TTSServiceEndpoint.model_validate(endpoint)
        locator = validated.portable_locator
        if validated.control_kind != "portable-package" or locator is None:
            raise ValueError("portable service upsert requires a portable-package locator")
        descriptor = resolve_locator(self.controller_root, locator, []) or _inspect_locator(
            self.controller_root, locator, []
        )
        if descriptor is None:
            raise ValueError("portable service locator does not identify a complete schema v2 package")
        trusted = self._apply_descriptor(validated, descriptor)

        path = self._safe_path(create_parent=True)
        with _store_lock(path.parent):
            raw_services = self._read_raw(path) if path.exists() else []
            existing = self._validate_services(raw_services)
            retained = [
                item
                for item in existing
                if not (
                    item.portable_locator is not None
                    and item.portable_locator.component == locator.component
                    and item.portable_locator.package_id == locator.package_id
                )
            ]
            retained.append(trusted)
            retained.sort(key=_service_sort_key)
            self._write_atomic(path, [item.model_dump(mode="json") for item in retained])
        return retained

    def _safe_path(self, *, create_parent: bool) -> Path:
        root = self.controller_root
        if not root.is_dir():
            raise FileNotFoundError(f"TTS More root does not exist: {root}")
        if _is_reparse_point(root) or _path_identity(root.resolve(strict=True)) != _path_identity(root):
            raise ValueError("TTS More root is a link or reparse point")
        current = root
        for part in ("data", "local"):
            current = current / part
            if current.exists():
                if _is_reparse_point(current):
                    raise ValueError("services path traverses a link or reparse point")
                if not current.is_dir():
                    raise ValueError("services path parent is not a directory")
            elif create_parent:
                current.mkdir(exist_ok=True)
                if _is_reparse_point(current) or not current.is_dir():
                    raise ValueError("services path parent became a reparse point")
            else:
                return self.path
        if self.path.exists():
            if _is_reparse_point(self.path):
                raise ValueError("services file is a link or reparse point")
            if not self.path.is_file():
                raise ValueError("services path is not a file")
        physical = self.path.resolve(strict=False)
        try:
            physical.relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ValueError("services path escapes the TTS More root") from exc
        return self.path

    def _read_raw(self, path: Path) -> list[dict[str, object]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"services store is unreadable: {exc}") from exc
        if isinstance(payload, list):
            services = payload
        elif isinstance(payload, dict):
            if set(payload) != {"schema_version", "services"}:
                raise ValueError("services store has unknown or missing document fields")
            if payload.get("schema_version") != STORE_SCHEMA_VERSION:
                raise ValueError("services store schema_version is unsupported")
            services = payload.get("services")
        else:
            raise ValueError("services store must be a legacy list or versioned object")
        if not isinstance(services, list) or any(not isinstance(item, dict) for item in services):
            raise ValueError("services store services must be a list of objects")
        return services

    def _validate_services(
        self, services: Sequence[TTSServiceEndpoint | dict[str, object]]
    ) -> list[TTSServiceEndpoint]:
        try:
            return [TTSServiceEndpoint.model_validate(item) for item in services]
        except ValidationError as exc:
            raise ValueError(f"services store contains an invalid endpoint: {exc}") from exc

    def _sanitize_loaded(self, endpoint: TTSServiceEndpoint) -> TTSServiceEndpoint:
        if endpoint.control_kind != "portable-package":
            return endpoint
        locator = endpoint.portable_locator
        descriptor = None
        if locator is not None:
            descriptor = resolve_locator(self.controller_root, locator, []) or _inspect_locator(
                self.controller_root, locator, []
            )
        if descriptor is None:
            return endpoint.model_copy(
                update={
                    "managed": False,
                    "repo_path": None,
                    "start_command": [],
                    "start_cwd": None,
                }
            )
        return self._apply_descriptor(endpoint, descriptor)

    @staticmethod
    def _apply_descriptor(
        endpoint: TTSServiceEndpoint, descriptor: PortablePackageDescriptor
    ) -> TTSServiceEndpoint:
        return endpoint.model_copy(
            update={
                "base_url": descriptor.default_url,
                "managed": descriptor.manageable,
                "repo_path": descriptor.package_root if descriptor.manageable else None,
                "start_command": [],
                "start_cwd": None,
                "setup_state": "ready" if descriptor.initialized else "env_missing",
            }
        )

    @staticmethod
    def _write_atomic(path: Path, services: list[dict[str, object]]) -> None:
        document = {"schema_version": STORE_SCHEMA_VERSION, "services": services}
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _service_sort_key(endpoint: TTSServiceEndpoint) -> tuple[str, str, str]:
    locator = endpoint.portable_locator
    if locator is None:
        return ("0", "", endpoint.service_id)
    return ("1", locator.component, locator.package_id)


@contextmanager
def _store_lock(directory: Path) -> Iterator[None]:
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


def _manifest_path(root: Path) -> Path:
    return root / "package" / "tts-more-package.json"


def _is_broad_search_root(root: Path) -> bool:
    home = _lexical_absolute(Path.home())
    broad_roots = {home, home.parent}
    if root.anchor:
        broad_roots.add(_lexical_absolute(Path(root.anchor)))
    identity = _path_identity(root)
    return any(identity == _path_identity(candidate) for candidate in broad_roots)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _path_identity(path: Path) -> str:
    return unicodedata.normalize("NFKC", os.path.normcase(str(_lexical_absolute(path)))).casefold()


def _is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & flag)
