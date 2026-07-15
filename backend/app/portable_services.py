from __future__ import annotations

import os
import stat
import unicodedata
from itertools import islice
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from app.models import PortableServiceLocator, TTSServiceEndpoint
from app.portable_endpoint_trust import (
    preflight_service_endpoints,
    require_unique_service_identities,
    sanitize_portable_endpoint,
    trust_resolved_portable_endpoint,
)
from app.portable_discovery import PortablePackageDescriptor, inspect_locator_candidate
from app.service_store_io import (
    ServiceDocument,
    read_service_document,
    update_service_document,
)

STORE_SCHEMA_VERSION = 1
MAX_SEARCH_CHILDREN = 256
MAX_EXPLICIT_SEARCH_ROOTS = 16
MAX_TOTAL_SEARCH_ENTRIES = 512
MAX_TOTAL_CANDIDATES = 512

__all__ = [
    "PortableServiceLocator",
    "PortableServiceStore",
    "discover_bounded_portable_packages",
    "resolve_locator",
]


def discover_bounded_portable_packages(
    controller_root: Path,
    search_roots: Sequence[Path],
) -> list[PortablePackageDescriptor]:
    """Inspect only bounded direct children of the controller parent and explicit roots."""

    controller = _lexical_absolute(controller_root)
    roots = [controller.parent, *search_roots[:MAX_EXPLICIT_SEARCH_ROOTS]]
    remaining_entries = MAX_TOTAL_SEARCH_ENTRIES
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw_root in roots:
        if remaining_entries <= 0 or len(candidates) >= MAX_TOTAL_CANDIDATES:
            break
        root = _lexical_absolute(Path(raw_root))
        bounded, enumerated = _bounded_root_candidates(
            root,
            min(MAX_SEARCH_CHILDREN, remaining_entries),
        )
        remaining_entries -= enumerated
        for candidate in bounded:
            identity = _path_identity(candidate)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(candidate)
            if len(candidates) >= MAX_TOTAL_CANDIDATES:
                break

    descriptors: list[PortablePackageDescriptor] = []
    for candidate in candidates:
        descriptor = inspect_locator_candidate(candidate)
        if descriptor is not None:
            descriptors.append(descriptor)
    return sorted(
        descriptors,
        key=lambda item: (item.component, item.package_id, item.package_root.casefold()),
    )


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

    # Bound both caller-controlled roots and the aggregate directory work.
    remaining_entries = MAX_TOTAL_SEARCH_ENTRIES
    roots = [controller.parent, *search_roots[:MAX_EXPLICIT_SEARCH_ROOTS]]
    for search_root in roots:
        if remaining_entries <= 0 or len(raw_candidates) >= MAX_TOTAL_CANDIDATES:
            break
        candidates, enumerated = _bounded_root_candidates(
            _lexical_absolute(Path(search_root)),
            min(MAX_SEARCH_CHILDREN, remaining_entries),
        )
        remaining_entries -= enumerated
        raw_candidates.extend(candidates[: MAX_TOTAL_CANDIDATES - len(raw_candidates)])

    ordered: list[Path] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        absolute = _lexical_absolute(candidate)
        identity = _path_identity(absolute)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(absolute)
        if len(ordered) >= MAX_TOTAL_CANDIDATES:
            break
    return ordered


def _bounded_root_candidates(root: Path, scan_limit: int) -> tuple[list[Path], int]:
    try:
        if _manifest_path(root).is_file():
            return [root], 0
        if _is_broad_search_root(root) or not root.is_dir() or _is_reparse_point(root):
            return [], 0
        children = sorted(
            islice(root.iterdir(), scan_limit),
            key=lambda path: _path_identity(path),
        )
    except OSError:
        return [], 0
    output: list[Path] = []
    for child in children:
        try:
            if child.is_dir() and not _is_reparse_point(child) and _manifest_path(child).is_file():
                output.append(child)
        except OSError:
            continue
    return output, len(children)


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


def _merge_service_delta(
    current: list[TTSServiceEndpoint],
    baseline: list[TTSServiceEndpoint] | None,
    desired: list[TTSServiceEndpoint],
) -> list[TTSServiceEndpoint]:
    if baseline is None:
        return preflight_service_endpoints(desired)
    current_by_id = {endpoint.service_id: endpoint for endpoint in current}
    baseline_by_id = {
        endpoint.service_id: sanitize_portable_endpoint(endpoint) for endpoint in baseline
    }
    desired_by_id = {
        endpoint.service_id: sanitize_portable_endpoint(endpoint) for endpoint in desired
    }
    for service_id in baseline_by_id.keys() - desired_by_id.keys():
        current_by_id.pop(service_id, None)
    for service_id, endpoint in desired_by_id.items():
        previous = baseline_by_id.get(service_id)
        if previous is None or endpoint.model_dump(mode="json") != previous.model_dump(mode="json"):
            current_by_id[service_id] = endpoint
    merged = sorted(current_by_id.values(), key=_service_sort_key)
    return preflight_service_endpoints(merged)


