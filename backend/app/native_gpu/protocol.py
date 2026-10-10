"""Small, dependency-free contracts shared with native Python launchers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping


class CoordinationError(RuntimeError):
    code = "coordination_error"


class CoordinationUnavailable(CoordinationError):
    code = "coordination_unavailable"


class LeaseDenied(CoordinationError):
    code = "lease_denied"


class CoordinationTimeout(LeaseDenied):
    code = "coordination_timeout"


class ProtocolError(CoordinationError):
    code = "invalid_request"


def identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ProtocolError(f"{name} must be a nonempty string of at most 256 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProtocolError(f"{name} contains control characters")
    return value


def finite_seconds(value: Any, name: str, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{name} must be a finite nonnegative number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (maximum is not None and result > maximum):
        raise ProtocolError(f"{name} is outside its allowed range")
    return result


def nonnegative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"{name} must be a nonnegative integer")
    return value


@dataclass(frozen=True)
class NativeStatus:
    instance_id: str
    pid: int
    active: int
    waiting: int
    revision: int
    idle_seconds: float
    residency: str
    ready: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NativeStatus":
        expected = {"instance_id", "pid", "active", "waiting", "revision", "idle_seconds", "residency", "ready"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ProtocolError("native status fields do not match the coordination contract")
        instance_id = identifier(value["instance_id"], "instance_id")
        pid = nonnegative_integer(value["pid"], "pid")
        if pid == 0:
            raise ProtocolError("pid must be positive")
        if not isinstance(value["residency"], str) or value["residency"] not in {"cpu", "gpu", "offloading", "restoring", "unknown"}:
            raise ProtocolError("invalid native residency")
        if not isinstance(value["ready"], bool):
            raise ProtocolError("ready must be a boolean")
        return cls(
            instance_id=instance_id,
            pid=pid,
            active=nonnegative_integer(value["active"], "active"),
            waiting=nonnegative_integer(value["waiting"], "waiting"),
            revision=nonnegative_integer(value["revision"], "revision"),
            idle_seconds=finite_seconds(value["idle_seconds"], "idle_seconds"),
            residency=value["residency"],
            ready=value["ready"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ACTION_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "acquire_native": ({"participant", "timeout"}, set()),
    "release_native": ({"token"}, set()),
    "acquire_comfy": ({"holder", "timeout"}, {"resource_group"}),
    "check_comfy": ({"token"}, set()),
    "release_comfy": ({"token", "clean"}, set()),
    "update_native": ({"participant", "status"}, set()),
    "snapshot": (set(), {"resource_group"}),
    "status": (set(), set()),
    "offload_if_idle": ({"instance_id", "revision"}, set()),
}
MAX_WAIT_SECONDS = 120.0


def validate_action(action: Any, payload: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(action, str) or action not in ACTION_FIELDS or not isinstance(payload, dict):
        raise ProtocolError("unknown action or invalid payload")
    required, optional = ACTION_FIELDS[action]
    if not required <= set(payload) or set(payload) - required - optional:
        raise ProtocolError("action payload fields do not match the coordination contract")
    for name in ("participant", "holder", "token", "instance_id", "resource_group"):
        if name in payload:
            identifier(payload[name], name)
    if "timeout" in payload:
        finite_seconds(payload["timeout"], "timeout", maximum=MAX_WAIT_SECONDS)
    if "revision" in payload:
        nonnegative_integer(payload["revision"], "revision")
    if "clean" in payload and not isinstance(payload["clean"], bool):
        raise ProtocolError("clean must be a boolean")
    if "status" in payload:
        NativeStatus.from_dict(payload["status"])
    return action, payload
