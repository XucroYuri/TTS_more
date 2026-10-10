"""Native-priority GPU admission with explicit cleanup acknowledgement.

No driver, native process, or model is controlled here. Trusted native wrappers
publish lifecycle snapshots and implement the atomic offload callback. A lost
Comfy heartbeat closes admission; it never proves that GPU memory was released.
"""

from __future__ import annotations

import secrets
import threading
import time
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .protocol import (
    CoordinationTimeout, CoordinationUnavailable, LeaseDenied, NativeStatus, ProtocolError,
    finite_seconds, identifier, validate_action,
)
from .journal import GPUJournal, JournalError

OffloadCallback = Callable[[str, str, int], NativeStatus | Mapping[str, Any]]


@dataclass
class _Observation:
    status: NativeStatus
    received_at: float


@dataclass
class _ComfyLease:
    token: str
    holder: str
    expires_at: float
    revoked: bool = False
    reason: str | None = None
    cleanup_failed: bool = False


class GPUCoordinator:
    def __init__(
        self,
        groups: Mapping[str, Iterable[str]],
        *,
        idle_seconds: float = 300.0,
        freshness_seconds: float = 5.0,
        comfy_ttl_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        offload_callbacks: Mapping[str, OffloadCallback] | None = None,
        journal_path: str | Path | None = None,
    ) -> None:
        self.idle_seconds = finite_seconds(idle_seconds, "idle_seconds")
        if self.idle_seconds < 300:
            raise ProtocolError("native idle interval must be at least 300 seconds")
        self.freshness_seconds = finite_seconds(freshness_seconds, "freshness_seconds")
        self.comfy_ttl_seconds = finite_seconds(comfy_ttl_seconds, "comfy_ttl_seconds")
        if not self.freshness_seconds or not self.comfy_ttl_seconds:
            raise ProtocolError("freshness and lease TTL must be positive")
        self._groups: dict[str, tuple[str, ...]] = {}
        self._participant_groups: dict[str, str] = {}
        for group, participants in groups.items():
            identifier(group, "resource_group")
            if isinstance(participants, str):
                raise ProtocolError("participants must be a collection of fixed IDs")
            members = tuple(participants)
            if not members or len(set(members)) != len(members):
                raise ProtocolError("each resource group needs unique native participants")
            for participant in members:
                identifier(participant, "participant")
                if participant in self._participant_groups:
                    raise ProtocolError("native participants cannot belong to multiple GPU groups")
                self._participant_groups[participant] = group
            self._groups[group] = members
        if not self._groups:
            raise ProtocolError("at least one resource group is required")
        self._clock = monotonic
        self._condition = threading.Condition(threading.RLock())
        self._observations: dict[str, _Observation] = {}
        self._native_tokens: dict[str, tuple[str, str | None]] = {}
        self._native_waiters: dict[str, int] = {participant: 0 for participant in self._participant_groups}
        self._comfy: dict[str, _ComfyLease] = {}
        self._offloading: dict[str, tuple[str, int]] = {}
        self._offload_failed: dict[str, tuple[str, int]] = {}
        self._callbacks: dict[str, OffloadCallback] = {}
        self.epoch = str(uuid.uuid4())
        self._persistent = journal_path is not None
        self._journal: GPUJournal | None = None
        self._closed = False
        self._recovery_reasons: dict[str, str] = {}
        if journal_path is not None:
            try:
                self._journal = GPUJournal(journal_path, self._groups, self.epoch)
                self._recovery_reasons = {group: "journal_dirty_restart" for group in self._journal.dirty_groups}
            except JournalError:
                self._recovery_reasons = {group: "journal_unavailable" for group in self._groups}
        for participant, callback in (offload_callbacks or {}).items():
            self.set_offload_callback(participant, callback)

    def _group(self, resource_group: str | None) -> str:
        if resource_group is None:
            if len(self._groups) != 1:
                raise LeaseDenied("resource_group is required for a multi-GPU coordinator")
            return next(iter(self._groups))
        identifier(resource_group, "resource_group")
        if resource_group not in self._groups:
            raise LeaseDenied("unknown resource group")
        return resource_group

    def _participant_group(self, participant: str) -> str:
        try:
            return self._participant_groups[participant]
        except (KeyError, TypeError) as exc:
            raise LeaseDenied("unknown native participant") from exc

    def set_offload_callback(self, participant: str, callback: OffloadCallback) -> None:
        self._participant_group(participant)
        if not callable(callback):
            raise ProtocolError("offload callback must be callable")
        with self._condition:
            self._callbacks[participant] = callback
            self._offload_failed.pop(participant, None)
            self._condition.notify_all()

    def update_native(self, participant: str, status: NativeStatus | Mapping[str, Any]) -> None:
        group = self._participant_group(participant)
        status = NativeStatus.from_dict(status.to_dict() if isinstance(status, NativeStatus) else status)
        with self._condition:
            previous = self._observations.get(participant)
            if previous and previous.status.instance_id == status.instance_id:
                if previous.status.pid != status.pid or status.revision < previous.status.revision:
                    raise LeaseDenied("native identity or activity revision regressed")
            if previous and previous.status.instance_id != status.instance_id:
                # A restarted native cannot inherit a lease granted to its old
                # instance. Old tokens remain fences until explicitly returned.
                self._revoke(group, "native_instance_changed")
            self._observations[participant] = _Observation(status, self._clock())
            if self._offload_failed.get(participant) != (status.instance_id, status.revision) or status.residency == "cpu":
                self._offload_failed.pop(participant, None)
            if status.active or status.waiting or not status.ready or status.residency != "cpu":
                self._revoke(group, "native_not_idle_cpu")
            self._condition.notify_all()

    def _native_active(self, group: str) -> int:
        return sum(1 for participant, _instance in self._native_tokens.values() if self._participant_groups[participant] == group)

    def _native_waiting(self, group: str) -> int:
        return sum(self._native_waiters[participant] for participant in self._groups[group])

    def _revoke(self, group: str, reason: str) -> None:
        lease = self._comfy.get(group)
        if lease is not None:
            lease.revoked = True
            lease.reason = lease.reason or reason

    def _native_cpu_reasons(self, group: str) -> list[str]:
        now = self._clock()
        reasons: list[str] = []
        if group in self._recovery_reasons:
            reasons.append("maintenance_recovery_required")
        if self._native_active(group) or self._native_waiting(group):
            reasons.append("native_priority")
        for participant in self._groups[group]:
            observation = self._observations.get(participant)
            if observation is None:
                reasons.append(f"native_status_missing:{participant}")
                continue
            status = observation.status
            if now - observation.received_at >= self.freshness_seconds:
                reasons.append(f"native_status_stale:{participant}")
            if not status.ready:
                reasons.append(f"native_not_ready:{participant}")
            if status.active or status.waiting:
                reasons.append(f"native_busy:{participant}")
            if status.residency != "cpu":
                reasons.append(f"native_not_cpu:{participant}")
            if participant in self._offloading:
                reasons.append(f"native_offloading:{participant}")
            if participant in self._offload_failed:
                reasons.append(f"native_offload_failed:{participant}")
        return reasons

    def _tick(self, group: str) -> None:
        lease = self._comfy.get(group)
        if lease is not None:
            if self._clock() >= lease.expires_at:
                self._revoke(group, "comfy_heartbeat_expired")
                if not lease.cleanup_failed:
                    lease.reason = "comfy_heartbeat_expired"
            if self._native_cpu_reasons(group):
                self._revoke(group, "native_state_untrusted")

    def acquire_native(self, participant: str, timeout: float = 30.0) -> str:
        group = self._participant_group(participant)
        timeout = finite_seconds(timeout, "timeout")
        with self._condition:
            self._assert_operational(group)
            deadline = self._clock() + timeout
            self._native_waiters[participant] += 1
            self._revoke(group, "native_priority")
            self._condition.notify_all()
            try:
                while True:
                    self._assert_operational(group)
                    self._tick(group)
                    if group not in self._comfy and not any(member in self._offloading for member in self._groups[group]):
                        token = secrets.token_urlsafe(32)
                        observation = self._observations.get(participant)
                        instance = observation.status.instance_id if observation else None
                        self._native_tokens[token] = (participant, instance)
                        return token
                    self._wait(deadline, "native GPU admission timed out waiting for confirmed Comfy cleanup")
            finally:
                self._native_waiters[participant] -= 1
                self._condition.notify_all()

    def release_native(self, token: str) -> bool:
        with self._condition:
            result = self._native_tokens.pop(token, None) is not None
            self._condition.notify_all()
            return result

    def _offload_candidate(self, group: str) -> str | None:
        if self._native_active(group) or self._native_waiting(group) or group in self._comfy:
            return None
        if any(member in self._offloading or member in self._offload_failed for member in self._groups[group]):
            return None
        now = self._clock()
        # No offload is initiated when another participant is unknown or busy.
        for participant in self._groups[group]:
            observation = self._observations.get(participant)
            if observation is None:
                return None
            status = observation.status
            if now - observation.received_at >= self.freshness_seconds or not status.ready or status.active or status.waiting or status.residency not in {"cpu", "gpu"}:
                return None
        for participant in self._groups[group]:
            status = self._observations[participant].status
            if (
                status.residency == "gpu"
                and status.idle_seconds >= self.idle_seconds
                and participant in self._callbacks
                and participant not in self._offloading
                and participant not in self._offload_failed
            ):
                return participant
        return None

    def _begin_offload(self, participant: str) -> None:
        status = self._observations[participant].status
        identity = (status.instance_id, status.revision)
        self._offloading[participant] = identity
        callback = self._callbacks[participant]

        def perform() -> None:
            result: NativeStatus | None = None
            try:
                returned = callback(participant, identity[0], identity[1])
                result = NativeStatus.from_dict(returned.to_dict() if isinstance(returned, NativeStatus) else returned)
            except Exception:
                # Callback diagnostics may contain private paths. Only publish a
                # stable failure code; native logs own the detailed diagnostics.
                pass
            with self._condition:
                current = self._observations.get(participant)
                trusted = (
                    result is not None and current is not None
                    and (result.instance_id, result.revision) == identity
                    and (current.status.instance_id, current.status.revision) == identity
                    and result.pid == current.status.pid
                    and result.residency == "cpu" and result.ready
                    and not result.active and not result.waiting
                )
                if trusted:
                    self._observations[participant] = _Observation(result, self._clock())
                    self._offload_failed.pop(participant, None)
                else:
                    self._offload_failed[participant] = identity
                self._offloading.pop(participant, None)
                self._condition.notify_all()

        threading.Thread(target=perform, name="native-gpu-offload", daemon=True).start()

    def acquire_comfy(self, holder: str, timeout: float = 30.0, *, resource_group: str | None = None) -> str:
        identifier(holder, "holder")
        group = self._group(resource_group)
        timeout = finite_seconds(timeout, "timeout")
        with self._condition:
            deadline = self._clock() + timeout
            while True:
                self._assert_operational(group)
                self._tick(group)
                if group not in self._comfy and not self._native_cpu_reasons(group):
                    token = secrets.token_urlsafe(32)
                    self._persist_comfy(group, token)
                    self._comfy[group] = _ComfyLease(token, holder, self._clock() + self.comfy_ttl_seconds)
                    return token
                candidate = self._offload_candidate(group)
                if candidate is not None:
                    self._begin_offload(candidate)
                self._wait(deadline, "Comfy GPU admission timed out waiting for trustworthy idle CPU natives")

    def check_comfy(self, token: str) -> bool:
        """Renew a live lease; a revoked or expired lease can never revive."""
        with self._condition:
            for group, lease in self._comfy.items():
                if secrets.compare_digest(lease.token, token):
                    self._tick(group)
                    if lease.revoked:
                        return False
                    lease.expires_at = self._clock() + self.comfy_ttl_seconds
                    return True
            return False

    def release_comfy(self, token: str, clean: bool) -> bool:
        if not isinstance(clean, bool):
            raise ProtocolError("clean must be a boolean")
        with self._condition:
            for group, lease in tuple(self._comfy.items()):
                if secrets.compare_digest(lease.token, token):
                    if clean:
                        self._persist_comfy(group, None)
                        self._comfy.pop(group)
                    else:
                        self._revoke(group, "comfy_cleanup_unconfirmed")
                        lease.reason = "comfy_cleanup_unconfirmed"
                        lease.cleanup_failed = True
                    self._condition.notify_all()
                    return True
            return False

    def _assert_operational(self, group: str) -> None:
        if group in self._recovery_reasons:
            raise LeaseDenied("GPU coordination requires explicit maintenance recovery")

    def _persist_comfy(self, group: str, token: str | None) -> None:
        if not self._persistent:
            return
        try:
            if self._journal is None:
                raise JournalError("coordination journal is unavailable")
            digest = hashlib.sha256(token.encode("ascii")).hexdigest() if token else None
            self._journal.commit(group, lease_hash=digest)
        except JournalError:
            for resource_group in self._groups:
                self._recovery_reasons[resource_group] = "journal_write_failed"
                self._revoke(resource_group, "journal_write_failed")
            self._condition.notify_all()
            raise CoordinationUnavailable("GPU ownership journal commit failed; maintenance recovery is required") from None

    def close(self) -> None:
        """Stop this controller; retain every persistent ownership fence.

        This is not GPU cleanup. A replacement controller must honor dirty
        entries and cannot use unknown old tokens to acknowledge cleanup.
        """
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for group in self._groups:
                self._recovery_reasons[group] = "coordinator_closed"
                self._revoke(group, "coordinator_closed")
            if self._journal is not None:
                self._journal.close()
            self._condition.notify_all()

    def _wait(self, deadline: float, message: str) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise CoordinationTimeout(message)
        self._condition.wait(min(0.05, remaining))

    def snapshot(self, resource_group: str | None = None) -> dict[str, Any]:
        groups = (self._group(resource_group),) if resource_group is not None else tuple(self._groups)
        with self._condition:
            output: dict[str, Any] = {}
            for group in groups:
                self._tick(group)
                lease = self._comfy.get(group)
                reasons = self._native_cpu_reasons(group)
                if lease is not None and lease.revoked:
                    reasons.append("comfy_cleanup_required")
                if group in self._recovery_reasons:
                    state = "recovery_required"
                elif lease is not None:
                    state = "yielding" if lease.revoked else "comfy_running"
                elif self._native_active(group) or self._native_waiting(group):
                    state = "native_busy"
                elif any(member in self._offloading for member in self._groups[group]):
                    state = "native_offloading"
                else:
                    state = "blocked" if reasons else "comfy_available"
                output[group] = {
                    "state": state,
                    "reasons": reasons,
                    "native_active": self._native_active(group),
                    "native_waiting": self._native_waiting(group),
                    "recovery_required": group in self._recovery_reasons,
                    "recovery_reason": self._recovery_reasons.get(group),
                    "cleanup_failed": group in self._recovery_reasons or bool(lease and lease.cleanup_failed),
                    "natives": {
                        participant: {
                            "status": self._observations[participant].status.to_dict() if participant in self._observations else None,
                            "fresh": participant in self._observations and self._clock() - self._observations[participant].received_at < self.freshness_seconds,
                        }
                        for participant in self._groups[group]
                    },
                    "comfy": {
                        "holder": lease.holder,
                        "revoked": lease.revoked,
                        "reason": lease.reason,
                        "cleanup_required": lease.revoked,
                        "cleanup_failed": lease.cleanup_failed,
                        "expires_in_seconds": max(0.0, lease.expires_at - self._clock()),
                    } if lease else None,
                }
            return {"epoch": self.epoch, "persistent": self._persistent, "groups": output}

    def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        action, payload = validate_action(action, payload)
        if action == "update_native":
            self.update_native(payload["participant"], payload["status"])
            return {"updated": True}
        if action == "acquire_native":
            return {"token": self.acquire_native(payload["participant"], payload["timeout"])}
        if action == "release_native":
            return {"released": self.release_native(payload["token"])}
        if action == "acquire_comfy":
            return {"token": self.acquire_comfy(payload["holder"], payload["timeout"], resource_group=payload.get("resource_group"))}
        if action == "check_comfy":
            return {"valid": self.check_comfy(payload["token"])}
        if action == "release_comfy":
            return {"released": self.release_comfy(payload["token"], payload["clean"])}
        if action == "snapshot":
            return self.snapshot(payload.get("resource_group"))
        raise ProtocolError("action is not supported by this coordinator")
