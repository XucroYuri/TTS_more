from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from app.comfyui import reliability_evidence as evidence
from app.comfyui import reliability_private_recovery as private_recovery
from app.comfyui import reliability_validation as validation
from app.comfyui.reliability_private_recovery import (
    PrivateRecoveryBoundary,
    observe_private_recovery,
    write_private_recovery_snapshot,
)
from app.comfyui.reliability_validation import FailureMarker, ReliabilityRunFailure


class SupervisionError(RuntimeError):
    """A sanitized formal-supervision failure."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        frozen=True,
        revalidate_instances="always",
        allow_inf_nan=False,
    )


class InnerRunResult(_StrictModel):
    schema_version: Literal[1]
    kind: Literal["reliability-inner-run-result"]
    run_key: evidence.RunKey
    mode: Literal["preflight", "matrix"]
    outcome: Literal["passed", "failed"]
    failure_source: Literal["none", "validator", "cleanup", "launcher"]
    validator_exit_code: StrictInt | None = Field(
        ge=-(2**31),
        le=2**31 - 1,
    )
    cleanup_status: Literal["completed", "failed", "not-started"]
    reported_by: Literal["inner", "supervisor-fallback"]

    @model_validator(mode="after")
    def _coherent_result(self) -> "InnerRunResult":
        if self.outcome == "passed":
            if (
                self.failure_source != "none"
                or self.validator_exit_code != 0
                or self.cleanup_status != "completed"
                or self.reported_by != "inner"
            ):
                raise ValueError("passing inner result is incoherent")
        elif self.failure_source == "validator":
            if (
                self.validator_exit_code is None
                or self.validator_exit_code == 0
                or self.cleanup_status != "completed"
                or self.reported_by != "inner"
            ):
                raise ValueError("validator failure result is incoherent")
        elif self.failure_source == "cleanup":
            if (
                self.validator_exit_code is None
                or self.cleanup_status != "failed"
                or self.reported_by != "inner"
            ):
                raise ValueError("cleanup failure result is incoherent")
        elif self.failure_source == "launcher":
            if (
                self.validator_exit_code is not None
                or (
                    self.reported_by == "supervisor-fallback"
                    and self.cleanup_status != "not-started"
                )
                or (
                    self.reported_by == "inner"
                    and self.cleanup_status not in {"completed", "failed"}
                )
            ):
                raise ValueError("launcher fallback result is incoherent")
        else:
            raise ValueError("failed inner result has no failure source")
        return self


class SupervisorRecord(_StrictModel):
    schema_version: Literal[1]
    kind: Literal["reliability-supervisor-result"]
    run_key: evidence.RunKey
    mode: Literal["preflight", "matrix"]
    child_start_count: Literal[0, 1]
    launcher_exit_code: StrictInt = Field(ge=-(2**31), le=2**31 - 1)
    validator_exit_code: StrictInt | None = Field(
        ge=-(2**31),
        le=2**31 - 1,
    )
    cleanup_status: Literal["completed", "failed", "not-started"]
    outcome: Literal["passed", "failed"]
    failure_source: Literal["none", "launcher", "validator", "cleanup"]

    @model_validator(mode="after")
    def _coherent_record(self) -> "SupervisorRecord":
        if self.outcome == "passed":
            if (
                self.failure_source != "none"
                or self.child_start_count != 1
                or self.launcher_exit_code != 0
                or self.validator_exit_code != 0
                or self.cleanup_status != "completed"
            ):
                raise ValueError("passing supervisor result is incoherent")
        elif self.failure_source == "launcher":
            if (
                self.launcher_exit_code == 0
                or self.validator_exit_code is not None
                or (
                    self.child_start_count == 0
                    and self.cleanup_status != "not-started"
                )
                or (
                    self.child_start_count == 1
                    and self.cleanup_status not in {"completed", "failed"}
                )
            ):
                raise ValueError("launcher supervisor result is incoherent")
        elif self.failure_source == "validator":
            if (
                self.child_start_count != 1
                or self.launcher_exit_code == 0
                or self.validator_exit_code is None
                or self.validator_exit_code == 0
                or self.cleanup_status != "completed"
            ):
                raise ValueError("validator supervisor result is incoherent")
        elif self.failure_source == "cleanup":
            if (
                self.child_start_count != 1
                or self.launcher_exit_code == 0
                or self.cleanup_status != "failed"
            ):
                raise ValueError("cleanup supervisor result is incoherent")
        else:
            raise ValueError("failed supervisor result has no failure source")
        return self


class FinalizedSupervision(_StrictModel):
    status: Literal["current"]
    run_key: evidence.RunKey
    pointer_token: evidence.SHA256
    formal_exit_code: StrictInt = Field(ge=-(2**31), le=2**31 - 1)


class StreamCommitment(_StrictModel):
    schema_version: Literal[1]
    kind: Literal["reliability-stream-commitment"]
    size_bytes: StrictInt = Field(ge=0, le=evidence.MAX_ARTIFACT_BYTES)
    sha256: evidence.SHA256


class PreparedRun(_StrictModel):
    status: Literal["prepared"]
    run_key: evidence.RunKey
    output_root: str
    root_identity: evidence.SHA256
    run_root: str
    run_root_identity: evidence.SHA256
    private_root: str
    private_root_identity: evidence.SHA256
    private_namespace_identity: evidence.SHA256


class PreparedOutputRoot(_StrictModel):
    status: Literal["prepared"]
    output_root: str
    root_identity: evidence.SHA256


class ValidatedRunBoundary(_StrictModel):
    status: Literal["validated"]
    run_key: evidence.RunKey
    output_root: str
    root_identity: evidence.SHA256
    run_root: str
    run_root_identity: evidence.SHA256
    private_root: str
    private_root_identity: evidence.SHA256
    private_namespace_identity: evidence.SHA256


class PreparedPrivateFinalization(_StrictModel):
    status: Literal["ready"]
    run_key: evidence.RunKey
    cleanup_status: Literal["completed", "failed", "not-started"]


def prepare_output_root(output_root: Path) -> PreparedOutputRoot:
    try:
        root, root_identity = evidence.prepare_output_root_directory(Path(output_root))
        return PreparedOutputRoot(
            status="prepared",
            output_root=str(root),
            root_identity=root_identity,
        )
    except (ValidationError, evidence.EvidenceStoreError, OSError, ValueError, TypeError) as exc:
        raise SupervisionError("formal output root could not be prepared") from exc


def prepare_run(
    output_root: Path,
    run_key: str,
    *,
    expected_root_identity: str,
) -> PreparedRun:
    try:
        root = Path(output_root)
        validated_key = evidence._validated_run_key(run_key)
        validated_root, root_identity = evidence.validate_directory_identity(
            root,
            expected_root_identity,
        )
        with evidence._run_operation_lock(validated_root, validated_key):
            evidence.validate_directory_identity(validated_root, root_identity)
            (
                validated_root,
                root_identity,
                run_root,
                run_root_identity,
            ) = evidence.prepare_new_run_directory(
                validated_root,
                validated_key,
                root_identity,
            )
            files, directories = evidence._scan_run_membership(run_root)
            if files or directories:
                raise SupervisionError("formal run is not empty")
            private_boundary = private_recovery.prepare_private_recovery(
                validated_root,
                validated_key,
                expected_root_identity=root_identity,
            )
            private_root = private_recovery.private_recovery_root(
                validated_root,
                validated_key,
            )
            private_root, private_root_identity = evidence.read_directory_identity(
                private_root
            )
        return PreparedRun(
            status="prepared",
            run_key=validated_key,
            output_root=str(validated_root),
            root_identity=root_identity,
            run_root=str(run_root),
            run_root_identity=run_root_identity,
            private_root=str(private_root),
            private_root_identity=private_root_identity,
            private_namespace_identity=private_boundary.private_root_identity,
        )
    except (
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        if isinstance(exc, SupervisionError):
            raise
        raise SupervisionError("formal run could not be prepared") from exc


def validate_run_boundary(
    output_root: Path,
    run_key: str,
    *,
    expected_root_identity: str,
    expected_run_root_identity: str,
    expected_private_root_identity: str,
    expected_private_namespace_identity: str,
) -> ValidatedRunBoundary:
    try:
        validated_key = evidence._validated_run_key(run_key)
        validated_root, root_identity = evidence.validate_directory_identity(
            Path(output_root),
            expected_root_identity,
        )
        run_root, run_root_identity = evidence.validate_directory_identity(
            validated_root / "runs" / validated_key,
            expected_run_root_identity,
        )
        private_boundary = private_recovery.validate_private_recovery(
            validated_root,
            validated_key,
            expected_root_identity=root_identity,
            expected_private_root_identity=expected_private_namespace_identity,
        )
        private_root = private_recovery.private_recovery_root(
            validated_root,
            validated_key,
        )
        private_root, private_root_identity = evidence.validate_directory_identity(
            private_root,
            expected_private_root_identity,
        )
        return ValidatedRunBoundary(
            status="validated",
            run_key=validated_key,
            output_root=str(validated_root),
            root_identity=root_identity,
            run_root=str(run_root),
            run_root_identity=run_root_identity,
            private_root=str(private_root),
            private_root_identity=private_root_identity,
            private_namespace_identity=private_boundary.private_root_identity,
        )
    except (
        ValidationError,
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise SupervisionError("formal run boundary is invalid") from exc


def _canonical_json(model: BaseModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def record_inner_result(
    output_root: Path,
    run_key: str,
    *,
    mode: Literal["preflight", "matrix"],
    validator_exit_code: int | None,
    cleanup_status: Literal["completed", "failed"],
    failure_source: Literal["launcher"] | None = None,
) -> evidence.ArtifactCommitment:
    if failure_source == "launcher":
        if validator_exit_code is not None:
            raise SupervisionError("launcher result cannot report a validator exit")
        outcome = "failed"
        reported_failure_source = "launcher"
    elif validator_exit_code is None:
        raise SupervisionError("inner result is missing a validator exit")
    elif cleanup_status == "failed":
        outcome = "failed"
        reported_failure_source = "cleanup"
    elif validator_exit_code == 0:
        outcome = "passed"
        reported_failure_source = "none"
    else:
        outcome = "failed"
        reported_failure_source = "validator"
    try:
        result = InnerRunResult(
            schema_version=1,
            kind="reliability-inner-run-result",
            run_key=run_key,
            mode=mode,
            outcome=outcome,
            failure_source=reported_failure_source,
            validator_exit_code=validator_exit_code,
            cleanup_status=cleanup_status,
            reported_by="inner",
        )
        return evidence.write_artifact(
            Path(output_root),
            run_key,
            "run-result",
            _canonical_json(result),
        )
    except (ValidationError, evidence.EvidenceStoreError, ValueError, TypeError) as exc:
        raise SupervisionError("inner result could not be recorded") from exc


def commit_log(
    output_root: Path,
    run_key: str,
    name: str,
    source_file: Path,
) -> evidence.ArtifactCommitment:
    try:
        source = Path(source_file)
        metadata = source.lstat()
        if not source.is_file() or source.is_symlink() or metadata.st_size > evidence.MAX_ARTIFACT_BYTES:
            raise SupervisionError("supervisor log source is invalid")
        payload = evidence._read_bounded_regular(
            source,
            max_bytes=evidence.MAX_ARTIFACT_BYTES,
        )
        if len(payload) != metadata.st_size:
            raise SupervisionError("supervisor log source changed")
        public_commitment = StreamCommitment(
            schema_version=1,
            kind="reliability-stream-commitment",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        return evidence.write_artifact(
            Path(output_root),
            run_key,
            "log",
            _canonical_json(public_commitment),
            name=name,
        )
    except (OSError, evidence.EvidenceStoreError, ValueError, TypeError) as exc:
        raise SupervisionError("supervisor log could not be committed") from exc


def _read_inner_result(output_root: Path, run_key: str) -> InnerRunResult:
    try:
        raw = evidence.read_artifact(output_root, run_key, "run-result")
        result = InnerRunResult.model_validate_json(raw, strict=True)
        if raw != _canonical_json(result) or result.run_key != run_key:
            raise ValueError("inner result binding is invalid")
        return result
    except (ValidationError, evidence.EvidenceStoreError, ValueError, TypeError) as exc:
        raise SupervisionError("inner result is invalid") from exc


def _has_run_member(output_root: Path, run_key: str, relative_name: str) -> bool:
    try:
        run_root, _ = evidence._run_root(output_root, run_key, create=False)
        files, _directories = evidence._scan_run_membership(run_root)
        return relative_name in files
    except evidence.EvidenceStoreError as exc:
        raise SupervisionError("run membership is invalid") from exc


def _write_failure_marker(
    output_root: Path,
    run_key: str,
    *,
    failure_source: Literal["launcher", "validator", "cleanup"],
    validator_exit_code: int | None,
) -> None:
    if _has_run_member(output_root, run_key, "failure.json"):
        return
    marker_source = (
        "validator"
        if failure_source == "cleanup"
        and validator_exit_code is not None
        and validator_exit_code != 0
        else failure_source
    )
    marker = ReliabilityRunFailure(
        run_key=run_key,
        failure=FailureMarker(
            code={
                "launcher": "launcher-failed",
                "validator": "validator-failed",
                "cleanup": "cleanup-failed",
            }[marker_source],
            stage="preflight" if marker_source == "launcher" else "finalize",
        ),
    )
    evidence.write_artifact(
        output_root,
        run_key,
        "failure",
        _canonical_json(marker),
    )


def _read_failure_marker(output_root: Path, run_key: str) -> ReliabilityRunFailure:
    try:
        raw = evidence.read_artifact(output_root, run_key, "failure")
        marker = ReliabilityRunFailure.model_validate_json(raw, strict=True)
        if raw != _canonical_json(marker) or marker.run_key != run_key:
            raise ValueError("failure marker binding is invalid")
        return marker
    except (ValidationError, evidence.EvidenceStoreError, ValueError, TypeError) as exc:
        raise SupervisionError("failure marker is invalid") from exc


def _launcher_fallback_result(
    output_root: Path,
    run_key: str,
    *,
    mode: Literal["preflight", "matrix"],
) -> InnerRunResult:
    result = InnerRunResult(
        schema_version=1,
        kind="reliability-inner-run-result",
        run_key=run_key,
        mode=mode,
        outcome="failed",
        failure_source="launcher",
        validator_exit_code=None,
        cleanup_status="not-started",
        reported_by="supervisor-fallback",
    )
    evidence.write_artifact(
        output_root,
        run_key,
        "run-result",
        _canonical_json(result),
    )
    _write_failure_marker(
        output_root,
        run_key,
        failure_source="launcher",
        validator_exit_code=None,
    )
    return result


def _artifact_commitment(output_root: Path, run_key: str, relative_name: str) -> evidence.ArtifactCommitment:
    if relative_name in evidence._FIXED_ARTIFACT_NAMES.values():
        kind = next(
            key
            for key, value in evidence._FIXED_ARTIFACT_NAMES.items()
            if value == relative_name
        )
        payload = evidence.read_artifact(output_root, run_key, kind)
    else:
        directory, filename = relative_name.split("/", 1)
        stem = filename.rsplit(".", 1)[0]
        kind = {"cases": "case", "audio": "audio", "logs": "log"}[directory]
        payload = evidence.read_artifact(output_root, run_key, kind, name=stem)
    return evidence.ArtifactCommitment(
        relative_name=relative_name,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _build_terminal(
    output_root: Path,
    run_key: str,
    inner_result: InnerRunResult,
    supervisor: SupervisorRecord,
    *,
    expected_private_root_identity: str,
) -> evidence.RunTerminal:
    run_root, _ = evidence._run_root(output_root, run_key, create=False)
    files, _directories = evidence._scan_run_membership(run_root)
    if "terminal.json" in files:
        raise SupervisionError("run is already terminal")
    try:
        validation.verify_run_artifacts(
            output_root,
            run_key,
            mode=supervisor.mode,
            outcome=supervisor.outcome,
            supervision=validation.RunArtifactSupervisionFacts(
                inner_mode=inner_result.mode,
                supervisor_mode=supervisor.mode,
                inner_outcome=inner_result.outcome,
                supervisor_outcome=supervisor.outcome,
                inner_failure_source=inner_result.failure_source,
                supervisor_failure_source=supervisor.failure_source,
                inner_validator_exit_code=inner_result.validator_exit_code,
                supervisor_validator_exit_code=supervisor.validator_exit_code,
                inner_cleanup_status=inner_result.cleanup_status,
                supervisor_cleanup_status=supervisor.cleanup_status,
            ),
            expected_private_recovery_namespace_identity=(
                expected_private_root_identity
            ),
        )
    except ValueError as exc:
        raise SupervisionError("run artifacts are invalid") from exc
    commitments = {
        relative_name: _artifact_commitment(output_root, run_key, relative_name)
        for relative_name in sorted(files)
    }
    audio_count = sum(name.startswith("audio/") for name in commitments)
    if supervisor.outcome == "passed" and supervisor.mode == "matrix":
        if len([name for name in commitments if name.startswith("cases/")]) != 47:
            raise SupervisionError("matrix case membership is invalid")
        if audio_count != 39:
            raise SupervisionError("matrix audio membership is invalid")
    if supervisor.mode == "preflight" and audio_count:
        raise SupervisionError("preflight audio membership is invalid")
    if supervisor.outcome == "failed":
        _read_failure_marker(output_root, run_key)
    try:
        return evidence.RunTerminal(
            schema_version=1,
            kind="reliability-run-terminal",
            run_key=run_key,
            mode=supervisor.mode,
            outcome=supervisor.outcome,
            failure_source=supervisor.failure_source,
            evidence_complete=True,
            launcher_exit_code=supervisor.launcher_exit_code,
            validator_exit_code=supervisor.validator_exit_code,
            cleanup_status=supervisor.cleanup_status,
            preflight=commitments.get("preflight.json"),
            failure=commitments.get("failure.json"),
            summary=commitments.get("reliability-summary.json"),
            cases=tuple(
                commitment
                for name, commitment in commitments.items()
                if name.startswith("cases/")
            ),
            artifacts=tuple(
                commitment
                for name, commitment in commitments.items()
                if name in {"supervisor.json", "run-result.json"}
                or name.startswith(("logs/", "audio/"))
            ),
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise SupervisionError("terminal classification is invalid") from exc


def _resolve_supervision_result(
    root: Path,
    run_key: str,
    *,
    mode: Literal["preflight", "matrix"],
    launcher_exit_code: int,
    child_start_count: int,
) -> InnerRunResult:
    if child_start_count not in {0, 1}:
        raise SupervisionError("supervision binding is invalid")
    if _has_run_member(root, run_key, "run-result.json"):
        if child_start_count != 1:
            raise SupervisionError("supervision binding is invalid")
        result = _read_inner_result(root, run_key)
    elif child_start_count == 0 and launcher_exit_code != 0:
        result = _launcher_fallback_result(root, run_key, mode=mode)
    else:
        raise SupervisionError("inner result is missing")
    if result.mode != mode:
        raise SupervisionError("supervision binding is invalid")
    if result.outcome == "passed" and launcher_exit_code != 0:
        raise SupervisionError("launcher exit contradicts inner result")
    if result.outcome == "failed" and launcher_exit_code == 0:
        raise SupervisionError("launcher exit contradicts inner result")
    return result


def _private_recovery_top_level_names(
    boundary: PrivateRecoveryBoundary,
) -> frozenset[str]:
    run_handle, _safe_key = private_recovery._open_observation_run(boundary)
    try:
        return frozenset(
            name for name, _kind in private_recovery._directory_names(run_handle)
        )
    finally:
        private_recovery._close_directory(run_handle)


def _observe_exact_private_recovery(
    boundary: PrivateRecoveryBoundary,
) -> private_recovery.PrivateRecoverySnapshot:
    allowed = frozenset({".p", *private_recovery.PRIVATE_ROLES})
    before = _private_recovery_top_level_names(boundary)
    if not before.issubset(allowed):
        raise SupervisionError("private recovery membership is invalid")
    snapshot = observe_private_recovery(boundary)
    after = _private_recovery_top_level_names(boundary)
    if after != before or not after.issubset(allowed):
        raise SupervisionError("private recovery membership is invalid")
    return snapshot


def prepare_private_finalization(
    output_root: Path,
    run_key: str,
    *,
    mode: Literal["preflight", "matrix"],
    expected_root_identity: str,
    expected_run_root_identity: str,
    expected_private_root_identity: str,
    expected_private_namespace_identity: str,
    launcher_exit_code: int,
    child_start_count: int,
) -> PreparedPrivateFinalization:
    try:
        boundary = validate_run_boundary(
            Path(output_root),
            run_key,
            expected_root_identity=expected_root_identity,
            expected_run_root_identity=expected_run_root_identity,
            expected_private_root_identity=expected_private_root_identity,
            expected_private_namespace_identity=expected_private_namespace_identity,
        )
        root = Path(boundary.output_root)
        result = _resolve_supervision_result(
            root,
            run_key,
            mode=mode,
            launcher_exit_code=launcher_exit_code,
            child_start_count=child_start_count,
        )
        if result.cleanup_status == "failed":
            held_private_boundary = PrivateRecoveryBoundary(
                status="validated",
                run_key=boundary.run_key,
                output_root=boundary.output_root,
                root_identity=boundary.root_identity,
                private_root=str(Path(boundary.private_root).parent),
                private_root_identity=boundary.private_namespace_identity,
            )
            snapshot = _observe_exact_private_recovery(held_private_boundary)
            write_private_recovery_snapshot(root, run_key, snapshot)
        return PreparedPrivateFinalization(
            status="ready",
            run_key=boundary.run_key,
            cleanup_status=result.cleanup_status,
        )
    except (
        ValidationError,
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
        SupervisionError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        if isinstance(exc, SupervisionError):
            raise
        raise SupervisionError("private finalization could not be prepared") from exc


def _private_leaf_is_absent_or_delete_pending(private_leaf: Path) -> bool:
    try:
        observed = private_leaf.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise SupervisionError("private recovery cleanup is unverifiable") from exc
    return (
        os.name == "nt"
        and observed.st_dev == 0
        and observed.st_ino == 0
        and observed.st_nlink == 0
    )


def finalize_supervision(
    output_root: Path,
    run_key: str,
    *,
    mode: Literal["preflight", "matrix"],
    expected_token: str,
    expected_root_identity: str,
    expected_run_root_identity: str,
    expected_private_root_identity: str,
    expected_private_namespace_identity: str,
    launcher_exit_code: int,
    child_start_count: int,
) -> FinalizedSupervision:
    try:
        validated_key = evidence._validated_run_key(run_key)
        root, root_identity = evidence.validate_directory_identity(
            Path(output_root), expected_root_identity
        )
        evidence.validate_directory_identity(
            root / "runs" / validated_key,
            expected_run_root_identity,
        )
        private_namespace, private_namespace_identity = evidence.validate_directory_identity(
            root / private_recovery.PRIVATE_RECOVERY_DIRECTORY,
            expected_private_namespace_identity,
        )
        private_leaf = private_namespace / validated_key
        result = _resolve_supervision_result(
            root,
            validated_key,
            mode=mode,
            launcher_exit_code=launcher_exit_code,
            child_start_count=child_start_count,
        )
        if result.cleanup_status == "completed":
            if not _private_leaf_is_absent_or_delete_pending(private_leaf):
                raise SupervisionError("private recovery cleanup is incomplete")
        elif result.cleanup_status == "failed":
            evidence.validate_directory_identity(
                private_leaf,
                expected_private_root_identity,
            )
            if not _has_run_member(root, validated_key, "logs/private-recovery.log"):
                raise SupervisionError("private recovery snapshot is missing")
        if result.outcome == "failed":
            _write_failure_marker(
                root,
                run_key,
                failure_source=result.failure_source,
                validator_exit_code=result.validator_exit_code,
            )
        supervisor = SupervisorRecord(
            schema_version=1,
            kind="reliability-supervisor-result",
            run_key=run_key,
            mode=mode,
            child_start_count=child_start_count,
            launcher_exit_code=launcher_exit_code,
            validator_exit_code=result.validator_exit_code,
            cleanup_status=result.cleanup_status,
            outcome=result.outcome,
            failure_source=result.failure_source,
        )
        evidence.write_artifact(
            root,
            run_key,
            "supervisor",
            _canonical_json(supervisor),
        )
        terminal = _build_terminal(
            root,
            run_key,
            result,
            supervisor,
            expected_private_root_identity=private_namespace_identity,
        )
        evidence.write_terminal(
            root,
            terminal,
            expected_private_recovery_namespace_identity=(
                private_namespace_identity
            ),
        )
        pointer = evidence.compare_and_swap_current(
            root,
            run_key,
            expected_token=expected_token,
        )
        current = evidence.verify_current(root)
        if not isinstance(current, evidence.CurrentVerification):
            raise SupervisionError("current verification is invalid")
        if current.pointer != pointer or current.run.terminal != terminal:
            raise SupervisionError("current verification is invalid")
        return FinalizedSupervision(
            status="current",
            run_key=run_key,
            pointer_token=evidence.pointer_token(pointer),
            formal_exit_code=launcher_exit_code,
        )
    except (
        ValidationError,
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
        SupervisionError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        if isinstance(exc, SupervisionError):
            raise
        raise SupervisionError("supervision could not be finalized") from exc
