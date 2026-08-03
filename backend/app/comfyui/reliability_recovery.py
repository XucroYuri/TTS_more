from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, TypeAdapter, ValidationError, field_validator

from . import reliability_evidence as evidence
from . import reliability_private_recovery as private_recovery


MAX_OBSERVED_PROCESSES = 4096
MAX_OBSERVED_PORTS = 64
DELETE_ORDER: tuple[Literal[".p", ".c", ".h", ".o"], ...] = (
    ".p",
    ".c",
    ".h",
    ".o",
)


class RecoveryResult(evidence._StrictModel):
    status: Literal["removed", "rejected"]
    run_key: evidence.RunKey
    deleted_roles: tuple[str, ...]
    reason_code: str | None


class RecoveryPlan(evidence._StrictModel):
    run_key: evidence.RunKey
    private_root: str
    namespace_identity_sha256: evidence.SHA256
    delete_order: tuple[Literal[".p", ".c", ".h", ".o"], ...]
    prevalidated: Literal[True]
    output_root: str
    root_identity_sha256: evidence.SHA256
    private_leaf_identity_sha256: evidence.SHA256
    terminal_sha256: evidence.SHA256
    snapshot_sha256: evidence.SHA256
    owner_manifest_sha256: evidence.SHA256

    @field_validator("delete_order", mode="before")
    @classmethod
    def _immutable_delete_order(cls, value: object) -> object:
        return tuple(value) if type(value) is list else value


class _ObservedProcess(evidence._StrictModel):
    pid: StrictInt = Field(gt=0, le=2**31 - 1)
    creation_time: str
    executable_sha256: evidence.SHA256
    command_line_sha256: evidence.SHA256
    parent_pid: StrictInt = Field(ge=0, le=2**31 - 1)
    parent_creation_time: str


def _rejected(run_key: str, *, deleted_roles: tuple[str, ...] = (), partial: bool = False) -> RecoveryResult:
    return RecoveryResult(
        status="rejected",
        run_key=TypeAdapter(evidence.RunKey).validate_python(run_key, strict=True),
        deleted_roles=deleted_roles,
        reason_code="recovery-delete-partial" if partial else "recovery-proof-failed",
    )


