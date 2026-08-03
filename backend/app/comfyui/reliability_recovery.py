from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, TypeAdapter, ValidationError, field_validator

from . import reliability_evidence as evidence
from . import reliability_private_recovery as private_recovery


MAX_OBSERVED_PROCESSES = 4096
MAX_OBSERVED_PORTS = 64
CAPABILITY_LIFETIME_SECONDS = 120
CAPABILITY_DIRECTORY = "tts-more-reliability-recovery-capabilities"
CAPABILITY_MAGIC = b"TTSRCAP2\0"
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
    owner_public_identity_sha256: evidence.SHA256
    public_ownership_sha256: evidence.SHA256

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


def _public_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    public_created = _public_timestamp(created)
    public_parent_created = _public_timestamp(parent_created)
    identity_fields = (
        str(pid),
        public_created,
        executable,
        command,
        str(parent_pid),
        public_parent_created,
    )
    public_identity = hashlib.sha256(
        "|".join(
            f"{len(value.encode('utf-16-le')) // 2}:{value}" for value in identity_fields
        ).encode("utf-8")
    ).hexdigest()
    return {
        "pid": pid,
        "creation_time": created,
        "executable_sha256": hashlib.sha256(executable.encode("utf-8")).hexdigest(),
        "command_line_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "parent_pid": parent_pid,
        "parent_creation_time": parent_created,
        "public_creation_time": public_created,
        "public_parent_creation_time": public_parent_created,
        "public_identity_sha256": public_identity,
    }


def _owner_proof(
    payload: bytes, run_key: str
) -> tuple[tuple[dict[str, object], ...], tuple[int, ...], str]:
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
    public_rows: list[str] = []
    sections = (("launch_roots", "launch-root"), ("owned_processes", "listener"))
    parsed_sections: dict[str, dict[str, dict[str, object] | None]] = {}
    for section_name, _kind in sections:
        section = document[section_name]
        if type(section) is not dict or set(section) != {"tts-more", "comfyui"}:
            raise ValueError("owner manifest process set is invalid")
        parsed_sections[section_name] = {}
        for role in ("tts-more", "comfyui"):
            if section[role] is not None:
                parsed = _record(section[role])
                parsed_sections[section_name][role] = parsed
                records.append(parsed)
            else:
                parsed_sections[section_name][role] = None
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
    for role in ("tts-more", "comfyui"):
        for section_name, kind in sections:
            item = parsed_sections[section_name][role]
            if item is None:
                continue
            public_rows.append(
                "|".join(
                    (
                        role,
                        kind,
                        str(item["pid"]),
                        str(item["public_creation_time"]),
                        str(item["parent_pid"]),
                        str(item["public_identity_sha256"]).upper(),
                    )
                )
            )
    launch = document["launch"]
    if type(launch) is not dict or len(launch) > MAX_OBSERVED_PORTS:
        raise ValueError("owner manifest port set is invalid")
    ports: set[int] = {8000}
    for launch_record in launch.values():
        if type(launch_record) is not dict:
            raise ValueError("owner manifest launch record is invalid")
        if "port" not in launch_record:
            continue
        port = launch_record["port"]
        if type(port) is not int or not 1 <= port <= 65535 or port in ports:
            raise ValueError("owner manifest port is invalid")
        ports.add(port)
    if ports != {8000, 8188}:
        raise ValueError("owner manifest port binding is invalid")
    owner_public_identity = hashlib.sha256("\n".join(public_rows).encode("utf-8")).hexdigest()
    return tuple(by_identity.values()), tuple(sorted(ports)), owner_public_identity


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
    if set(observed_ports) != expected_keys:
        raise ValueError("port observation is incomplete")
    for key, owner_pid in observed_ports.items():
        if type(key) is not str or not key.isascii() or not key.isdigit() or not 1 <= int(key) <= 65535:
            raise ValueError("port observation is invalid")
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


