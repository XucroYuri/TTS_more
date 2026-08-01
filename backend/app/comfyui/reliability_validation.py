from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import warnings
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import soundfile
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator, model_validator


Engine = Literal["gpt-sovits", "indextts", "cosyvoice"]
Outcome = Literal["completed", "cancelled", "failed", "timeout"]
Phase = Literal["steady", "fault", "recovery"]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
REQUIRED_BOUNDARY_LABELS = ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")
ENGINE_ORDER: tuple[Engine, ...] = ("gpt-sovits", "indextts", "cosyvoice")
_SENSITIVE_KEYS = {
    "access_key",
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "aws_access_key_id",
    "aws_secret_access_key",
    "client_secret",
    "password",
    "private",
    "refresh_token",
    "secret",
    "token",
}
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ModelT = TypeVar("ModelT", bound="_StrictModel")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        frozen=True,
        revalidate_instances="always",
        allow_inf_nan=False,
    )

    @model_validator(mode="before")
    @classmethod
    def _public_values_only(cls, value: Any) -> Any:
        _assert_public_evidence(value)
        return value


class FixtureResource(_StrictModel):
    resource_id: str
    reference_audio: str
    reference_text: str

    @field_validator("reference_audio")
    @classmethod
    def _relative_audio_label(cls, value: str) -> str:
        if value and (Path(value).is_absolute() or "\\" in value):
            raise ValueError("reference_audio must be a relative label")
        return value


class ReliabilityFixture(_StrictModel):
    version: Literal[1]
    base_urls: dict[Literal["tts_more", "comfyui"], str]
    resources: dict[Engine, FixtureResource]
    rounds: StrictInt

    @model_validator(mode="after")
    def _fixture_contract(self) -> "ReliabilityFixture":
        if set(self.base_urls) != {"tts_more", "comfyui"}:
            raise ValueError("base_urls must contain tts_more and comfyui")
        if set(self.resources) != {"gpt-sovits", "indextts", "cosyvoice"}:
            raise ValueError("resources must contain all three engines")
        if self.rounds != 10:
            raise ValueError("fixture rounds must be exactly 10")
        return self


class AudioProof(_StrictModel):
    sha256: SHA256
    size_bytes: StrictInt = Field(gt=0)
    sample_rate: StrictInt = Field(gt=0)
    frames: StrictInt = Field(gt=0)
    peak: StrictFloat = Field(gt=1e-5, le=1.0)


class CleanupEvidence(_StrictModel):
    ok: StrictBool
    owned_processes_stopped: StrictBool
    temp_paths_removed: StrictBool


