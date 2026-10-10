"""Private operator configuration; never part of portable service endpoints."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.base import SynthesisCoordinationError, SynthesisPreempted


@dataclass(frozen=True)
class GPUCoordinationConfig:
    resource_group: str
    coordinator_url: str
    token: str = field(repr=False)
    admission_timeout: float = 2.0
    cleanup_timeout: float = 30.0

    def client(self) -> Any:
        from app.native_gpu.transport import CoordinatorClient

        try:
            return CoordinatorClient(self.coordinator_url, self.token, resource_group=self.resource_group)
        except Exception as exc:
            raise SynthesisCoordinationError("Invalid local GPU coordinator endpoint") from exc


def coordination_config(resource_group: str) -> GPUCoordinationConfig | None:
    path = os.environ.get("TTS_MORE_GPU_COORDINATION_CONFIG", "").strip()
    if not path:
        return None
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if document.get("enabled", False) is not True:
            return None
        groups = document["groups"]
        # Unconfigured remote/LAN groups remain independent.
        entry = groups.get(resource_group)
        if entry is None or entry.get("enabled", True) is False:
            return None
        url, token = entry["coordinator_url"], entry["token"]
        if not isinstance(url, str) or not isinstance(token, str) or not token:
            raise ValueError("invalid coordinator credentials")
        times = [float(entry.get("admission_timeout", 2.0)), float(entry.get("cleanup_timeout", 30.0))]
        if not all(math.isfinite(value) and 0 < value <= 30 for value in times):
            raise ValueError("invalid coordination deadlines")
        return GPUCoordinationConfig(resource_group, url, token, *times)
    except Exception as exc:
        # Configuration can contain secrets, so do not echo arbitrary JSON/errors.
        raise SynthesisCoordinationError("Invalid local GPU coordination configuration") from exc


def require_suite_coordination(capabilities: dict[str, Any], config: GPUCoordinationConfig) -> None:
    value = capabilities.get("gpu_coordination", {})
    if (
        not isinstance(value, dict)
        or value.get("enabled") is not True
        or value.get("resource_group") != config.resource_group
        or value.get("protocol_version") != 1
    ):
        raise SynthesisCoordinationError("Suite GPU coordination is not enabled for this resource group")


def admission_probe(client: Any, config: GPUCoordinationConfig, holder: str) -> None:
    from app.native_gpu.protocol import CoordinationTimeout

    try:
        token = client.acquire_comfy(holder, timeout=config.admission_timeout)
    except CoordinationTimeout as exc:
        native_priority_pending(client, config)  # also rejects stale/dirty/unknown ownership
        raise SynthesisPreempted("Waiting for native GPU operations", details={"cleanup_confirmed": True}) from exc
    except Exception as exc:
        raise SynthesisCoordinationError("GPU coordinator admission unavailable") from exc
    valid = False
    released = False
    try:
        try:
            valid = client.check_comfy(token) is True
        finally:
            # Probe never constructs an engine or holds GPU state.
            released = client.release_comfy(token, clean=True) is True
    except Exception as exc:
        raise SynthesisCoordinationError("GPU admission probe status/release unavailable") from exc
    if not released:
        raise SynthesisCoordinationError("GPU admission probe release was not confirmed")
    if not valid:
        native_priority_pending(client, config)
        raise SynthesisPreempted("Native GPU priority requested", details={"cleanup_confirmed": True})


def native_priority_pending(client: Any, config: GPUCoordinationConfig) -> bool:
    try:
        group = client.snapshot()["groups"][config.resource_group]
        lease = group.get("comfy") or {}
        if group.get("recovery_required") or group.get("cleanup_failed") or lease.get("cleanup_failed") or lease.get("reason") in {"comfy_cleanup_unconfirmed", "comfy_heartbeat_expired"}:
            raise SynthesisCoordinationError("GPU cleanup fence requires operator recovery")
        if group.get("state") not in {"comfy_available", "comfy_running", "native_busy", "native_offloading", "blocked", "yielding"}:
            raise SynthesisCoordinationError("GPU coordinator group status is unknown")
        natives = group.get("natives")
        if not isinstance(natives, dict) or not natives or any(
            native.get("fresh") is not True or not isinstance(native.get("status"), dict)
            or (native["status"].get("ready") is not True and group.get("state") != "native_offloading")
            for native in natives.values()
        ):
            raise SynthesisCoordinationError("Native GPU participant status is untrusted")
        if group.get("native_active") or group.get("native_waiting"):
            return True
        if lease.get("revoked") or group.get("state") == "yielding":
            return True
        return any(
            (native.get("status") or {}).get("active") or (native.get("status") or {}).get("waiting")
            for native in group.get("natives", {}).values()
        )
    except SynthesisCoordinationError:
        raise
    except Exception as exc:
        raise SynthesisCoordinationError("GPU coordinator status unavailable") from exc
