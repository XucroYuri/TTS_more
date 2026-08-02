from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

import httpx
import soundfile
import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, TypeAdapter, ValidationError, field_validator, model_validator


Engine = Literal["gpt-sovits", "indextts", "cosyvoice"]
Outcome = Literal["completed", "cancelled", "failed", "timeout"]
Phase = Literal["steady", "fault", "recovery"]
CaseAction = Literal[
    "synthesize",
    "cancel-queued",
    "cancel-running",
    "timeout",
    "terminate-comfyui",
    "restart-readiness",
]
SHA256 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
REQUIRED_BOUNDARY_LABELS = ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")
ENGINE_ORDER: tuple[Engine, ...] = ("gpt-sovits", "indextts", "cosyvoice")
DEFAULT_NORMAL_REQUEST_TIMEOUT_SECONDS: dict[Engine, float] = {
    "gpt-sovits": 120.0,
    "indextts": 240.0,
    "cosyvoice": 180.0,
}
MAX_NORMAL_REQUEST_TIMEOUT_SECONDS = 600.0
TERMINAL_CONVERGENCE_SECONDS = 30.0
MAX_PUBLIC_WINDOWS_PID = 2_147_483_647
MAX_PUBLIC_EXECUTABLE_NAME_LENGTH = 255
MAX_PUBLIC_GPU_MIB = 9_223_372_036_854_775_807
MAX_FAILED_CASE_PROCESSES = 1_024
MAX_FAILED_CASE_POLL_COUNT = 10_000
MAX_FAILED_CASE_QUEUE_COUNT = 10_000
MAX_FAILED_CASE_AUDIO_BYTES = 4_294_967_295
MAX_FAILED_CASE_ID_LENGTH = 128
MAX_FAILURE_CODE_LENGTH = 64
MAX_FAILED_CASE_JSON_BYTES = 4_194_304
MAX_RELIABILITY_SUMMARY_JSON_BYTES = 67_108_864
MAX_TERMINAL_COHORT_CASES = 128
_TERMINAL_TRANSITION_THREAD_LOCK = threading.Lock()
_BRIDGE_ENGINE_IDS: dict[str, Engine] = {
    "gpt_sovits": "gpt-sovits",
    "index_tts": "indextts",
    "cosyvoice": "cosyvoice",
}
_REGISTERED_SERVICE_CONTRACT = "comfyui-tts-audio-suite-v1"
_REGISTERED_SERVICE_CAPABILITIES = frozenset(
    {
        "tts",
        "reference-audio-voice",
        "wav-output",
        "comfyui",
        "tts-audio-suite",
    }
)
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
_SCHEME_URI_PATTERN = re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_NETWORK_URI_PATTERN = re.compile(r"(?<![:/])//[^\s<>\"']+")
ModelT = TypeVar("ModelT", bound="_StrictModel")
_PUBLIC_UTC_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
_PublicUtcTimestamp = Annotated[
    str,
    Field(min_length=20, max_length=27, pattern=_PUBLIC_UTC_PATTERN),
]
ObservedStatus = Literal["queued", "running", "cancelling", "cancelled", "failed", "completed"]


def _parse_public_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        raise ValueError("timestamp must be valid UTC") from None
    if not _is_utc(parsed):
        raise ValueError("timestamp must be timezone-aware UTC")
    return parsed


