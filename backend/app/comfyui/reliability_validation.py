from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
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
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, ValidationError, field_validator, model_validator


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
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
REQUIRED_BOUNDARY_LABELS = ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")
ENGINE_ORDER: tuple[Engine, ...] = ("gpt-sovits", "indextts", "cosyvoice")
_BRIDGE_ENGINE_IDS: dict[str, Engine] = {
    "gpt_sovits": "gpt-sovits",
    "index_tts": "indextts",
    "cosyvoice": "cosyvoice",
}
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
    request_timeout_seconds: StrictFloat = Field(gt=0.0, le=180.0)
    convergence_seconds: StrictFloat = Field(gt=0.0, le=180.0)


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


class FailureMarker(_StrictModel):
    code: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    stage: Literal["preflight", "case", "finalize"]


class FailedCaseEvidence(_StrictModel):
    status: Literal["failed"]
    case_id: str = Field(min_length=1)
    phase: Phase
    engine: Engine
    expected: Outcome
    failure: FailureMarker
    host: HostCaseObservation | None

    @field_validator("case_id")
    @classmethod
    def _neutral_case_id(cls, value: str) -> str:
        if "\\" in value or "/" in value or Path(value).is_absolute():
            raise ValueError("case_id must not contain paths")
        return value


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
    ) -> None:
        self.transport = transport
        self.reference_root = Path(reference_root).resolve()
        self.poll_interval_seconds = poll_interval_seconds
        self.monotonic = monotonic
        self.sleep = sleep
        self._fixture: ReliabilityFixture | None = None
        self._released = False
        self._seen_job_ids: set[str] = set()
        self._seen_prompt_ids: set[str] = set()
        self._seen_version_ids: set[str] = set()

    def preflight(self, fixture: ReliabilityFixture) -> HttpPreflightObservation:
        self._fixture = _revalidate_model(fixture, ReliabilityFixture)
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
            if response.get("status") != "ready" or any(
                not isinstance(item, dict) or item.get("status") != "ready"
                for item in response.get("items", [])
            ):
                raise RuntimeError("TTS More generation preflight is not ready")
        return HttpPreflightObservation(
            resources=resources,
            queue=self._queue_snapshot(fixture),
        )

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
        tts_more_url = fixture.base_urls["tts_more"].rstrip("/")
        comfyui_url = fixture.base_urls["comfyui"].rstrip("/")
        payload = self._generation_payload(case, fixture)
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
        if job_id in self._seen_job_ids:
            raise RuntimeError("job id was reused")
        self._seen_job_ids.add(job_id)

        deadline = self.monotonic() + case.convergence_seconds
        prompt_id: str | None = None
        queue_before: list[str] | None = None
        acted = False
        endpoint_unavailable = False
        terminal: dict[str, Any] | None = None
        while self.monotonic() <= deadline:
            job = self._json("GET", f"{tts_more_url}/api/jobs/{job_id}", timeout=10.0)
            items = job.get("items")
            item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else {}
            external_id = item.get("external_job_id")
            if external_id:
                observed_prompt = _required_opaque_id(external_id, "prompt")
                if prompt_id is not None and prompt_id != observed_prompt:
                    raise RuntimeError("job changed prompt id")
                prompt_id = observed_prompt
                try:
                    queue = self._comfy_queue(fixture)
                except httpx.TransportError:
                    if case.action != "terminate-comfyui" or not acted:
                        raise
                    endpoint_unavailable = True
                    queue = None
                prompt_ids = _comfy_prompt_ids(queue) if queue is not None else []
                if prompt_id in prompt_ids and queue_before is None:
                    queue_before = prompt_ids
                state = _comfy_prompt_state(queue, prompt_id) if queue is not None else "absent"
                if case.action == "cancel-queued" and not acted and state == "pending":
                    self._cancel_job(tts_more_url, job_id)
                    acted = True
                elif case.action == "cancel-running" and not acted and state == "running":
                    self._cancel_job(tts_more_url, job_id)
                    acted = True
                elif case.action == "terminate-comfyui" and not acted and state == "running":
                    if action_hook is None:
                        raise RuntimeError("terminate action hook is missing")
                    action_hook()
                    acted = True
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
        queue_after_document = self._comfy_queue(fixture)
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
            deadline = self.monotonic() + case.convergence_seconds
            while self.monotonic() <= deadline:
                blocker = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{blocker_job_id}",
                    timeout=10.0,
                )
                blocker_item = _single_job_item(blocker)
                external_id = blocker_item.get("external_job_id")
                if external_id:
                    blocker_prompt_id = _required_opaque_id(external_id, "prompt")
                    if _comfy_prompt_state(self._comfy_queue(fixture), blocker_prompt_id) in {
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
            self._remember_unique(self._seen_job_ids, target_job_id, "job")
            queued_target: dict[str, Any] | None = None
            while self.monotonic() <= deadline:
                target = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{target_job_id}",
                    timeout=10.0,
                )
                target_item = _single_job_item(target)
                if target.get("status") == "running" and _queued_item_is_pristine(target_item):
                    queued_target = target
                    break
                if target_item.get("external_job_id") or target_item.get("status") != "queued":
                    raise RuntimeError("queued-cancel target escaped pre-dispatch admission")
                self.sleep(self.poll_interval_seconds)
            if queued_target is None:
                raise RuntimeError("queued-cancel target did not reach held admission")

            cancelled = self._cancel_job(tts_more_url, target_job_id)
            _require_pristine_queued_cancellation(cancelled)
            target_cancelled = True
            cancelled_updated_at = cancelled.get("updated_at")
            if not isinstance(cancelled_updated_at, str) or not cancelled_updated_at:
                raise RuntimeError("queued-cancel response omitted update time")

            self._cancel_job(tts_more_url, blocker_job_id)
            blocker_cancelled = True
            settled_target: dict[str, Any] | None = None
            while self.monotonic() <= deadline:
                target = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{target_job_id}",
                    timeout=10.0,
                )
                blocker = self._json(
                    "GET",
                    f"{tts_more_url}/api/jobs/{blocker_job_id}",
                    timeout=10.0,
                )
                queue_ids = _comfy_prompt_ids(self._comfy_queue(fixture))
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