def _public_ownership_binding(
    output_root: Path,
    run: evidence.RunVerification,
) -> tuple[str, str]:
    # Local import avoids adding a recovery dependency to supervision startup.
    from .reliability_supervision import (
        InnerRunResult,
        LauncherLifecycleCommitment,
        StreamCommitment,
        SupervisorRecord,
    )

    terminal = run.terminal
    supervisor_payload = evidence.read_artifact(output_root, run.run_key, "supervisor")
    inner_payload = evidence.read_artifact(output_root, run.run_key, "run-result")
    supervisor = SupervisorRecord.model_validate_json(supervisor_payload, strict=True)
    inner = InnerRunResult.model_validate_json(inner_payload, strict=True)
    if supervisor_payload != _canonical(supervisor) or inner_payload != _canonical(inner):
        raise ValueError("public supervision artifact is not canonical")
    if (
        supervisor.run_key != run.run_key
        or inner.run_key != run.run_key
        or supervisor.mode != terminal.mode
        or inner.mode != terminal.mode
        or supervisor.outcome != terminal.outcome
        or inner.outcome != terminal.outcome
        or supervisor.failure_source != terminal.failure_source
        or inner.failure_source != terminal.failure_source
        or supervisor.launcher_exit_code != terminal.launcher_exit_code
        or supervisor.validator_exit_code != terminal.validator_exit_code
        or inner.validator_exit_code != terminal.validator_exit_code
        or supervisor.cleanup_status != terminal.cleanup_status
        or inner.cleanup_status != terminal.cleanup_status
        or supervisor.child_start_count != 1
        or inner.reported_by != "inner"
    ):
        raise ValueError("public supervision artifact binding is invalid")
    lifecycle_payloads: list[bytes] = []
    terminal_names = {item.relative_name for item in terminal.artifacts}
    if "logs/launcher-lifecycle.log" not in terminal_names:
        raise ValueError("public lifecycle ownership binding is missing")
    lifecycle_payload = evidence.read_artifact(
        output_root, run.run_key, "log", name="launcher-lifecycle"
    )
    lifecycle = LauncherLifecycleCommitment.model_validate_json(
        lifecycle_payload, strict=True
    )
    if lifecycle_payload != _canonical(lifecycle):
        raise ValueError("public lifecycle commitment is not canonical")
    if lifecycle.run_key != run.run_key:
        raise ValueError("public lifecycle run binding is invalid")
    lifecycle_payloads.append(lifecycle_payload)
    # The secondary file is emitted only when lifecycle publication or raw
    # cleanup itself fails.  It has a distinct private schema, so commit_log
    # intentionally exposes it as the ordinary canonical stream commitment.
    # Bind that public commitment when present without pretending it carries a
    # second process-ownership assertion.
    if "logs/launcher-lifecycle-secondary.log" in terminal_names:
        secondary_payload = evidence.read_artifact(
            output_root,
            run.run_key,
            "log",
            name="launcher-lifecycle-secondary",
        )
        secondary = StreamCommitment.model_validate_json(secondary_payload, strict=True)
        if secondary_payload != _canonical(secondary):
            raise ValueError("public secondary lifecycle commitment is not canonical")
        lifecycle_payloads.append(secondary_payload)
    material = b"\0".join((supervisor_payload, inner_payload, *lifecycle_payloads))
    return hashlib.sha256(material).hexdigest(), lifecycle.promotion_ownership_sha256


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
        public_binding, lifecycle_ownership = _public_ownership_binding(root, run)
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
            expected_processes, expected_ports, owner_public_identity = _owner_proof(
                owner_payload, run_key
            )
            if owner_public_identity != lifecycle_ownership:
                raise ValueError("public/private process ownership does not match")
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
            owner_public_identity_sha256=owner_public_identity,
            public_ownership_sha256=hashlib.sha256(
                f"{public_binding}|{owner_public_identity}".encode("ascii")
            ).hexdigest(),
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


def _validate_plan_state(
    plan: RecoveryPlan,
) -> tuple[private_recovery.PrivateRecoverySnapshot, bytes, str, str]:
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
    public_binding, lifecycle_ownership = _public_ownership_binding(root, run)
    return snapshot, snapshot_payload, public_binding, lifecycle_ownership


def _delete_validated_role(
    transaction: private_recovery.PrivateRecoveryDeleteTransaction,
    _plan: RecoveryPlan,
    role: str,
) -> None:
    transaction.delete_role(role)