def _public_utc(value: datetime) -> str:
    if not _is_utc(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_document(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        encoded = type(value).__name__.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _publish_object_property_bound(schema: dict[str, Any]) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["maxProperties"] = len(properties)


class _BoundedPublicModel(_StrictModel):
    model_config = ConfigDict(json_schema_extra=_publish_object_property_bound)


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


def _validate_normal_request_timeouts(value: Any) -> dict[Engine, float]:
    if not isinstance(value, dict) or set(value) != set(ENGINE_ORDER):
        raise ValueError("normal request timeouts must contain exactly the three engines")
    validated: dict[Engine, float] = {}
    for engine in ENGINE_ORDER:
        seconds = value.get(engine)
        if type(seconds) is not float or not math.isfinite(seconds):
            raise ValueError("normal request timeouts must be finite strict floats")
        if (
            seconds < DEFAULT_NORMAL_REQUEST_TIMEOUT_SECONDS[engine]
            or seconds > MAX_NORMAL_REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError("normal request timeout is outside the reviewed safe range")
        validated[engine] = seconds
    return validated


class ReliabilityFixture(_StrictModel):
    version: Literal[1]
    base_urls: dict[Literal["tts_more", "comfyui"], str]
    resources: dict[Engine, FixtureResource]
    rounds: StrictInt
    normal_request_timeout_seconds: dict[Engine, StrictFloat] = Field(
        default_factory=lambda: dict(DEFAULT_NORMAL_REQUEST_TIMEOUT_SECONDS),
    )

    @field_validator("normal_request_timeout_seconds", mode="before")
    @classmethod
    def _normal_request_timeout_contract(cls, value: Any) -> dict[Engine, float]:
        return _validate_normal_request_timeouts(value)

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


class CleanupEvidence(_BoundedPublicModel):
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
            or not _prompt_ids_are_unique(self.queue_before_prompt_ids)
            or not _prompt_ids_are_unique(self.queue_after_prompt_ids)
            or not _prompt_ids_are_unique(self.history_prompt_ids)
            or self.queue_before_prompt_ids.count(self.prompt_id) != 1
            or self.queue_after_prompt_ids
            or self.history_prompt_ids.count(self.prompt_id) != 1
        ):
            raise ValueError("ComfyUI queue/history proof is incomplete")
        return self


class FaultControlEvidence(_StrictModel):
    control_code: Literal["cancelled", "timeout"]
    failure_stage: Literal["timeout"] | None
    prompt_id: str = Field(min_length=1)
    initial_state: Literal["running"]
    final_state: Literal["interrupted"]
    actions: list[Literal["interrupt"]] = Field(min_length=1, max_length=1)
    duration_seconds: StrictFloat = Field(ge=0.0, le=30.0)
    converged: StrictBool

    @field_validator("prompt_id")
    @classmethod
    def _opaque_prompt_id(cls, value: str) -> str:
        if "\\" in value or "/" in value or Path(value).is_absolute():
            raise ValueError("prompt_id must not contain paths")
        return value

    @model_validator(mode="after")
    def _truthful_control_terminal(self) -> "FaultControlEvidence":
        expected_stage = "timeout" if self.control_code == "timeout" else None
        if self.failure_stage != expected_stage or self.actions != ["interrupt"] or not self.converged:
            raise ValueError("fault control evidence is incomplete")
        return self


class TtsTerminalEvidence(_StrictModel):
    job_status: Outcome
    item_status: Outcome
    version_status: Outcome | None
    manifest_version_absent: StrictBool
    version_audio_absent: StrictBool
    control: FaultControlEvidence | None = None


class TerminationEvidence(_StrictModel):
    endpoint_unavailable: StrictBool
    prompt_id: str = Field(min_length=1)
    queue_before_prompt_ids: list[str]
    manifest_audio_absent: StrictBool

    @field_validator("queue_before_prompt_ids")
    @classmethod
    def _sorted_unique_prompt_ids(cls, value: list[str]) -> list[str]:
        if not _prompt_ids_are_unique(value):
            raise ValueError("termination queue prompt ids must be unique")
        return sorted(value)

    @model_validator(mode="after")
    def _target_was_running_before_termination(self) -> "TerminationEvidence":
        if self.queue_before_prompt_ids.count(self.prompt_id) != 1:
            raise ValueError("termination target prompt was not observed")
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
    prompt_id: str | None = None
    version_id: str | None = None
    prompt_submitted: StrictBool = True
    tts_more: TtsTerminalEvidence | None = None
    termination: TerminationEvidence | None = None
    started_at: datetime
    finished_at: datetime
    audio: AudioProof | None = None
    cleanup: CleanupEvidence
    processes: list[ProcessEvidence] = Field(min_length=1)
    comfyui: ComfyQueueEvidence | None
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
    def _opaque_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
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


class CasePlan(_StrictModel):
    case_id: str = Field(min_length=1)
    phase: Phase
    engine: Engine
    expected: Outcome
    action: CaseAction
    request_timeout_seconds: StrictFloat = Field(gt=0.0, le=MAX_NORMAL_REQUEST_TIMEOUT_SECONDS)
    convergence_seconds: StrictFloat = Field(gt=0.0, le=TERMINAL_CONVERGENCE_SECONDS)


class ReadyResource(_StrictModel):
    engine: Engine
    resource_id: str = Field(min_length=1)
    ready: StrictBool


class QueueSnapshot(_StrictModel):
    tts_queued: StrictInt = Field(ge=0)
    tts_running: StrictInt = Field(ge=0)
    comfy_pending_prompt_ids: list[str]
    comfy_running_prompt_ids: list[str]

    @field_validator("comfy_pending_prompt_ids", "comfy_running_prompt_ids")
    @classmethod
    def _unique_sorted_prompt_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("queue prompt ids must be unique")
        return sorted(value)


class HttpPreflightObservation(_StrictModel):
    resources: list[ReadyResource]
    queue: QueueSnapshot


class OwnedProcessIdentity(_StrictModel):
    pid: StrictInt = Field(gt=0)
    creation_time: datetime
    executable_name: str = Field(min_length=1)
    ownership_hash: SHA256

    @field_validator("creation_time")
    @classmethod
    def _utc_creation_time(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("process identity time must be timezone-aware UTC")
        return value

    @field_validator("executable_name")
    @classmethod
    def _neutral_executable_name(cls, value: str) -> str:
        if not _is_neutral_basename(value):
            raise ValueError("executable_name must be a neutral basename")
        return value


class BoundarySnapshot(_StrictModel):
    aggregate_hash: SHA256
    private_registry_hash: SHA256
    reference_hashes: dict[str, SHA256]
    repositories: list[RepositorySnapshot]

    @field_validator("reference_hashes")
    @classmethod
    def _sorted_reference_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: value[key] for key in sorted(value)}

    @field_validator("repositories")
    @classmethod
    def _sorted_repositories(cls, value: list[RepositorySnapshot]) -> list[RepositorySnapshot]:
        return sorted(value, key=lambda item: (item.label, item.head, item.branch, item.porcelain_hash))


class HostPreflightObservation(_StrictModel):
    port_owners: dict[StrictInt, OwnedProcessIdentity]
    boundary: BoundarySnapshot
    gpu_idle_baseline: GpuSnapshot


@dataclass(frozen=True)
class HttpCaseObservation:
    actual: Outcome
    job_id: str
    prompt_id: str | None
    version_id: str | None
    wav_path: Path | None
    comfyui: ComfyQueueEvidence | None
    prompt_submitted: bool = True
    tts_more: TtsTerminalEvidence | None = None
    termination: TerminationEvidence | None = None


class HostCaseObservation(_StrictModel):
    started_at: datetime
    finished_at: datetime
    cleanup: CleanupEvidence
    processes: list[ProcessEvidence]
    gpu_before: GpuSnapshot
    gpu_peak: GpuSnapshot
    gpu_after: GpuSnapshot

    @field_validator("started_at", "finished_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("host observation times must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _ordered_times(self) -> "HostCaseObservation":
        if self.finished_at < self.started_at:
            raise ValueError("host observation times are not ordered")
        return self


class FailureMarker(_BoundedPublicModel):
    code: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_FAILURE_CODE_LENGTH,
            pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        ),
    ]
    stage: Literal["preflight", "case", "finalize"]


class _PublicPreflightResource(_StrictModel):
    engine: Engine
    ready: StrictBool
    resource_id_hash: SHA256

    @field_validator("ready")
    @classmethod
    def _ready_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("public preflight resource must be ready")
        return value


class _PublicPreflightQueue(_StrictModel):
    tts_queued: StrictInt = Field(ge=0, le=0)
    tts_running: StrictInt = Field(ge=0, le=0)
    comfy_pending_prompt_ids: Annotated[list[str], Field(max_length=0)]
    comfy_running_prompt_ids: Annotated[list[str], Field(max_length=0)]


class _PublicPreflightProcess(_StrictModel):
    pid: StrictInt = Field(gt=0, le=MAX_PUBLIC_WINDOWS_PID)
    creation_time: Annotated[
        str,
        Field(
            min_length=20,
            max_length=27,
            pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$",
        ),
    ]
    executable_name: Annotated[
        str,
        Field(min_length=1, max_length=MAX_PUBLIC_EXECUTABLE_NAME_LENGTH),
    ]
    ownership_hash: SHA256

    @field_validator("creation_time")
    @classmethod
    def _utc_creation_time(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
        except ValueError:
            raise ValueError("process identity time must be valid UTC") from None
        if not _is_utc(parsed):
            raise ValueError("process identity time must be timezone-aware UTC")
        return value

    @field_validator("executable_name")
    @classmethod
    def _neutral_executable_name(cls, value: str) -> str:
        if not _is_neutral_basename(value):
            raise ValueError("executable_name must be a neutral basename")
        return value


class _PublicPreflightGpu(_StrictModel):
    used_mib: StrictInt = Field(ge=0, le=MAX_PUBLIC_GPU_MIB)
    free_mib: StrictInt = Field(ge=0, le=MAX_PUBLIC_GPU_MIB)


class _PublicPreflightRepository(_StrictModel):
    label: Annotated[str, Field(min_length=1, max_length=64)]
    head: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    branch: Annotated[str, Field(min_length=1, max_length=255)]
    porcelain_hash: SHA256


class _PublicPreflightBoundary(_StrictModel):
    aggregate_hash: SHA256
    private_registry_hash: SHA256
    reference_hashes: Annotated[
        dict[Annotated[str, Field(min_length=1, max_length=128)], SHA256],
        Field(min_length=1, max_length=64),
    ]
    repositories: Annotated[
        list[_PublicPreflightRepository],
        Field(min_length=len(REQUIRED_BOUNDARY_LABELS), max_length=len(REQUIRED_BOUNDARY_LABELS)),
    ]

    @field_validator("reference_hashes")
    @classmethod
    def _ordered_reference_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: value[key] for key in sorted(value)}

    @field_validator("repositories")
    @classmethod
    def _exact_ordered_repositories(
        cls,
        value: list[_PublicPreflightRepository],
    ) -> list[_PublicPreflightRepository]:
        labels = [item.label for item in value]
        if labels != sorted(REQUIRED_BOUNDARY_LABELS):
            raise ValueError("public preflight repository set is incomplete")
        return value


class _PublicPreflightMarker(_StrictModel):
    status: Literal["passed"]
    resources: Annotated[
        list[_PublicPreflightResource],
        Field(min_length=len(ENGINE_ORDER), max_length=len(ENGINE_ORDER)),
    ]
    queue: _PublicPreflightQueue
    port_owners: Annotated[dict[str, _PublicPreflightProcess], Field(max_length=16)]
    gpu_idle_baseline: _PublicPreflightGpu
    boundary: _PublicPreflightBoundary

    @field_validator("resources")
    @classmethod
    def _exact_ordered_resources(
        cls,
        value: list[_PublicPreflightResource],
    ) -> list[_PublicPreflightResource]:
        if [item.engine for item in value] != sorted(ENGINE_ORDER):
            raise ValueError("public preflight resource set is incomplete")
        return value

    @field_validator("port_owners")
    @classmethod
    def _bounded_port_owner_keys(
        cls,
        value: dict[str, _PublicPreflightProcess],
    ) -> dict[str, _PublicPreflightProcess]:
        if any(
            re.fullmatch(r"[1-9][0-9]{0,4}", port) is None
            or not 1 <= int(port) <= 65_535
            for port in value
        ):
            raise ValueError("public preflight port owner key is invalid")
        return {port: value[port] for port in sorted(value, key=int)}


class FailedCaseProcessObservation(_BoundedPublicModel):
    pid: StrictInt = Field(gt=0, le=MAX_PUBLIC_WINDOWS_PID)
    ownership: Literal["validator-owned", "pre-existing"]
    command_hash: SHA256
    creation_time: _PublicUtcTimestamp
    parent_pid: StrictInt = Field(gt=0, le=MAX_PUBLIC_WINDOWS_PID)
    parent_creation_time: _PublicUtcTimestamp
    stopped_at: _PublicUtcTimestamp
    executable_name: str = Field(
        min_length=1,
        max_length=MAX_PUBLIC_EXECUTABLE_NAME_LENGTH,
    )
    executable_hash: SHA256
    ownership_hash: SHA256
    started: StrictBool
    stopped: StrictBool
    descendants_stopped: StrictBool
    alive_after: StrictBool

    @field_validator("creation_time", "parent_creation_time", "stopped_at")
    @classmethod
    def _valid_utc_timestamp(cls, value: str) -> str:
        _parse_public_utc(value)
        return value

    @field_validator("executable_name")
    @classmethod
    def _neutral_executable_name(cls, value: str) -> str:
        if not _is_neutral_basename(value):
            raise ValueError("executable_name must be a neutral basename")
        return value

    @model_validator(mode="after")
    def _complete_lifecycle(self) -> "FailedCaseProcessObservation":
        parent_created = _parse_public_utc(self.parent_creation_time)
        created = _parse_public_utc(self.creation_time)
        stopped = _parse_public_utc(self.stopped_at)
        if parent_created > created or created > stopped:
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


class FailedCaseGpuObservation(_BoundedPublicModel):
    used_mib: StrictInt = Field(ge=0, le=MAX_PUBLIC_GPU_MIB)
    free_mib: StrictInt = Field(ge=0, le=MAX_PUBLIC_GPU_MIB)


class FailedCaseHostObservation(_BoundedPublicModel):
    started_at: _PublicUtcTimestamp
    finished_at: _PublicUtcTimestamp
    cleanup: CleanupEvidence
    processes: Annotated[
        list[FailedCaseProcessObservation],
        Field(max_length=MAX_FAILED_CASE_PROCESSES),
    ]
    gpu_before: FailedCaseGpuObservation
    gpu_peak: FailedCaseGpuObservation
    gpu_after: FailedCaseGpuObservation

    @field_validator("started_at", "finished_at")
    @classmethod
    def _valid_utc_timestamp(cls, value: str) -> str:
        _parse_public_utc(value)
        return value

    @field_validator("processes")
    @classmethod
    def _sorted_processes(
        cls,
        value: list[FailedCaseProcessObservation],
    ) -> list[FailedCaseProcessObservation]:
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

    @model_validator(mode="after")
    def _ordered_times(self) -> "FailedCaseHostObservation":
        started = _parse_public_utc(self.started_at)
        finished = _parse_public_utc(self.finished_at)
        if finished < started:
            raise ValueError("host observation times are not ordered")
        process_identities: set[tuple[int, str]] = set()
        for process in self.processes:
            identity = (process.pid, process.creation_time)
            if identity in process_identities:
                raise ValueError("process identities must be unique")
            process_identities.add(identity)
            if (
                _parse_public_utc(process.creation_time) < started
                or _parse_public_utc(process.stopped_at) > finished
            ):
                raise ValueError("process timestamps fall outside the host observation")
        return self


class FailedCaseQueueObservation(_BoundedPublicModel):
    observed_at: _PublicUtcTimestamp
    snapshot_sha256: SHA256
    running_count: StrictInt = Field(ge=0, le=MAX_FAILED_CASE_QUEUE_COUNT)
    pending_count: StrictInt = Field(ge=0, le=MAX_FAILED_CASE_QUEUE_COUNT)
    target_state: Literal["running", "pending", "absent"] | None

    @field_validator("observed_at")
    @classmethod
    def _valid_utc_timestamp(cls, value: str) -> str:
        _parse_public_utc(value)
        return value


class FailedCaseControlObservation(_BoundedPublicModel):
    interrupt_reason: Literal[
        "request-timeout",
        "user-cancel",
        "owned-comfyui-termination",
        "validator-release",
    ]
    interrupt_class: Literal[
        "job-cancel-request",
        "prompt-scoped-interrupt",
        "owned-service-termination",
    ]
    requested_at: _PublicUtcTimestamp | None
    converged_at: _PublicUtcTimestamp | None
    initial_state: Literal["running", "pending", "absent"] | None
    final_state: Literal["interrupted", "dequeued", "absent"] | None
    converged: StrictBool | None
    duration_seconds: StrictFloat | None = Field(default=None, ge=0.0, le=30.0)
    diagnostic_sha256: SHA256 | None

    @field_validator("requested_at", "converged_at")
    @classmethod
    def _valid_optional_utc_timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            _parse_public_utc(value)
        return value

    @model_validator(mode="after")
    def _ordered_control_times(self) -> "FailedCaseControlObservation":
        if (
            self.requested_at is not None
            and self.converged_at is not None
            and _parse_public_utc(self.converged_at) < _parse_public_utc(self.requested_at)
        ):
            raise ValueError("control timestamps are not ordered")
        if self.converged is True and (self.converged_at is None or self.final_state is None):
            raise ValueError("converged control evidence is incomplete")
        return self


class FailedCaseObservation(_BoundedPublicModel):
    detail_status: Literal["incremental", "minimal"]
    action: CaseAction
    request_sha256: SHA256 | None
    service_id_sha256: SHA256 | None
    resource_id_sha256: SHA256 | None
    job_created: StrictBool
    prompt_observed: StrictBool
    job_id_sha256: SHA256 | None
    prompt_id_sha256: SHA256 | None
    version_id_sha256: SHA256 | None
    job_created_at: _PublicUtcTimestamp | None
    first_prompt_at: _PublicUtcTimestamp | None
    last_poll_at: _PublicUtcTimestamp | None
    terminal_at: _PublicUtcTimestamp | None
    request_timeout_seconds: StrictFloat = Field(gt=0.0, le=MAX_NORMAL_REQUEST_TIMEOUT_SECONDS)
    convergence_seconds: StrictFloat = Field(gt=0.0, le=TERMINAL_CONVERGENCE_SECONDS)
    poll_count: StrictInt = Field(ge=0, le=MAX_FAILED_CASE_POLL_COUNT)
    terminal_observed: StrictBool
    last_job_status: ObservedStatus | None
    last_item_status: ObservedStatus | None
    last_external_status: ObservedStatus | None
    last_response_sha256: SHA256 | None
    last_control_code: Literal["cancelled", "timeout"] | None
    last_failure_stage: Literal["timeout", "cancellation_cleanup"] | None
    diagnostic_sha256: SHA256 | None
    queue: FailedCaseQueueObservation | None
    control: FailedCaseControlObservation | None
    wav_observed: StrictBool
    audio_sha256: SHA256 | None
    audio_size_bytes: StrictInt | None = Field(
        default=None,
        gt=0,
        le=MAX_FAILED_CASE_AUDIO_BYTES,
    )
    secondary_error_sha256: SHA256 | None

    @field_validator("job_created_at", "first_prompt_at", "last_poll_at", "terminal_at")
    @classmethod
    def _valid_optional_utc_timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            _parse_public_utc(value)
        return value

    @model_validator(mode="after")
    def _partial_observation_contract(self) -> "FailedCaseObservation":
        if self.job_created != (self.job_id_sha256 is not None and self.job_created_at is not None):
            raise ValueError("job creation observation is inconsistent")
        if self.prompt_observed != (
            self.prompt_id_sha256 is not None and self.first_prompt_at is not None
        ):
            raise ValueError("prompt observation is inconsistent")
        if self.prompt_observed and not self.job_created:
            raise ValueError("prompt observation precedes job creation")
        if self.poll_count == 0:
            if any(
                value is not None
                for value in (
                    self.last_poll_at,
                    self.last_job_status,
                    self.last_item_status,
                    self.last_external_status,
                    self.last_response_sha256,
                )
            ):
                raise ValueError("zero-poll observation contains poll detail")
        elif self.last_poll_at is None or self.last_response_sha256 is None:
            raise ValueError("poll observation is incomplete")
        if self.terminal_observed != (
            self.terminal_at is not None
            and self.last_job_status in {"completed", "cancelled", "failed"}
        ):
            raise ValueError("terminal observation is inconsistent")
        if self.wav_observed != (
            self.audio_sha256 is not None and self.audio_size_bytes is not None
        ):
            raise ValueError("audio observation is inconsistent")
        if (
            self.queue is not None
            and self.queue.target_state in {"running", "pending"}
            and not self.prompt_observed
        ):
            raise ValueError("queue target state lacks a prompt commitment")
        if self.job_created_at is not None:
            created = _parse_public_utc(self.job_created_at)
            if self.first_prompt_at is not None and _parse_public_utc(self.first_prompt_at) < created:
                raise ValueError("prompt observation precedes job creation")
            if self.last_poll_at is not None and _parse_public_utc(self.last_poll_at) < created:
                raise ValueError("poll observation precedes job creation")
            if self.terminal_at is not None and _parse_public_utc(self.terminal_at) < created:
                raise ValueError("terminal observation precedes job creation")
        if self.detail_status == "minimal" and any(
            (
                self.request_sha256,
                self.service_id_sha256,
                self.resource_id_sha256,
                self.job_created,
                self.prompt_observed,
                self.job_id_sha256,
                self.prompt_id_sha256,
                self.version_id_sha256,
                self.job_created_at,
                self.first_prompt_at,
                self.last_poll_at,
                self.terminal_at,
                self.poll_count,
                self.terminal_observed,
                self.last_job_status,
                self.last_item_status,
                self.last_external_status,
                self.last_response_sha256,
                self.last_control_code,
                self.last_failure_stage,
                self.diagnostic_sha256,
                self.queue,
                self.control,
                self.wav_observed,
                self.audio_sha256,
                self.audio_size_bytes,
            )
        ):
            raise ValueError("minimal observation contains detailed evidence")
        return self


class _FailedCaseEvidenceCore(_BoundedPublicModel):
    status: Literal["failed"]
    case_id: str = Field(min_length=1, max_length=MAX_FAILED_CASE_ID_LENGTH)
    phase: Phase
    engine: Engine
    expected: Outcome
    failure: FailureMarker
    host: FailedCaseHostObservation | None

    @field_validator("case_id")
    @classmethod
    def _neutral_case_id(cls, value: str) -> str:
        if "\\" in value or "/" in value or Path(value).is_absolute():
            raise ValueError("case_id must not contain paths")
        return value


class LegacyFailedCaseEvidence(_FailedCaseEvidenceCore):
    """Exact versionless schema retained only for historical failed artifacts."""


def _exact_current_failed_case_schema_version(value: Any) -> int:
    if type(value) is not int or value != 2:
        raise ValueError("current failed case schema version must be the exact integer 2")
    return value


CurrentFailedCaseSchemaVersion = Annotated[
    Literal[2],
    BeforeValidator(_exact_current_failed_case_schema_version),
]


class CurrentFailedCaseEvidence(_FailedCaseEvidenceCore):
    schema_version: CurrentFailedCaseSchemaVersion
    observation: FailedCaseObservation


FailedCaseDocument = LegacyFailedCaseEvidence | CurrentFailedCaseEvidence
_FAILED_CASE_SCHEMA_ADAPTER = TypeAdapter(FailedCaseDocument)
_FAILED_CASE_ERROR_LOC_FIELDS = frozenset(
    field_name
    for model in (
        CleanupEvidence,
        FailureMarker,
        FailedCaseGpuObservation,
        FailedCaseProcessObservation,
        FailedCaseHostObservation,
        FailedCaseQueueObservation,
        FailedCaseControlObservation,
        FailedCaseObservation,
        LegacyFailedCaseEvidence,
        CurrentFailedCaseEvidence,
    )
    for field_name in model.model_fields
)


def _safe_failed_case_error_loc(loc: tuple[Any, ...]) -> tuple[str | int, ...]:
    return tuple(
        segment
        if (
            isinstance(segment, str)
            and segment in _FAILED_CASE_ERROR_LOC_FIELDS
        )
        or (
            type(segment) is int
            and 0 <= segment <= MAX_FAILED_CASE_PROCESSES
        )
        else "<redacted>"
        for segment in loc
    )


def _safe_failed_case_error_context(context: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in context.items():
        if key == "error":
            safe[key] = "invalid failed case evidence"
        elif value is None or type(value) in {bool, int, float}:
            safe[key] = value
        elif isinstance(value, str) and len(value) <= 256:
            try:
                _assert_public_string(value)
            except ValueError:
                safe[key] = "<redacted>"
            else:
                safe[key] = value
        else:
            safe[key] = "<redacted>"
    return safe


def _failed_case_reader_error(
    error_type: str,
    *,
    loc: tuple[Any, ...] = (),
    context: dict[str, Any] | None = None,
) -> ValidationError:
    detail: dict[str, Any] = {
        "type": error_type,
        "loc": _safe_failed_case_error_loc(loc),
        "input": None,
    }
    if context:
        detail["ctx"] = _safe_failed_case_error_context(context)
    return ValidationError.from_exception_data(
        "FailedCaseEvidence",
        [detail],
        hide_input=True,
    )


def _redact_failed_case_validation_error(error: ValidationError) -> ValidationError:
    details: list[dict[str, Any]] = []
    for original in error.errors(include_url=False, include_input=False):
        detail: dict[str, Any] = {
            "type": original["type"],
            "loc": _safe_failed_case_error_loc(tuple(original["loc"])),
            "input": None,
        }
        context = original.get("ctx")
        if isinstance(context, dict) and context:
            detail["ctx"] = _safe_failed_case_error_context(context)
        details.append(detail)
    return ValidationError.from_exception_data(
        "FailedCaseEvidence",
        details,
        hide_input=True,
    )


def _parse_failed_case_json(value: str | bytes | bytearray) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_FAILED_CASE_JSON_BYTES:
            raise _failed_case_reader_error(
                "json_invalid",
                context={"error": "input exceeds the failed-case JSON byte limit"},
            )
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise _failed_case_reader_error(
                "json_invalid",
                context={"error": "input is not valid UTF-8"},
            ) from None
    elif isinstance(value, (bytes, bytearray)):
        if len(value) > MAX_FAILED_CASE_JSON_BYTES:
            raise _failed_case_reader_error(
                "json_invalid",
                context={"error": "input exceeds the failed-case JSON byte limit"},
            )
        encoded = bytes(value)
    else:
        raise _failed_case_reader_error("json_type")
    if len(encoded) > MAX_FAILED_CASE_JSON_BYTES:
        raise _failed_case_reader_error(
            "json_invalid",
            context={"error": "input exceeds the failed-case JSON byte limit"},
        )
    try:
        document = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise _failed_case_reader_error(
            "json_invalid",
            context={"error": "invalid failed-case JSON"},
        ) from None
    return document


class FailedCaseEvidence:
    """Formal version-aware reader for legacy and current failed-case JSON."""

    @classmethod
    def model_validate(cls, value: Any) -> FailedCaseDocument:
        try:
            if isinstance(value, CurrentFailedCaseEvidence):
                return CurrentFailedCaseEvidence.model_validate(value)
            if isinstance(value, LegacyFailedCaseEvidence):
                return LegacyFailedCaseEvidence.model_validate(value)
            if not isinstance(value, dict):
                raise _failed_case_reader_error(
                    "model_type",
                    context={"class_name": "FailedCaseEvidence"},
                )
            if "schema_version" in value:
                schema_version = value["schema_version"]
                if type(schema_version) is not int or schema_version != 2:
                    raise _failed_case_reader_error(
                        "literal_error",
                        loc=("schema_version",),
                        context={"expected": "2"},
                    )
                return CurrentFailedCaseEvidence.model_validate(value)
            return LegacyFailedCaseEvidence.model_validate(value)
        except ValidationError as error:
            if error.title == "FailedCaseEvidence" and all(
                detail.get("input") is None for detail in error.errors()
            ):
                raise
            raise _redact_failed_case_validation_error(error) from None

    @classmethod
    def model_validate_json(cls, value: str | bytes | bytearray) -> FailedCaseDocument:
        document = _parse_failed_case_json(value)
        return cls.model_validate(document)

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        return _FAILED_CASE_SCHEMA_ADAPTER.json_schema()


def _failed_case_host_observation(
    host: HostCaseObservation | None,
) -> FailedCaseHostObservation | None:
    if host is None:
        return None
    return FailedCaseHostObservation.model_validate(host.model_dump(mode="json"))


def _minimal_failed_case_observation(
    case: CasePlan,
    *,
    secondary_error: BaseException | None = None,
) -> FailedCaseObservation:
    return FailedCaseObservation(
        detail_status="minimal",
        action=case.action,
        request_sha256=None,
        service_id_sha256=None,
        resource_id_sha256=None,
        job_created=False,
        prompt_observed=False,
        job_id_sha256=None,
        prompt_id_sha256=None,
        version_id_sha256=None,
        job_created_at=None,
        first_prompt_at=None,
        last_poll_at=None,
        terminal_at=None,
        request_timeout_seconds=case.request_timeout_seconds,
        convergence_seconds=case.convergence_seconds,
        poll_count=0,
        terminal_observed=False,
        last_job_status=None,
        last_item_status=None,
        last_external_status=None,
        last_response_sha256=None,
        last_control_code=None,
        last_failure_stage=None,
        diagnostic_sha256=None,
        queue=None,
        control=None,
        wav_observed=False,
        audio_sha256=None,
        audio_size_bytes=None,
        secondary_error_sha256=(
            _sha256_text(f"{type(secondary_error).__name__}\0{secondary_error}")
            if secondary_error is not None
            else None
        ),
    )


class HttpFinalObservation(_StrictModel):
    queue: QueueSnapshot
    runtime_released: StrictBool


class HostFinalObservation(_StrictModel):
    boundary: BoundarySnapshot
    owned_processes_stopped: StrictBool
    temp_paths_removed: StrictBool
    gpu_after_release: GpuSnapshot


class ReliabilityHttpProbe(Protocol):
    def preflight(self, fixture: ReliabilityFixture) -> HttpPreflightObservation: ...

    def execute_case(
        self,
        case: CasePlan,
        fixture: ReliabilityFixture,
        output_directory: Path,
        *,
        action_hook: Callable[[], None] | None = None,
    ) -> HttpCaseObservation: ...

    def release(self) -> None: ...

    def final_state(self) -> HttpFinalObservation: ...


class ReliabilityHostProbe(Protocol):
    def preflight(self, fixture: ReliabilityFixture) -> HostPreflightObservation: ...

    def begin_case(self, case: CasePlan) -> datetime: ...

    def finish_case(self, case: CasePlan, started_at: datetime) -> HostCaseObservation: ...

    def terminate_comfyui(self) -> None: ...

    def restart_comfyui(self) -> None: ...

    def final_state(self) -> HostFinalObservation: ...


@dataclass(frozen=True)
class RecordedProcessIdentity:
    pid: int
    creation_time: datetime
    executable_path: Path
    command_line: str
    parent_pid: int
    parent_creation_time: datetime

    @classmethod
    def from_document(cls, document: Any) -> "RecordedProcessIdentity":
        if not isinstance(document, dict):
            raise ValueError("recorded process identity must be an object")
        if set(document) != {
            "pid",
            "creation_time",
            "executable_path",
            "command_line",
            "parent_pid",
            "parent_creation_time",
        }:
            raise ValueError("recorded process identity fields are invalid")
        pid = document["pid"]
        parent_pid = document["parent_pid"]
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(parent_pid, bool)
            or not isinstance(parent_pid, int)
            or parent_pid <= 0
            or parent_pid == pid
        ):
            raise ValueError("recorded process PIDs are invalid")
        creation_time = _parse_utc_datetime(document["creation_time"])
        parent_creation_time = _parse_utc_datetime(document["parent_creation_time"])
        executable_path = _absolute_private_path(document["executable_path"])
        command_line = document["command_line"]
        if not isinstance(command_line, str) or not command_line.strip():
            raise ValueError("recorded process command line is missing")
        if parent_creation_time > creation_time:
            raise ValueError("recorded process timestamps are not ordered")
        return cls(
            pid=pid,
            creation_time=creation_time,
            executable_path=executable_path,
            command_line=command_line,
            parent_pid=parent_pid,
            parent_creation_time=parent_creation_time,
        )

    def public_identity(self) -> OwnedProcessIdentity:
        return OwnedProcessIdentity(
            pid=self.pid,
            creation_time=self.creation_time,
            executable_name=self.executable_path.name,
            ownership_hash=_hash_private_process_identity(self),
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "creation_time": self.creation_time.isoformat(),
            "executable_path": str(self.executable_path),
            "command_line": self.command_line,
            "parent_pid": self.parent_pid,
            "parent_creation_time": self.parent_creation_time.isoformat(),
        }


@dataclass(frozen=True)
class PrivateLaunchSpecification:
    executable_path: Path
    arguments: tuple[str, ...]
    working_directory: Path
    port: int
    temp_root: Path


@dataclass(frozen=True)
class PrivateBoundarySpecification:
    repositories: dict[str, Path]
    private_registry: Path
    references: dict[str, Path]


@dataclass(frozen=True)
class PrivateRunnerSpecification:
    engine: Engine
    executable_path: Path
    entrypoint_path: Path
    temp_prefix: str
    request_roots: tuple[Path, ...]


@dataclass(frozen=True)
class PrivateRestartLaunchIntent:
    marker: str
    executable_path: Path
    arguments: tuple[str, ...]
    working_directory: Path
    child_temp_root: Path
    parent_pid: int
    parent_creation_time: datetime
    started_after: datetime

    def to_document(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "executable_path": str(self.executable_path),
            "arguments": list(self.arguments),
            "working_directory": str(self.working_directory),
            "child_temp_root": str(self.child_temp_root),
            "parent_pid": self.parent_pid,
            "parent_creation_time": self.parent_creation_time.isoformat(),
            "started_after": self.started_after.isoformat(),
        }


@dataclass(frozen=True)
class PrivateRestartProvisionalProcess:
    pid: int
    executable_path: Path
    parent_pid: int
    parent_creation_time: datetime
    started_after: datetime
    started_before: datetime

    def to_document(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "executable_path": str(self.executable_path),
            "parent_pid": self.parent_pid,
            "parent_creation_time": self.parent_creation_time.isoformat(),
            "started_after": self.started_after.isoformat(),
            "started_before": self.started_before.isoformat(),
        }


@dataclass(frozen=True)
class PrivateRestartLifecycle:
    persist_launch_intent: Callable[[PrivateRestartLaunchIntent], None]
    persist_provisional: Callable[[PrivateRestartProvisionalProcess], None]
    promote: Callable[[RecordedProcessIdentity], None]


@dataclass(frozen=True)
class PrivateHostManifest:
    run_id: str
    owned_processes: dict[str, RecordedProcessIdentity]
    launch_roots: dict[str, RecordedProcessIdentity]
    launch: dict[str, PrivateLaunchSpecification]
    boundary: PrivateBoundarySpecification
    temp_roots: tuple[Path, ...]

    @classmethod
    def read(cls, path: Path) -> "PrivateHostManifest":
        document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("host manifest version is invalid")
        run_id = document.get("run_id")
        if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise ValueError("host manifest run id is invalid")
        owned_raw = document.get("owned_processes")
        if not isinstance(owned_raw, dict) or set(owned_raw) != {"tts-more", "comfyui"}:
            raise ValueError("host manifest owned process set is invalid")
        owned = {
            label: RecordedProcessIdentity.from_document(value)
            for label, value in owned_raw.items()
        }
        launch_roots_raw = document.get("launch_roots")
        if not isinstance(launch_roots_raw, dict) or set(launch_roots_raw) != {
            "tts-more",
            "comfyui",
        }:
            raise ValueError("host manifest launch root set is invalid")
        launch_roots = {
            label: RecordedProcessIdentity.from_document(value)
            for label, value in launch_roots_raw.items()
        }
        launch_raw = document.get("launch")
        if not isinstance(launch_raw, dict) or set(launch_raw) != {"comfyui"}:
            raise ValueError("host manifest launch set is invalid")
        launch: dict[str, PrivateLaunchSpecification] = {}
        for label, value in launch_raw.items():
            if not isinstance(value, dict) or set(value) != {
                "executable_path",
                "arguments",
                "working_directory",
                "port",
                "temp_root",
            }:
                raise ValueError("host manifest launch fields are invalid")
            arguments = value["arguments"]
            port = value["port"]
            if (
                not isinstance(arguments, list)
                or not arguments
                or any(not isinstance(item, str) or not item for item in arguments)
                or isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
            ):
                raise ValueError("host manifest launch values are invalid")
            launch[label] = PrivateLaunchSpecification(
                executable_path=_absolute_private_path(value["executable_path"]),
                arguments=tuple(arguments),
                working_directory=_absolute_private_path(value["working_directory"]),
                port=port,
                temp_root=_absolute_private_path(value["temp_root"]),
            )
        boundary_raw = document.get("boundary")
        if not isinstance(boundary_raw, dict) or set(boundary_raw) != {
            "repositories",
            "private_registry",
            "references",
        }:
            raise ValueError("host manifest boundary fields are invalid")
        repositories_raw = boundary_raw["repositories"]
        references_raw = boundary_raw["references"]
        if (
            not isinstance(repositories_raw, dict)
            or set(repositories_raw) != set(REQUIRED_BOUNDARY_LABELS)
            or not isinstance(references_raw, dict)
            or not references_raw
        ):
            raise ValueError("host manifest boundary set is invalid")
        boundary = PrivateBoundarySpecification(
            repositories={
                label: _absolute_private_path(value)
                for label, value in repositories_raw.items()
            },
            private_registry=_absolute_private_path(boundary_raw["private_registry"]),
            references={
                label: _absolute_private_path(value)
                for label, value in references_raw.items()
            },
        )
        temp_roots_raw = document.get("temp_roots")
        if not isinstance(temp_roots_raw, list) or not temp_roots_raw:
            raise ValueError("host manifest temp roots are invalid")
        temp_roots = tuple(_absolute_private_path(value) for value in temp_roots_raw)
        return cls(
            run_id=run_id,
            owned_processes=owned,
            launch_roots=launch_roots,
            launch=launch,
            boundary=boundary,
            temp_roots=temp_roots,
        )


class WindowsHostSystem(Protocol):
    def inspect_process(self, pid: int) -> RecordedProcessIdentity: ...

    def port_owner(self, port: int) -> RecordedProcessIdentity | None: ...

    def capture_boundary(self, specification: PrivateBoundarySpecification) -> BoundarySnapshot: ...

    def gpu_snapshot(self) -> GpuSnapshot: ...

    def matching_runners(
        self,
        specifications: tuple[PrivateRunnerSpecification, ...],
    ) -> tuple[int, ...]: ...

    def begin_case(
        self,
        case: CasePlan,
        roots: tuple[RecordedProcessIdentity, ...],
        temp_roots: tuple[Path, ...],
    ) -> object: ...

    def finish_case(self, token: object, convergence_seconds: float) -> HostCaseObservation: ...

    def stop_owned(self, identity: RecordedProcessIdentity) -> None: ...

    def restart_owned(
        self,
        identity: RecordedProcessIdentity,
        launch: PrivateLaunchSpecification,
        convergence_seconds: float,
        *,
        run_id: str,
        lifecycle: PrivateRestartLifecycle,
    ) -> RecordedProcessIdentity: ...

    def final_cleanup_state(self, temp_roots: tuple[Path, ...]) -> tuple[bool, bool]: ...


class LiveValidationError(RuntimeError):
    def __init__(self, code: str, *, stage: Literal["preflight", "case", "finalize"]):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.failure_persistence_attempted = False
        self.preserve_terminal_marker = False


class RestartLifecycleError(RuntimeError):
    def __init__(self, message: str, *, cleanup_proven: bool):
        super().__init__(message)
        self.cleanup_proven = cleanup_proven


class WindowsReliabilityHostProbe:
    def __init__(
        self,
        manifest: PrivateHostManifest,
        *,
        system: WindowsHostSystem,
        manifest_path: Path,
    ) -> None:
        self.manifest = manifest
        self.system = system
        self.manifest_path = Path(manifest_path).resolve()
        self.validation_root = self.manifest_path.parent
        self.control_state_path = Path(f"{self.manifest_path}.current.json")
        self._current: dict[str, RecordedProcessIdentity | None] = dict(manifest.owned_processes)
        self._launch_roots: dict[str, RecordedProcessIdentity] = dict(manifest.launch_roots)
        self._active_cases: dict[str, object] = {}
        self._gpu_idle_baseline: GpuSnapshot | None = None
        self._runner_specifications: tuple[PrivateRunnerSpecification, ...] = ()
        self._restart_intent: PrivateRestartLaunchIntent | None = None
        self._restart_provisional: PrivateRestartProvisionalProcess | None = None
        self._persist_control_state()

    @classmethod
    def from_manifest(
        cls,
        path: Path,
        *,
        system: WindowsHostSystem | None = None,
    ) -> "WindowsReliabilityHostProbe":
        manifest = PrivateHostManifest.read(path)
        return cls(
            manifest,
            system=system or NativeWindowsHostSystem(),
            manifest_path=path,
        )

    @property
    def owned_processes(self) -> dict[str, OwnedProcessIdentity]:
        return {
            label: identity.public_identity()
            for label, identity in self._current.items()
            if identity is not None
        }

    def preflight(self, fixture: ReliabilityFixture) -> HostPreflightObservation:
        fixture = _revalidate_model(fixture, ReliabilityFixture)
        self._runner_specifications = _build_private_runner_specifications(
            self.manifest,
            fixture,
            validation_root=self.validation_root,
        )
        if self.system.matching_runners(self._runner_specifications):
            raise LiveValidationError("pre-existing-external-runner", stage="preflight")
        port_owners: dict[int, OwnedProcessIdentity] = {}
        for label, recorded in self.manifest.owned_processes.items():
            current = self._inspect_exact(recorded)
            self._current[label] = current
        labels_by_url = {"tts_more": "tts-more", "comfyui": "comfyui"}
        for url_label, process_label in labels_by_url.items():
            port = urlsplit(fixture.base_urls[url_label]).port
            if port is None:
                raise LiveValidationError("endpoint-port-missing", stage="preflight")
            owner = self.system.port_owner(port)
            expected = self._current[process_label]
            if owner is None or expected is None or owner != expected:
                raise LiveValidationError("port-owner-mismatch", stage="preflight")
            port_owners[port] = owner.public_identity()
        boundary = self.system.capture_boundary(self.manifest.boundary)
        _validate_boundary_snapshot(boundary)
        self._gpu_idle_baseline = self.system.gpu_snapshot()
        return HostPreflightObservation(
            port_owners=port_owners,
            boundary=boundary,
            gpu_idle_baseline=self._gpu_idle_baseline,
        )

    def begin_case(self, case: CasePlan) -> datetime:
        if case.case_id in self._active_cases:
            raise LiveValidationError("duplicate-active-case", stage="case")
        roots = tuple(identity for identity in self._current.values() if identity is not None)
        token = self.system.begin_case(case, roots, self.manifest.temp_roots)
        self._active_cases[case.case_id] = token
        if isinstance(token, datetime) and _is_utc(token):
            return token
        started_at = getattr(token, "started_at", None)
        if not _is_utc(started_at):
            raise LiveValidationError("host-monitor-start-failed", stage="case")
        return started_at

    def finish_case(self, case: CasePlan, started_at: datetime) -> HostCaseObservation:
        del started_at
        token = self._active_cases.pop(case.case_id, None)
        if token is None:
            raise LiveValidationError("host-monitor-token-missing", stage="case")
        try:
            return _revalidate_model(
                self.system.finish_case(token, case.convergence_seconds),
                HostCaseObservation,
            )
        except (ValueError, TypeError, AttributeError):
            raise LiveValidationError("host-monitor-observation-invalid", stage="case") from None

    def terminate_comfyui(self) -> None:
        current = self._require_current("comfyui")
        self._inspect_exact(current)
        self.system.stop_owned(current)
        self._current["comfyui"] = None
        self._persist_control_state()

    def restart_comfyui(self) -> None:
        current = self._current.get("comfyui")
        launch = self.manifest.launch["comfyui"]
        if current is not None:
            self._inspect_exact(current)
            self.system.stop_owned(current)
            self._current["comfyui"] = None
            self._persist_control_state()
        provenance = current or self.manifest.owned_processes["comfyui"]
        lifecycle = PrivateRestartLifecycle(
            persist_launch_intent=self._persist_restart_launch_intent,
            persist_provisional=self._persist_restart_provisional,
            promote=self._promote_restart_identity,
        )
        try:
            replacement = self.system.restart_owned(
                provenance,
                launch,
                180.0,
                run_id=self.manifest.run_id,
                lifecycle=lifecycle,
            )
            if self._current.get("comfyui") != replacement:
                raise LiveValidationError("restart-promotion-missing", stage="case")
            self._inspect_exact(replacement)
            port_owner = self.system.port_owner(launch.port)
            if port_owner != replacement:
                raise LiveValidationError("restart-port-owner-mismatch", stage="case")
        except RestartLifecycleError as exc:
            if exc.cleanup_proven:
                self._clear_restart_state()
                try:
                    self._persist_control_state()
                except Exception:
                    pass
                raise
            try:
                self._persist_control_state()
            except Exception:
                pass
            raise LiveValidationError("restart-cleanup-failed", stage="case") from None
        except Exception:
            cleanup_failed = False
            replacement = self._current.get("comfyui")
            if replacement is not None:
                try:
                    self.system.stop_owned(replacement)
                except Exception:
                    cleanup_failed = True
            elif self._restart_intent is not None:
                cleanup_failed = True
            if not cleanup_failed:
                self._clear_restart_state()
            try:
                self._persist_control_state()
            except Exception:
                pass
            if cleanup_failed:
                raise LiveValidationError("restart-cleanup-failed", stage="case") from None
            raise

    def _persist_restart_launch_intent(self, intent: PrivateRestartLaunchIntent) -> None:
        launch = self.manifest.launch["comfyui"]
        if (
            self._current.get("comfyui") is not None
            or self._restart_intent is not None
            or self._restart_provisional is not None
            or re.fullmatch(
                rf"tts_more_reliability_run={self.manifest.run_id}-comfyui-restart-[0-9a-f]{{32}}",
                intent.marker,
            )
            is None
            or sum(
                intent.arguments[index : index + 2] == ("-X", intent.marker)
                for index in range(max(0, len(intent.arguments) - 1))
            )
            != 1
            or intent.parent_pid <= 0
            or not _same_private_path(intent.executable_path, launch.executable_path)
            or not _same_private_path(intent.working_directory, launch.working_directory)
            or not _same_private_path(intent.child_temp_root, launch.temp_root)
            or not _is_utc(intent.parent_creation_time)
            or not _is_utc(intent.started_after)
        ):
            raise ValueError("restart launch intent is invalid")
        self._restart_intent = intent
        self._persist_control_state()

    def _persist_restart_provisional(self, provisional: PrivateRestartProvisionalProcess) -> None:
        intent = self._restart_intent
        if (
            intent is None
            or self._restart_provisional is not None
            or provisional.pid <= 0
            or provisional.parent_pid != intent.parent_pid
            or provisional.parent_creation_time != intent.parent_creation_time
            or not _same_private_path(provisional.executable_path, intent.executable_path)
            or not _is_utc(provisional.started_after)
            or not _is_utc(provisional.started_before)
            or provisional.started_after != intent.started_after
            or provisional.started_before < provisional.started_after
        ):
            raise ValueError("restart provisional identity is invalid")
        self._restart_provisional = provisional
        self._persist_control_state()

    def _promote_restart_identity(self, replacement: RecordedProcessIdentity) -> None:
        intent = self._restart_intent
        provisional = self._restart_provisional
        if (
            intent is None
            or provisional is None
            or not _restart_identity_promotes_pending(replacement, intent, provisional)
        ):
            raise ValueError("restart full identity does not promote pending recovery state")
        self._current["comfyui"] = replacement
        self._launch_roots["comfyui"] = replacement
        self._restart_intent = None
        self._restart_provisional = None
        self._persist_control_state()

    def _clear_restart_state(self) -> None:
        self._current["comfyui"] = None
        self._restart_intent = None
        self._restart_provisional = None

    def final_state(self) -> HostFinalObservation:
        if not self._runner_specifications:
            raise LiveValidationError("runner-fingerprint-missing", stage="finalize")
        if self.system.matching_runners(self._runner_specifications):
            raise LiveValidationError("final-external-runner-present", stage="finalize")
        boundary = self.system.capture_boundary(self.manifest.boundary)
        runners_stopped, temp_removed = self.system.final_cleanup_state(self.manifest.temp_roots)
        if self._gpu_idle_baseline is None:
            raise LiveValidationError("gpu-idle-baseline-missing", stage="finalize")
        deadline = time.monotonic() + 30.0
        while True:
            gpu_after_release = self.system.gpu_snapshot()
            if _gpu_recovered_to_idle_baseline(self._gpu_idle_baseline, gpu_after_release):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        return HostFinalObservation(
            boundary=boundary,
            owned_processes_stopped=runners_stopped,
            temp_paths_removed=temp_removed,
            gpu_after_release=gpu_after_release,
        )

    def _require_current(self, label: str) -> RecordedProcessIdentity:
        current = self._current.get(label)
        if current is None:
            raise LiveValidationError("owned-process-not-running", stage="case")
        return current

    def _inspect_exact(self, recorded: RecordedProcessIdentity) -> RecordedProcessIdentity:
        try:
            current = self.system.inspect_process(recorded.pid)
        except (OSError, RuntimeError, ValueError):
            raise LiveValidationError("process-identity-mismatch", stage="preflight") from None
        if current != recorded:
            raise LiveValidationError("process-identity-mismatch", stage="preflight")
        return current

    def _persist_control_state(self) -> None:
        _write_private_json_atomic(
            self.control_state_path,
            {
                "version": 2,
                "run_id": self.manifest.run_id,
                "owned_processes": {
                    label: identity.to_document() if identity is not None else None
                    for label, identity in self._current.items()
                },
                "launch_roots": {
                    label: identity.to_document()
                    for label, identity in self._launch_roots.items()
                },
                "provisional_processes": {
                    "tts-more": None,
                    "comfyui": (
                        self._restart_provisional.to_document()
                        if self._restart_provisional is not None
                        else None
                    ),
                },
                "launch_intents": {
                    "tts-more": None,
                    "comfyui": (
                        self._restart_intent.to_document()
                        if self._restart_intent is not None
                        else None
                    ),
                },
            },
        )


def _parse_utc_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp is invalid") from None
    if not _is_utc(parsed):
        raise ValueError("timestamp must be UTC")
    return parsed


def _absolute_private_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("private path is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("private path must be absolute")
    return path.resolve()


def _build_private_runner_specifications(
    manifest: PrivateHostManifest,
    fixture: ReliabilityFixture,
    *,
    validation_root: Path,
) -> tuple[PrivateRunnerSpecification, ...]:
    try:
        registry = yaml.safe_load(manifest.boundary.private_registry.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError("private runner registry is unreadable") from None
    resources = registry.get("resources") if isinstance(registry, dict) else None
    if registry.get("version") != 1 or not isinstance(resources, dict):
        raise ValueError("private runner registry is invalid")

    request_roots = _verified_validation_runner_roots(
        manifest,
        validation_root=validation_root,
    )
    suite_root = manifest.boundary.repositories["tts-audio-suite"]
    definitions: tuple[tuple[Engine, str, str, str], ...] = (
        ("gpt-sovits", "gpt_sovits", "gpt_sovits", "tts-audio-suite-gptsovits-"),
        ("indextts", "index_tts", "index_tts", "tts-audio-suite-indextts-"),
        ("cosyvoice", "cosyvoice", "cosyvoice", "tts-audio-suite-cosyvoice-"),
    )
    specifications: list[PrivateRunnerSpecification] = []
    for engine, registry_engine, suite_engine, temp_prefix in definitions:
        resource_id = fixture.resources[engine].resource_id
        resource = resources.get(resource_id)
        if not isinstance(resource, dict) or resource.get("engine") != registry_engine:
            raise ValueError("private runner resource is missing or mismatched")
        source_root = _absolute_private_path(resource.get("source_root"))
        if not _same_private_path(source_root, manifest.boundary.repositories[engine]):
            raise ValueError("private runner source root mismatches the boundary")
        configured_python = resource.get("python_executable") if engine == "gpt-sovits" else None
        executable_path = (
            _absolute_private_path(configured_python)
            if configured_python is not None
            else (source_root / ".venv" / "Scripts" / "python.exe").resolve()
        )
        entrypoint_path = (
            suite_root / "engines" / suite_engine / "external_subprocess_runner.py"
        ).resolve()
        if not executable_path.is_file() or not entrypoint_path.is_file():
            raise ValueError("private runner fingerprint files are missing")
        specifications.append(
            PrivateRunnerSpecification(
                engine=engine,
                executable_path=executable_path,
                entrypoint_path=entrypoint_path,
                temp_prefix=temp_prefix,
                request_roots=request_roots,
            )
        )
    return tuple(specifications)


def _verified_validation_runner_roots(
    manifest: PrivateHostManifest,
    *,
    validation_root: Path,
) -> tuple[Path, ...]:
    validation_root = validation_root.resolve()
    current_temp_root = (validation_root / f"reliability-temp-{manifest.run_id}").resolve()
    current_runner_root = (current_temp_root / "runner").resolve()
    current_comfy_root = (current_temp_root / "comfyui" / "temp").resolve()
    if {
        path.resolve() for path in manifest.temp_roots
    } != {current_runner_root, current_comfy_root}:
        raise ValueError("current validation temp roots are outside the owned boundary")

    roots: set[Path] = set()
    marker_pattern = re.compile(r"^\.request-temp-([0-9a-f]{32})\.owner\.json$")
    for marker_path in sorted(validation_root.glob(".request-temp-*.owner.json")):
        match = marker_pattern.fullmatch(marker_path.name)
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("validation temp owner marker is invalid") from None
        if (
            match is None
            or not isinstance(marker, dict)
            or set(marker) != {"run_id", "temp_root", "runner_temp_root", "comfy_temp_root"}
            or marker.get("run_id") != match.group(1)
        ):
            raise ValueError("validation temp owner marker is invalid")
        run_temp_root = (validation_root / f"reliability-temp-{match.group(1)}").resolve()
        runner_root = (run_temp_root / "runner").resolve()
        comfy_root = (run_temp_root / "comfyui" / "temp").resolve()
        try:
            marker_paths_match = (
                _same_private_path(_absolute_private_path(marker.get("temp_root")), run_temp_root)
                and _same_private_path(_absolute_private_path(marker.get("runner_temp_root")), runner_root)
                and _same_private_path(_absolute_private_path(marker.get("comfy_temp_root")), comfy_root)
            )
        except ValueError:
            marker_paths_match = False
        if not marker_paths_match or not runner_root.is_dir():
            raise ValueError("validation temp owner marker is invalid")
        roots.add(runner_root)
    if current_runner_root not in roots:
        raise ValueError("current validation temp owner marker is missing")
    return tuple(sorted(roots, key=lambda path: os.path.normcase(str(path))))


def _same_private_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(os.path.realpath(str(right)))


def _process_matches_runner_specification(
    identity: RecordedProcessIdentity,
    specification: PrivateRunnerSpecification,
) -> bool:
    return _runner_command_matches_specification(
        identity.executable_path,
        identity.command_line,
        specification,
    )


def _restart_identity_promotes_pending(
    replacement: RecordedProcessIdentity,
    intent: PrivateRestartLaunchIntent,
    provisional: PrivateRestartProvisionalProcess,
) -> bool:
    try:
        argv = _windows_command_line_argv(replacement.command_line)
        return (
            replacement.pid == provisional.pid
            and replacement.parent_pid == provisional.parent_pid == intent.parent_pid
            and replacement.parent_creation_time
            == provisional.parent_creation_time
            == intent.parent_creation_time
            and _same_private_path(replacement.executable_path, provisional.executable_path)
            and _same_private_path(replacement.executable_path, intent.executable_path)
            and provisional.started_after
            <= replacement.creation_time
            <= provisional.started_before
            and len(argv) == len(intent.arguments) + 1
            and _same_private_path(_absolute_private_path(argv[0]), intent.executable_path)
            and tuple(argv[1:]) == intent.arguments
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _runner_command_matches_specification(
    executable_path: Path,
    command_line: str,
    specification: PrivateRunnerSpecification,
) -> bool:
    if not _same_private_path(executable_path, specification.executable_path):
        return False
    try:
        argv = _windows_command_line_argv(command_line)
    except ValueError:
        return False
    if len(argv) != 3:
        return False
    try:
        executable_arg = _absolute_private_path(argv[0])
        entrypoint_arg = _absolute_private_path(argv[1])
        request_arg = _absolute_private_path(argv[2])
    except ValueError:
        return False
    if (
        not _same_private_path(executable_arg, specification.executable_path)
        or not _same_private_path(entrypoint_arg, specification.entrypoint_path)
        or request_arg.name != "request.json"
        or not request_arg.parent.name.startswith(specification.temp_prefix)
        or len(request_arg.parent.name) <= len(specification.temp_prefix)
    ):
        return False
    return any(
        _same_private_path(request_arg.parent.parent, request_root)
        for request_root in specification.request_roots
    )


def _matching_runner_pids(
    document: Any,
    specifications: tuple[PrivateRunnerSpecification, ...],
) -> tuple[int, ...]:
    if isinstance(document, dict):
        items: list[Any] = [document]
    elif isinstance(document, list):
        items = document
    elif document is None:
        items = []
    else:
        raise RuntimeError("runner inventory document is invalid")

    matches: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("runner inventory item is invalid")
        executable_raw = item.get("executable_path")
        command_raw = item.get("command_line")
        name_raw = item.get("name")
        name_candidates = tuple(
            specification
            for specification in specifications
            if isinstance(name_raw, str)
            and name_raw.casefold() == specification.executable_path.name.casefold()
        )
        executable_path: Path | None = None
        if isinstance(executable_raw, str) and executable_raw.strip():
            try:
                executable_path = _absolute_private_path(executable_raw)
            except ValueError:
                raise RuntimeError("runner inventory executable is invalid") from None

        executable_candidates = tuple(
            specification
            for specification in specifications
            if executable_path is not None
            and _same_private_path(executable_path, specification.executable_path)
        )
        argv: tuple[str, ...] | None = None
        argv_executable: Path | None = None
        if isinstance(command_raw, str) and command_raw.strip():
            try:
                argv = _windows_command_line_argv(command_raw)
                argv_executable = _absolute_private_path(argv[0])
            except ValueError:
                if executable_candidates or (name_candidates and executable_path is None):
                    raise RuntimeError("runner inventory command line is invalid") from None
        command_candidates = tuple(
            specification
            for specification in specifications
            if argv_executable is not None
            and _same_private_path(argv_executable, specification.executable_path)
        )
        if not executable_candidates and not command_candidates:
            if name_candidates and executable_path is None and argv_executable is None:
                raise RuntimeError("runner inventory candidate identity is incomplete")
            continue
        if (
            executable_path is None
            or not isinstance(command_raw, str)
            or not command_raw.strip()
            or argv is None
            or not executable_candidates
            or not command_candidates
        ):
            raise RuntimeError("runner inventory candidate identity is incomplete")
        pid = item.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("runner inventory candidate PID is invalid")
        try:
            _parse_utc_datetime(item.get("creation_time"))
        except ValueError:
            raise RuntimeError("runner inventory candidate timestamp is invalid") from None
        if any(
            _runner_command_matches_specification(executable_path, command_raw, specification)
            for specification in specifications
        ):
            matches.add(pid)
    return tuple(sorted(matches))


def _windows_command_line_argv(command_line: str) -> tuple[str, ...]:
    if not isinstance(command_line, str) or not command_line.strip():
        raise ValueError("process command line is missing")
    arguments: list[str] = []
    index = 0
    length = len(command_line)
    while index < length:
        while index < length and command_line[index] in " \t":
            index += 1
        if index >= length:
            break
        output: list[str] = []
        in_quotes = False
        while index < length and (in_quotes or command_line[index] not in " \t"):
            if command_line[index] == "\\":
                slash_start = index
                while index < length and command_line[index] == "\\":
                    index += 1
                slash_count = index - slash_start
                if index < length and command_line[index] == '"':
                    output.extend("\\" * (slash_count // 2))
                    if slash_count % 2:
                        output.append('"')
                        index += 1
                    else:
                        in_quotes = not in_quotes
                        index += 1
                else:
                    output.extend("\\" * slash_count)
                continue
            if command_line[index] == '"':
                if in_quotes and index + 1 < length and command_line[index + 1] == '"':
                    output.append('"')
                    index += 2
                else:
                    in_quotes = not in_quotes
                    index += 1
                continue
            output.append(command_line[index])
            index += 1
        if in_quotes:
            raise ValueError("process command line quoting is invalid")
        arguments.append("".join(output))
    if not arguments:
        raise ValueError("process command line is missing")
    return tuple(arguments)


def _hash_private_process_identity(identity: RecordedProcessIdentity) -> str:
    document = {
        "pid": identity.pid,
        "creation_time": identity.creation_time.isoformat(),
        "executable_path": str(identity.executable_path),
        "command_line": identity.command_line,
        "parent_pid": identity.parent_pid,
        "parent_creation_time": identity.parent_creation_time.isoformat(),
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _NativeCaseToken:
    def __init__(
        self,
        *,
        started_at: datetime,
        roots: tuple[RecordedProcessIdentity, ...],
        baseline_pids: set[int],
        temp_roots: tuple[Path, ...],
        temp_before: set[str],
        gpu_before: GpuSnapshot,
    ) -> None:
        self.started_at = started_at
        self.roots = list(roots)
        self.baseline_pids = baseline_pids
        self.temp_roots = temp_roots
        self.temp_before = temp_before
        self.gpu_before = gpu_before
        self.gpu_peak = gpu_before
        self.observed: dict[tuple[int, datetime], RecordedProcessIdentity] = {}
        self.stopped_at: dict[tuple[int, datetime], datetime] = {}
        self.error: Exception | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None


class NativeWindowsHostSystem:
    """Windows/CIM implementation used only by the explicit live command."""

    def __init__(self, *, sample_interval_seconds: float = 0.1) -> None:
        if os.name != "nt":
            raise RuntimeError("native Windows host probe requires Windows")
        self.sample_interval_seconds = sample_interval_seconds
        self._started_identities: dict[int, RecordedProcessIdentity] = {}
        self._active_tokens: list[_NativeCaseToken] = []

    def inspect_process(self, pid: int) -> RecordedProcessIdentity:
        snapshot = self._process_snapshot()
        identity = snapshot.get(pid)
        if identity is None:
            raise RuntimeError("process is absent")
        cached = self._started_identities.get(pid)
        if cached is not None:
            if (
                identity.pid != cached.pid
                or identity.creation_time != cached.creation_time
                or identity.executable_path != cached.executable_path
                or identity.command_line != cached.command_line
                or identity.parent_pid != cached.parent_pid
            ):
                raise RuntimeError("started process identity changed")
            return cached
        return identity

    def port_owner(self, port: int) -> RecordedProcessIdentity | None:
        document = self._powershell_document(
            "$owners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$env:TTS_MORE_VALIDATION_PORT) "
            "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); "
            "if ($owners.Count -eq 0) { $null | ConvertTo-Json -Compress } "
            "elseif ($owners.Count -eq 1) { $owners[0] | ConvertTo-Json -Compress } "
            "else { throw 'multiple port owners' }",
            {"TTS_MORE_VALIDATION_PORT": str(port)},
        )
        if document is None:
            return None
        if isinstance(document, bool) or not isinstance(document, int):
            raise RuntimeError("port owner observation is invalid")
        return self.inspect_process(document)

    def capture_boundary(self, specification: PrivateBoundarySpecification) -> BoundarySnapshot:
        repositories: list[RepositorySnapshot] = []
        for label, root in specification.repositories.items():
            head = self._run_text(["git", "-C", str(root), "rev-parse", "HEAD"]).strip()
            branch = self._run_text(
                ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
                allowed_returncodes={0, 1},
            ).strip() or "DETACHED"
            porcelain = self._run_bytes(
                ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )
            repositories.append(
                RepositorySnapshot(
                    label=label,
                    head=head,
                    branch=branch,
                    porcelain_hash=hashlib.sha256(porcelain).hexdigest(),
                )
            )
        private_registry_hash = _sha256_file(specification.private_registry)
        reference_hashes = {
            label: _sha256_file(path)
            for label, path in specification.references.items()
        }
        aggregate_document = {
            "repositories": [item.model_dump(mode="json") for item in sorted(repositories, key=lambda item: item.label)],
            "private_registry_hash": private_registry_hash,
            "reference_hashes": reference_hashes,
        }
        aggregate_hash = hashlib.sha256(
            json.dumps(aggregate_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return BoundarySnapshot(
            aggregate_hash=aggregate_hash,
            private_registry_hash=private_registry_hash,
            reference_hashes=reference_hashes,
            repositories=repositories,
        )

    def gpu_snapshot(self) -> GpuSnapshot:
        return self._gpu_snapshot()

    def matching_runners(
        self,
        specifications: tuple[PrivateRunnerSpecification, ...],
    ) -> tuple[int, ...]:
        return _matching_runner_pids(
            self._process_inventory_document(),
            specifications,
        )

    def begin_case(
        self,
        case: CasePlan,
        roots: tuple[RecordedProcessIdentity, ...],
        temp_roots: tuple[Path, ...],
    ) -> object:
        del case
        baseline = self._process_snapshot()
        token = _NativeCaseToken(
            started_at=datetime.now(timezone.utc),
            roots=roots,
            baseline_pids=set(baseline),
            temp_roots=temp_roots,
            temp_before=_temp_entries(temp_roots),
            gpu_before=self._gpu_snapshot(),
        )
        token.thread = threading.Thread(target=self._sample_until_stopped, args=(token,), daemon=True)
        self._active_tokens.append(token)
        token.thread.start()
        return token

    def finish_case(self, token: object, convergence_seconds: float) -> HostCaseObservation:
        if not isinstance(token, _NativeCaseToken):
            raise RuntimeError("native case token is invalid")
        token.stop_event.set()
        if token.thread is not None:
            token.thread.join(timeout=5.0)
        deadline = time.monotonic() + min(30.0, convergence_seconds)
        while time.monotonic() <= deadline:
            self._sample_token(token)
            with token.lock:
                alive = [key for key in token.observed if key not in token.stopped_at]
            if not alive:
                break
            time.sleep(self.sample_interval_seconds)
        if token in self._active_tokens:
            self._active_tokens.remove(token)
        if token.error is not None:
            raise RuntimeError("native case sampling failed") from None
        finished_at = datetime.now(timezone.utc)
        with token.lock:
            if not token.observed or any(key not in token.stopped_at for key in token.observed):
                raise RuntimeError("request runner lifecycle did not converge")
            processes = [
                _runner_process_evidence(identity, token.stopped_at[key])
                for key, identity in token.observed.items()
            ]
            gpu_peak = token.gpu_peak
        temp_removed = not _temp_entries_for_delta(token.temp_before, token_roots=token.temp_roots)
        return HostCaseObservation(
            started_at=token.started_at,
            finished_at=finished_at,
            cleanup=CleanupEvidence(
                ok=temp_removed,
                owned_processes_stopped=True,
                temp_paths_removed=temp_removed,
            ),
            processes=processes,
            gpu_before=token.gpu_before,
            gpu_peak=gpu_peak,
            gpu_after=self._gpu_snapshot(),
        )

    def stop_owned(self, identity: RecordedProcessIdentity) -> None:
        if self.inspect_process(identity.pid) != identity:
            raise RuntimeError("owned process identity changed")
        snapshot = self._process_snapshot()
        descendant_pids = _descendant_pids(snapshot, {identity.pid})
        for pid in sorted(descendant_pids, reverse=True):
            if pid == identity.pid:
                continue
            observed = snapshot.get(pid)
            if observed is not None and self.inspect_process(pid) == observed:
                self._stop_pid(pid)
        if self.inspect_process(identity.pid) == identity:
            self._stop_pid(identity.pid)
        deadline = time.monotonic() + 30.0
        while time.monotonic() <= deadline:
            try:
                current = self.inspect_process(identity.pid)
            except RuntimeError:
                return
            if current != identity:
                return
            time.sleep(0.1)
        raise RuntimeError("owned process did not stop")

    def restart_owned(
        self,
        identity: RecordedProcessIdentity,
        launch: PrivateLaunchSpecification,
        convergence_seconds: float,
        *,
        run_id: str,
        lifecycle: PrivateRestartLifecycle,
    ) -> RecordedProcessIdentity:
        del identity
        process: subprocess.Popen[bytes] | None = None
        replacement: RecordedProcessIdentity | None = None
        launch_intent_persisted = False
        registered = False
        try:
            parent = self.inspect_process(os.getpid())
            marker = (
                f"tts_more_reliability_run={run_id}-comfyui-restart-"
                f"{secrets.token_hex(16)}"
            )
            arguments = ("-X", marker, *launch.arguments)
            started_after = datetime.now(timezone.utc)
            intent = PrivateRestartLaunchIntent(
                marker=marker,
                executable_path=launch.executable_path,
                arguments=arguments,
                working_directory=launch.working_directory,
                child_temp_root=launch.temp_root,
                parent_pid=parent.pid,
                parent_creation_time=parent.creation_time,
                started_after=started_after,
            )
            lifecycle.persist_launch_intent(intent)
            launch_intent_persisted = True
            environment = os.environ.copy()
            environment.update({"TEMP": str(launch.temp_root), "TMP": str(launch.temp_root)})
            process = subprocess.Popen(
                [str(launch.executable_path), *arguments],
                cwd=str(launch.working_directory),
                env=environment,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            provisional = PrivateRestartProvisionalProcess(
                pid=process.pid,
                executable_path=launch.executable_path,
                parent_pid=parent.pid,
                parent_creation_time=parent.creation_time,
                started_after=started_after,
                started_before=datetime.now(timezone.utc),
            )
            lifecycle.persist_provisional(provisional)
            identity_deadline = time.monotonic() + min(10.0, convergence_seconds)
            while True:
                try:
                    candidate = self.inspect_process(process.pid)
                except (OSError, RuntimeError, ValueError):
                    candidate = None
                if candidate is not None and _restart_identity_promotes_pending(
                    candidate,
                    intent,
                    provisional,
                ):
                    replacement = candidate
                    break
                if time.monotonic() >= identity_deadline:
                    raise RuntimeError("restarted process identity did not converge")
                time.sleep(0.1)
            self._started_identities[replacement.pid] = replacement
            for token in self._active_tokens:
                with token.lock:
                    token.roots.append(replacement)
            registered = True
            lifecycle.promote(replacement)
            deadline = time.monotonic() + convergence_seconds
            while time.monotonic() <= deadline:
                owner = self.port_owner(launch.port)
                if owner == replacement:
                    return replacement
                time.sleep(0.25)
            raise RuntimeError("restarted process did not own its port")
        except Exception as exc:
            cleanup_failed = False
            try:
                if replacement is not None:
                    self.stop_owned(replacement)
                elif process is not None:
                    process.terminate()
                    process.wait(timeout=30.0)
            except Exception:
                cleanup_failed = True
            finally:
                if registered and replacement is not None:
                    self._started_identities.pop(replacement.pid, None)
                    for token in self._active_tokens:
                        with token.lock:
                            if replacement in token.roots:
                                token.roots.remove(replacement)
            raise RestartLifecycleError(
                "restarted process lifecycle failed",
                cleanup_proven=(
                    not cleanup_failed
                    and not (
                        launch_intent_persisted
                        and process is None
                        and replacement is None
                    )
                ),
            ) from exc

    def final_cleanup_state(self, temp_roots: tuple[Path, ...]) -> tuple[bool, bool]:
        return not self._active_tokens, not _temp_entries(temp_roots)

    def _sample_until_stopped(self, token: _NativeCaseToken) -> None:
        try:
            while not token.stop_event.wait(self.sample_interval_seconds):
                self._sample_token(token)
        except Exception as exc:
            token.error = exc

    def _sample_token(self, token: _NativeCaseToken) -> None:
        snapshot = self._process_snapshot()
        with token.lock:
            descendants = _descendant_pids(snapshot, {root.pid for root in token.roots})
            for pid in descendants - token.baseline_pids - {root.pid for root in token.roots}:
                identity = snapshot.get(pid)
                if identity is not None and identity.creation_time >= token.started_at:
                    token.observed.setdefault((identity.pid, identity.creation_time), identity)
            now = datetime.now(timezone.utc)
            for key in token.observed:
                pid, creation_time = key
                current = snapshot.get(pid)
                if current is None or current.creation_time != creation_time:
                    token.stopped_at.setdefault(key, now)
            gpu = self._gpu_snapshot()
            if gpu.used_mib > token.gpu_peak.used_mib:
                token.gpu_peak = gpu

    def _gpu_snapshot(self) -> GpuSnapshot:
        output = self._run_text(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"]
        )
        rows = [line.strip() for line in output.splitlines() if line.strip()]
        if len(rows) != 1:
            raise RuntimeError("live validation requires exactly one visible GPU")
        values = [part.strip() for part in rows[0].split(",")]
        if len(values) != 2:
            raise RuntimeError("nvidia-smi memory output is invalid")
        return GpuSnapshot(used_mib=int(values[0]), free_mib=int(values[1]))

    def _process_snapshot(self) -> dict[int, RecordedProcessIdentity]:
        document = self._process_inventory_document()
        items = document if isinstance(document, list) else [document]
        raw_by_pid = {
            item["pid"]: item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("pid"), int)
            and item.get("executable_path")
            and item.get("command_line")
        }
        output: dict[int, RecordedProcessIdentity] = {}
        for pid, item in raw_by_pid.items():
            parent = raw_by_pid.get(item.get("parent_pid"))
            cached = self._started_identities.get(pid)
            if parent is None and cached is None:
                continue
            parent_pid = cached.parent_pid if parent is None and cached is not None else parent["pid"]
            parent_creation = (
                cached.parent_creation_time
                if parent is None and cached is not None
                else _parse_utc_datetime(parent["creation_time"])
            )
            output[pid] = RecordedProcessIdentity(
                pid=pid,
                creation_time=_parse_utc_datetime(item["creation_time"]),
                executable_path=Path(item["executable_path"]).resolve(),
                command_line=item["command_line"],
                parent_pid=parent_pid,
                parent_creation_time=parent_creation,
            )
        return output

    def _process_inventory_document(self) -> Any:
        return self._powershell_document(
            "$items = @(Get-CimInstance Win32_Process | ForEach-Object { "
            "[pscustomobject]@{pid=[int]$_.ProcessId;creation_time=$_.CreationDate.ToUniversalTime().ToString('o');"
            "name=[string]$_.Name;executable_path=[string]$_.ExecutablePath;"
            "command_line=[string]$_.CommandLine;parent_pid=[int]$_.ParentProcessId} }); "
            "$items | ConvertTo-Json -Compress -Depth 3",
            {},
        )

    def _stop_pid(self, pid: int) -> None:
        self._powershell_document(
            "Stop-Process -Id ([int]$env:TTS_MORE_VALIDATION_PID) -Force -ErrorAction Stop; "
            "@{stopped=$true} | ConvertTo-Json -Compress",
            {"TTS_MORE_VALIDATION_PID": str(pid)},
        )

    def _powershell_document(self, script: str, updates: dict[str, str]) -> Any:
        environment = os.environ.copy()
        environment.update(updates)
        executable = shutil.which("powershell.exe") or shutil.which("pwsh")
        if executable is None:
            raise RuntimeError("PowerShell is unavailable")
        completed = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("PowerShell host probe failed")
        return json.loads(completed.stdout.strip())

    @staticmethod
    def _run_bytes(command: list[str], *, allowed_returncodes: set[int] = {0}) -> bytes:
        completed = subprocess.run(command, capture_output=True, check=False)
        if completed.returncode not in allowed_returncodes:
            raise RuntimeError("host command failed")
        return completed.stdout

    @classmethod
    def _run_text(cls, command: list[str], *, allowed_returncodes: set[int] = {0}) -> str:
        return cls._run_bytes(command, allowed_returncodes=allowed_returncodes).decode("utf-8", errors="replace")


def _descendant_pids(
    snapshot: dict[int, RecordedProcessIdentity],
    roots: set[int],
) -> set[int]:
    descendants = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, identity in snapshot.items():
            if identity.parent_pid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return descendants


def _runner_process_evidence(identity: RecordedProcessIdentity, stopped_at: datetime) -> ProcessEvidence:
    return ProcessEvidence(
        pid=identity.pid,
        ownership="validator-owned",
        command_hash=hashlib.sha256(identity.command_line.encode("utf-8")).hexdigest(),
        creation_time=identity.creation_time,
        parent_pid=identity.parent_pid,
        parent_creation_time=identity.parent_creation_time,
        stopped_at=stopped_at,
        executable_name=identity.executable_path.name,
        executable_hash=_sha256_file(identity.executable_path),
        ownership_hash=_hash_private_process_identity(identity),
        started=True,
        stopped=True,
        descendants_stopped=True,
        alive_after=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temp_entries(roots: tuple[Path, ...]) -> set[str]:
    entries: set[str] = set()
    for root in roots:
        if root.exists():
            entries.update(f"{index}:{path.relative_to(root).as_posix()}" for index, path in enumerate(root.rglob("*")))
    return entries


def _temp_entries_for_delta(before: set[str], *, token_roots: tuple[Path, ...]) -> set[str]:
    return _temp_entries(token_roots) - before


def _resolve_registered_service_bindings(
    document: dict[str, Any],
    fixture: ReliabilityFixture,
) -> dict[Engine, str]:
    services = document.get("services")
    if not isinstance(services, list):
        raise LiveValidationError("registered-service-binding", stage="preflight")
    expected_base_url = _normalized_registered_service_url(
        fixture.base_urls["comfyui"],
    )
    if expected_base_url is None:
        raise LiveValidationError("registered-service-binding", stage="preflight")

    bindings: dict[Engine, str] = {}
    resource_groups: set[str] = set()
    for engine in ENGINE_ORDER:
        expected_resource_id = fixture.resources[engine].resource_id
        matches = [
            service
            for service in services
            if _registered_service_matches(
                service,
                engine=engine,
                resource_id=expected_resource_id,
                base_url=expected_base_url,
            )
        ]
        if len(matches) != 1:
            raise LiveValidationError("registered-service-binding", stage="preflight")
        match = matches[0]
        service_id = match.get("service_id")
        resource_group = match.get("resource_group")
        if (
            not isinstance(service_id, str)
            or not service_id.strip()
            or not isinstance(resource_group, str)
            or not resource_group.strip()
        ):
            raise LiveValidationError("registered-service-binding", stage="preflight")
        bindings[engine] = service_id
        resource_groups.add(resource_group)

    if len(bindings) != len(ENGINE_ORDER) or len(set(bindings.values())) != len(ENGINE_ORDER):
        raise LiveValidationError("registered-service-binding", stage="preflight")
    if len(resource_groups) != 1:
        raise LiveValidationError("registered-service-binding", stage="preflight")
    return bindings


def _registered_service_matches(
    service: Any,
    *,
    engine: Engine,
    resource_id: str,
    base_url: tuple[str, str, int, str],
) -> bool:
    if not isinstance(service, dict):
        return False
    capabilities = service.get("capabilities")
    default_params = service.get("default_params")
    if not isinstance(capabilities, list) or not all(
        isinstance(capability, str) for capability in capabilities
    ):
        return False
    normalized_capabilities = {
        capability.replace("_", "-").casefold()
        for capability in capabilities
    }
    capacity = service.get("capacity")
    return bool(
        service.get("service_kind") == "tts"
        and service.get("enabled") is True
        and service.get("ready") is True
        and service.get("api_contract") == _REGISTERED_SERVICE_CONTRACT
        and service.get("engine") == engine
        and service.get("provider_type") == engine
        and isinstance(default_params, dict)
        and default_params.get("resource_id") == resource_id
        and _normalized_registered_service_url(service.get("base_url")) == base_url
        and _REGISTERED_SERVICE_CAPABILITIES <= normalized_capabilities
        and type(capacity) is int
        and capacity == 1
    )


def _normalized_registered_service_url(value: Any) -> tuple[str, str, int, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    path = parsed.path.rstrip("/") or "/"
    return (scheme, parsed.hostname.casefold(), effective_port, path)


class HttpReliabilityProbe:
    """Concrete local HTTP probe for the TTS More and ComfyUI contracts."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        reference_root: Path,
        poll_interval_seconds: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.transport = transport
        self.reference_root = Path(reference_root).resolve()
        self.poll_interval_seconds = poll_interval_seconds
        self.monotonic = monotonic
        self.sleep = sleep
        self.utcnow = utcnow
        self._fixture: ReliabilityFixture | None = None
        self._registered_service_ids: dict[Engine, str] = {}
        self._released = False
        self._seen_job_ids: set[str] = set()
        self._seen_prompt_ids: set[str] = set()
        self._seen_version_ids: set[str] = set()
        self._failed_case_observations: dict[str, FailedCaseObservation] = {}

    def preflight(self, fixture: ReliabilityFixture) -> HttpPreflightObservation:
        self._fixture = _revalidate_model(fixture, ReliabilityFixture)
        self._registered_service_ids = {}
        comfyui_url = fixture.base_urls["comfyui"].rstrip("/")
        tts_more_url = fixture.base_urls["tts_more"].rstrip("/")
        system_stats = self._json("GET", f"{comfyui_url}/system_stats", timeout=5.0)
        object_info = self._json("GET", f"{comfyui_url}/object_info", timeout=10.0)
        capabilities = self._json(
            "GET",
            f"{comfyui_url}/api/tts-audio-suite/v1/capabilities",
            timeout=10.0,
        )
        if not isinstance(system_stats, dict) or not isinstance(object_info, dict):
            raise RuntimeError("ComfyUI readiness probes returned invalid documents")
        resources_raw = capabilities.get("resources") if isinstance(capabilities, dict) else None
        if not isinstance(resources_raw, list):
            raise RuntimeError("ComfyUI capabilities omitted resources")
        resources = [
            ReadyResource(
                engine=_canonical_bridge_engine(item.get("engine")),
                resource_id=item.get("resource_id"),
                ready=item.get("ready") is True,
            )
            for item in resources_raw
            if isinstance(item, dict)
        ]
        service_status = self._json(
            "GET",
            f"{tts_more_url}/api/services",
            timeout=30.0,
        )
        self._registered_service_ids = _resolve_registered_service_bindings(
            service_status,
            fixture,
        )
        for engine in ENGINE_ORDER:
            response = self._json(
                "POST",
                f"{tts_more_url}/api/generation/preflight",
                json_body=self._generation_payload(
                    CasePlan(
                        case_id=f"preflight-{engine}",
                        phase="steady",
                        engine=engine,
                        expected="completed",
                        action="synthesize",
                        request_timeout_seconds=30.0,
                        convergence_seconds=30.0,
                    ),
                    fixture,
                ),
                timeout=30.0,
            )
            response_items = response.get("items")
            if (
                response.get("status") != "ready"
                or not isinstance(response_items, list)
                or len(response_items) != 1
                or not isinstance(response_items[0], dict)
                or response_items[0].get("status") != "ready"
            ):
                raise LiveValidationError(
                    "tts-more-preflight-not-ready",
                    stage="preflight",
                )
        return HttpPreflightObservation(
            resources=resources,
            queue=self._queue_snapshot(fixture),
        )

    def _observation_now(self) -> str:
        return _public_utc(self.utcnow())

    def _begin_failed_case_observation(
        self,
        case: CasePlan,
        fixture: ReliabilityFixture,
    ) -> None:
        service_id = self._registered_service_ids.get(case.engine)
        try:
            self._failed_case_observations[case.case_id] = FailedCaseObservation(
                detail_status="incremental",
                action=case.action,
                request_sha256=None,
                service_id_sha256=_sha256_text(service_id) if service_id is not None else None,
                resource_id_sha256=_sha256_text(fixture.resources[case.engine].resource_id),
                job_created=False,
                prompt_observed=False,
                job_id_sha256=None,
                prompt_id_sha256=None,
                version_id_sha256=None,
                job_created_at=None,
                first_prompt_at=None,
                last_poll_at=None,
                terminal_at=None,
                request_timeout_seconds=case.request_timeout_seconds,
                convergence_seconds=case.convergence_seconds,
                poll_count=0,
                terminal_observed=False,
                last_job_status=None,
                last_item_status=None,
                last_external_status=None,
                last_response_sha256=None,
                last_control_code=None,
                last_failure_stage=None,
                diagnostic_sha256=None,
                queue=None,
                control=None,
                wav_observed=False,
                audio_sha256=None,
                audio_size_bytes=None,
                secondary_error_sha256=None,
            )
        except Exception as exc:
            self._failed_case_observations[case.case_id] = _minimal_failed_case_observation(
                case,
                secondary_error=exc,
            )

    def _update_failed_case_observation(self, case: CasePlan, **updates: Any) -> None:
        current = self._failed_case_observations.get(case.case_id)
        if current is None or current.detail_status == "minimal":
            return
        try:
            document = current.model_dump(mode="python")
            document.update(updates)
            self._failed_case_observations[case.case_id] = FailedCaseObservation.model_validate(
                document
            )
        except Exception as exc:
            self._failed_case_observations[case.case_id] = _minimal_failed_case_observation(
                case,
                secondary_error=exc,
            )

    def failed_case_observation(self, case: CasePlan) -> FailedCaseObservation:
        observation = self._failed_case_observations.get(case.case_id)
        if observation is None:
            return _minimal_failed_case_observation(case)
        return _revalidate_model(observation, FailedCaseObservation)

    @staticmethod
    def _observed_status(value: Any) -> ObservedStatus | None:
        if isinstance(value, str) and value in {
            "queued",
            "running",
            "cancelling",
            "cancelled",
            "failed",
            "completed",
        }:
            return value
        return None

    def _record_request_payload(self, case: CasePlan, payload: dict[str, Any]) -> None:
        self._update_failed_case_observation(
            case,
            request_sha256=_sha256_document(payload),
        )

    def _record_job_created(self, case: CasePlan, job_id: str) -> None:
        self._update_failed_case_observation(
            case,
            job_created=True,
            job_id_sha256=_sha256_text(job_id),
            job_created_at=self._observation_now(),
        )

    def _record_job_poll(self, case: CasePlan, job: dict[str, Any]) -> None:
        current = self._failed_case_observations.get(case.case_id)
        if current is None or current.detail_status == "minimal":
            return
        observed_at = self._observation_now()
        items = job.get("items")
        item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else {}
        job_status = self._observed_status(job.get("status"))
        item_status = self._observed_status(item.get("status"))
        external_status = self._observed_status(item.get("external_status"))
        terminal_observed = job_status in {"completed", "cancelled", "failed"}
        diagnostics = [job.get("error"), item.get("error")]
        unknown_external_status = item.get("external_status") if external_status is None else None
        if unknown_external_status is not None:
            diagnostics.append(unknown_external_status)
        diagnostic_values = [value for value in diagnostics if value is not None and value != ""]
        self._update_failed_case_observation(
            case,
            poll_count=current.poll_count + 1,
            last_poll_at=observed_at,
            terminal_at=observed_at if terminal_observed else current.terminal_at,
            terminal_observed=terminal_observed,
            last_job_status=job_status,
            last_item_status=item_status,
            last_external_status=external_status,
            last_response_sha256=_sha256_document(job),
            diagnostic_sha256=(
                _sha256_document(diagnostic_values)
                if diagnostic_values
                else current.diagnostic_sha256
            ),
        )

    def _record_prompt(self, case: CasePlan, prompt_id: str) -> None:
        current = self._failed_case_observations.get(case.case_id)
        if current is None or current.detail_status == "minimal" or current.prompt_observed:
            return
        self._update_failed_case_observation(
            case,
            prompt_observed=True,
            prompt_id_sha256=_sha256_text(prompt_id),
            first_prompt_at=self._observation_now(),
        )

    def _record_queue(
        self,
        case: CasePlan,
        queue: dict[str, Any],
        prompt_id: str | None,
    ) -> None:
        try:
            running_ids = _comfy_prompt_ids(queue, key="queue_running")
            pending_ids = _comfy_prompt_ids(queue, key="queue_pending")
            if prompt_id is None:
                target_state = None
            elif prompt_id in running_ids:
                target_state = "running"
            elif prompt_id in pending_ids:
                target_state = "pending"
            else:
                target_state = "absent"
            observation = FailedCaseQueueObservation(
                observed_at=self._observation_now(),
                snapshot_sha256=_sha256_document(queue),
                running_count=len(running_ids),
                pending_count=len(pending_ids),
                target_state=target_state,
            )
            self._update_failed_case_observation(case, queue=observation)
        except Exception as exc:
            self._failed_case_observations[case.case_id] = _minimal_failed_case_observation(
                case,
                secondary_error=exc,
            )

    def _record_control_request(
        self,
        case: CasePlan,
        *,
        reason: Literal["user-cancel", "owned-comfyui-termination"],
        interrupt_class: Literal["job-cancel-request", "owned-service-termination"],
        initial_state: Literal["running", "pending", "absent"],
    ) -> None:
        self._update_failed_case_observation(
            case,
            control=FailedCaseControlObservation(
                interrupt_reason=reason,
                interrupt_class=interrupt_class,
                requested_at=self._observation_now(),
                converged_at=None,
                initial_state=initial_state,
                final_state=None,
                converged=None,
                duration_seconds=None,
                diagnostic_sha256=None,
            ),
        )

    def _record_terminal_control(
        self,
        case: CasePlan,
        control: FaultControlEvidence,
    ) -> None:
        current = self._failed_case_observations.get(case.case_id)
        prior_control = current.control if current is not None else None
        reason: Literal["request-timeout", "user-cancel"] = (
            "request-timeout" if control.control_code == "timeout" else "user-cancel"
        )
        self._update_failed_case_observation(
            case,
            last_control_code=control.control_code,
            last_failure_stage=control.failure_stage,
            control=FailedCaseControlObservation(
                interrupt_reason=reason,
                interrupt_class="prompt-scoped-interrupt",
                requested_at=prior_control.requested_at if prior_control is not None else None,
                converged_at=self._observation_now(),
                initial_state=control.initial_state,
                final_state=control.final_state,
                converged=control.converged,
                duration_seconds=control.duration_seconds,
                diagnostic_sha256=None,
            ),
        )

    def _record_queued_control_terminal(self, case: CasePlan) -> None:
        current = self._failed_case_observations.get(case.case_id)
        prior_control = current.control if current is not None else None
        self._update_failed_case_observation(
            case,
            control=FailedCaseControlObservation(
                interrupt_reason="user-cancel",
                interrupt_class="job-cancel-request",
                requested_at=prior_control.requested_at if prior_control is not None else None,
                converged_at=self._observation_now(),
                initial_state="absent",
                final_state="dequeued",
                converged=True,
                duration_seconds=None,
                diagnostic_sha256=None,
            ),
        )

    def _record_version_and_audio(
        self,
        case: CasePlan,
        raw_version_id: str,
        wav_path: Path | None,
    ) -> None:
        updates: dict[str, Any] = {"version_id_sha256": _sha256_text(raw_version_id)}
        if wav_path is not None and wav_path.is_file():
            try:
                size = wav_path.stat().st_size
                if size > 0:
                    updates.update(
                        wav_observed=True,
                        audio_sha256=_sha256_file(wav_path),
                        audio_size_bytes=size,
                    )
            except OSError as exc:
                updates["diagnostic_sha256"] = _sha256_text(
                    f"{type(exc).__name__}\0{exc}"
                )
        self._update_failed_case_observation(case, **updates)

    def execute_case(
        self,
        case: CasePlan,
        fixture: ReliabilityFixture,
        output_directory: Path,
        *,
        action_hook: Callable[[], None] | None = None,
    ) -> HttpCaseObservation:
        del output_directory
        fixture = _revalidate_model(fixture, ReliabilityFixture)
        self._begin_failed_case_observation(case, fixture)
        tts_more_url = fixture.base_urls["tts_more"].rstrip("/")
        comfyui_url = fixture.base_urls["comfyui"].rstrip("/")
        payload = self._generation_payload(case, fixture)
        self._record_request_payload(case, payload)
        if case.action == "restart-readiness" and action_hook is not None:
            action_hook()
        preflight = self._json(
            "POST",
            f"{tts_more_url}/api/generation/preflight",
            json_body=payload,
            timeout=min(30.0, case.convergence_seconds),
        )
        if preflight.get("status") != "ready":
            raise RuntimeError("case generation preflight is not ready")
        if case.action == "cancel-queued":
            return self._execute_queued_cancel_case(case, fixture, payload)
        created = self._json(
            "POST",
            f"{tts_more_url}/api/jobs/generation",
            json_body=payload,
            timeout=case.request_timeout_seconds,
        )
        job_id = _required_opaque_id(created.get("job_id"), "job")
        self._record_job_created(case, job_id)
        if job_id in self._seen_job_ids:
            raise RuntimeError("job id was reused")
        self._seen_job_ids.add(job_id)

        action_case = case.action in {"cancel-running", "terminate-comfyui"}
        deadline = self.monotonic() + case.request_timeout_seconds
        if not action_case:
            deadline += case.convergence_seconds
        prompt_id: str | None = None
        queue_before: list[str] | None = None
        acted = False
        endpoint_unavailable = False
        terminal: dict[str, Any] | None = None

        def require_case_window_open() -> None:
            if self.monotonic() < deadline:
                return
            if not action_case:
                raise RuntimeError("job terminal observation window expired")
            if acted:
                raise RuntimeError("fault terminal convergence window expired")
            raise RuntimeError("fault action request window expired")

        while self.monotonic() <= deadline:
            job = self._json("GET", f"{tts_more_url}/api/jobs/{job_id}", timeout=10.0)
            self._record_job_poll(case, job)
            require_case_window_open()
            items = job.get("items")
            item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else {}
            external_id = item.get("external_job_id")
            if external_id:
                observed_prompt = _required_opaque_id(external_id, "prompt")
                if prompt_id is not None and prompt_id != observed_prompt:
                    raise RuntimeError("job changed prompt id")
                prompt_id = observed_prompt
                self._record_prompt(case, prompt_id)
                try:
                    queue = self._comfy_queue(fixture)
                except httpx.TransportError:
                    if case.action != "terminate-comfyui" or not acted:
                        raise
                    endpoint_unavailable = True
                    queue = None
                require_case_window_open()
                if queue is not None:
                    self._record_queue(case, queue, prompt_id)
                prompt_ids = _comfy_prompt_ids(queue) if queue is not None else []
                if prompt_id in prompt_ids and queue_before is None:
                    queue_before = prompt_ids
                state = _comfy_prompt_state(queue, prompt_id) if queue is not None else "absent"
                if case.action == "cancel-queued" and not acted and state == "pending":
                    self._cancel_job(tts_more_url, job_id)
                    acted = True
                elif case.action == "cancel-running" and not acted and state == "running":
                    require_case_window_open()
                    deadline = self.monotonic() + case.convergence_seconds
                    self._record_control_request(
                        case,
                        reason="user-cancel",
                        interrupt_class="job-cancel-request",
                        initial_state="running",
                    )
                    self._cancel_job(tts_more_url, job_id)
                    acted = True
                    require_case_window_open()
                elif case.action == "terminate-comfyui" and not acted and state == "running":
                    if action_hook is None:
                        raise RuntimeError("terminate action hook is missing")
                    require_case_window_open()
                    deadline = self.monotonic() + case.convergence_seconds
                    self._record_control_request(
                        case,
                        reason="owned-comfyui-termination",
                        interrupt_class="owned-service-termination",
                        initial_state="running",
                    )
                    action_hook()
                    acted = True
                    require_case_window_open()
            if job.get("status") in {"completed", "cancelled", "failed"}:
                terminal = job
                break
            self.sleep(self.poll_interval_seconds)
        if terminal is None:
            raise RuntimeError("job did not converge")
        if case.action in {"cancel-queued", "cancel-running", "terminate-comfyui"} and not acted:
            raise RuntimeError("fault action window was not observed")

        items = terminal.get("items")
        item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else {}
        if prompt_id is None and item.get("external_job_id"):
            prompt_id = _required_opaque_id(item.get("external_job_id"), "prompt")
        if prompt_id is None or queue_before is None:
            raise RuntimeError("prompt queue lifecycle was not observed")
        if prompt_id in self._seen_prompt_ids:
            raise RuntimeError("prompt id was reused")
        self._seen_prompt_ids.add(prompt_id)

        actual = _job_outcome(case, terminal)
        manifest = self._json(
            "GET",
            f"{tts_more_url}/api/projects/windows-reliability-validation/manifest",
            timeout=10.0,
        )
        version = _find_manifest_version(manifest, case.case_id, item.get("version_id"))
        raw_version_id = _required_opaque_id(version.get("version_id"), "version")
        version_id = _public_manifest_version_id(case.case_id, raw_version_id)
        if version_id in self._seen_version_ids:
            raise RuntimeError("version id was reused")
        self._seen_version_ids.add(version_id)
        wav_path = Path(version["audio_path"]) if actual == "completed" and version.get("audio_path") else None
        self._record_version_and_audio(case, raw_version_id, wav_path)
        if case.action == "terminate-comfyui":
            if (
                not endpoint_unavailable
                or actual != "failed"
                or item.get("status") != "failed"
                or version.get("status") != "failed"
                or version.get("audio_path")
            ):
                raise RuntimeError("ComfyUI termination proof is incomplete")
            return HttpCaseObservation(
                actual=actual,
                job_id=job_id,
                prompt_id=prompt_id,
                version_id=version_id,
                wav_path=None,
                comfyui=None,
                tts_more=TtsTerminalEvidence(
                    job_status="failed",
                    item_status="failed",
                    version_status="failed",
                    manifest_version_absent=False,
                    version_audio_absent=True,
                ),
                termination=TerminationEvidence(
                    endpoint_unavailable=True,
                    prompt_id=prompt_id,
                    queue_before_prompt_ids=queue_before,
                    manifest_audio_absent=True,
                ),
            )
        tts_terminal = None
        if case.action in {"cancel-running", "timeout"}:
            tts_terminal = _fault_terminal_evidence(
                case,
                terminal=terminal,
                item=item,
                version=version,
                prompt_id=prompt_id,
            )
            assert tts_terminal.control is not None
            self._record_terminal_control(case, tts_terminal.control)
        queue_after_document = self._comfy_queue(fixture)
        self._record_queue(case, queue_after_document, prompt_id)
        queue_after = _comfy_prompt_ids(queue_after_document)
        history = self._json("GET", f"{comfyui_url}/history/{prompt_id}", timeout=10.0)
        history_ids = sorted(str(key) for key in history if isinstance(key, str))
        terminal_history_status = _terminal_comfy_history_status(
            history.get(prompt_id),
            expected=actual,
        )
        return HttpCaseObservation(
            actual=actual,
            job_id=job_id,
            prompt_id=prompt_id,
            version_id=version_id,
            wav_path=wav_path,
            comfyui=ComfyQueueEvidence(
                queue_empty=not queue_after,
                history_present=prompt_id in history_ids,
                prompt_id=prompt_id,
                queue_before_prompt_ids=queue_before,
                queue_after_prompt_ids=queue_after,
                history_prompt_ids=history_ids,
                terminal_history_status=terminal_history_status,
            ),
            tts_more=tts_terminal,
        )

    def release(self) -> None:
        if self._fixture is None:
            raise RuntimeError("HTTP probe was not preflighted")
        comfyui_url = self._fixture.base_urls["comfyui"].rstrip("/")
        self._json(
            "POST",
            f"{comfyui_url}/api/tts-audio-suite/v1/runtime/release",
            json_body={"all": True},
            timeout=120.0,
        )
        self._json(
            "POST",
            f"{comfyui_url}/free",
            json_body={"unload_models": True, "free_memory": True},
            timeout=60.0,
        )
        self._released = True

    def final_state(self) -> HttpFinalObservation:
        if self._fixture is None:
            raise RuntimeError("HTTP probe was not preflighted")
        return HttpFinalObservation(
            queue=self._queue_snapshot(self._fixture),
            runtime_released=self._released,
        )

    def _generation_payload(self, case: CasePlan, fixture: ReliabilityFixture) -> dict[str, Any]:
        service_id = self._registered_service_ids.get(case.engine)
        if (
            service_id is None
            or self._fixture is None
            or self._fixture != fixture
            or set(self._registered_service_ids) != set(ENGINE_ORDER)
        ):
            raise LiveValidationError(
                "registered-service-binding",
                stage="preflight",
            )
        resource = fixture.resources[case.engine]
        reference_path = (self.reference_root / resource.reference_audio).resolve()
        if not reference_path.is_relative_to(self.reference_root):
            raise RuntimeError("reference audio escapes fixture root")
        return {
            "project_id": "windows-reliability-validation",
            "tasks": [
                {
                    "line": {
                        "id": case.case_id,
                        "character_id": f"validator-{case.engine}",
                        "text": f"Windows reliability validation {case.engine}",
                    },
                    "engine": case.engine,
                    "profile": resource.resource_id,
                    "service_id": service_id,
                    "provider_type": case.engine,
                    "required_capabilities": ["tts", "reference_audio_voice"],
                    "parameters": {
                        "engine": case.engine,
                        "resource_id": resource.resource_id,
                        "ref_audio_path": str(reference_path),
                        "prompt_text": resource.reference_text,
                        "timeout_seconds": case.request_timeout_seconds,
                    },
                },
            ],
        }

    def _queue_snapshot(self, fixture: ReliabilityFixture) -> QueueSnapshot:
        tts_more_url = fixture.base_urls["tts_more"].rstrip("/")
        tts_queue = self._json("GET", f"{tts_more_url}/api/queue/status", timeout=10.0)
        comfy_queue = self._comfy_queue(fixture)
        return QueueSnapshot(
            tts_queued=tts_queue.get("queued"),
            tts_running=tts_queue.get("running"),
            comfy_pending_prompt_ids=_comfy_prompt_ids(comfy_queue, key="queue_pending"),
            comfy_running_prompt_ids=_comfy_prompt_ids(comfy_queue, key="queue_running"),
        )

    def _comfy_queue(self, fixture: ReliabilityFixture) -> dict[str, Any]:
        comfyui_url = fixture.base_urls["comfyui"].rstrip("/")
        return self._json("GET", f"{comfyui_url}/queue", timeout=10.0)

    def _execute_queued_cancel_case(
        self,
        case: CasePlan,
        fixture: ReliabilityFixture,
        payload: dict[str, Any],
    ) -> HttpCaseObservation:
        tts_more_url = fixture.base_urls["tts_more"].rstrip("/")
        comfyui_url = fixture.base_urls["comfyui"].rstrip("/")
        manifest_url = f"{tts_more_url}/api/projects/windows-reliability-validation/manifest"
        manifest_before = self._json("GET", manifest_url, timeout=10.0)
        if _manifest_version_ids_for_line(manifest_before, case.case_id):
            raise RuntimeError("queued-cancel target already has manifest versions")

        blocker_case = case.model_copy(
            update={
                "case_id": f"validator-blocker-{secrets.token_hex(8)}",
                "action": "synthesize",
                "request_timeout_seconds": 30.0,
            }
        )
        blocker_created = self._json(
            "POST",
            f"{tts_more_url}/api/jobs/generation",
            json_body=self._generation_payload(blocker_case, fixture),
            timeout=30.0,
        )
        blocker_job_id = _required_opaque_id(blocker_created.get("job_id"), "job")
        self._remember_unique(self._seen_job_ids, blocker_job_id, "job")
        blocker_prompt_id: str | None = None
        target_job_id: str | None = None
        blocker_cancelled = False
        target_cancelled = False
        try:
            admission_deadline = self.monotonic() + case.request_timeout_seconds

            def require_admission_window_open() -> None:
                if self.monotonic() >= admission_deadline:
                    raise RuntimeError("queued-cancel admission window expired")

            def require_settlement_window_open(settlement_deadline: float) -> None:
                if self.monotonic() >= settlement_deadline:
                    raise RuntimeError("queued-cancel settlement window expired")

            while self.monotonic() <= admission_deadline:
                blocker = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{blocker_job_id}",
                    timeout=10.0,
                )
                require_admission_window_open()
                blocker_item = _single_job_item(blocker)
                external_id = blocker_item.get("external_job_id")
                if external_id:
                    blocker_prompt_id = _required_opaque_id(external_id, "prompt")
                    blocker_queue = self._comfy_queue(fixture)
                    require_admission_window_open()
                    if _comfy_prompt_state(blocker_queue, blocker_prompt_id) in {
                        "pending",
                        "running",
                    }:
                        break
                if blocker.get("status") in {"completed", "cancelled", "failed"}:
                    raise RuntimeError("queued-cancel blocker completed before admission was held")
                self.sleep(self.poll_interval_seconds)
            else:
                raise RuntimeError("queued-cancel blocker did not hold admission")
            self._remember_unique(self._seen_prompt_ids, blocker_prompt_id, "prompt")

            target_created = self._json(
                "POST",
                f"{tts_more_url}/api/jobs/generation",
                json_body=payload,
                timeout=case.request_timeout_seconds,
            )
            target_job_id = _required_opaque_id(target_created.get("job_id"), "job")
            self._record_job_created(case, target_job_id)
            self._remember_unique(self._seen_job_ids, target_job_id, "job")
            require_admission_window_open()
            queued_target: dict[str, Any] | None = None
            while self.monotonic() <= admission_deadline:
                target = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{target_job_id}",
                    timeout=10.0,
                )
                self._record_job_poll(case, target)
                require_admission_window_open()
                target_item = _single_job_item(target)
                if target.get("status") == "running" and _queued_item_is_pristine(target_item):
                    queued_target = target
                    break
                if target_item.get("external_job_id") or target_item.get("status") != "queued":
                    raise RuntimeError("queued-cancel target escaped pre-dispatch admission")
                self.sleep(self.poll_interval_seconds)
            if queued_target is None:
                raise RuntimeError("queued-cancel target did not reach held admission")

            require_admission_window_open()
            settlement_deadline = self.monotonic() + case.convergence_seconds
            self._record_control_request(
                case,
                reason="user-cancel",
                interrupt_class="job-cancel-request",
                initial_state="absent",
            )
            cancelled = self._cancel_job(tts_more_url, target_job_id)
            self._record_job_poll(case, cancelled)
            _require_pristine_queued_cancellation(cancelled)
            target_cancelled = True
            require_settlement_window_open(settlement_deadline)
            cancelled_updated_at = cancelled.get("updated_at")
            if not isinstance(cancelled_updated_at, str) or not cancelled_updated_at:
                raise RuntimeError("queued-cancel response omitted update time")

            self._cancel_job(tts_more_url, blocker_job_id)
            blocker_cancelled = True
            require_settlement_window_open(settlement_deadline)
            settled_target: dict[str, Any] | None = None
            while self.monotonic() <= settlement_deadline:
                target = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{target_job_id}",
                    timeout=10.0,
                )
                self._record_job_poll(case, target)
                require_settlement_window_open(settlement_deadline)
                blocker = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{blocker_job_id}",
                    timeout=10.0,
                )
                require_settlement_window_open(settlement_deadline)
                settled_queue = self._comfy_queue(fixture)
                self._record_queue(case, settled_queue, None)
                require_settlement_window_open(settlement_deadline)
                queue_ids = _comfy_prompt_ids(settled_queue)
                if (
                    target.get("updated_at") != cancelled_updated_at
                    and blocker.get("status") in {"cancelled", "failed", "completed"}
                    and blocker_prompt_id not in queue_ids
                ):
                    _require_pristine_queued_cancellation(target)
                    settled_target = target
                    break
                self.sleep(self.poll_interval_seconds)
            if settled_target is None:
                raise RuntimeError("queued-cancel worker did not settle after admission release")
            self._record_queued_control_terminal(case)

            manifest_after = self._json("GET", manifest_url, timeout=10.0)
            manifest_absent = (
                not _manifest_version_ids_for_line(manifest_before, case.case_id)
                and not _manifest_version_ids_for_line(manifest_after, case.case_id)
            )
            if not manifest_absent:
                raise RuntimeError("queued-cancel target fabricated a manifest version")
            return HttpCaseObservation(
                actual="cancelled",
                job_id=target_job_id,
                prompt_id=None,
                version_id=None,
                wav_path=None,
                comfyui=None,
                prompt_submitted=False,
                tts_more=TtsTerminalEvidence(
                    job_status="cancelled",
                    item_status="cancelled",
                    version_status=None,
                    manifest_version_absent=True,
                    version_audio_absent=True,
                ),
            )
        finally:
            if target_job_id is not None and not target_cancelled:
                self._best_effort_cancel(tts_more_url, target_job_id)
            if not blocker_cancelled:
                self._best_effort_cancel(tts_more_url, blocker_job_id)

    @staticmethod
    def _remember_unique(seen: set[str], value: str, label: str) -> None:
        if value in seen:
            raise RuntimeError(f"{label} id was reused")
        seen.add(value)

    def _cancel_job(self, tts_more_url: str, job_id: str) -> dict[str, Any]:
        return self._json("POST", f"{tts_more_url}/api/jobs/{job_id}/cancel", timeout=30.0)

    def _best_effort_cancel(self, tts_more_url: str, job_id: str) -> None:
        try:
            self._cancel_job(tts_more_url, job_id)
        except Exception:
            pass

    def _json(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        with httpx.Client(transport=self.transport, timeout=timeout, trust_env=False) as client:
            response = client.request(method, url, json=json_body)
            response.raise_for_status()
            document = response.json()
        if not isinstance(document, dict):
            raise RuntimeError("HTTP probe returned a non-object document")
        return document


def _required_opaque_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value:
        raise RuntimeError(f"{label} id is missing or invalid")
    return value


def _comfy_prompt_ids(queue: dict[str, Any], *, key: str | None = None) -> list[str]:
    keys = (key,) if key is not None else ("queue_running", "queue_pending")
    prompt_ids: list[str] = []
    for queue_key in keys:
        items = queue.get(queue_key, [])
        if not isinstance(items, list):
            raise RuntimeError("ComfyUI queue shape is invalid")
        for item in items:
            if not isinstance(item, (list, tuple)) or len(item) < 2 or not isinstance(item[1], str):
                raise RuntimeError("ComfyUI queue item is invalid")
            prompt_ids.append(item[1])
    if len(prompt_ids) != len(set(prompt_ids)):
        raise RuntimeError("ComfyUI queue contains duplicate prompt ids")
    return sorted(prompt_ids)


def _comfy_prompt_state(queue: dict[str, Any], prompt_id: str) -> str:
    if prompt_id in _comfy_prompt_ids(queue, key="queue_running"):
        return "running"
    if prompt_id in _comfy_prompt_ids(queue, key="queue_pending"):
        return "pending"
    return "absent"


def _terminal_comfy_history_status(entry: Any, *, expected: Outcome) -> Outcome:
    if not isinstance(entry, dict):
        raise RuntimeError("ComfyUI terminal history entry is missing")
    status = entry.get("status")
    outputs = entry.get("outputs")
    if not isinstance(status, dict):
        raise RuntimeError("ComfyUI terminal history status is missing")
    status_string = status.get("status_str")
    completed = status.get("completed")
    messages = status.get("messages")
    if expected == "completed":
        if status_string != "success" or completed is not True or not isinstance(outputs, dict) or not outputs:
            raise RuntimeError("ComfyUI success history is incomplete")
        return "completed"
    interrupted = isinstance(messages, list) and any(
        isinstance(message, list)
        and len(message) >= 1
        and message[0] == "execution_interrupted"
        for message in messages
    )
    if expected in {"cancelled", "timeout"} and (
        status_string == "error" and completed is False and interrupted
    ):
        return expected
    if expected == "failed" and status_string == "error" and completed is False:
        return "failed"
    raise RuntimeError("ComfyUI terminal history does not match the TTS outcome")


def _canonical_bridge_engine(value: Any) -> Engine:
    if not isinstance(value, str) or value not in _BRIDGE_ENGINE_IDS:
        raise RuntimeError("bridge reported an unsupported engine")
    return _BRIDGE_ENGINE_IDS[value]


def _job_outcome(case: CasePlan, job: dict[str, Any]) -> Outcome:
    status = job.get("status")
    if status == "completed":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    if status != "failed":
        raise RuntimeError("job did not report a supported terminal outcome")
    if case.action == "timeout":
        # The manifest's typed failure_stage/control_code is checked immediately
        # after this provisional outcome; free-form error wording is not evidence.
        return "timeout"
    return "failed"


def _single_job_item(job: dict[str, Any]) -> dict[str, Any]:
    items = job.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError("generation job did not contain exactly one item")
    return items[0]


def _queued_item_is_pristine(item: dict[str, Any]) -> bool:
    return (
        item.get("status") == "queued"
        and item.get("progress") == 0.0
        and all(
            item.get(field) is None
            for field in ("external_job_id", "external_status", "error", "version_id")
        )
    )


def _require_pristine_queued_cancellation(job: dict[str, Any]) -> None:
    item = _single_job_item(job)
    if (
        job.get("status") != "cancelled"
        or job.get("progress") != 1.0
        or job.get("error") is not None
        or item.get("status") != "cancelled"
        or item.get("progress") != 1.0
        or any(
            item.get(field) is not None
            for field in ("external_job_id", "external_status", "error", "version_id")
        )
    ):
        raise RuntimeError("queued-cancel terminal response was not pristine")


def _manifest_version_ids_for_line(manifest: dict[str, Any], case_id: str) -> list[str]:
    lines = manifest.get("lines")
    if not isinstance(lines, dict):
        raise RuntimeError("manifest lines are missing")
    version_ids: list[str] = []
    for line_key, history in lines.items():
        if not isinstance(history, dict) or (
            line_key != case_id and history.get("line_id") != case_id
        ):
            continue
        versions = history.get("versions")
        if not isinstance(versions, list):
            raise RuntimeError("manifest line versions are missing")
        for version in versions:
            if not isinstance(version, dict) or not isinstance(version.get("version_id"), str):
                raise RuntimeError("manifest version identity is invalid")
            version_ids.append(version["version_id"])
    return sorted(version_ids)


def _find_manifest_version(
    manifest: dict[str, Any],
    case_id: str,
    expected_version_id: Any,
) -> dict[str, Any]:
    lines = manifest.get("lines")
    if not isinstance(lines, dict):
        raise RuntimeError("manifest lines are missing")
    candidates: list[dict[str, Any]] = []
    for line_key, history in lines.items():
        if not isinstance(history, dict) or (
            line_key != case_id and history.get("line_id") != case_id
        ):
            continue
        versions = history.get("versions")
        if not isinstance(versions, list):
            continue
        for version in versions:
            if isinstance(version, dict) and version.get("version_id") == expected_version_id:
                candidates.append(version)
    if len(candidates) != 1:
        raise RuntimeError("manifest version is missing or ambiguous")
    return candidates[0]


def _fault_terminal_evidence(
    case: CasePlan,
    *,
    terminal: dict[str, Any],
    item: dict[str, Any],
    version: dict[str, Any],
    prompt_id: str,
) -> TtsTerminalEvidence:
    if case.action == "cancel-running":
        expected_job_status: Outcome = "cancelled"
        expected_control_code: Literal["cancelled", "timeout"] = "cancelled"
        expected_failure_stage: Literal["timeout"] | None = None
    elif case.action == "timeout":
        expected_job_status = "failed"
        expected_control_code = "timeout"
        expected_failure_stage = "timeout"
    else:
        raise RuntimeError("fault terminal evidence requested for a non-control case")

    metadata = version.get("metadata")
    control_details = metadata.get("control_details") if isinstance(metadata, dict) else None
    cancellation = control_details.get("cancellation") if isinstance(control_details, dict) else None
    failure_stage_matches = (
        isinstance(metadata, dict)
        and (
            metadata.get("failure_stage") == expected_failure_stage
            if expected_failure_stage is not None
            else "failure_stage" not in metadata
        )
    )
    if (
        terminal.get("status") != expected_job_status
        or item.get("status") != expected_job_status
        or version.get("status") != expected_job_status
        or bool(version.get("audio_path"))
        or not isinstance(metadata, dict)
        or metadata.get("control_code") != expected_control_code
        or not failure_stage_matches
        or not isinstance(control_details, dict)
        or control_details.get("prompt_id") != prompt_id
        or not isinstance(cancellation, dict)
        or cancellation.get("prompt_id") != prompt_id
    ):
        raise RuntimeError("fault terminal evidence is incomplete")
    try:
        control = FaultControlEvidence(
            control_code=expected_control_code,
            failure_stage=expected_failure_stage,
            prompt_id=prompt_id,
            initial_state=cancellation.get("initial_state"),
            final_state=cancellation.get("final_state"),
            actions=cancellation.get("actions"),
            duration_seconds=cancellation.get("duration_seconds"),
            converged=cancellation.get("converged"),
        )
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RuntimeError("fault terminal evidence is incomplete") from None
    return TtsTerminalEvidence(
        job_status=expected_job_status,
        item_status=expected_job_status,
        version_status=expected_job_status,
        manifest_version_absent=False,
        version_audio_absent=True,
        control=control,
    )


def _public_manifest_version_id(case_id: str, raw_version_id: str) -> str:
    return hashlib.sha256(f"{case_id}\0{raw_version_id}".encode("utf-8")).hexdigest()


def build_case_plan(
    rounds: int = 10,
    *,
    normal_request_timeout_seconds: dict[Engine, float] | None = None,
) -> tuple[CasePlan, ...]:
    if isinstance(rounds, bool) or rounds != 10:
        raise ValueError("reliability plan requires exactly 10 rounds")
    normal_timeouts = _validate_normal_request_timeouts(
        dict(DEFAULT_NORMAL_REQUEST_TIMEOUT_SECONDS)
        if normal_request_timeout_seconds is None
        else normal_request_timeout_seconds
    )

    plan: list[CasePlan] = []
    for round_number in range(1, rounds + 1):
        for engine in ENGINE_ORDER:
            plan.append(
                CasePlan(
                    case_id=f"steady-{round_number:02d}-{engine}",
                    phase="steady",
                    engine=engine,
                    expected="completed",
                    action="synthesize",
                    request_timeout_seconds=normal_timeouts[engine],
                    convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
                )
            )

    plan.append(
        CasePlan(
            case_id="cancel-queued",
            phase="fault",
            engine="gpt-sovits",
            expected="cancelled",
            action="cancel-queued",
            request_timeout_seconds=30.0,
            convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
        )
    )
    for engine in ENGINE_ORDER:
        plan.extend(
            (
                CasePlan(
                    case_id=f"cancel-running-{engine}",
                    phase="fault",
                    engine=engine,
                    expected="cancelled",
                    action="cancel-running",
                    request_timeout_seconds=30.0,
                    convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
                ),
                CasePlan(
                    case_id=f"recover-cancel-{engine}",
                    phase="recovery",
                    engine=engine,
                    expected="completed",
                    action="synthesize",
                    request_timeout_seconds=normal_timeouts[engine],
                    convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
                ),
            )
        )
    for engine in ENGINE_ORDER:
        plan.extend(
            (
                CasePlan(
                    case_id=f"timeout-{engine}",
                    phase="fault",
                    engine=engine,
                    expected="timeout",
                    action="timeout",
                    request_timeout_seconds=1.0,
                    convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
                ),
                CasePlan(
                    case_id=f"recover-timeout-{engine}",
                    phase="recovery",
                    engine=engine,
                    expected="completed",
                    action="synthesize",
                    request_timeout_seconds=normal_timeouts[engine],
                    convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
                ),
            )
        )
    plan.append(
        CasePlan(
            case_id="terminate-comfyui-indextts",
            phase="fault",
            engine="indextts",
            expected="failed",
            action="terminate-comfyui",
            request_timeout_seconds=30.0,
            convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
        )
    )
    for engine in ENGINE_ORDER:
        plan.append(
            CasePlan(
                case_id=f"restart-{engine}",
                phase="recovery",
                engine=engine,
                expected="completed",
                action="restart-readiness",
                request_timeout_seconds=normal_timeouts[engine],
                convergence_seconds=TERMINAL_CONVERGENCE_SECONDS,
            )
        )
    return tuple(plan)


def required_case_specs(plan: Sequence[CasePlan]) -> tuple[RequiredCase, ...]:
    required = tuple(
        RequiredCase(
            case_id=case.case_id,
            engine=case.engine,
            phase=case.phase,
            expected=case.expected,
        )
        for case in plan
        if case.phase != "steady"
    )
    if len({case.case_id for case in required}) != len(required):
        raise ValueError("required case specifications must be unique")
    return required


def _failed_case_observation_from_probe(
    http_probe: ReliabilityHttpProbe,
    case: CasePlan,
) -> FailedCaseObservation:
    reader = getattr(http_probe, "failed_case_observation", None)
    if not callable(reader):
        return _minimal_failed_case_observation(case)
    try:
        observation = _revalidate_model(reader(case), FailedCaseObservation)
        if (
            observation.action != case.action
            or observation.request_timeout_seconds != case.request_timeout_seconds
            or observation.convergence_seconds != case.convergence_seconds
        ):
            raise ValueError("failed case observation does not match its case plan")
        return observation
    except Exception as exc:
        return _minimal_failed_case_observation(case, secondary_error=exc)


def _strict_failed_case_payload(evidence: CurrentFailedCaseEvidence) -> dict[str, Any]:
    try:
        encoded = evidence.model_dump_json(warnings="error")
        validated = FailedCaseEvidence.model_validate_json(encoded)
        if not isinstance(validated, CurrentFailedCaseEvidence):
            raise ValueError("current failed case evidence lost its version")
        return validated.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError("invalid failed case evidence") from None


def _persist_failed_case_evidence(
    path: Path,
    evidence: CurrentFailedCaseEvidence,
    *,
    case: CasePlan,
) -> None:
    try:
        write_atomic_json(path, _strict_failed_case_payload(evidence))
        return
    except Exception as detail_error:
        try:
            fallback = CurrentFailedCaseEvidence(
                schema_version=2,
                status="failed",
                case_id=evidence.case_id,
                phase=evidence.phase,
                engine=evidence.engine,
                expected=evidence.expected,
                failure=evidence.failure,
                host=evidence.host,
                observation=_minimal_failed_case_observation(
                    case,
                    secondary_error=detail_error,
                ),
            )
            write_atomic_json(path, _strict_failed_case_payload(fallback))
        except Exception:
            return


def execute_reliability_validation(
    fixture: ReliabilityFixture,
    *,
    output_root: Path,
    http_probe: ReliabilityHttpProbe,
    host_probe: ReliabilityHostProbe,
    owned_processes: dict[str, OwnedProcessIdentity],
    allow_lan: bool = False,
    plan: Sequence[CasePlan] | None = None,
) -> "ReliabilityRunSummary":
    """Execute the opt-in matrix through injected HTTP and Windows host probes.

    This controller deliberately owns no service startup or broad cleanup. The
    PowerShell wrapper owns those processes; the probes must return exact,
    evidence-safe observations. Every failure is persisted before it is raised.
    """
    try:
        fixture = _revalidate_model(fixture, ReliabilityFixture)
        owned_processes = {
            label: _revalidate_model(identity, OwnedProcessIdentity)
            for label, identity in owned_processes.items()
        }
        expected_plan = build_case_plan(
            fixture.rounds,
            normal_request_timeout_seconds=fixture.normal_request_timeout_seconds,
        )
        selected_plan = tuple(plan) if plan is not None else expected_plan
        selected_plan = tuple(_revalidate_model(case, CasePlan) for case in selected_plan)
        if selected_plan != expected_plan:
            raise LiveValidationError("case-plan-mismatch", stage="preflight")
    except LiveValidationError:
        raise
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise LiveValidationError("invalid-validator-input", stage="preflight") from None

    output_root = Path(output_root)
    completed_cases: list[CaseEvidence] = []
    failure: LiveValidationError | None = None
    preflight_passed = False
    release_attempted = False
    baseline: BoundarySnapshot | None = None
    gpu_idle_baseline: GpuSnapshot | None = None
    active_case: CasePlan | None = None
    active_host_observation: HostCaseObservation | None = None

    try:
        _require_endpoint_scope(fixture, allow_lan=allow_lan)
        http_preflight = _revalidate_model(http_probe.preflight(fixture), HttpPreflightObservation)
        host_preflight = _revalidate_model(host_probe.preflight(fixture), HostPreflightObservation)
        baseline = host_preflight.boundary
        gpu_idle_baseline = host_preflight.gpu_idle_baseline
        _validate_preflight(fixture, http_preflight, host_preflight, owned_processes)
        preflight_passed = True

        provisional_boundary = _boundary_evidence(baseline, baseline)
        for case in selected_plan:
            active_case = case
            active_host_observation = None
            started_at = host_probe.begin_case(case)
            action_hook: Callable[[], None] | None = None
            if case.action == "terminate-comfyui":
                action_hook = host_probe.terminate_comfyui
            elif case.action == "restart-readiness":
                action_hook = host_probe.restart_comfyui
            try:
                http_observation = http_probe.execute_case(
                    case,
                    fixture,
                    output_root / "audio",
                    action_hook=action_hook,
                )
            finally:
                active_host_observation = _revalidate_model(
                    host_probe.finish_case(case, started_at),
                    HostCaseObservation,
                )
            if not isinstance(http_observation, HttpCaseObservation):
                raise LiveValidationError("invalid-http-case-observation", stage="case")
            evidence = _case_evidence(
                case,
                http_observation,
                active_host_observation,
                provisional_boundary,
            )
            validation = validate_case(evidence, wav_path=http_observation.wav_path)
            if not validation.valid:
                raise LiveValidationError("case-validation-failed", stage="case")
            completed_cases.append(validation.evidence)
            active_case = None
            active_host_observation = None

        release_attempted = True
        http_probe.release()
        http_final = _revalidate_model(http_probe.final_state(), HttpFinalObservation)
        host_final = _revalidate_model(host_probe.final_state(), HostFinalObservation)
        assert gpu_idle_baseline is not None
        _validate_final_state(http_final, host_final, gpu_idle_baseline=gpu_idle_baseline)
        assert baseline is not None
        final_boundary = _boundary_evidence(baseline, host_final.boundary)
        completed_cases = [
            _revalidate_model(case.model_copy(update={"boundary": final_boundary}), CaseEvidence)
            for case in completed_cases
        ]
        if any(not validate_case(case).valid for case in completed_cases):
            raise LiveValidationError("boundary-drift", stage="finalize")
        summary = finalize_run(
            fixture,
            completed_cases,
            required_cases=required_case_specs(selected_plan),
        )
        if summary.status != "passed":
            raise LiveValidationError("matrix-incomplete", stage="finalize")
    except LiveValidationError as exc:
        failure = exc
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError, httpx.HTTPError):
        failure = LiveValidationError(
            "preflight-observation-failed" if not preflight_passed else "case-execution-failed",
            stage="preflight" if not preflight_passed else "case",
        )
    finally:
        if preflight_passed and not release_attempted:
            try:
                http_probe.release()
            except Exception:
                if failure is None:
                    failure = LiveValidationError("runtime-release-failed", stage="finalize")

    if failure is not None:
        try:
            summary = finalize_run(
                fixture,
                completed_cases,
                required_cases=required_case_specs(selected_plan),
            )
        except Exception:
            _persist_failure_marker(output_root, failure)
            raise failure
        for completed_case in summary.cases:
            try:
                write_atomic_json(
                    output_root / "cases" / f"{completed_case.case_id}.json",
                    completed_case.model_dump(mode="json"),
                )
            except Exception:
                pass
        if active_case is not None:
            try:
                failed_host = _failed_case_host_observation(active_host_observation)
                failed_observation = _failed_case_observation_from_probe(
                    http_probe,
                    active_case,
                )
                failed_case = CurrentFailedCaseEvidence(
                    schema_version=2,
                    status="failed",
                    case_id=active_case.case_id,
                    phase=active_case.phase,
                    engine=active_case.engine,
                    expected=active_case.expected,
                    failure=FailureMarker(code=failure.code, stage=failure.stage),
                    host=failed_host,
                    observation=failed_observation,
                )
            except Exception as detail_error:
                failed_case = CurrentFailedCaseEvidence(
                    schema_version=2,
                    status="failed",
                    case_id=active_case.case_id,
                    phase=active_case.phase,
                    engine=active_case.engine,
                    expected=active_case.expected,
                    failure=FailureMarker(code=failure.code, stage=failure.stage),
                    host=None,
                    observation=_minimal_failed_case_observation(
                        active_case,
                        secondary_error=detail_error,
                    ),
                )
            _persist_failed_case_evidence(
                output_root / "cases" / f"{active_case.case_id}.json",
                failed_case,
                case=active_case,
            )
        _persist_failure_marker(output_root, failure)
        try:
            write_atomic_json(output_root / "reliability-summary.json", summary)
        except Exception:
            pass
        raise failure

    _transition_public_terminal_markers(output_root, stage="finalize")
    for case in summary.cases:
        write_atomic_json(output_root / "cases" / f"{case.case_id}.json", case.model_dump(mode="json"))
    write_atomic_json(output_root / "reliability-summary.json", summary)
    return summary


def execute_reliability_preflight(
    fixture: ReliabilityFixture,
    *,
    output_root: Path,
    http_probe: ReliabilityHttpProbe,
    host_probe: ReliabilityHostProbe,
    owned_processes: dict[str, OwnedProcessIdentity],
    allow_lan: bool = False,
) -> None:
    failure: LiveValidationError | None = None
    try:
        _execute_reliability_preflight_success(
            fixture,
            output_root=output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=owned_processes,
            allow_lan=allow_lan,
        )
    except LiveValidationError as exc:
        failure = exc
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        RuntimeError,
        httpx.HTTPError,
    ):
        failure = LiveValidationError(
            "preflight-observation-failed",
            stage="preflight",
        )
    else:
        return
    assert failure is not None
    _persist_failure_marker(Path(output_root), failure)
    raise failure


def _execute_reliability_preflight_success(
    fixture: ReliabilityFixture,
    *,
    output_root: Path,
    http_probe: ReliabilityHttpProbe,
    host_probe: ReliabilityHostProbe,
    owned_processes: dict[str, OwnedProcessIdentity],
    allow_lan: bool = False,
) -> None:
    fixture = _revalidate_model(fixture, ReliabilityFixture)
    _require_endpoint_scope(fixture, allow_lan=allow_lan)
    http = _revalidate_model(http_probe.preflight(fixture), HttpPreflightObservation)
    host = _revalidate_model(host_probe.preflight(fixture), HostPreflightObservation)
    _validate_preflight(fixture, http, host, owned_processes)
    document = {
        "status": "passed",
        "resources": [
            {
                "engine": item.engine,
                "ready": item.ready,
                "resource_id_hash": hashlib.sha256(
                    item.resource_id.encode("utf-8")
                ).hexdigest(),
            }
            for item in sorted(http.resources, key=lambda item: (item.engine, item.resource_id))
        ],
        "queue": http.queue.model_dump(mode="json"),
        "port_owners": {
            str(port): identity.model_dump(mode="json")
            for port, identity in sorted(host.port_owners.items())
        },
        "gpu_idle_baseline": host.gpu_idle_baseline.model_dump(mode="json"),
        "boundary": {
            "aggregate_hash": host.boundary.aggregate_hash,
            "private_registry_hash": host.boundary.private_registry_hash,
            "reference_hashes": host.boundary.reference_hashes,
            "repositories": [item.model_dump(mode="json") for item in host.boundary.repositories],
        },
    }
    output_root = Path(output_root)
    _publish_public_terminal_marker(
        output_root,
        marker_name="preflight.json",
        document=document,
        stage="preflight",
    )


def _persist_failure_marker(output_root: Path, failure: LiveValidationError) -> None:
    failure.failure_persistence_attempted = True
    if failure.preserve_terminal_marker:
        return
    for _attempt in range(2):
        try:
            _publish_public_terminal_marker(
                Path(output_root),
                marker_name="failure.json",
                document={"code": failure.code, "stage": failure.stage},
                stage=failure.stage,
            )
            return
        except Exception:
            continue


def _publish_public_terminal_marker(
    output_root: Path,
    *,
    marker_name: Literal["failure.json", "preflight.json"],
    document: dict[str, Any],
    stage: Literal["preflight", "case", "finalize"],
) -> None:
    retired: _RetiredTerminalCohort | None = None
    marker_path = Path(output_root) / marker_name
    publication_attempted = False
    retirement_cleanup_attempted = False
    published_marker: _PreparedTerminalArtifact | None = None
    expected_marker = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    try:
        with _TERMINAL_TRANSITION_THREAD_LOCK:
            retired = _transition_public_terminal_markers(
                Path(output_root),
                stage=stage,
                retained_marker=marker_name,
            )
            if marker_name == "preflight.json":
                _assert_passed_root_clear(Path(output_root))
            if retired is not None:
                retired_cases = retired.root / "cases"
                if retired_cases.exists():
                    _assert_exact_case_members(
                        Path(output_root), retired_cases, retired.case_artifacts
                    )
                elif retired.case_artifacts:
                    raise OSError("terminal cohort cases disappeared before cleanup")
                retirement_cleanup_attempted = True
                shutil.rmtree(retired.root)
                if retired.root.exists() or retired.root.is_symlink():
                    raise OSError("terminal cohort quarantine cleanup is incomplete")
                retired = None
            publication_attempted = True
            if marker_name == "preflight.json":
                write_atomic_json(marker_path, document, replace_existing=False)
            else:
                write_atomic_json(marker_path, document)
            published_marker = _capture_owned_terminal_marker(
                Path(output_root), marker_path, expected_marker
            )
            if marker_name == "preflight.json":
                _assert_passed_root_clear(Path(output_root), marker_path=marker_path)
    except Exception as error:
        preserve_marker = False
        if marker_name == "preflight.json" and publication_attempted:
            if published_marker is not None:
                preserve_marker = not _unlink_owned_terminal_marker(
                    Path(output_root), published_marker
                )
            else:
                try:
                    _safe_terminal_path(Path(output_root), marker_path, directory=False)
                    preserve_marker = True
                except FileNotFoundError:
                    pass
        if not retirement_cleanup_attempted:
            _rollback_retired_terminal_cohort(Path(output_root), retired)
        if isinstance(error, LiveValidationError):
            error.preserve_terminal_marker |= preserve_marker
            raise
        transition_error = LiveValidationError(
            "public-marker-transition-failed",
            stage=stage,
        )
        transition_error.preserve_terminal_marker = preserve_marker
        raise transition_error from None


def _transition_public_terminal_markers(
    output_root: Path,
    *,
    stage: Literal["preflight", "case", "finalize"],
    retained_marker: Literal["failure.json", "preflight.json"] | None = None,
) -> _RetiredTerminalCohort | None:
    retired: _RetiredTerminalCohort | None = None
    try:
        markers: list[tuple[Literal["failure", "preflight"], Path]] = []
        for kind in ("failure", "preflight"):
            marker = Path(output_root) / f"{kind}.json"
            try:
                _safe_terminal_path(Path(output_root), marker, directory=False)
            except FileNotFoundError:
                continue
            markers.append((kind, marker))
        cohort = (
            _prepare_terminal_cohort_archive(Path(output_root))
            if retained_marker == "preflight.json"
            else None
        )
        for kind, marker in markers:
            _archive_public_terminal_marker(
                Path(output_root),
                marker=marker,
                kind=kind,
            )
        if cohort is not None:
            _archive_terminal_cohort(Path(output_root), cohort)
        if cohort is not None:
            retired = _retire_terminal_cohort(Path(output_root), cohort)
        for _kind, marker in markers:
            marker.unlink()
    except Exception:
        _rollback_retired_terminal_cohort(Path(output_root), retired)
        raise LiveValidationError(
            "public-marker-transition-failed",
            stage=stage,
        ) from None
    return retired


@dataclass(frozen=True)
class _PreparedTerminalArtifact:
    path: Path
    identity: tuple[int, int, int, int, int]
    sha256: str


def _capture_owned_terminal_marker(
    output_root: Path, marker_path: Path, expected: bytes
) -> _PreparedTerminalArtifact:
    content = _read_bounded_terminal_artifact(
        marker_path, maximum_bytes=MAX_FAILED_CASE_JSON_BYTES
    )
    if content != expected:
        raise OSError("published terminal marker was replaced concurrently")
    return _PreparedTerminalArtifact(
        path=marker_path,
        identity=_safe_terminal_path(output_root, marker_path, directory=False),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _unlink_owned_terminal_marker(
    output_root: Path, marker: _PreparedTerminalArtifact
) -> bool:
    try:
        _assert_prepared_terminal_artifact(output_root, marker)
        marker.path.unlink()
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class _PreparedTerminalCohort:
    archive: dict[str, Any] | None
    summary_path: Path | None
    case_root: Path | None
    case_paths: tuple[Path, ...]
    summary_artifact: _PreparedTerminalArtifact | None
    case_root_identity: tuple[int, int, int, int, int] | None
    case_artifacts: tuple[_PreparedTerminalArtifact, ...]


@dataclass(frozen=True)
class _RetiredTerminalCohort:
    root: Path
    case_artifacts: tuple[_PreparedTerminalArtifact, ...]


def _terminal_path_identity(path: Path, *, directory: bool) -> tuple[int, int, int, int, int]:
    observed = path.lstat()
    if stat.S_ISLNK(observed.st_mode) or getattr(observed, "st_file_attributes", 0) & 0x400:
        raise OSError("terminal evidence path must not be a reparse point")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(observed.st_mode):
        raise OSError("terminal evidence path has the wrong object type")
    return (observed.st_dev, observed.st_ino, observed.st_mode, observed.st_size, observed.st_mtime_ns)


def _safe_terminal_path(output_root: Path, path: Path, *, directory: bool, create: bool = False) -> tuple[int, int, int, int, int]:
    root = Path(os.path.abspath(output_root))
    target = Path(os.path.abspath(path))
    relative = target.relative_to(root)
    _terminal_path_identity(root, directory=True)
    current = root
    for index, component in enumerate(relative.parts):
        current /= component
        expected_directory = index < len(relative.parts) - 1 or directory
        try:
            _terminal_path_identity(current, directory=expected_directory)
        except FileNotFoundError:
            if not create or not expected_directory:
                raise
            current.mkdir()
            _terminal_path_identity(current, directory=True)
    target.resolve(strict=True).relative_to(root.resolve(strict=True))
    return _terminal_path_identity(target, directory=directory)


def _read_bounded_terminal_artifact(path: Path, *, maximum_bytes: int) -> bytes:
    before_stat = path.lstat()
    before = _terminal_path_identity(path, directory=False)
    if before_stat.st_size < 0 or before_stat.st_size > maximum_bytes:
        raise OSError("terminal cohort artifact exceeds its byte bound")
    content = path.read_bytes()
    after = _terminal_path_identity(path, directory=False)
    if (
        len(content) != before_stat.st_size
        or after != before
    ):
        raise OSError("terminal cohort artifact changed while being read")
    return content


def _assert_prepared_terminal_artifact(
    output_root: Path, artifact: _PreparedTerminalArtifact | None, *, path: Path | None = None
) -> None:
    if artifact is None:
        raise OSError("terminal cohort artifact preparation is incomplete")
    checked = artifact.path if path is None else path
    if _safe_terminal_path(output_root, checked, directory=False) != artifact.identity:
        raise OSError("terminal cohort artifact identity changed before mutation")
    maximum_bytes = (
        MAX_RELIABILITY_SUMMARY_JSON_BYTES
        if checked.name == "reliability-summary.json"
        else MAX_FAILED_CASE_JSON_BYTES
    )
    content = _read_bounded_terminal_artifact(checked, maximum_bytes=maximum_bytes)
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise OSError("terminal cohort artifact hash changed before mutation")


def _assert_exact_case_members(
    output_root: Path,
    case_root: Path,
    artifacts: tuple[_PreparedTerminalArtifact, ...],
) -> None:
    entries = tuple(sorted(case_root.iterdir(), key=lambda item: item.name))
    if tuple(path.name for path in entries) != tuple(
        artifact.path.name for artifact in artifacts
    ):
        raise OSError("terminal cohort cases membership changed before mutation")
    for entry, artifact in zip(entries, artifacts, strict=True):
        _assert_prepared_terminal_artifact(output_root, artifact, path=entry)


def _retire_terminal_cohort(
    output_root: Path,
    cohort: _PreparedTerminalCohort,
) -> _RetiredTerminalCohort:
    quarantine_root = (
        output_root / "reliability-temp-test" / "terminal-cohort-quarantine"
    )
    _safe_terminal_path(output_root, quarantine_root, directory=True, create=True)
    transaction_root = quarantine_root / secrets.token_hex(16)
    transaction_root.mkdir()
    retired = _RetiredTerminalCohort(
        root=transaction_root,
        case_artifacts=cohort.case_artifacts,
    )
    try:
        if cohort.summary_artifact is not None:
            _assert_prepared_terminal_artifact(output_root, cohort.summary_artifact)
            saved_summary = transaction_root / "reliability-summary.json"
            os.replace(cohort.summary_artifact.path, saved_summary)
            _assert_prepared_terminal_artifact(
                output_root, cohort.summary_artifact, path=saved_summary
            )
        if cohort.case_root is not None:
            _safe_terminal_path(output_root, cohort.case_root, directory=True)
            if _terminal_path_identity(cohort.case_root, directory=True) != cohort.case_root_identity:
                raise OSError("terminal cohort cases root changed before mutation")
            _assert_exact_case_members(
                output_root, cohort.case_root, cohort.case_artifacts
            )
            saved_cases = transaction_root / "cases"
            os.replace(cohort.case_root, saved_cases)
            if _terminal_path_identity(saved_cases, directory=True) != cohort.case_root_identity:
                raise OSError("terminal cohort cases root changed during mutation")
            _assert_exact_case_members(
                output_root, saved_cases, cohort.case_artifacts
            )
    except Exception:
        _rollback_retired_terminal_cohort(output_root, retired)
        raise
    return retired


def _rollback_retired_terminal_cohort(
    output_root: Path,
    retired: _RetiredTerminalCohort | None,
) -> None:
    if retired is None:
        return
    for saved, active in (
        (retired.root / "cases", output_root / "cases"),
        (retired.root / "reliability-summary.json", output_root / "reliability-summary.json"),
    ):
        saved_exists = saved.exists() or saved.is_symlink()
        active_exists = active.exists() or active.is_symlink()
        if saved_exists and not active_exists:
            try:
                os.replace(saved, active)
            except OSError:
                pass


def _assert_passed_root_clear(output_root: Path, *, marker_path: Path | None = None) -> None:
    forbidden = (output_root / "failure.json", output_root / "reliability-summary.json", output_root / "cases")
    if any(path.exists() or path.is_symlink() for path in forbidden):
        raise OSError("passed terminal marker conflicts with active failed evidence")
    if marker_path is not None:
        _safe_terminal_path(output_root, marker_path, directory=False)


def _canonical_public_model_json(content: bytes, model: BaseModel) -> bool:
    try:
        document = json.loads(content)
        canonical_input = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        canonical_model = json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (UnicodeError, ValueError, TypeError, AttributeError, RecursionError):
        return False
    return canonical_input == canonical_model


def _prepare_terminal_cohort_archive(output_root: Path) -> _PreparedTerminalCohort | None:
    summary_path = output_root / "reliability-summary.json"
    summary: ReliabilityRunSummary | None = None
    summary_content: bytes | None = None
    summary_artifact: _PreparedTerminalArtifact | None = None
    if summary_path.is_symlink():
        raise OSError("terminal cohort summary must not be a link")
    if summary_path.exists():
        _safe_terminal_path(output_root, summary_path, directory=False)
        summary_content = _read_bounded_terminal_artifact(
            summary_path,
            maximum_bytes=MAX_RELIABILITY_SUMMARY_JSON_BYTES,
        )
        summary_artifact = _PreparedTerminalArtifact(
            path=summary_path,
            identity=_terminal_path_identity(summary_path, directory=False),
            sha256=hashlib.sha256(summary_content).hexdigest(),
        )
        summary = read_reliability_summary(summary_content)
    else:
        summary_path = None

    case_root = output_root / "cases"
    case_paths: list[Path] = []
    case_root_identity: tuple[int, int, int, int, int] | None = None
    case_artifacts: list[_PreparedTerminalArtifact] = []
    if case_root.is_symlink():
        raise OSError("terminal cohort cases root must not be a link")
    if case_root.exists():
        _safe_terminal_path(output_root, case_root, directory=True)
        case_root_identity = _terminal_path_identity(case_root, directory=True)
        entries = sorted(case_root.iterdir(), key=lambda item: item.name)
        if len(entries) > MAX_TERMINAL_COHORT_CASES:
            raise OSError("terminal cohort case count exceeds its bound")
        for case_path in entries:
            if (
                re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}\.json", case_path.name)
                is None
            ):
                raise OSError("terminal cohort case name is invalid")
            case_paths.append(case_path)
    else:
        case_root = None

    if summary is None and not case_paths:
        if case_root is None:
            return None
        return _PreparedTerminalCohort(
            archive=None,
            summary_path=None,
            case_root=case_root,
            case_paths=(),
            summary_artifact=None,
            case_root_identity=case_root_identity,
            case_artifacts=(),
        )
    if summary is None:
        raise OSError("terminal cohort cases require a reliability summary")

    summary_cases = {case.case_id: case for case in summary.cases}
    completed_case_ids: set[str] = set()
    commitments: list[dict[str, Any]] = []
    for case_path in case_paths:
        _safe_terminal_path(output_root, case_path, directory=False)
        content = _read_bounded_terminal_artifact(
            case_path,
            maximum_bytes=MAX_FAILED_CASE_JSON_BYTES,
        )
        case_artifacts.append(
            _PreparedTerminalArtifact(
                path=case_path,
                identity=_terminal_path_identity(case_path, directory=False),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
        case_kind: Literal["completed", "failed"]
        case_schema: Literal["case-evidence", "legacy-failed", "current-failed-v2"]
        try:
            failed_case = FailedCaseEvidence.model_validate_json(content)
        except ValidationError:
            try:
                completed_case = CaseEvidence.model_validate_json(content)
            except ValidationError:
                raise OSError("terminal cohort case failed formal validation") from None
            if not _canonical_public_model_json(content, completed_case):
                raise OSError("terminal cohort completed case is not canonical")
            expected_case = summary_cases.get(completed_case.case_id)
            if expected_case is None or expected_case != completed_case:
                raise OSError("terminal cohort completed case is not bound to its summary")
            completed_case_ids.add(completed_case.case_id)
            case_id = completed_case.case_id
            case_kind = "completed"
            case_schema = "case-evidence"
        else:
            if summary.status != "failed":
                raise OSError("passed terminal cohort contains a failed case")
            if not _canonical_public_model_json(content, failed_case):
                raise OSError("terminal cohort failed case is not canonical")
            case_id = failed_case.case_id
            case_kind = "failed"
            case_schema = (
                "current-failed-v2"
                if isinstance(failed_case, CurrentFailedCaseEvidence)
                else "legacy-failed"
            )
        if case_path.stem != case_id:
            raise OSError("terminal cohort case filename does not match its document")
        commitments.append(
            {
                "kind": case_kind,
                "case_schema": case_schema,
                "case_id_sha256": _sha256_text(case_id),
                "artifact_sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if completed_case_ids != set(summary_cases):
        raise OSError("terminal cohort summary cases are incomplete")
    commitments.sort(
        key=lambda item: (
            item["case_id_sha256"],
            item["kind"],
            item["case_schema"],
            item["artifact_sha256"],
        )
    )

    archive = {
        "schema_version": 1,
        "kind": "reliability-terminal-cohort",
        "document_status": "validated",
        "summary": {
            "artifact_sha256": hashlib.sha256(summary_content or b"").hexdigest(),
            "status": summary.status,
            "completed_case_count": len(summary.cases),
        },
        "cases": commitments,
    }
    validated_archive = _ArchivedTerminalCohort.model_validate(archive)
    return _PreparedTerminalCohort(
        archive=validated_archive.model_dump(mode="json"),
        summary_path=summary_path,
        case_root=case_root,
        case_paths=tuple(case_paths),
        summary_artifact=summary_artifact,
        case_root_identity=case_root_identity,
        case_artifacts=tuple(case_artifacts),
    )


def _archive_terminal_cohort(
    output_root: Path,
    cohort: _PreparedTerminalCohort,
) -> None:
    if cohort.archive is None:
        return
    archive_root = output_root / "history" / "terminal-cohorts"
    _safe_terminal_path(output_root, archive_root, directory=True, create=True)
    archive_digest = _sha256_document(cohort.archive)
    archive_path = archive_root / f"cohort-{archive_digest}.json"
    try:
        write_atomic_json(archive_path, cohort.archive, replace_existing=False)
    except OSError:
        if archive_path.is_symlink() or not archive_path.is_file():
            raise
        existing = _read_bounded_terminal_artifact(
            archive_path,
            maximum_bytes=MAX_FAILED_CASE_JSON_BYTES,
        )
        try:
            validated = _ArchivedTerminalCohort.model_validate_json(existing)
        except ValidationError:
            raise OSError("terminal cohort archive conflicts with prior history") from None
        if validated.model_dump(mode="json") != cohort.archive:
            raise OSError("terminal cohort archive conflicts with prior history")


def _archive_public_terminal_marker(
    output_root: Path,
    *,
    marker: Path,
    kind: Literal["failure", "preflight"],
) -> None:
    maximum_document_bytes = 1024 * 1024
    digest = hashlib.sha256()
    document_bytes: bytes | None = None
    with marker.open("rb") as handle:
        size = marker.stat().st_size
        if size <= maximum_document_bytes:
            document_bytes = handle.read(maximum_document_bytes + 1)
            if len(document_bytes) > maximum_document_bytes:
                document_bytes = None
            else:
                digest.update(document_bytes)
        if document_bytes is None:
            handle.seek(0)
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
    sha256 = digest.hexdigest()
    archived_document, document_status = _validated_archived_terminal_document(
        kind,
        document_bytes,
    )
    archive: dict[str, Any] = {
        "kind": kind,
        "sha256": sha256,
        "document_status": document_status,
    }
    if archived_document is not None:
        archive["document"] = archived_document
    archive_root = Path(output_root) / "history" / "terminal-markers"
    _safe_terminal_path(output_root, archive_root, directory=True, create=True)
    archive_stem = f"{kind}-{sha256[:16]}-{secrets.token_hex(16)}"
    for collision_index in range(32):
        collision_suffix = "" if collision_index == 0 else f"-{collision_index:02d}"
        archive_path = archive_root / f"{archive_stem}{collision_suffix}.json"
        try:
            write_atomic_json(archive_path, archive, replace_existing=False)
            break
        except OSError:
            if archive_path.exists():
                continue
            raise
    else:
        raise OSError("terminal marker archive namespace exhausted")


def _validated_archived_terminal_document(
    kind: Literal["failure", "preflight"],
    document_bytes: bytes | None,
) -> tuple[dict[str, Any] | None, Literal["validated", "invalid", "redacted"]]:
    if document_bytes is None:
        return None, "redacted"
    try:
        document = json.loads(document_bytes.decode("utf-8-sig"))
        _assert_public_evidence(document)
    except (UnicodeError, ValueError, TypeError, AttributeError, RecursionError):
        return None, "redacted"
    try:
        if kind == "failure":
            validated = FailureMarker.model_validate(document)
        else:
            validated = _PublicPreflightMarker.model_validate_json(document_bytes)
        return validated.model_dump(mode="json"), "validated"
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        return None, "invalid"


def _require_endpoint_scope(fixture: ReliabilityFixture, *, allow_lan: bool) -> None:
    if allow_lan:
        return
    for base_url in fixture.base_urls.values():
        try:
            hostname = urlsplit(base_url).hostname
        except ValueError:
            hostname = None
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise LiveValidationError("non-loopback-endpoint", stage="preflight")


def _validate_preflight(
    fixture: ReliabilityFixture,
    http: HttpPreflightObservation,
    host: HostPreflightObservation,
    owned_processes: dict[str, OwnedProcessIdentity],
) -> None:
    expected_resources = {
        (engine, fixture.resources[engine].resource_id, True)
        for engine in ENGINE_ORDER
    }
    observed_resources = {
        (resource.engine, resource.resource_id, resource.ready)
        for resource in http.resources
    }
    if len(http.resources) != len(expected_resources) or observed_resources != expected_resources:
        raise LiveValidationError("resource-readiness", stage="preflight")
    if not _queue_is_idle(http.queue):
        raise LiveValidationError("initial-queue-not-idle", stage="preflight")
    expected_owned = {"tts-more", "comfyui"}
    if set(owned_processes) != expected_owned:
        raise LiveValidationError("owned-process-set", stage="preflight")
    labels_by_url = {"tts_more": "tts-more", "comfyui": "comfyui"}
    for url_label, process_label in labels_by_url.items():
        parsed = urlsplit(fixture.base_urls[url_label])
        port = parsed.port
        if port is None:
            raise LiveValidationError("endpoint-port-missing", stage="preflight")
        owner = host.port_owners.get(port)
        if owner is not None and owner != owned_processes[process_label]:
            raise LiveValidationError("port-owner-mismatch", stage="preflight")
    _validate_boundary_snapshot(host.boundary)


def _validate_final_state(
    http: HttpFinalObservation,
    host: HostFinalObservation,
    *,
    gpu_idle_baseline: GpuSnapshot,
) -> None:
    if not http.runtime_released:
        raise LiveValidationError("runtime-release-failed", stage="finalize")
    if not _queue_is_idle(http.queue):
        raise LiveValidationError("final-queue-not-idle", stage="finalize")
    if not host.owned_processes_stopped or not host.temp_paths_removed:
        raise LiveValidationError("final-cleanup-incomplete", stage="finalize")
    if not _gpu_recovered_to_idle_baseline(gpu_idle_baseline, host.gpu_after_release):
        raise LiveValidationError("final-gpu-not-recovered", stage="finalize")
    _validate_boundary_snapshot(host.boundary)


def _gpu_recovered_to_idle_baseline(baseline: GpuSnapshot, final: GpuSnapshot) -> bool:
    try:
        values = (baseline.used_mib, baseline.free_mib, final.used_mib, final.free_mib)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            return False
        baseline_total = baseline.used_mib + baseline.free_mib
        final_total = final.used_mib + final.free_mib
        return (
            final.used_mib - baseline.used_mib <= 1024
            and baseline.free_mib - final.free_mib <= 1024
            and abs(final_total - baseline_total) <= 1024
        )
    except (AttributeError, TypeError):
        return False


def _queue_is_idle(queue: QueueSnapshot) -> bool:
    return (
        queue.tts_queued == 0
        and queue.tts_running == 0
        and not queue.comfy_pending_prompt_ids
        and not queue.comfy_running_prompt_ids
    )


def _validate_boundary_snapshot(boundary: BoundarySnapshot) -> None:
    labels = [repository.label for repository in boundary.repositories]
    if sorted(labels) != sorted(REQUIRED_BOUNDARY_LABELS) or len(labels) != len(set(labels)):
        raise LiveValidationError("boundary-observation-incomplete", stage="preflight")
    if not boundary.reference_hashes:
        raise LiveValidationError("boundary-observation-incomplete", stage="preflight")


def _boundary_evidence(before: BoundarySnapshot, after: BoundarySnapshot) -> BoundaryEvidence:
    return BoundaryEvidence(
        before_hash=before.aggregate_hash,
        after_hash=after.aggregate_hash,
        private_registry_hash=before.private_registry_hash,
        reference_hashes=before.reference_hashes,
        repositories_before=before.repositories,
        repositories_after=after.repositories,
        private_registry_before_hash=before.private_registry_hash,
        private_registry_after_hash=after.private_registry_hash,
        reference_hashes_before=before.reference_hashes,
        reference_hashes_after=after.reference_hashes,
    )


def _case_evidence(
    case: CasePlan,
    http: HttpCaseObservation,
    host: HostCaseObservation,
    boundary: BoundaryEvidence,
) -> CaseEvidence:
    return CaseEvidence(
        case_id=case.case_id,
        phase=case.phase,
        engine=case.engine,
        expected=case.expected,
        actual=http.actual,
        job_id=http.job_id,
        prompt_id=http.prompt_id,
        version_id=http.version_id,
        prompt_submitted=http.prompt_submitted,
        tts_more=http.tts_more,
        termination=http.termination,
        started_at=host.started_at,
        finished_at=host.finished_at,
        audio=None,
        cleanup=host.cleanup,
        processes=host.processes,
        comfyui=http.comfyui,
        gpu_before=host.gpu_before,
        gpu_peak=host.gpu_peak,
        gpu_after=host.gpu_after,
        boundary=boundary,
    )


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


def read_reliability_summary(content: bytes) -> ReliabilityRunSummary:
    if not isinstance(content, bytes) or len(content) > MAX_RELIABILITY_SUMMARY_JSON_BYTES:
        raise ValueError("reliability summary is invalid")
    try:
        document = json.loads(content)
        summary = ReliabilityRunSummary.model_validate_json(content, strict=False)
        canonical_input = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        canonical_model = json.dumps(
            summary.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical_input != canonical_model:
            raise ValueError("reliability summary is not canonical")
        return summary
    except Exception:
        raise ValueError("reliability summary is invalid") from None


class _ArchivedTerminalSummaryCommitment(_StrictModel):
    artifact_sha256: SHA256
    status: Literal["passed", "failed"]
    completed_case_count: StrictInt = Field(ge=0, le=MAX_TERMINAL_COHORT_CASES)


class _ArchivedTerminalCaseCommitment(_StrictModel):
    kind: Literal["completed", "failed"]
    case_schema: Literal["case-evidence", "legacy-failed", "current-failed-v2"]
    case_id_sha256: SHA256
    artifact_sha256: SHA256


class _ArchivedTerminalCohort(_StrictModel):
    schema_version: StrictInt = Field(ge=1, le=1)
    kind: Literal["reliability-terminal-cohort"]
    document_status: Literal["validated"]
    summary: _ArchivedTerminalSummaryCommitment
    cases: Annotated[
        list[_ArchivedTerminalCaseCommitment],
        Field(max_length=MAX_TERMINAL_COHORT_CASES),
    ]

    @field_validator("cases")
    @classmethod
    def _ordered_unique_cases(
        cls,
        value: list[_ArchivedTerminalCaseCommitment],
    ) -> list[_ArchivedTerminalCaseCommitment]:
        ordered = sorted(
            value,
            key=lambda item: (
                item.case_id_sha256,
                item.kind,
                item.case_schema,
                item.artifact_sha256,
            ),
        )
        if value != ordered or len({item.case_id_sha256 for item in value}) != len(value):
            raise ValueError("terminal cohort case commitments must be ordered and unique")
        return value


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
        if case.case_id == "cancel-queued":
            terminal = case.tts_more
            return (
                case.actual == "cancelled"
                and case.prompt_submitted is False
                and case.prompt_id is None
                and case.version_id is None
                and case.comfyui is None
                and terminal is not None
                and terminal.job_status == "cancelled"
                and terminal.item_status == "cancelled"
                and terminal.version_status is None
                and terminal.manifest_version_absent is True
                and terminal.version_audio_absent is True
                and terminal.control is None
                and case.termination is None
            )
        if case.case_id == "terminate-comfyui-indextts":
            terminal = case.tts_more
            termination = case.termination
            return (
                case.actual == "failed"
                and case.prompt_submitted is True
                and case.prompt_id is not None
                and case.version_id is not None
                and case.comfyui is None
                and terminal is not None
                and terminal.job_status == "failed"
                and terminal.item_status == "failed"
                and terminal.version_status == "failed"
                and terminal.manifest_version_absent is False
                and terminal.version_audio_absent is True
                and terminal.control is None
                and termination is not None
                and termination.endpoint_unavailable is True
                and termination.prompt_id == case.prompt_id
                and termination.queue_before_prompt_ids.count(case.prompt_id) == 1
                and termination.manifest_audio_absent is True
            )
        terminal = case.tts_more
        if case.phase == "fault" and case.expected in {"cancelled", "timeout"}:
            expected_terminal_status = "cancelled" if case.expected == "cancelled" else "failed"
            expected_control_code = "cancelled" if case.expected == "cancelled" else "timeout"
            expected_failure_stage = None if case.expected == "cancelled" else "timeout"
            control = terminal.control if terminal is not None else None
            if not (
                case.actual == case.expected
                and case.audio is None
                and terminal is not None
                and terminal.job_status == expected_terminal_status
                and terminal.item_status == expected_terminal_status
                and terminal.version_status == expected_terminal_status
                and terminal.manifest_version_absent is False
                and terminal.version_audio_absent is True
                and control is not None
                and control.control_code == expected_control_code
                and control.failure_stage == expected_failure_stage
                and control.prompt_id == case.prompt_id
                and control.initial_state == "running"
                and control.final_state == "interrupted"
                and control.actions == ["interrupt"]
                and control.converged is True
            ):
                return False
        queue = case.comfyui
        return (
            case.prompt_submitted is True
            and case.prompt_id is not None
            and queue is not None
            and queue.queue_empty is True
            and queue.history_present is True
            and queue.prompt_id == case.prompt_id
            and _prompt_ids_are_unique(queue.queue_before_prompt_ids)
            and queue.queue_before_prompt_ids.count(case.prompt_id) == 1
            and _prompt_ids_are_unique(queue.queue_after_prompt_ids)
            and not queue.queue_after_prompt_ids
            and _prompt_ids_are_unique(queue.history_prompt_ids)
            and queue.history_prompt_ids.count(case.prompt_id) == 1
            and queue.terminal_history_status == case.actual
            and case.termination is None
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


def write_atomic_json(
    path: Path,
    payload: ReliabilityRunSummary | dict[str, Any],
    *,
    replace_existing: bool = True,
) -> None:
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
    if had_prior and not replace_existing:
        _best_effort_unlink(temporary)
        raise OSError("atomic evidence publication conflict; prior evidence is unchanged") from None
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


def _write_private_json_atomic(path: Path, document: dict[str, Any]) -> None:
    descriptor, temporary = _reserve_owned_file(path, suffix="private.tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        _best_effort_unlink(temporary)
        raise


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


def _assert_public_evidence(value: Any, active: set[int] | None = None) -> None:
    if active is None:
        active = set()
    tracked = isinstance(value, (BaseModel, dict, list, tuple, set))
    identity = id(value)
    if tracked and identity in active:
        raise ValueError("unsafe evidence")
    if tracked:
        active.add(identity)
    try:
        if isinstance(value, BaseModel):
            _assert_public_evidence(vars(value), active)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if _is_sensitive_key(key_text) and _contains_private_value(item):
                    raise ValueError("unsafe evidence")
                _assert_public_string(key_text)
                _assert_public_evidence(item, active)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _assert_public_evidence(item, active)
            return
        if isinstance(value, str):
            _assert_public_string(value)
    finally:
        if tracked:
            active.remove(identity)


def _assert_public_string(value: str) -> None:
    lowered = value.lower()
    contains_path = bool(
        re.search(r"(?i)(?<![a-z0-9])[a-z]:[\\/]", value)
        or re.search(r"\\\\[^\\/\s]+[\\/][^\\/\s]+", value)
        or re.search(r"(?i)\bfile://", value)
        or re.search(r"(?:^|[\s\"'=(:,;])/(?!/)[^\s]+", value)
    )
    contains_secret = bool(
        _contains_uri_userinfo(value)
        or
        re.search(
            r"(?:\bbearer\s+\S+|\b(?:access[_-]?key|access[_-]?token|api[_-]?key|authorization|client[_-]?secret|password|private[_-]?key|refresh[_-]?token|secret|token)\s*[:=]\s*\S+)",
            lowered,
        )
    )
    if contains_path or contains_secret:
        raise ValueError("unsafe evidence")


def _contains_uri_userinfo(value: str) -> bool:
    scheme_matches = list(_SCHEME_URI_PATTERN.finditer(value))
    scheme_spans = [match.span() for match in scheme_matches]
    candidates = [match.group(0) for match in scheme_matches]
    candidates.extend(
        match.group(0)
        for match in _NETWORK_URI_PATTERN.finditer(value)
        if not any(start <= match.start() < end for start, end in scheme_spans)
    )
    for candidate in candidates:
        try:
            authority = urlsplit(candidate).netloc
        except ValueError:
            authority = re.split(r"[/?#]", candidate.split("//", 1)[1], maxsplit=1)[0]
        if "@" in unquote(authority):
            return True
    return False


def _prompt_ids_are_unique(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        and len(value) == len(set(value))
    )


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


ProbeFactory = Callable[
    [ReliabilityFixture, argparse.Namespace],
    tuple[ReliabilityHttpProbe, ReliabilityHostProbe, dict[str, OwnedProcessIdentity]],
]


def main(
    argv: list[str] | None = None,
    *,
    probe_factory: ProbeFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run the opt-in Windows ComfyUI reliability gate.")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--comfyui-pid", type=int, required=True)
    parser.add_argument("--tts-more-pid", type=int, required=True)
    parser.add_argument("--host-manifest", type=Path)
    parser.add_argument("--allow-lan", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.fixture.read_text(encoding="utf-8-sig"))
        fixture = ReliabilityFixture.model_validate(document)
        if probe_factory is None:
            probe_factory = _default_probe_factory
        http_probe, host_probe, owned_processes = probe_factory(fixture, args)
        if args.preflight_only:
            execute_reliability_preflight(
                fixture,
                output_root=args.output_root,
                http_probe=http_probe,
                host_probe=host_probe,
                owned_processes=owned_processes,
                allow_lan=args.allow_lan,
            )
            return 0
        summary = execute_reliability_validation(
            fixture,
            output_root=args.output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=owned_processes,
            allow_lan=args.allow_lan,
        )
    except LiveValidationError as exc:
        failure = exc
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        RuntimeError,
        httpx.HTTPError,
    ):
        failure = LiveValidationError(
            "preflight-observation-failed",
            stage="preflight",
        )
    else:
        return 0 if summary.status == "passed" else 1
    if not failure.failure_persistence_attempted:
        _persist_failure_marker(Path(args.output_root), failure)
    return 1


def _default_probe_factory(
    fixture: ReliabilityFixture,
    args: argparse.Namespace,
) -> tuple[ReliabilityHttpProbe, ReliabilityHostProbe, dict[str, OwnedProcessIdentity]]:
    del fixture
    if args.host_manifest is None:
        raise LiveValidationError("host-probe-manifest-required", stage="preflight")
    host_probe = WindowsReliabilityHostProbe.from_manifest(args.host_manifest)
    recorded = host_probe.manifest.owned_processes
    if recorded["comfyui"].pid != args.comfyui_pid or recorded["tts-more"].pid != args.tts_more_pid:
        raise LiveValidationError("host-manifest-pid-mismatch", stage="preflight")
    http_probe = HttpReliabilityProbe(reference_root=args.fixture.resolve().parent)
    return http_probe, host_probe, host_probe.owned_processes


if __name__ == "__main__":
    raise SystemExit(main())