class ProcessEvidence(_StrictModel):
    pid: StrictInt = Field(gt=0)
    ownership: Literal["validator-owned", "pre-existing"]
    command_hash: SHA256
    creation_time: datetime
    parent_pid: StrictInt = Field(gt=0)
    parent_creation_time: datetime
    stopped_at: datetime
    executable_name: str = Field(min_length=1)
    executable_hash: SHA256
    ownership_hash: SHA256
    started: StrictBool
    stopped: StrictBool
    descendants_stopped: StrictBool
    alive_after: StrictBool

    @field_validator("creation_time", "parent_creation_time", "stopped_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("process timestamps must be timezone-aware UTC")
        return value

    @field_validator("executable_name")
    @classmethod
    def _neutral_executable_name(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value or Path(value).is_absolute():
            raise ValueError("executable_name must be a neutral basename")
        return value

    @model_validator(mode="after")
    def _complete_lifecycle(self) -> "ProcessEvidence":
        if self.parent_creation_time > self.creation_time or self.creation_time > self.stopped_at:
            raise ValueError("process timestamps are not ordered")
        if self.parent_pid == self.pid:
            raise ValueError("process parent_pid must differ from pid")
        if (
            self.ownership != "validator-owned"
            or not self.started
            or not self.stopped
            or not self.descendants_stopped
            or self.alive_after
        ):
            raise ValueError("process lifecycle proof is incomplete")
        return self


class ComfyQueueEvidence(_StrictModel):
    queue_empty: StrictBool
    history_present: StrictBool
    prompt_id: str = Field(min_length=1)
    queue_before_prompt_ids: list[str]
    queue_after_prompt_ids: list[str]
    history_prompt_ids: list[str]
    terminal_history_status: Outcome

    @field_validator("queue_before_prompt_ids", "queue_after_prompt_ids", "history_prompt_ids")
    @classmethod
    def _sorted_prompt_ids(cls, value: list[str]) -> list[str]:
        return sorted(value)

    @model_validator(mode="after")
    def _complete_observation(self) -> "ComfyQueueEvidence":
        if (
            not self.queue_empty
            or not self.history_present
            or self.queue_before_prompt_ids.count(self.prompt_id) != 1
            or self.queue_after_prompt_ids
            or self.history_prompt_ids.count(self.prompt_id) != 1
        ):
            raise ValueError("ComfyUI queue/history proof is incomplete")
        return self


class GpuSnapshot(_StrictModel):
    used_mib: StrictInt = Field(ge=0)
    free_mib: StrictInt = Field(ge=0)


class RepositorySnapshot(_StrictModel):
    label: str = Field(min_length=1)
    head: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    branch: str = Field(min_length=1)
    porcelain_hash: SHA256


class BoundaryEvidence(_StrictModel):
    before_hash: SHA256
    after_hash: SHA256
    private_registry_hash: SHA256
    reference_hashes: dict[str, SHA256]
    repositories_before: list[RepositorySnapshot] = Field(default_factory=list)
    repositories_after: list[RepositorySnapshot] = Field(default_factory=list)
    private_registry_before_hash: SHA256 | None = None
    private_registry_after_hash: SHA256 | None = None
    reference_hashes_before: dict[str, SHA256] = Field(default_factory=dict)
    reference_hashes_after: dict[str, SHA256] = Field(default_factory=dict)

    @field_validator("repositories_before", "repositories_after")
    @classmethod
    def _sorted_repositories(cls, value: list[RepositorySnapshot]) -> list[RepositorySnapshot]:
        return sorted(
            value,
            key=lambda snapshot: (snapshot.label, snapshot.head, snapshot.branch, snapshot.porcelain_hash),
        )

    @field_validator("reference_hashes", "reference_hashes_before", "reference_hashes_after")
    @classmethod
    def _sorted_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: value[key] for key in sorted(value)}


class CaseEvidence(_StrictModel):
    case_id: str = Field(min_length=1)
    phase: Phase
    engine: Engine
    expected: Outcome
    actual: Outcome
    job_id: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    audio: AudioProof | None = None
    cleanup: CleanupEvidence
    processes: list[ProcessEvidence] = Field(min_length=1)
    comfyui: ComfyQueueEvidence
    gpu_before: GpuSnapshot
    gpu_peak: GpuSnapshot
    gpu_after: GpuSnapshot
    boundary: BoundaryEvidence

    @field_validator("processes")
    @classmethod
    def _sorted_processes(cls, value: list[ProcessEvidence]) -> list[ProcessEvidence]:
        return sorted(
            value,
            key=lambda process: (
                process.pid,
                process.creation_time,
                process.parent_pid,
                process.parent_creation_time,
                process.stopped_at,
            ),
        )

    @field_validator("case_id", "job_id", "prompt_id", "version_id")
    @classmethod
    def _opaque_identifier(cls, value: str) -> str:
        if "\\" in value or "/" in value or Path(value).is_absolute():
            raise ValueError("identifiers must not contain paths")
        if re.search(r"(?i)(?:token|secret|api[_-]?key)\s*[:=]", value):
            raise ValueError("identifiers must not contain secrets")
        return value

    @field_validator("started_at", "finished_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("timestamps must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _ordered_timestamps(self) -> "CaseEvidence":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        process_identities: set[tuple[int, datetime]] = set()
        for process in self.processes:
            identity = (process.pid, process.creation_time)
            if identity in process_identities:
                raise ValueError("process identities must be unique")
            process_identities.add(identity)
            if process.creation_time < self.started_at or process.stopped_at > self.finished_at:
                raise ValueError("process timestamps fall outside the case observation")
        if not _pid_lifetimes_are_disjoint(self.processes):
            raise ValueError("same-PID lifetimes must be strictly ordered and non-overlapping")
        if not _queue_proof_valid(self):
            raise ValueError("ComfyUI queue/history proof is incomplete")
        if not _gpu_proof_valid(self):
            raise ValueError("GPU memory observation/recovery proof is incomplete")
        return self


class CaseValidation(_StrictModel):
    evidence: CaseEvidence
    valid: StrictBool
    diagnostics: list[str]


class RequiredCase(_StrictModel):
    case_id: str = Field(min_length=1)
    engine: Engine
    phase: Literal["fault", "recovery"]
    expected: Outcome

    @model_validator(mode="after")
    def _phase_outcome_contract(self) -> "RequiredCase":
        if self.phase == "recovery" and self.expected != "completed":
            raise ValueError("recovery cases must expect completed")
        if self.phase == "fault" and self.expected == "completed":
            raise ValueError("fault cases must expect a non-completed outcome")
        return self


class ReliabilityRunSummary(_StrictModel):
    status: Literal["passed", "failed"]
    fixture_version: Literal[1]
    rounds: StrictInt
    required_cases: list[RequiredCase]
    cases: list[CaseEvidence]
    missing_cases: list[str] = Field(default_factory=list)
    duplicate_case_ids: list[str] = Field(default_factory=list)
    cleanup_failures: list[str] = Field(default_factory=list)
    validation_failures: list[str] = Field(default_factory=list)
    boundary_failures: list[str] = Field(default_factory=list)
    steady_counts: dict[Engine, StrictInt]

    @field_validator(
        "missing_cases",
        "duplicate_case_ids",
        "cleanup_failures",
        "validation_failures",
        "boundary_failures",
    )
    @classmethod
    def _sorted_unique_strings(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @field_validator("steady_counts")
    @classmethod
    def _ordered_steady_counts(cls, value: dict[Engine, int]) -> dict[Engine, int]:
        return {engine: value[engine] for engine in ENGINE_ORDER if engine in value}

    @field_validator("required_cases")
    @classmethod
    def _sorted_unique_required_cases(cls, value: list[RequiredCase]) -> list[RequiredCase]:
        case_ids = [required.case_id for required in value]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("required case specifications must be unique")
        return sorted(
            value,
            key=lambda required: (required.case_id, required.engine, required.phase, required.expected),
        )

    @model_validator(mode="after")
    def _consistent_status(self) -> "ReliabilityRunSummary":
        case_key = lambda case: (case.case_id, case.engine, case.phase, case.expected, case.actual)
        if self.cases != sorted(self.cases, key=case_key):
            raise ValueError("summary cases must be deterministically sorted")
        if self.status != "passed":
            return self
        failures = (
            self.missing_cases
            or self.duplicate_case_ids
            or self.cleanup_failures
            or self.validation_failures
            or self.boundary_failures
        )
        expected_counts = {engine: self.rounds for engine in ENGINE_ORDER}
        case_ids = [case.case_id for case in self.cases]
        computed_counts = {
            engine: sum(
                case.phase == "steady"
                and case.engine == engine
                and case.expected == "completed"
                and case.actual == "completed"
                and validate_case(case).valid
                for case in self.cases
            )
            for engine in ENGINE_ORDER
        }
        raw_counts = {
            engine: sum(case.phase == "steady" and case.engine == engine for case in self.cases)
            for engine in ENGINE_ORDER
        }
        required_by_id = {required.case_id: required for required in self.required_cases}
        nonsteady_cases = [case for case in self.cases if case.phase != "steady"]
        nonsteady_ids = [case.case_id for case in nonsteady_cases]
        required_match = (
            {required.phase for required in self.required_cases} == {"fault", "recovery"}
            and len(nonsteady_cases) == len(self.required_cases)
            and set(nonsteady_ids) == set(required_by_id)
            and all(
                case.engine == required_by_id[case.case_id].engine
                and case.phase == required_by_id[case.case_id].phase
                and case.expected == required_by_id[case.case_id].expected
                and case.actual == required_by_id[case.case_id].expected
                and validate_case(case).valid
                for case in nonsteady_cases
            )
        )
        if (
            self.rounds != 10
            or failures
            or self.steady_counts != expected_counts
            or computed_counts != expected_counts
            or raw_counts != expected_counts
            or len(case_ids) != len(set(case_ids))
            or any(not validate_case(case).valid for case in self.cases)
            or not required_match
        ):
            raise ValueError("passed summary contains failed evidence")
        return self


def _process_proof_valid(case: CaseEvidence) -> bool:
    try:
        identities: set[tuple[int, datetime]] = set()
        if not isinstance(case.processes, list) or not case.processes:
            return False
        for process in case.processes:
            identity = (process.pid, process.creation_time)
            if identity in identities:
                return False
            identities.add(identity)
            ordered = (
                process.parent_creation_time
                <= process.creation_time
                <= process.stopped_at
                <= case.finished_at
                and case.started_at <= process.creation_time
            )
            if (
                isinstance(process.pid, bool)
                or not isinstance(process.pid, int)
                or process.pid <= 0
                or isinstance(process.parent_pid, bool)
                or not isinstance(process.parent_pid, int)
                or process.parent_pid <= 0
                or process.parent_pid == process.pid
                or process.ownership != "validator-owned"
                or not _is_sha256(process.command_hash)
                or not _is_sha256(process.executable_hash)
                or not _is_sha256(process.ownership_hash)
                or not _is_neutral_basename(process.executable_name)
                or not _is_utc(process.parent_creation_time)
                or not _is_utc(process.creation_time)
                or not _is_utc(process.stopped_at)
                or not ordered
                or process.started is not True
                or process.stopped is not True
                or process.descendants_stopped is not True
                or process.alive_after is not False
            ):
                return False
        return _pid_lifetimes_are_disjoint(case.processes)
    except (AttributeError, TypeError):
        return False


def _queue_proof_valid(case: CaseEvidence) -> bool:
    try:
        queue = case.comfyui
        return (
            queue.queue_empty is True
            and queue.history_present is True
            and queue.prompt_id == case.prompt_id
            and isinstance(queue.queue_before_prompt_ids, list)
            and queue.queue_before_prompt_ids.count(case.prompt_id) == 1
            and isinstance(queue.queue_after_prompt_ids, list)
            and not queue.queue_after_prompt_ids
            and isinstance(queue.history_prompt_ids, list)
            and queue.history_prompt_ids.count(case.prompt_id) == 1
            and queue.terminal_history_status == case.actual
        )
    except (AttributeError, TypeError):
        return False


def _pid_lifetimes_are_disjoint(processes: Sequence[ProcessEvidence]) -> bool:
    try:
        ordered = sorted(processes, key=lambda process: (process.pid, process.creation_time))
    except (AttributeError, TypeError):
        return False
    previous_stopped_by_pid: dict[int, datetime] = {}
    for process in ordered:
        previous_stopped = previous_stopped_by_pid.get(process.pid)
        if previous_stopped is not None and process.creation_time <= previous_stopped:
            return False
        previous_stopped_by_pid[process.pid] = process.stopped_at
    return True


def _gpu_proof_valid(case: CaseEvidence) -> bool:
    try:
        snapshots = (case.gpu_before, case.gpu_peak, case.gpu_after)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for snapshot in snapshots
            for value in (snapshot.used_mib, snapshot.free_mib)
        ):
            return False
        totals = [snapshot.used_mib + snapshot.free_mib for snapshot in snapshots]
        return (
            case.gpu_peak.used_mib >= max(case.gpu_before.used_mib, case.gpu_after.used_mib)
            and case.gpu_after.used_mib - case.gpu_before.used_mib <= 1024
            and case.gpu_before.free_mib - case.gpu_after.free_mib <= 1024
            and max(totals) - min(totals) <= 1024
        )
    except (AttributeError, TypeError):
        return False


def _cleanup_proof_valid(case: CaseEvidence) -> bool:
    try:
        cleanup = case.cleanup
        return (
            cleanup.ok is True
            and cleanup.owned_processes_stopped is True
            and cleanup.temp_paths_removed is True
        )
    except AttributeError:
        return False


def _audio_proof_valid(audio: AudioProof | None) -> bool:
    if audio is None:
        return False
    try:
        return (
            _is_sha256(audio.sha256)
            and isinstance(audio.size_bytes, int)
            and not isinstance(audio.size_bytes, bool)
            and audio.size_bytes > 0
            and isinstance(audio.sample_rate, int)
            and not isinstance(audio.sample_rate, bool)
            and audio.sample_rate > 0
            and isinstance(audio.frames, int)
            and not isinstance(audio.frames, bool)
            and audio.frames > 0
            and isinstance(audio.peak, float)
            and math.isfinite(audio.peak)
            and 1e-5 < audio.peak <= 1.0
        )
    except (AttributeError, TypeError):
        return False


def _revalidate_model(value: Any, expected: type[ModelT]) -> ModelT:
    _assert_public_evidence(value)
    raw = dict(vars(value)) if isinstance(value, BaseModel) else value
    return expected.model_validate(raw)


def validate_case(case: CaseEvidence, *, wav_path: Path | None = None) -> CaseValidation:
    try:
        case = _revalidate_model(case, CaseEvidence)
    except (ValueError, TypeError, AttributeError, RecursionError):
        diagnostics: list[str] = []
        if not _cleanup_proof_valid(case):
            diagnostics.append("cleanup proof is incomplete")
        if not _queue_proof_valid(case):
            diagnostics.append("ComfyUI queue/history proof is incomplete")
        if not _process_proof_valid(case):
            diagnostics.append("process identity/lifecycle proof is incomplete")
        if not _gpu_proof_valid(case):
            diagnostics.append("GPU memory observation/recovery proof is incomplete")
        try:
            if case.actual == "completed" and not _audio_proof_valid(case.audio):
                diagnostics.append("WAV proof is invalid")
            elif case.actual != "completed" and case.audio is not None:
                diagnostics.append("non-completed case must not publish WAV proof")
        except AttributeError:
            diagnostics.append("case evidence failed schema validation")
        if not diagnostics:
            diagnostics.append("case evidence failed schema validation")
        return CaseValidation.model_construct(evidence=case, valid=False, diagnostics=diagnostics)

    diagnostics: list[str] = []
    evidence = case
    if case.expected != case.actual:
        diagnostics.append("expected outcome does not match actual outcome")
    if not _cleanup_proof_valid(case):
        diagnostics.append("cleanup proof is incomplete")
    if not _queue_proof_valid(case):
        diagnostics.append("ComfyUI queue/history proof is incomplete")
    if not _process_proof_valid(case):
        diagnostics.append("process identity/lifecycle proof is incomplete")
    if not _gpu_proof_valid(case):
        diagnostics.append("GPU memory observation/recovery proof is incomplete")
    boundary = case.boundary
    repository_before = {snapshot.label: snapshot for snapshot in boundary.repositories_before}
    repository_after = {snapshot.label: snapshot for snapshot in boundary.repositories_after}
    if (
        not repository_before
        or not repository_after
        or boundary.private_registry_before_hash is None
        or boundary.private_registry_after_hash is None
        or not boundary.reference_hashes_before
        or not boundary.reference_hashes_after
    ):
        diagnostics.append("boundary observations are incomplete")
    if (set(repository_before) != set(REQUIRED_BOUNDARY_LABELS) or set(repository_after) != set(REQUIRED_BOUNDARY_LABELS) or len(repository_before) != len(boundary.repositories_before) or len(repository_after) != len(boundary.repositories_after)):
        diagnostics.append("boundary repository set is incomplete or duplicated")
    if (
        boundary.before_hash != boundary.after_hash
        or repository_before != repository_after
        or boundary.private_registry_before_hash != boundary.private_registry_after_hash
        or boundary.reference_hashes_before != boundary.reference_hashes_after
    ):
        diagnostics.append("repository/model/private-registry boundary drift detected")
    if case.actual == "completed":
        if wav_path is not None:
            try:
                proof = _wav_proof(wav_path)
            except (OSError, RuntimeError, ValueError):
                diagnostics.append("WAV proof is invalid")
            else:
                evidence = case.model_copy(update={"audio": proof})
        if evidence.audio is None:
            diagnostics.append("completed case is missing WAV proof")
        elif not _audio_proof_valid(evidence.audio):
            diagnostics.append("WAV proof is invalid")
    elif case.audio is not None:
        diagnostics.append("non-completed case must not publish WAV proof")
    return CaseValidation(evidence=evidence, valid=not diagnostics, diagnostics=diagnostics)


def finalize_run(
    fixture: ReliabilityFixture,
    cases: Sequence[CaseEvidence],
    *,
    required_cases: Sequence[RequiredCase],
) -> ReliabilityRunSummary:
    try:
        fixture = _revalidate_model(fixture, ReliabilityFixture)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError("invalid fixture evidence") from None
    try:
        case_snapshot = tuple(cases)
        cases = tuple(_revalidate_model(case, CaseEvidence) for case in case_snapshot)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError("invalid case evidence") from None
    required = _required_cases(required_cases)
    duplicate_case_ids = sorted(_duplicates(case.case_id for case in cases))
    present = {case.case_id for case in cases}
    missing_cases = sorted(set(required) - present)
    validations = [validate_case(case) for case in cases]
    validation_failures = sorted(
        f"{validation.evidence.case_id}: {diagnostic}"
        for validation in validations
        for diagnostic in validation.diagnostics
    )
    boundary_failures = sorted(
        {
            validation.evidence.case_id
            for validation in validations
            if any(
                diagnostic.startswith("boundary ")
                or diagnostic.startswith("repository/model/private-registry ")
                for diagnostic in validation.diagnostics
            )
        }
    )
    cleanup_failures = sorted(
        case.case_id
        for case in cases
        if not _cleanup_proof_valid(case)
    )
    steady_counts: dict[Engine, int] = {engine: 0 for engine in ENGINE_ORDER}
    raw_steady_counts: dict[Engine, int] = {engine: 0 for engine in ENGINE_ORDER}
    for validation in validations:
        case = validation.evidence
        if case.phase == "steady": raw_steady_counts[case.engine] += 1
        if case.phase == "steady" and validation.valid and case.expected == "completed" and case.actual == "completed":
            steady_counts[case.engine] += 1
    for engine, count in steady_counts.items():
        if raw_steady_counts[engine] != fixture.rounds or count != fixture.rounds:
            validation_failures.append(f"steady {engine} count is {raw_steady_counts[engine]}, expected {fixture.rounds}")
    for case, validation in zip(cases, validations, strict=True):
        spec = required.get(case.case_id)
        if case.phase != "steady" and spec is None:
            validation_failures.append(f"extra required case: {case.case_id}")
        if spec is None:
            continue
        if case.engine != spec.engine:
            validation_failures.append(f"required case {case.case_id} has wrong engine")
        if case.phase != spec.phase:
            validation_failures.append(f"required case {case.case_id} has wrong phase")
        if case.expected != spec.expected:
            validation_failures.append(f"required case {case.case_id} has wrong expected outcome")
        if case.actual != spec.expected:
            validation_failures.append(f"required case {case.case_id} has wrong actual outcome")
        if not validation.valid:
            validation_failures.append(f"required case failed: {case.case_id}")
    validation_failures = sorted(set(validation_failures))
    failed = bool(duplicate_case_ids or missing_cases or cleanup_failures or validation_failures or boundary_failures)
    output_cases = sorted(
        (validation.evidence for validation in validations),
        key=lambda case: (case.case_id, case.engine, case.phase, case.expected, case.actual),
    )
    return ReliabilityRunSummary(
        status="failed" if failed else "passed",
        fixture_version=fixture.version,
        rounds=fixture.rounds,
        required_cases=list(required.values()),
        cases=output_cases,
        missing_cases=missing_cases,
        duplicate_case_ids=duplicate_case_ids,
        cleanup_failures=cleanup_failures,
        validation_failures=validation_failures,
        boundary_failures=boundary_failures,
        steady_counts=steady_counts,
    )


def write_atomic_json(path: Path, payload: ReliabilityRunSummary | dict[str, Any]) -> None:
    _assert_public_evidence(payload)
    if isinstance(payload, ReliabilityRunSummary):
        try:
            payload = _revalidate_model(payload, ReliabilityRunSummary)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                document = payload.model_dump(mode="json", warnings="error")
            if caught:
                raise ValueError("serializer warning")
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise ValueError("invalid reliability summary") from None
    else:
        document = payload
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _write_reserved_json(path, document)
    except Exception:
        raise OSError("atomic evidence preparation failed before publication") from None

    backup: Path | None = None
    had_prior = path.exists()
    if had_prior:
        try:
            backup = _copy_to_reserved_file(path, suffix="bak")
            _fsync_directory(path.parent)
        except Exception:
            _best_effort_unlink(temporary)
            _best_effort_unlink(backup)
            raise OSError("atomic evidence preparation failed; live evidence is unchanged") from None

    if had_prior:
        try:
            os.replace(temporary, path)
        except Exception:
            _best_effort_unlink(temporary)
            if backup is not None:
                _best_effort_unlink(backup)
            raise OSError("atomic evidence publication failed before commit") from None
    else:
        try:
            os.link(temporary, path)
        except FileExistsError:
            _best_effort_unlink(temporary)
            raise OSError("atomic evidence publication conflict; concurrent evidence is unchanged") from None
        except Exception:
            _best_effort_unlink(temporary)
            raise OSError("atomic evidence publication failed before commit") from None
        _best_effort_unlink(temporary)

    try:
        _fsync_directory(path.parent)
    except Exception:
        if backup is not None:
            _restore_existing_after_dirsync_failure(path, backup)
        _retain_first_write_after_dirsync_failure()

    if backup is not None:
        _best_effort_unlink(backup)


def _write_reserved_json(path: Path, document: dict[str, Any]) -> Path:
    descriptor, temporary = _reserve_owned_file(path, suffix="tmp")
    handle_opened = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle_opened = True
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if not handle_opened:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _best_effort_unlink(temporary)
        raise
    return temporary


def _copy_to_reserved_file(source: Path, *, suffix: str) -> Path:
    descriptor, destination = _reserve_owned_file(source, suffix=suffix)
    handle_opened = False
    try:
        with os.fdopen(descriptor, "wb") as destination_handle:
            handle_opened = True
            with source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    except BaseException:
        if not handle_opened:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _best_effort_unlink(destination)
        raise
    return destination


def _reserve_owned_file(path: Path, *, suffix: str) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    for _attempt in range(128):
        candidate = path.with_name(f".{path.name}.{secrets.token_hex(16)}.{suffix}")
        try:
            return os.open(candidate, flags, 0o600), candidate
        except FileExistsError:
            continue
    raise OSError("could not reserve an evidence artifact")


def _restore_existing_after_dirsync_failure(path: Path, backup: Path) -> None:
    restoration: Path | None = None
    try:
        restoration = _copy_to_reserved_file(backup, suffix="restore.tmp")
        os.replace(restoration, path)
        restoration = None
        _fsync_directory(path.parent)
    except Exception:
        _best_effort_unlink(restoration)
        raise OSError("atomic evidence recovery incomplete; last-good backup retained") from None
    _best_effort_unlink(backup)
    raise OSError("atomic evidence publication was rolled back") from None


def _retain_first_write_after_dirsync_failure() -> None:
    raise OSError("atomic evidence durability unconfirmed; recoverable evidence retained") from None


def _best_effort_unlink(path: Path | None) -> bool:
    if path is None:
        return True
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _wav_proof(path: Path) -> AudioProof:
    samples, sample_rate = soundfile.read(path, dtype="float32", always_2d=True)
    if sample_rate <= 0 or len(samples) <= 0:
        raise ValueError("empty audio")
    minimum = float(samples.min())
    maximum = float(samples.max())
    if not math.isfinite(minimum) or not math.isfinite(maximum):
        raise ValueError("non-finite audio")
    peak = max(abs(minimum), abs(maximum))
    if peak <= 1e-5:
        raise ValueError("silent audio")
    data = path.read_bytes()
    return AudioProof(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        sample_rate=sample_rate,
        frames=int(samples.shape[0]),
        peak=float(peak),
    )


def _duplicates(values: Any) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _required_cases(value: Sequence[RequiredCase]) -> dict[str, RequiredCase]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("required_cases must be an ordered RequiredCase sequence")
    output: dict[str, RequiredCase] = {}
    for item in value:
        if not isinstance(item, RequiredCase):
            raise ValueError("required_cases must be an ordered RequiredCase sequence")
        validated = _revalidate_model(item, RequiredCase)
        if validated.case_id in output:
            raise ValueError("duplicate required case specification")
        output[validated.case_id] = validated
    return {case_id: output[case_id] for case_id in sorted(output)}


def _assert_public_evidence(value: Any) -> None:
    if isinstance(value, BaseModel):
        _assert_public_evidence(vars(value))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text) and _contains_private_value(item):
                raise ValueError("unsafe evidence")
            _assert_public_string(key_text)
            _assert_public_evidence(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _assert_public_evidence(item)
        return
    if isinstance(value, str):
        _assert_public_string(value)


def _assert_public_string(value: str) -> None:
    lowered = value.lower()
    contains_path = bool(
        re.search(r"(?i)(?<![a-z0-9])[a-z]:[\\/]", value)
        or re.search(r"\\\\[^\\/\s]+[\\/][^\\/\s]+", value)
        or re.search(r"(?i)\bfile://", value)
        or re.search(r"(?:^|[\s\"'=(:,;])/(?!/)[^\s]+", value)
    )
    contains_secret = bool(
        re.search(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/?#@]+:[^\s/?#@]+@[^\s/?#]+", value)
        or
        re.search(
            r"(?:\bbearer\s+\S+|\b(?:access[_-]?key|access[_-]?token|api[_-]?key|authorization|client[_-]?secret|password|private[_-]?key|refresh[_-]?token|secret|token)\s*[:=]\s*\S+)",
            lowered,
        )
    )
    if contains_path or contains_secret:
        raise ValueError("unsafe evidence")


def _contains_private_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (dict, list, tuple, set)) and not value:
        return False
    return True


def _is_sensitive_key(value: str) -> bool:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
    compact = normalized.replace("_", "")
    compact_sensitive = {key.replace("_", "") for key in _SENSITIVE_KEYS}
    parts = set(normalized.split("_"))
    return (
        normalized in _SENSITIVE_KEYS
        or compact in compact_sensitive
        or bool(parts & {"authorization", "credential", "credentials", "password", "secret", "token"})
        or {"api", "key"} <= parts
        or {"access", "key"} <= parts
        or {"private", "key"} <= parts
    )


def _is_utc(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timezone.utc.utcoffset(value)
    )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _HASH_PATTERN.fullmatch(value) is not None


def _is_neutral_basename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not Path(value).is_absolute()
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
