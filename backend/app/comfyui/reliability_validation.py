from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Sequence

import soundfile
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator, model_validator


Engine = Literal["gpt-sovits", "indextts", "cosyvoice"]
Outcome = Literal["completed", "cancelled", "failed", "timeout"]
Phase = Literal["steady", "fault", "recovery"]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
REQUIRED_BOUNDARY_LABELS = ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

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
    parent_pid: StrictInt = Field(ge=0)
    parent_creation_time: datetime
    executable_name: str = Field(min_length=1)
    executable_hash: SHA256
    ownership_hash: SHA256
    started: StrictBool
    stopped: StrictBool
    descendants_stopped: StrictBool
    alive_after: StrictBool


class ComfyQueueEvidence(_StrictModel):
    queue_empty: StrictBool
    history_present: StrictBool
    prompt_id: str = Field(min_length=1)
    queue_before_prompt_ids: list[str]
    queue_after_prompt_ids: list[str]
    history_prompt_ids: list[str]
    terminal_history_status: Outcome


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
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("timestamps must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _ordered_timestamps(self) -> "CaseEvidence":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self


class CaseValidation(_StrictModel):
    evidence: CaseEvidence
    valid: StrictBool
    diagnostics: list[str]


class ReliabilityRunSummary(_StrictModel):
    status: Literal["passed", "failed"]
    fixture_version: Literal[1]
    rounds: StrictInt
    cases: list[CaseEvidence]
    missing_cases: list[str] = Field(default_factory=list)
    duplicate_case_ids: list[str] = Field(default_factory=list)
    cleanup_failures: list[str] = Field(default_factory=list)
    validation_failures: list[str] = Field(default_factory=list)
    steady_counts: dict[Engine, StrictInt]

    @model_validator(mode="after")
    def _consistent_status(self) -> "ReliabilityRunSummary":
        failures = self.missing_cases or self.duplicate_case_ids or self.cleanup_failures or self.validation_failures
        if self.status == "passed" and (failures or any(count != self.rounds for count in self.steady_counts.values())):
            raise ValueError("passed summary contains failed evidence")
        return self


class RequiredCase(_StrictModel):
    case_id: str = Field(min_length=1)
    phase: Literal["fault", "recovery"]
    expected: Outcome


def validate_case(case: CaseEvidence, *, wav_path: Path | None = None) -> CaseValidation:
    diagnostics: list[str] = []
    evidence = case
    if case.expected != case.actual:
        diagnostics.append("expected outcome does not match actual outcome")
    if not case.cleanup.ok or not case.cleanup.owned_processes_stopped or not case.cleanup.temp_paths_removed:
        diagnostics.append("cleanup proof is incomplete")
    if (not case.comfyui.queue_empty or not case.comfyui.history_present or case.comfyui.prompt_id != case.prompt_id or case.prompt_id in case.comfyui.queue_after_prompt_ids or case.prompt_id not in case.comfyui.history_prompt_ids or case.comfyui.terminal_history_status != case.actual):
        diagnostics.append("ComfyUI queue/history proof is incomplete")
    if any(process.alive_after or not process.stopped or not process.descendants_stopped or process.ownership != "validator-owned" for process in case.processes):
        diagnostics.append("process ownership/cleanup proof is incomplete")
    if case.gpu_peak.used_mib < max(case.gpu_before.used_mib, case.gpu_after.used_mib) or case.gpu_after.used_mib - case.gpu_before.used_mib > 1024:
        diagnostics.append("GPU memory did not recover")
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
            except (OSError, RuntimeError, ValueError) as exc:
                diagnostics.append(f"WAV proof invalid: {exc}")
            else:
                evidence = case.model_copy(update={"audio": proof})
        if evidence.audio is None:
            diagnostics.append("completed case is missing WAV proof")
    elif case.audio is not None:
        diagnostics.append("non-completed case must not publish WAV proof")
    return CaseValidation(evidence=evidence, valid=not diagnostics, diagnostics=diagnostics)


def finalize_run(
    fixture: ReliabilityFixture,
    cases: list[CaseEvidence],
    *,
    required_case_ids: Mapping[str, Mapping[str, str] | RequiredCase] | Sequence[RequiredCase] | set[str],
) -> ReliabilityRunSummary:
    required = _required_cases(required_case_ids)
    duplicate_case_ids = sorted(_duplicates(case.case_id for case in cases))
    present = {case.case_id for case in cases}
    missing_cases = sorted(set(required) - present)
    validations = [validate_case(case) for case in cases]
    validation_failures = sorted(
        f"{validation.evidence.case_id}: {diagnostic}"
        for validation in validations
        for diagnostic in validation.diagnostics
    )
    cleanup_failures = sorted(
        case.case_id
        for case in cases
        if not case.cleanup.ok or not case.cleanup.owned_processes_stopped or not case.cleanup.temp_paths_removed
    )
    steady_counts: dict[Engine, int] = {"gpt-sovits": 0, "indextts": 0, "cosyvoice": 0}
    raw_steady_counts: dict[Engine, int] = {"gpt-sovits": 0, "indextts": 0, "cosyvoice": 0}
    for validation in validations:
        case = validation.evidence
        if case.phase == "steady": raw_steady_counts[case.engine] += 1
        if case.phase == "steady" and validation.valid and case.expected == "completed" and case.actual == "completed":
            steady_counts[case.engine] += 1
    for engine, count in steady_counts.items():
        if raw_steady_counts[engine] != fixture.rounds or count != fixture.rounds:
            validation_failures.append(f"steady {engine} count is {raw_steady_counts[engine]}, expected {fixture.rounds}")
    for case in cases:
        spec = required.get(case.case_id)
        if case.phase != "steady" and spec is None: validation_failures.append(f"extra required case: {case.case_id}")
        if spec is not None and (case.phase != spec.phase or case.actual != spec.expected or not validate_case(case).valid):
            validation_failures.append(f"required case {case.case_id} has wrong phase" if case.phase != spec.phase else f"required case failed: {case.case_id}")
    validation_failures = sorted(set(validation_failures))
    failed = bool(duplicate_case_ids or missing_cases or cleanup_failures or validation_failures)
    return ReliabilityRunSummary(
        status="failed" if failed else "passed",
        fixture_version=fixture.version,
        rounds=fixture.rounds,
        cases=[validation.evidence for validation in validations],
        missing_cases=missing_cases,
        duplicate_case_ids=duplicate_case_ids,
        cleanup_failures=cleanup_failures,
        validation_failures=validation_failures,
        steady_counts=steady_counts,
    )


def write_atomic_json(path: Path, payload: ReliabilityRunSummary | dict[str, Any]) -> None:
    _assert_public_evidence(payload.model_dump(mode="json") if isinstance(payload, ReliabilityRunSummary) else payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = payload.model_dump(mode="json") if isinstance(payload, ReliabilityRunSummary) else payload
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    backup = path.with_name(f".{path.name}.{next(tempfile._get_candidate_names())}.bak")
    had_prior = path.exists()
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if had_prior:
            os.replace(path, backup)
        try:
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except BaseException:
            if had_prior and backup.exists():
                os.replace(backup, path)
                try: _fsync_directory(path.parent)
                except OSError: pass
            elif path.exists():
                path.unlink(missing_ok=True)
            raise
        backup.unlink(missing_ok=True)
    except BaseException:
        temporary.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise


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


def _required_cases(value: Mapping[str, Mapping[str, str] | RequiredCase] | Sequence[RequiredCase] | set[str]) -> dict[str, RequiredCase]:
    if isinstance(value, set):
        return {case_id: RequiredCase(case_id=case_id, phase="fault", expected="completed") for case_id in value}
    if isinstance(value, Mapping):
        return {case_id: item if isinstance(item, RequiredCase) else RequiredCase(case_id=case_id, **item) for case_id, item in value.items()}
    output: dict[str, RequiredCase] = {}
    for item in value:
        if item.case_id in output:
            raise ValueError("duplicate required case IDs")
        output[item.case_id] = item
    return output


def _assert_public_evidence(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_public_evidence(str(key))
            _assert_public_evidence(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value: _assert_public_evidence(item)
        return
    if not isinstance(value, str): return
    lowered = value.lower()
    if (re.match(r"^[a-z]:[\\/]", lowered) or lowered.startswith("\\\\") or lowered.startswith("/home/") or lowered.startswith("/users/") or re.search(r"(?:bearer\s+|token\s*[:=]|password\s*[:=]|api[_-]?key\s*[:=]|authorization)", lowered)):
        raise ValueError("unsafe evidence")


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
