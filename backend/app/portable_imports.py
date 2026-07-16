from __future__ import annotations

import hmac
import math
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Callable
from uuid import uuid4

from app.models import PortableComponent
from app.portable_file_io import PortableFileError, safe_read_bytes


_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_SUPPORTED_COMPONENTS = frozenset({"gpt-sovits", "indextts", "cosyvoice"})
_IMPORTER_MAX_BYTES = 2 * 1024 * 1024


class PortableImportPlanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class StoredPortableImportPlan:
    plan_id: str
    plan: Any
    component: PortableComponent
    service_id: str
    package_id: str
    build_id: str
    new_root: Path
    created_at: float
    expires_at: float


class PortableImportPlanStore:
    """Small process-local, single-use store for path-bearing migration plans."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300,
        capacity: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive and finite")
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("capacity must be between 1 and 256")
        self.ttl_seconds = float(ttl_seconds)
        self.capacity = capacity
        self._clock = clock
        self._plans: OrderedDict[str, StoredPortableImportPlan] = OrderedDict()
        import threading

        self._lock = threading.RLock()

    def create(
        self,
        plan: Any,
        *,
        component: PortableComponent,
        service_id: str,
        package_id: str,
        build_id: str,
    ) -> StoredPortableImportPlan:
        digest = getattr(plan, "plan_digest", None)
        raw_root = getattr(plan, "new_root", None)
        if (
            component not in _SUPPORTED_COMPONENTS
            or type(service_id) is not str
            or not service_id
            or type(package_id) is not str
            or not package_id
            or type(build_id) is not str
            or not build_id
            or type(digest) is not str
            or _DIGEST_RE.fullmatch(digest) is None
            or not isinstance(raw_root, Path)
            or not raw_root.is_absolute()
        ):
            raise PortableImportPlanError(
                "PORTABLE_IMPORT_PLAN_INVALID", "portable import plan is invalid"
            )
        now = self._clock()
        stored = StoredPortableImportPlan(
            plan_id=str(uuid4()),
            plan=plan,
            component=component,
            service_id=service_id,
            package_id=package_id,
            build_id=build_id,
            new_root=raw_root,
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._purge_expired(now)
            while len(self._plans) >= self.capacity:
                self._plans.popitem(last=False)
            self._plans[stored.plan_id] = stored
        return stored

    def consume(self, plan_id: str, plan_digest: str) -> StoredPortableImportPlan:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            stored = self._plans.get(plan_id)
            if stored is None or not hmac.compare_digest(
                str(stored.plan.plan_digest), plan_digest
            ):
                raise PortableImportPlanError(
                    "PORTABLE_IMPORT_PLAN_UNAVAILABLE", "portable import plan is unavailable"
                )
            self._plans.pop(plan_id, None)
            return stored

    def invalidate_component(self, component: PortableComponent) -> None:
        with self._lock:
            for plan_id in tuple(self._plans):
                if self._plans[plan_id].component == component:
                    self._plans.pop(plan_id, None)

    def _purge_expired(self, now: float) -> None:
        for plan_id in tuple(self._plans):
            if self._plans[plan_id].expires_at <= now:
                self._plans.pop(plan_id, None)


def project_import_plan(
    stored: StoredPortableImportPlan, *, now: float | None = None
) -> dict[str, object]:
    plan = stored.plan
    current = time.monotonic() if now is None else now
    ttl_seconds = max(0.0, stored.expires_at - stored.created_at)
    remaining_seconds = min(ttl_seconds, max(0.0, stored.expires_at - current))
    return {
        "plan_id": stored.plan_id,
        "plan_digest": _digest(getattr(plan, "plan_digest", None)),
        "expires_in_seconds": int(remaining_seconds),
        "user_file_count": len(plan.user_files),
        "user_bytes": sum(_size(item) for item in plan.user_files),
        "reusable_assets": _relative_items(plan.reusable_assets),
        "reusable_asset_bytes": sum(_size(item) for item in plan.reusable_assets),
        "skipped_assets": _relative_values(plan.skipped_assets),
        "already_present": _relative_values(plan.already_present),
        "old_package_preserved": True,
    }


def project_import_report(report: Any) -> dict[str, object]:
    copied = getattr(report, "copied_user_files", None)
    if type(copied) is not int or copied < 0:
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import result is invalid"
        )
    return {
        "copied_user_files": copied,
        "reused_assets": _relative_values(report.reused_assets),
        "skipped_assets": _relative_values(report.skipped_assets),
        "already_present": _relative_values(report.already_present),
    }


def load_portable_importer(controller_root: Path) -> ModuleType:
    """Load the controller's private migration core from its one fixed path."""

    root = Path(controller_root)
    script = root / "scripts" / "import_portable_data.py"
    try:
        content = safe_read_bytes(
            root,
            script,
            max_bytes=_IMPORTER_MAX_BYTES,
            label="portable import core",
            retries=2,
        )
        assert content is not None
        code = compile(content.decode("utf-8-sig", errors="strict"), str(script), "exec")
        module_name = f"_tts_more_portable_import_{uuid4().hex}"
        module = ModuleType(module_name)
        module.__file__ = str(script)
        module.__package__ = ""
        sys.modules[module_name] = module
        try:
            exec(code, module.__dict__)
            after = safe_read_bytes(
                root,
                script,
                max_bytes=_IMPORTER_MAX_BYTES,
                label="portable import core",
                retries=2,
            )
            if after != content or not callable(getattr(module, "plan_import", None)) or not callable(
                getattr(module, "apply_import", None)
            ):
                raise ValueError("portable import core identity changed")
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        return module
    except (OSError, UnicodeError, ValueError, PortableFileError) as exc:
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_CORE_UNAVAILABLE", "portable import is unavailable"
        ) from exc


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import plan is invalid"
        )
    return value


def _size(item: Any) -> int:
    value = getattr(item, "size_bytes", None)
    if type(value) is not int or value < 0:
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import plan is invalid"
        )
    return value


def _relative_items(items: Any) -> list[str]:
    try:
        return [_relative_path(item.relative_path) for item in items]
    except (AttributeError, TypeError) as exc:
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import plan is invalid"
        ) from exc


def _relative_values(values: Any) -> list[str]:
    try:
        return [_relative_path(value) for value in values]
    except TypeError as exc:
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import plan is invalid"
        ) from exc


def _relative_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value or ":" in value:
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import path summary is invalid"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import path summary is invalid"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PortableImportPlanError(
            "PORTABLE_IMPORT_PROJECTION_INVALID", "portable import path summary is invalid"
        )
    return value


__all__ = [
    "PortableImportPlanError",
    "PortableImportPlanStore",
    "StoredPortableImportPlan",
    "load_portable_importer",
    "project_import_plan",
    "project_import_report",
]