def _execute_validated_plan(
    validated: RecoveryPlan,
    snapshot: private_recovery.PrivateRecoverySnapshot,
    public_binding: str,
    lifecycle_ownership: str,
    *,
    observed_processes: tuple[dict[str, object], ...],
    observed_ports: dict[str, int | None],
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
        owner_payload = transaction.role_payload(".h")
        expected_processes, expected_ports, owner_public_identity = _owner_proof(
            owner_payload, validated.run_key
        )
        if (
            owner_public_identity != validated.owner_public_identity_sha256
            or owner_public_identity != lifecycle_ownership
            or hashlib.sha256(
                f"{public_binding}|{owner_public_identity}".encode("ascii")
            ).hexdigest()
            != validated.public_ownership_sha256
        ):
            raise ValueError("public/private ownership binding changed")
        _validate_observations(
            expected_processes,
            expected_ports,
            observed_processes,
            observed_ports,
        )
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


def execute_recovery_delete(
    plan: RecoveryPlan,
    *,
    observed_processes: tuple[dict[str, object], ...],
    observed_ports: dict[str, int | None],
) -> RecoveryResult:
    try:
        validated = RecoveryPlan.model_validate(plan.model_dump(), strict=True)
        # Serialize with current publication.  A CAS writer cannot make this
        # run non-current between the eligibility proof and the first delete.
        with evidence._current_pointer_lock(Path(validated.output_root)):
            (
                snapshot,
                _snapshot_payload,
                public_binding,
                lifecycle_ownership,
            ) = _validate_plan_state(validated)
            return _execute_validated_plan(
                validated,
                snapshot,
                public_binding,
                lifecycle_ownership,
                observed_processes=observed_processes,
                observed_ports=observed_ports,
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
        try:
            run_key = plan.run_key
        except (AttributeError, TypeError):
            raise
        return _rejected(run_key)


def encode_plan_token(plan: RecoveryPlan) -> str:
    validated = RecoveryPlan.model_validate(plan.model_dump(), strict=True)
    token = secrets.token_hex(32)
    issued_at = int(time.time())
    document = {
        "schema_version": 1,
        "kind": "reliability-recovery-capability",
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "issued_at": issued_at,
        "expires_at": issued_at + CAPABILITY_LIFETIME_SECONDS,
        "plan": validated.model_dump(mode="json"),
    }
    plaintext = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    payload = CAPABILITY_MAGIC + _protect_capability(plaintext, token)
    directory = _capability_directory()
    target = directory / f"{hashlib.sha256(token.encode('ascii')).hexdigest()}.cap"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = _create_private_capability_file(target, flags)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("recovery capability write was incomplete")
            written += count
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)
    return token


def decode_plan_token(token: str) -> RecoveryPlan:
    if (
        type(token) is not str
        or len(token) != 64
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise ValueError("recovery plan token is invalid")
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    directory = _capability_directory()
    target = directory / f"{token_hash}.cap"
    claim = directory / f"{token_hash}.claim"
    lease = _CapabilityStoreLease(directory)
    descriptor = -1
    claim_descriptor = -1
    claim_created = False
    capability_consumed = False
    delete_pending = False
    try:
        lease.validate()
        _validate_capability_file(target)
        descriptor = _open_capability_for_decode(target)
        opened = os.fstat(descriptor)
        named = target.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_size > 32768
        ):
            raise ValueError("recovery plan token is invalid")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 32768))
            if not chunk:
                raise ValueError("recovery plan token is invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = target.lstat()
        if (
            len(payload) != opened.st_size
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("recovery plan token is invalid")
        if not payload.startswith(CAPABILITY_MAGIC):
            raise ValueError("recovery plan token is invalid")
        plaintext = _unprotect_capability(payload[len(CAPABILITY_MAGIC) :], token)
        document = _load_json_no_duplicates(plaintext)
        if type(document) is not dict or set(document) != {
            "schema_version",
            "kind",
            "token_sha256",
            "issued_at",
            "expires_at",
            "plan",
        }:
            raise ValueError("recovery plan token is invalid")
        now = int(time.time())
        if (
            document["schema_version"] != 1
            or document["kind"] != "reliability-recovery-capability"
            or document["token_sha256"] != token_hash
            or type(document["issued_at"]) is not int
            or type(document["expires_at"]) is not int
            or document["expires_at"] - document["issued_at"] != CAPABILITY_LIFETIME_SECONDS
            or not document["issued_at"] <= now <= document["expires_at"]
        ):
            raise ValueError("recovery plan token is invalid")
        plan = RecoveryPlan.model_validate(document["plan"], strict=True)
        canonical = (
            json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if plaintext != canonical:
            raise ValueError("recovery plan token is invalid")
        lease.validate()
        claim_descriptor = _create_private_capability_file(
            claim,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            delete_on_close=os.name == "nt",
        )
        claim_created = True
        _validate_capability_file(claim)
        lease.validate()
        # The exclusive claim and validated DELETE-capable cap handle remain
        # held while store identity and security are revalidated at consume.
        lease.validate()
        final = target.lstat()
        if (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("recovery plan token is invalid")
        _validate_capability_file(target)
        lease.validate()
        if os.name == "nt":
            _set_capability_delete_disposition(descriptor, delete=True)
            delete_pending = True
            try:
                lease.validate()
            except BaseException:
                _set_capability_delete_disposition(descriptor, delete=False)
                delete_pending = False
                raise
        else:
            os.unlink(target)
            capability_consumed = True
            lease.validate()
        # Separate immediate precondition for claim removal below.
        lease.validate()
        if os.name == "nt":
            # FILE_FLAG_DELETE_ON_CLOSE removes only the exact protected
            # claim handle created above; unsafe pre-consume exits close this
            # handle and leave the capability itself untouched.
            os.close(claim_descriptor)
            claim_descriptor = -1
        else:
            os.close(claim_descriptor)
            claim_descriptor = -1
            os.unlink(claim)
        claim_created = False
        lease.validate()
        if os.name == "nt":
            lease.validate()
            # The post-disposition validation above is the final authorization
            # point.  From here the deny-delete store handle plus share-zero
            # cap handle make close an exact handle-relative commit; no path or
            # ACL interposition can redirect it to another object.
            os.close(descriptor)
            descriptor = -1
            delete_pending = False
            capability_consumed = True
        return plan
    finally:
        if delete_pending and descriptor != -1:
            try:
                _set_capability_delete_disposition(descriptor, delete=False)
                delete_pending = False
            except OSError:
                pass
        if descriptor != -1:
            os.close(descriptor)
        if claim_descriptor != -1:
            claim_opened = os.fstat(claim_descriptor)
            os.close(claim_descriptor)
            claim_descriptor = -1
            if os.name != "nt" and claim_created and not capability_consumed:
                try:
                    claim_named = claim.lstat()
                    if (claim_opened.st_dev, claim_opened.st_ino) == (
                        claim_named.st_dev,
                        claim_named.st_ino,
                    ):
                        _validate_capability_file(claim)
                        os.unlink(claim)
                except OSError:
                    pass
        lease.close()


def _capability_directory() -> Path:
    directory = Path(tempfile.gettempdir()) / CAPABILITY_DIRECTORY
    try:
        if os.name == "nt":
            _create_windows_private_directory(directory)
        else:
            directory.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ValueError("recovery capability store is unavailable") from exc
    _validate_capability_store(directory)
    return directory


def _validate_capability_store(directory: Path) -> None:
    try:
        result = directory.lstat()
    except OSError as exc:
        raise ValueError("recovery capability store is unavailable") from exc
    if (
        stat.S_ISLNK(result.st_mode)
        or getattr(result, "st_file_attributes", 0) & evidence._FILE_ATTRIBUTE_REPARSE_POINT
        or not stat.S_ISDIR(result.st_mode)
    ):
        raise ValueError("recovery capability store is unsafe")
    _validate_capability_directory_security(directory)


class _CapabilityStoreLease:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory_handle: int | None = None
        self.notification_handle: int | None = None
        self.identity: tuple[int, int, int] | None = None
        if os.name == "nt":
            self._open_windows()

    def _open_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        invalid = wintypes.HANDLE(-1).value
        handle = kernel32.CreateFileW(
            str(self.directory),
            0x00020000 | 0x00000080,
            0x00000001 | 0x00000002,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == invalid:
            raise ValueError("recovery capability store lease is unavailable")
        self.directory_handle = handle
        try:
            self.identity = _windows_handle_identity(handle, require_directory=True)
            kernel32.FindFirstChangeNotificationW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.FindFirstChangeNotificationW.restype = wintypes.HANDLE
            notification = kernel32.FindFirstChangeNotificationW(
                str(self.directory.parent), False, 0x00000100
            )
            if notification == invalid:
                raise ValueError("recovery capability store lease is unavailable")
            self.notification_handle = notification
        except BaseException:
            kernel32.CloseHandle(handle)
            self.directory_handle = None
            raise

    def validate(self) -> None:
        _validate_capability_store(self.directory)
        if os.name != "nt":
            return
        import ctypes

        if self.directory_handle is None or self.notification_handle is None:
            raise ValueError("recovery capability store lease is invalid")
        if _windows_handle_identity(
            self.directory_handle, require_directory=True
        ) != self.identity:
            raise ValueError("recovery capability store lease identity changed")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait = kernel32.WaitForSingleObject(self.notification_handle, 0)
        if wait == 0:
            raise ValueError("recovery capability store security changed")
        if wait != 0x00000102:
            raise ValueError("recovery capability store lease is unavailable")

    def close(self) -> None:
        if os.name != "nt":
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if self.notification_handle is not None:
            kernel32.FindCloseChangeNotification(self.notification_handle)
            self.notification_handle = None
        if self.directory_handle is not None:
            kernel32.CloseHandle(self.directory_handle)
            self.directory_handle = None


def _windows_handle_identity(
    handle: int, *, require_directory: bool
) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("access_time", FileTime),
            ("write_time", FileTime),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    information = FileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise ValueError("recovery capability store lease is unavailable")
    is_directory = bool(information.attributes & 0x00000010)
    if (
        is_directory != require_directory
        or information.attributes & evidence._FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError("recovery capability store lease is unsafe")
    return (
        int(information.volume_serial),
        int(information.index_high),
        int(information.index_low),
    )


def _open_capability_for_decode(path: Path) -> int:
    if os.name != "nt":
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        return os.open(path, flags)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    invalid = wintypes.HANDLE(-1).value
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000 | 0x00010000,
        0,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == invalid:
        raise ValueError("recovery plan token is invalid")
    try:
        return msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _set_capability_delete_disposition(descriptor: int, *, delete: bool) -> None:
    if os.name != "nt":
        raise OSError("handle-relative disposition is Windows-only")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileDispositionInformation(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    information = FileDispositionInformation(bool(delete))
    handle = msvcrt.get_osfhandle(descriptor)
    if not kernel32.SetFileInformationByHandle(
        handle,
        4,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise OSError(ctypes.get_last_error(), "capability disposition failed")


def _protect_capability(plaintext: bytes, token: str) -> bytes:
    token_bytes = bytes.fromhex(token)
    if os.name == "nt":
        return _windows_dpapi(plaintext, token_bytes, protect=True)
    nonce = secrets.token_bytes(32)
    ciphertext = _xor_hmac_stream(plaintext, token_bytes, nonce)
    tag = hmac.new(token_bytes, b"auth\0" + nonce + ciphertext, hashlib.sha256).digest()
    return nonce + ciphertext + tag


def _unprotect_capability(payload: bytes, token: str) -> bytes:
    token_bytes = bytes.fromhex(token)
    if os.name == "nt":
        return _windows_dpapi(payload, token_bytes, protect=False)
    if len(payload) < 64:
        raise ValueError("recovery plan token is invalid")
    nonce, ciphertext, tag = payload[:32], payload[32:-32], payload[-32:]
    expected = hmac.new(
        token_bytes, b"auth\0" + nonce + ciphertext, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("recovery plan token is invalid")
    return _xor_hmac_stream(ciphertext, token_bytes, nonce)


def _xor_hmac_stream(payload: bytes, key: bytes, nonce: bytes) -> bytes:
    result = bytearray(len(payload))
    offset = 0
    counter = 0
    while offset < len(payload):
        block = hmac.new(
            key,
            b"enc\0" + nonce + counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        count = min(len(block), len(payload) - offset)
        for index in range(count):
            result[offset + index] = payload[offset + index] ^ block[index]
        offset += count
        counter += 1
    return bytes(result)


def _windows_dpapi(payload: bytes, entropy: bytes, *, protect: bool) -> bytes:
    try:
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        def blob(value: bytes) -> tuple[DataBlob, object]:
            buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
            return DataBlob(len(value), buffer), buffer

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        input_blob, input_buffer = blob(payload)
        entropy_blob, entropy_buffer = blob(entropy)
        output_blob = DataBlob()
        if protect:
            operation = crypt32.CryptProtectData
            operation.argtypes = [
                ctypes.POINTER(DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            arguments = (
                ctypes.byref(input_blob),
                "TTS More reliability recovery",
                ctypes.byref(entropy_blob),
                None,
                None,
                1,
                ctypes.byref(output_blob),
            )
        else:
            operation = crypt32.CryptUnprotectData
            description = wintypes.LPWSTR()
            operation.argtypes = [
                ctypes.POINTER(DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            arguments = (
                ctypes.byref(input_blob),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                1,
                ctypes.byref(output_blob),
            )
        operation.restype = wintypes.BOOL
        if not operation(*arguments):
            raise ValueError("recovery plan token is invalid")
        try:
            return ctypes.string_at(output_blob.data, output_blob.size)
        finally:
            kernel32.LocalFree(output_blob.data)
            if not protect and description:
                kernel32.LocalFree(description)
    except (OSError, ValueError):
        raise
    except Exception as exc:
        raise ValueError("recovery plan token is invalid") from exc


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise ValueError("recovery capability store is unavailable")
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, needed, ctypes.byref(needed)
        ):
            raise ValueError("recovery capability store is unavailable")
        sid = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents.user.sid
        rendered = wintypes.LPWSTR()
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
            raise ValueError("recovery capability store is unavailable")
        try:
            return rendered.value
        finally:
            kernel32.LocalFree(rendered)
    finally:
        kernel32.CloseHandle(token)


def _windows_security_descriptor_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000001 | 0x00000004,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise ValueError("recovery capability store is unavailable")
    rendered = wintypes.LPWSTR()
    length = wintypes.ULONG()
    try:
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.ULONG),
        ]
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            1,
            0x00000001 | 0x00000004,
            ctypes.byref(rendered),
            ctypes.byref(length),
        ):
            raise ValueError("recovery capability store is unavailable")
        return rendered.value
    finally:
        if rendered:
            kernel32.LocalFree(rendered)
        kernel32.LocalFree(descriptor)


def _windows_private_sddl(*, directory: bool) -> str:
    flags = "OICI" if directory else ""
    sid = _windows_current_user_sid()
    return f"O:{sid}D:P(A;{flags};FA;;;{sid})"


def _windows_security_descriptor(sddl: str) -> object:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    size = wintypes.DWORD()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)
    ):
        raise ValueError("recovery capability store is unavailable")
    return descriptor


def _create_windows_private_directory(directory: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = _windows_security_descriptor(_windows_private_sddl(directory=True))
    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(SecurityAttributes),
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    try:
        if not kernel32.CreateDirectoryW(str(directory), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183:
                raise ValueError("recovery capability store is unavailable")
    finally:
        kernel32.LocalFree(descriptor)


def _validate_capability_directory_security(directory: Path) -> None:
    metadata = directory.lstat()
    if os.name == "nt":
        if _windows_security_descriptor_sddl(directory) != _windows_private_sddl(
            directory=True
        ):
            raise ValueError("recovery capability store ACL is unsafe")
    elif metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("recovery capability store permissions are unsafe")


def _create_private_capability_file(
    path: Path, flags: int, *, delete_on_close: bool = False
) -> int:
    if os.name != "nt":
        descriptor = os.open(path, flags, 0o600)
        _validate_capability_file(path)
        return descriptor
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = _windows_security_descriptor(_windows_private_sddl(directory=False))
    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    invalid_handle = wintypes.HANDLE(-1).value
    try:
        handle = kernel32.CreateFileW(
            str(path),
            0x40000000 | (0x00010000 if delete_on_close else 0),
            0,
            ctypes.byref(attributes),
            1,
            0x00000080 | (0x04000000 if delete_on_close else 0),
            None,
        )
        if handle == invalid_handle:
            raise FileExistsError(str(path))
    finally:
        kernel32.LocalFree(descriptor)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _validate_capability_file(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0)
        & evidence._FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError("recovery plan token is invalid")
    if os.name == "nt":
        if _windows_security_descriptor_sddl(path) != _windows_private_sddl(
            directory=False
        ):
            raise ValueError("recovery plan token is invalid")
    elif metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("recovery plan token is invalid")
