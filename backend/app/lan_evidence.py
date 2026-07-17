from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.lan_topology import FORMAL_SERVICE_IDS


_SAFE_PUBLIC_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SAFE_RELATIVE_EVIDENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


class LanNodePreflight(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    host_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LanOrchestrationPreflight(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[2]
    mode: Literal["lan-shared", "lan-distributed"]
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    controller_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    controller_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nodes: dict[str, LanNodePreflight]
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("nodes")
    @classmethod
    def validate_node_names(
        cls, nodes: dict[str, LanNodePreflight]
    ) -> dict[str, LanNodePreflight]:
        if not nodes or any(not name.strip() for name in nodes):
            raise ValueError("nodes must contain nonempty node names")
        return nodes


class LanNodeEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    host_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gpu_uuid_sha256: list[str] = Field(min_length=1, max_length=16)
    gpu_log: str
    service_logs: dict[str, str]

    @field_validator("gpu_uuid_sha256")
    @classmethod
    def validate_gpu_hashes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[0-9a-f]{64}", value) for value in values
        ):
            raise ValueError("GPU identities must contain unique SHA-256 values")
        return values

    @field_validator("gpu_log")
    @classmethod
    def validate_gpu_log(cls, value: str) -> str:
        return _validate_relative_evidence(value)

    @field_validator("service_logs")
    @classmethod
    def validate_service_logs(cls, values: dict[str, str]) -> dict[str, str]:
        if not values or any(key not in FORMAL_SERVICE_IDS for key in values):
            raise ValueError("service logs must use formal service IDs")
        return {key: _validate_relative_evidence(value) for key, value in values.items()}


class LanEvidenceManifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[1]
    mode: Literal["lan-shared", "lan-distributed"]
    deployment: Literal["clean", "release"]
    controller_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_owners: dict[str, str]
    nodes: dict[str, LanNodeEvidence]
    fault_recovery: Literal["fault-recovery.json"] = "fault-recovery.json"
    playwright_junit: Literal["playwright-junit.xml"] = "playwright-junit.xml"
    recovery_summary: Literal["recovery/summary.json"] = "recovery/summary.json"
    human_review_status: Literal["pending"] = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_public_topology_bindings(self) -> "LanEvidenceManifest":
        if set(self.service_owners) != FORMAL_SERVICE_IDS:
            raise ValueError("service owners must bind every formal service")
        aliases = set(self.service_owners.values())
        if not aliases or aliases != set(self.nodes):
            raise ValueError("evidence nodes must match topology service owners")
        if any(not _SAFE_PUBLIC_NODE.fullmatch(alias) for alias in aliases):
            raise ValueError("topology node alias is not a safe public label")
        for alias in aliases:
            try:
                ipaddress.ip_address(alias)
            except ValueError:
                pass
            else:
                raise ValueError("raw IP addresses are not public node labels")
        for alias, evidence in self.nodes.items():
            owned = {
                service_id
                for service_id, owner in self.service_owners.items()
                if owner == alias
            }
            if set(evidence.service_logs) != owned:
                raise ValueError("node service logs must match topology ownership")
            prefix = f"worker-logs/{alias}/"
            if evidence.gpu_log != prefix + "nvidia-smi.csv" or any(
                path != prefix + f"{service_id}.log"
                for service_id, path in evidence.service_logs.items()
            ):
                raise ValueError("node evidence paths must use run-relative owned paths")
        return self


def _validate_relative_evidence(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _SAFE_RELATIVE_EVIDENCE.fullmatch(value)
        or value.startswith(("/", "\\"))
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("evidence path must be a safe relative path")
    return value


def write_lan_preflight(path: Path, payload: LanOrchestrationPreflight) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_text(
            payload.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_lan_evidence(path: Path, payload: LanEvidenceManifest) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("LAN evidence path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_components(path.parent)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("LAN evidence destination must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        metadata = temporary_path.lstat()
        if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("LAN evidence temporary file identity changed")
        _reject_link_components(path.parent)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def assert_required_evidence(
    output: Path,
    service_owners: dict[str, str],
    *,
    started_at: datetime,
) -> None:
    if (
        not isinstance(output, Path)
        or not output.is_absolute()
        or not isinstance(started_at, datetime)
        or started_at.tzinfo is None
    ):
        raise ValueError("evidence root and run start must be explicit")
    try:
        root_metadata = output.lstat()
    except OSError:
        raise RuntimeError("LAN validation evidence is incomplete: output") from None
    if _is_link(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("LAN validation evidence is incomplete: output")
    _reject_link_components(output)
    failures: list[str] = []
    required = (
        "summary.json",
        "junit.xml",
        "controller.log",
        "orchestration-preflight.json",
        "fault-recovery.json",
        "distributed-evidence.json",
        "human-listening-review.md",
        "playwright-junit.xml",
        "recovery/summary.json",
        "recovery/junit.xml",
    )
    paths = {
        relative: _required_file(output, relative, started_at, failures)
        for relative in required
    }
    for directory in ("wav", "recovery/wav"):
        wav_paths = _required_wavs(output, directory, started_at, failures)
        if len(wav_paths) < 5:
            failures.append(f"{directory}/five-core-samples")
    for node in sorted(set(service_owners.values())):
        gpu_path = _required_file(
            output,
            f"worker-logs/{node}/nvidia-smi.csv",
            started_at,
            failures,
        )
        if gpu_path is not None:
            try:
                rows = [line for line in gpu_path.read_text(encoding="utf-8").splitlines() if line]
            except (OSError, UnicodeError):
                rows = []
            if len(rows) < 2:
                failures.append(f"worker-logs/{node}/nvidia-smi.csv:samples")
    for service_id, node in sorted(service_owners.items()):
        _required_file(
            output,
            f"worker-logs/{node}/{service_id}.log",
            started_at,
            failures,
        )
    for relative in ("summary.json", "recovery/summary.json"):
        path = paths[relative]
        if path is not None:
            payload = _read_strict_json(path, failures, relative)
            if not isinstance(payload, dict) or payload.get("passed") is not True:
                failures.append(f"{relative}:passed")
    for relative in ("junit.xml", "playwright-junit.xml", "recovery/junit.xml"):
        path = paths[relative]
        if path is not None and not _junit_passed(path):
            failures.append(f"{relative}:passed")
    fault_path = paths["fault-recovery.json"]
    if fault_path is not None:
        fault = _read_strict_json(fault_path, failures, "fault-recovery.json")
        if not _fault_report_passed(fault):
            failures.append("fault-recovery.json:passed")
    manifest_path = paths["distributed-evidence.json"]
    if manifest_path is not None:
        manifest_payload = _read_strict_json(
            manifest_path, failures, "distributed-evidence.json"
        )
        try:
            manifest = LanEvidenceManifest.model_validate_json(
                json.dumps(manifest_payload, ensure_ascii=False, allow_nan=False)
            )
        except Exception:
            failures.append("distributed-evidence.json:strict")
        else:
            if manifest.service_owners != service_owners:
                failures.append("distributed-evidence.json:ownership")
    if failures:
        raise RuntimeError(
            "LAN validation evidence is incomplete: " + ", ".join(sorted(set(failures)))
        )


def _required_file(
    root: Path,
    relative: str,
    started_at: datetime,
    failures: list[str],
) -> Path | None:
    try:
        safe_relative = _validate_relative_evidence(relative)
        candidate = root.joinpath(*safe_relative.split("/"))
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
        _reject_link_components(candidate)
        metadata = candidate.lstat()
    except (OSError, ValueError):
        failures.append(relative)
        return None
    if (
        _is_link(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 1
        or metadata.st_size > _MAX_EVIDENCE_BYTES
        or metadata.st_mtime < started_at.timestamp()
    ):
        failures.append(relative)
        return None
    return candidate


def _required_wavs(
    root: Path,
    relative: str,
    started_at: datetime,
    failures: list[str],
) -> list[Path]:
    directory = root.joinpath(*relative.split("/"))
    try:
        _reject_link_components(directory)
        metadata = directory.lstat()
    except OSError:
        return []
    if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
        return []
    result: list[Path] = []
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return []
    for candidate in candidates:
        if candidate.suffix.casefold() != ".wav":
            continue
        checked = _required_file(
            root, candidate.relative_to(root).as_posix(), started_at, failures
        )
        if checked is not None:
            result.append(checked)
    return result


def _read_strict_json(path: Path, failures: list[str], label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError("duplicate JSON key")
            payload[key] = value
        return payload

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeError, ValueError):
        failures.append(f"{label}:json")
        return None


def _junit_passed(path: Path) -> bool:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return False
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return bool(suites) and all(
        int(suite.attrib.get("failures", "0")) == 0
        and int(suite.attrib.get("errors", "0")) == 0
        for suite in suites
    )


def _fault_report_passed(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    mode = payload.get("mode")
    degraded = payload.get("degraded_within_seconds")
    timing_fields = ("restart_seconds", "retry_seconds", "recovery_seconds")

    def nonnegative_timing(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        )

    return (
        payload.get("schema_version") == 1
        and mode in {"lan-shared", "lan-distributed"}
        and payload.get("service_id") in FORMAL_SERVICE_IDS
        and isinstance(payload.get("fault_node"), str)
        and bool(_SAFE_PUBLIC_NODE.fullmatch(payload["fault_node"]))
        and isinstance(degraded, (int, float))
        and not isinstance(degraded, bool)
        and 0 <= degraded <= 15
        and all(nonnegative_timing(payload.get(field)) for field in timing_fields)
        and payload.get("other_services_ready") is True
        and payload.get("application_survived") is True
        and payload.get("retry_passed") is True
        and payload.get("recovery_passed") is True
        and (
            payload.get("all_services_degraded") is True
            and nonnegative_timing(
                payload.get("all_services_degraded_within_seconds")
            )
            and payload["all_services_degraded_within_seconds"] <= 15
            and nonnegative_timing(payload.get("all_services_restart_seconds"))
            if mode == "lan-shared"
            else payload.get("all_services_degraded") is None
            and payload.get("all_services_degraded_within_seconds") is None
            and payload.get("all_services_restart_seconds") is None
        )
    )


def _reject_link_components(path: Path) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("evidence path component is unavailable") from None
        if _is_link(metadata):
            raise ValueError("evidence path contains a link or reparse point")


def _is_link(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400)