def _canonical(model: evidence._StrictModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _load_json_no_duplicates(payload: bytes) -> object:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    return json.loads(payload.decode("utf-8-sig"), object_pairs_hook=unique_pairs)


def _timestamp(value: object) -> datetime:
    if type(value) is not str or len(value) > 64:
        raise ValueError("process timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("process timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def _record(document: object) -> dict[str, object]:
    if type(document) is not dict or set(document) != {
        "pid",
        "creation_time",
        "executable_path",
        "command_line",
        "parent_pid",
        "parent_creation_time",
    }:
        raise ValueError("owner record is invalid")
    pid = document["pid"]
    parent_pid = document["parent_pid"]
    executable = document["executable_path"]
    command = document["command_line"]
    if (
        type(pid) is not int
        or not 0 < pid <= 2**31 - 1
        or type(parent_pid) is not int
        or not 0 <= parent_pid <= 2**31 - 1
        or pid == parent_pid
        or type(executable) is not str
        or not executable
        or len(executable) > 32768
        or type(command) is not str
        or len(command) > 131072
    ):
        raise ValueError("owner record is invalid")
    created = _timestamp(document["creation_time"])
    parent_created = _timestamp(document["parent_creation_time"])
    if parent_created > created:
        raise ValueError("owner graph is invalid")
    return {
        "pid": pid,
        "creation_time": created,
        "executable_sha256": hashlib.sha256(executable.encode("utf-8")).hexdigest(),
        "command_line_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "parent_pid": parent_pid,
        "parent_creation_time": parent_created,
    }


def _owner_proof(payload: bytes, run_key: str) -> tuple[tuple[dict[str, object], ...], tuple[int, ...]]:
    document = _load_json_no_duplicates(payload)
    if type(document) is not dict or set(document) != {
        "version",
        "run_id",
        "owned_processes",
        "launch_roots",
        "launch",
        "boundary",
        "temp_roots",
    }:
        raise ValueError("owner manifest is invalid")
    if document["version"] != 1 or type(document["run_id"]) is not str:
        raise ValueError("owner manifest is invalid")
    if hashlib.sha256(document["run_id"].encode("utf-8")).hexdigest() != run_key:
        raise ValueError("owner manifest run key is invalid")
    records: list[dict[str, object]] = []
    for section_name in ("owned_processes", "launch_roots"):
        section = document[section_name]
        if type(section) is not dict or set(section) != {"tts-more", "comfyui"}:
            raise ValueError("owner manifest process set is invalid")
        for role in ("tts-more", "comfyui"):
            if section[role] is not None:
                records.append(_record(section[role]))
    if not records:
        raise ValueError("owner manifest has no process proof")
    # Repeated listener/launch-root records are allowed only when byte-identical.
    by_identity: dict[tuple[object, object], dict[str, object]] = {}
    for item in records:
        identity = (item["pid"], item["creation_time"])
        previous = by_identity.get(identity)
        if previous is not None and previous != item:
            raise ValueError("owner manifest process identity conflicts")
        by_identity[identity] = item
    for item in by_identity.values():
        parent = next(
            (candidate for candidate in by_identity.values() if candidate["pid"] == item["parent_pid"]),
            None,
        )
        if parent is not None and parent["creation_time"] != item["parent_creation_time"]:
            raise ValueError("owner manifest parent identity conflicts")
    by_pid = {int(item["pid"]): item for item in by_identity.values()}
    if len(by_pid) != len(by_identity):
        raise ValueError("owner manifest reuses a PID")
    for item in by_pid.values():
        seen: set[int] = set()
        cursor = item
        while int(cursor["parent_pid"]) in by_pid:
            pid = int(cursor["pid"])
            if pid in seen:
                raise ValueError("owner manifest process graph is cyclic")
            seen.add(pid)
            cursor = by_pid[int(cursor["parent_pid"])]
    launch = document["launch"]
    if type(launch) is not dict or len(launch) > MAX_OBSERVED_PORTS:
        raise ValueError("owner manifest port set is invalid")
    ports: set[int] = set()
    for launch_record in launch.values():
        if type(launch_record) is not dict:
            raise ValueError("owner manifest launch record is invalid")
        if "port" not in launch_record:
            continue
        port = launch_record["port"]
        if type(port) is not int or not 1 <= port <= 65535 or port in ports:
            raise ValueError("owner manifest port is invalid")
        ports.add(port)
    return tuple(by_identity.values()), tuple(sorted(ports))


def _validate_observations(
    expected: tuple[dict[str, object], ...],
    expected_ports: tuple[int, ...],
    observed_processes: tuple[dict[str, object], ...],
    observed_ports: dict[str, int | None],
) -> None:
    if type(observed_processes) is not tuple or len(observed_processes) > MAX_OBSERVED_PROCESSES:
        raise ValueError("process observation is invalid")
    observed = tuple(_ObservedProcess.model_validate(item, strict=True) for item in observed_processes)
    if len({item.pid for item in observed}) != len(observed):
        raise ValueError("process observation is ambiguous")
    expected_by_pid = {int(item["pid"]): item for item in expected}
    if len(expected_by_pid) != len(expected):
        raise ValueError("owner PID is ambiguous")
    for item in observed:
        owner = expected_by_pid.get(item.pid)
        if owner is not None:
            # A live exact owner is still unsafe to forget; any drift is PID reuse.
            if (
                _timestamp(item.creation_time) != owner["creation_time"]
                or item.executable_sha256 != owner["executable_sha256"]
                or item.command_line_sha256 != owner["command_line_sha256"]
                or item.parent_pid != owner["parent_pid"]
                or _timestamp(item.parent_creation_time) != owner["parent_creation_time"]
            ):
                raise ValueError("owner PID was reused or changed")
            raise ValueError("owned process is still active")
        parent = expected_by_pid.get(item.parent_pid)
        if parent is not None:
            if _timestamp(item.parent_creation_time) != parent["creation_time"]:
                raise ValueError("descendant parent identity drifted")
            raise ValueError("owned descendant is still active")
    if type(observed_ports) is not dict or len(observed_ports) > MAX_OBSERVED_PORTS:
        raise ValueError("port observation is invalid")
    expected_keys = {str(port) for port in expected_ports}
    if not expected_keys.issubset(observed_ports):
        raise ValueError("port observation is incomplete")
    for key, owner_pid in observed_ports.items():
        if type(key) is not str or not key.isascii() or not key.isdigit() or not 1 <= int(key) <= 65535:
            raise ValueError("port observation is invalid")
        if key not in expected_keys:
            continue
        if owner_pid is not None:
            if type(owner_pid) is not int or not 0 < owner_pid <= 2**31 - 1:
                raise ValueError("port observation is ambiguous")
            raise ValueError("owned port is occupied")


def _public_snapshot(
    output_root: Path, run_key: str
) -> tuple[Path, str, evidence.RunVerification, private_recovery.PrivateRecoverySnapshot, bytes]:
    safe_key = evidence._validated_run_key(run_key)
    root, root_identity = evidence.read_directory_identity(Path(output_root))
    pointer = evidence.snapshot_current(root)
    if pointer["status"] != "valid" or pointer["pointer"]["run_key"] != safe_key:
        raise ValueError("recovery run is not current")
    if pointer["pointer"]["outcome"] != "failed":
        raise ValueError("recovery run did not fail")
    current = evidence.verify_current(root)
    if not isinstance(current, evidence.CurrentVerification) or current.pointer.run_key != safe_key:
        raise ValueError("recovery current pointer is invalid")
    snapshot_payload = evidence.read_artifact(root, safe_key, "log", name="private-recovery")
    snapshot = evidence.verify_private_recovery_log(snapshot_payload, expected_run_key=safe_key)
    run = evidence.verify_run(
        root,
        safe_key,
        expected_private_recovery_namespace_identity=snapshot.namespace_identity_sha256,
    )
    if run.terminal.outcome != "failed" or run.terminal.cleanup_status != "failed":
        raise ValueError("recovery terminal is not eligible")
    if pointer["pointer"]["terminal_sha256"] != run.terminal_sha256:
        raise ValueError("recovery pointer does not bind the terminal")
    if current.run.terminal_sha256 != run.terminal_sha256:
        raise ValueError("recovery current pointer changed")
    return root, root_identity, run, snapshot, snapshot_payload


def validate_recovery_owner(
    output_root: Path,
    run_key: str,
    *,
    observed_processes: tuple[dict[str, object], ...],
    observed_ports: dict[str, int | None],
) -> RecoveryPlan | RecoveryResult:
    try:
        root, root_identity, run, snapshot, snapshot_payload = _public_snapshot(
            Path(output_root), run_key
        )
        namespace = root / private_recovery.PRIVATE_RECOVERY_DIRECTORY
        evidence.validate_directory_identity(namespace, snapshot.namespace_identity_sha256)
        leaf = private_recovery.private_recovery_root(root, run_key)
        leaf, leaf_identity = evidence.read_directory_identity(leaf)
        with private_recovery.open_private_recovery_delete_transaction(
            root,
            run_key,
            expected_root_identity=root_identity,
            expected_namespace_identity=snapshot.namespace_identity_sha256,
            expected_leaf_identity=leaf_identity,
            snapshot=snapshot,
        ) as transaction:
            owner_payload = transaction.role_payload(".h")
            expected_processes, expected_ports = _owner_proof(owner_payload, run_key)
            _validate_observations(
                expected_processes,
                expected_ports,
                observed_processes,
                observed_ports,
            )
        return RecoveryPlan(
            run_key=run_key,
            private_root=str(leaf),
            namespace_identity_sha256=snapshot.namespace_identity_sha256,
            delete_order=DELETE_ORDER,
            prevalidated=True,
            output_root=str(root),
            root_identity_sha256=root_identity,
            private_leaf_identity_sha256=leaf_identity,
            terminal_sha256=run.terminal_sha256,
            snapshot_sha256=hashlib.sha256(snapshot_payload).hexdigest(),
            owner_manifest_sha256=hashlib.sha256(owner_payload).hexdigest(),
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
    ):
        return _rejected(run_key)


def _validate_plan_state(plan: RecoveryPlan) -> tuple[private_recovery.PrivateRecoverySnapshot, bytes]:
    root, root_identity, run, snapshot, snapshot_payload = _public_snapshot(
        Path(plan.output_root), plan.run_key
    )
    expected_leaf = private_recovery.private_recovery_root(root, plan.run_key)
    if (
        plan.prevalidated is not True
        or plan.delete_order != DELETE_ORDER
        or root_identity != plan.root_identity_sha256
        or str(expected_leaf) != plan.private_root
        or snapshot.namespace_identity_sha256 != plan.namespace_identity_sha256
        or run.terminal_sha256 != plan.terminal_sha256
        or hashlib.sha256(snapshot_payload).hexdigest() != plan.snapshot_sha256
    ):
        raise ValueError("recovery plan state changed")
    evidence.validate_directory_identity(
        expected_leaf, plan.private_leaf_identity_sha256
    )
    return snapshot, snapshot_payload


def _delete_validated_role(
    transaction: private_recovery.PrivateRecoveryDeleteTransaction,
    _plan: RecoveryPlan,
    role: str,
) -> None:
    transaction.delete_role(role)


def _execute_validated_plan(
    validated: RecoveryPlan,
    snapshot: private_recovery.PrivateRecoverySnapshot,
) -> RecoveryResult:
    deleted: list[str] = []
    transaction: private_recovery.PrivateRecoveryDeleteTransaction | None = None
    try:
        transaction = private_recovery.open_private_recovery_delete_transaction(
            Path(validated.output_root),
            validated.run_key,
            expected_root_identity=validated.root_identity_sha256,
            expected_namespace_identity=validated.namespace_identity_sha256,
            expected_leaf_identity=validated.private_leaf_identity_sha256,
            snapshot=snapshot,
        )
        if hashlib.sha256(transaction.role_payload(".h")).hexdigest() != validated.owner_manifest_sha256:
            raise ValueError("owner manifest changed")
        present_roles = {node.role for node in transaction.nodes}
        for role in validated.delete_order:
            _delete_validated_role(transaction, validated, role)
            if role in present_roles:
                deleted.append(role)
        transaction.delete_leaf()
        transaction.close()
        try:
            Path(validated.private_root).lstat()
        except FileNotFoundError:
            pass
        else:
            raise OSError("private recovery leaf remains after deletion")
        transaction = None
        return RecoveryResult(
            status="removed",
            run_key=validated.run_key,
            deleted_roles=tuple(deleted),
            reason_code=None,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ValidationError,
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
    ):
        partial_mutation = (
            transaction is not None
            and transaction.mutation_count > 0
        )
        return _rejected(
            validated.run_key,
            deleted_roles=tuple(deleted),
            partial=partial_mutation,
        )
    finally:
        if transaction is not None:
            transaction.close()


def execute_recovery_delete(plan: RecoveryPlan) -> RecoveryResult:
    try:
        validated = RecoveryPlan.model_validate(plan.model_dump(), strict=True)
        # Serialize with current publication.  A CAS writer cannot make this
        # run non-current between the eligibility proof and the first delete.
        with evidence._current_pointer_lock(Path(validated.output_root)):
            snapshot, _snapshot_payload = _validate_plan_state(validated)
            return _execute_validated_plan(validated, snapshot)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ValidationError,
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
    ):
        try:
            run_key = plan.run_key
        except (AttributeError, TypeError):
            raise
        return _rejected(run_key)


def encode_plan_token(plan: RecoveryPlan) -> str:
    payload = _canonical(plan)
    envelope = {
        "payload": base64.urlsafe_b64encode(payload).decode("ascii").rstrip("="),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    return base64.urlsafe_b64encode(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).decode("ascii").rstrip("=")


def decode_plan_token(token: str) -> RecoveryPlan:
    if type(token) is not str or not token or len(token) > 16384:
        raise ValueError("recovery plan token is invalid")
    padded = token + "=" * (-len(token) % 4)
    envelope = json.loads(base64.urlsafe_b64decode(padded).decode("ascii"))
    if type(envelope) is not dict or set(envelope) != {"payload", "sha256"}:
        raise ValueError("recovery plan token is invalid")
    encoded = envelope["payload"]
    if type(encoded) is not str:
        raise ValueError("recovery plan token is invalid")
    payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if hashlib.sha256(payload).hexdigest() != envelope["sha256"]:
        raise ValueError("recovery plan token is invalid")
    plan = RecoveryPlan.model_validate_json(payload, strict=True)
    if payload != _canonical(plan):
        raise ValueError("recovery plan token is invalid")
    return plan