def build_case_plan(rounds: int = 10) -> tuple[CasePlan, ...]:
    if isinstance(rounds, bool) or rounds != 10:
        raise ValueError("reliability plan requires exactly 10 rounds")

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
                    request_timeout_seconds=30.0,
                    convergence_seconds=30.0,
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
            convergence_seconds=30.0,
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
                    convergence_seconds=30.0,
                ),
                CasePlan(
                    case_id=f"recover-cancel-{engine}",
                    phase="recovery",
                    engine=engine,
                    expected="completed",
                    action="synthesize",
                    request_timeout_seconds=180.0,
                    convergence_seconds=180.0,
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
                    convergence_seconds=30.0,
                ),
                CasePlan(
                    case_id=f"recover-timeout-{engine}",
                    phase="recovery",
                    engine=engine,
                    expected="completed",
                    action="synthesize",
                    request_timeout_seconds=180.0,
                    convergence_seconds=180.0,
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
            convergence_seconds=30.0,
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
                request_timeout_seconds=180.0,
                convergence_seconds=180.0,
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
        selected_plan = tuple(plan) if plan is not None else build_case_plan(fixture.rounds)
        selected_plan = tuple(_revalidate_model(case, CasePlan) for case in selected_plan)
        if selected_plan != build_case_plan(fixture.rounds):
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
        summary = finalize_run(
            fixture,
            completed_cases,
            required_cases=required_case_specs(selected_plan),
        )
        for completed_case in summary.cases:
            write_atomic_json(
                output_root / "cases" / f"{completed_case.case_id}.json",
                completed_case.model_dump(mode="json"),
            )
        if active_case is not None:
            failed_case = FailedCaseEvidence(
                status="failed",
                case_id=active_case.case_id,
                phase=active_case.phase,
                engine=active_case.engine,
                expected=active_case.expected,
                failure=FailureMarker(code=failure.code, stage=failure.stage),
                host=active_host_observation,
            )
            write_atomic_json(
                output_root / "cases" / f"{active_case.case_id}.json",
                failed_case.model_dump(mode="json"),
            )
        write_atomic_json(
            output_root / "failure.json",
            {"code": failure.code, "stage": failure.stage},
        )
        write_atomic_json(output_root / "reliability-summary.json", summary)
        raise failure

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
    fixture = _revalidate_model(fixture, ReliabilityFixture)
    _require_endpoint_scope(fixture, allow_lan=allow_lan)
    http = _revalidate_model(http_probe.preflight(fixture), HttpPreflightObservation)
    host = _revalidate_model(host_probe.preflight(fixture), HostPreflightObservation)
    _validate_preflight(fixture, http, host, owned_processes)
    write_atomic_json(
        Path(output_root) / "preflight.json",
        {
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
        },
    )


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
    except (OSError, json.JSONDecodeError, ValidationError, LiveValidationError, ValueError, RuntimeError):
        return 1
    return 0 if summary.status == "passed" else 1


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