class PortableServiceStore:
    """Versioned, atomic storage for local endpoint records and locator hints."""

    def __init__(self, controller_root: Path) -> None:
        self.controller_root = _lexical_absolute(controller_root)
        self.path = self.controller_root / "data" / "local" / "services.json"
        self._baseline: list[TTSServiceEndpoint] | None = None

    def load(self) -> list[TTSServiceEndpoint]:
        path = self._safe_path(create_parent=False)
        if not path.exists():
            self._baseline = []
            return []
        raw_services = self._read_raw(path)
        loaded = [self._sanitize_loaded(endpoint) for endpoint in self._validate_services(raw_services)]
        self._baseline = loaded
        return loaded

    def save(self, services: Sequence[TTSServiceEndpoint | dict[str, object]]) -> list[TTSServiceEndpoint]:
        validated = self._validate_services(list(services))
        path = self._safe_path(create_parent=True)
        persisted = [sanitize_portable_endpoint(endpoint) for endpoint in validated]
        merged_result: list[TTSServiceEndpoint] = []

        def merge(current: ServiceDocument) -> ServiceDocument:
            current_endpoints = self._validate_services(current.services)
            merged = _merge_service_delta(current_endpoints, self._baseline, persisted)
            merged_result[:] = merged
            return ServiceDocument(
                STORE_SCHEMA_VERSION,
                [endpoint.model_dump(mode="json") for endpoint in merged],
            )

        update_service_document(path, merge, default_schema_version=STORE_SCHEMA_VERSION)
        self._baseline = list(merged_result)
        return merged_result

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
        resolved: list[TTSServiceEndpoint] = []

        def merge(current: ServiceDocument) -> ServiceDocument:
            existing = [self._sanitize_loaded(item) for item in self._validate_services(current.services)]
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
            validated_retained = preflight_service_endpoints(retained)
            resolved[:] = validated_retained
            return ServiceDocument(
                STORE_SCHEMA_VERSION,
                [
                    sanitize_portable_endpoint(item).model_dump(mode="json")
                    for item in validated_retained
                ],
            )

        update_service_document(path, merge, default_schema_version=STORE_SCHEMA_VERSION)
        self._baseline = list(resolved)
        return resolved

    def replace_component(
        self,
        endpoint: TTSServiceEndpoint,
        *,
        initial_services: Sequence[TTSServiceEndpoint | dict[str, object]] = (),
    ) -> list[TTSServiceEndpoint]:
        """Atomically replace every portable record for one component."""

        validated = TTSServiceEndpoint.model_validate(endpoint)
        locator = validated.portable_locator
        if validated.control_kind != "portable-package" or locator is None:
            raise ValueError("portable component replacement requires a portable-package locator")
        path = self._safe_path(create_parent=True)
        initial = self._validate_services(list(initial_services))
        resolved: list[TTSServiceEndpoint] = []

        def replace(current: ServiceDocument) -> ServiceDocument:
            descriptor = resolve_locator(self.controller_root, locator, []) or _inspect_locator(
                self.controller_root, locator, []
            )
            if descriptor is None:
                raise ValueError(
                    "portable service locator does not identify a complete schema v2 package"
                )
            trusted = self._apply_descriptor(validated, descriptor)
            source = current.services if current.services else initial
            existing = [
                self._sanitize_loaded(item)
                for item in self._validate_services(source)
            ]
            retained = [
                item
                for item in existing
                if item.portable_locator is None
                or item.portable_locator.component != locator.component
            ]
            retained.append(trusted)
            retained.sort(key=_service_sort_key)
            published = preflight_service_endpoints(retained)
            resolved[:] = published
            return ServiceDocument(
                STORE_SCHEMA_VERSION,
                [
                    sanitize_portable_endpoint(item).model_dump(mode="json")
                    for item in published
                ],
            )

        update_service_document(path, replace, default_schema_version=STORE_SCHEMA_VERSION)
        self._baseline = list(resolved)
        return resolved

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
        return read_service_document(path).services

    def _validate_services(
        self, services: Sequence[TTSServiceEndpoint | dict[str, object]]
    ) -> list[TTSServiceEndpoint]:
        try:
            endpoints = [TTSServiceEndpoint.model_validate(item) for item in services]
        except ValidationError as exc:
            raise ValueError(f"services store contains an invalid endpoint: {exc}") from exc
        require_unique_service_identities(endpoints)
        return endpoints

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
            return sanitize_portable_endpoint(endpoint)
        return self._apply_descriptor(endpoint, descriptor)

    @staticmethod
    def _apply_descriptor(
        endpoint: TTSServiceEndpoint, descriptor: PortablePackageDescriptor
    ) -> TTSServiceEndpoint:
        return trust_resolved_portable_endpoint(endpoint, descriptor)


def _service_sort_key(endpoint: TTSServiceEndpoint) -> tuple[str, str, str]:
    locator = endpoint.portable_locator
    if locator is None:
        return ("0", "", endpoint.service_id)
    return ("1", locator.component, locator.package_id)


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
