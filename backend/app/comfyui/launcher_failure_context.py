from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, StrictInt, field_validator, model_validator

from . import reliability_evidence
from .reliability_validation import (
    CaseEvidence,
    FailedCaseEvidence,
    FailureMarker,
    ReliabilityRunFailure,
    ReliabilityRunSummary,
    PublicArgumentError,
    PublicArgumentParser,
    _StrictModel,
    _parse_public_utc,
    _public_utc,
    _run_key_argument,
    read_reliability_summary,
)


_MAX_CONTEXT_ARTIFACT_BYTES = 67_108_864
_MAX_CONTEXT_CASES = 128
_SAFE_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_UPPER_SHA256 = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9A-F]{64}$"),
]
_PUBLIC_UTC = Annotated[
    str,
    Field(
        min_length=27,
        max_length=27,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
    ),
]
_CASE_CONTEXT_UNBOUND_SHA256 = hashlib.sha256(b"case-context-unbound").hexdigest().upper()


def _exact_schema_version(value: Any) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("launcher context schema version must be the exact integer 1")
    return value


_SchemaVersion = Annotated[Literal[1], BeforeValidator(_exact_schema_version)]


class LauncherEvidenceStamp(_StrictModel):
    relative_name: str = Field(min_length=1, max_length=160)
    length: StrictInt = Field(ge=0, le=_MAX_CONTEXT_ARTIFACT_BYTES)
    sha256: _UPPER_SHA256
    last_write_utc: _PUBLIC_UTC

    @field_validator("last_write_utc")
    @classmethod
    def _valid_last_write(cls, value: str) -> str:
        _parse_public_utc(value)
        return value


class LauncherFailureEvidenceBaseline(_StrictModel):
    schema_version: _SchemaVersion
    failure: LauncherEvidenceStamp | None
    summary: LauncherEvidenceStamp | None
    cases: Annotated[list[LauncherEvidenceStamp], Field(max_length=_MAX_CONTEXT_CASES)]

    @model_validator(mode="after")
    def _exact_relative_manifest(self) -> "LauncherFailureEvidenceBaseline":
        if self.failure is not None and self.failure.relative_name != "failure.json":
            raise ValueError("failure baseline relative name is invalid")
        if self.summary is not None and self.summary.relative_name != "reliability-summary.json":
            raise ValueError("summary baseline relative name is invalid")
        names = [entry.relative_name for entry in self.cases]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("case baseline manifest is not exact and ordered")
        if any(
            not name.startswith("cases/")
            or not name.endswith(".json")
            or _SAFE_CASE_ID.fullmatch(name[6:-5]) is None
            for name in names
        ):
            raise ValueError("case baseline relative name is invalid")
        return self


class LauncherFailurePrimary(_StrictModel):
    code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
    )
    stage: Literal["preflight", "case", "finalize", "validator"]


class LauncherFailureSummaryCommitment(_StrictModel):
    artifact_sha256: _UPPER_SHA256
    completed_case_count: StrictInt = Field(ge=0, le=_MAX_CONTEXT_CASES)


class LauncherFailureCaseCommitment(_StrictModel):
    case_id_sha256: _UPPER_SHA256
    artifact_sha256: _UPPER_SHA256
    started_at: _PUBLIC_UTC
    finished_at: _PUBLIC_UTC

    @model_validator(mode="after")
    def _ordered_times(self) -> "LauncherFailureCaseCommitment":
        if _parse_public_utc(self.finished_at) < _parse_public_utc(self.started_at):
            raise ValueError("launcher failure case times are not ordered")
        return self


class LauncherFailureContext(_StrictModel):
    schema_version: _SchemaVersion
    kind: Literal["launcher-failure-context"]
    status: Literal["failed"]
    primary: LauncherFailurePrimary
    failure_sha256: _UPPER_SHA256 | None
    summary: LauncherFailureSummaryCommitment | None
    case: LauncherFailureCaseCommitment | None
    case_context_secondary_sha256: Annotated[list[_UPPER_SHA256], Field(max_length=1)]

    @model_validator(mode="after")
    def _case_secondary_is_consistent(self) -> "LauncherFailureContext":
        expected = [] if self.case is not None else [_CASE_CONTEXT_UNBOUND_SHA256]
        if self.case_context_secondary_sha256 != expected:
            raise ValueError("launcher failure case context state is inconsistent")
        return self


def _artifact_stamp(path: Path, relative_name: str) -> LauncherEvidenceStamp | None:
    try:
        if not path.is_file():
            return None
        stat = path.stat()
        if stat.st_size < 0 or stat.st_size > _MAX_CONTEXT_ARTIFACT_BYTES:
            raise ValueError("launcher context artifact size is invalid")
        content = path.read_bytes()
        if len(content) != stat.st_size:
            raise ValueError("launcher context artifact changed while being read")
        last_write = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return LauncherEvidenceStamp(
            relative_name=relative_name,
            length=len(content),
            sha256=hashlib.sha256(content).hexdigest().upper(),
            last_write_utc=_public_utc(last_write),
        )
    except (OSError, OverflowError):
        raise ValueError("launcher context artifact is unreadable") from None


def _case_stamps(output_root: Path) -> list[LauncherEvidenceStamp]:
    case_root = output_root / "cases"
    if not case_root.exists():
        return []
    if not case_root.is_dir():
        raise ValueError("launcher context cases root is invalid")
    entries: list[LauncherEvidenceStamp] = []
    for path in sorted(case_root.glob("*.json"), key=lambda item: item.name):
        case_id = path.stem
        if _SAFE_CASE_ID.fullmatch(case_id) is None:
            raise ValueError("launcher context case filename is invalid")
        stamp = _artifact_stamp(path, f"cases/{path.name}")
        if stamp is None:
            raise ValueError("launcher context case artifact disappeared")
        entries.append(stamp)
    if len(entries) > _MAX_CONTEXT_CASES:
        raise ValueError("launcher context case manifest exceeds its bound")
    return entries


def snapshot_launcher_failure_evidence(
    output_root: Path,
) -> LauncherFailureEvidenceBaseline:
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    return LauncherFailureEvidenceBaseline(
        schema_version=1,
        failure=_artifact_stamp(output_root / "failure.json", "failure.json"),
        summary=_artifact_stamp(
            output_root / "reliability-summary.json",
            "reliability-summary.json",
        ),
        cases=_case_stamps(output_root),
    )


def write_launcher_failure_evidence_baseline(
    output_root: Path,
    baseline_path: Path,
) -> None:
    output_root = output_root.resolve()
    baseline_path = baseline_path.resolve()
    if not baseline_path.is_relative_to(output_root) or baseline_path == output_root:
        raise ValueError("launcher context baseline path is outside the output root")
    baseline = snapshot_launcher_failure_evidence(output_root)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = baseline_path.with_name(
        f".{baseline_path.name}.{secrets.token_hex(16)}.tmp"
    )
    try:
        temporary.write_text(baseline.model_dump_json(), encoding="utf-8")
        os.replace(temporary, baseline_path)
    except OSError:
        raise ValueError("launcher context baseline publication failed") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_baseline(output_root: Path, baseline_path: Path) -> LauncherFailureEvidenceBaseline:
    output_root = output_root.resolve()
    baseline_path = baseline_path.resolve()
    if not baseline_path.is_relative_to(output_root) or not baseline_path.is_file():
        raise ValueError("launcher context baseline is unavailable")
    try:
        content = baseline_path.read_bytes()
        if len(content) > _MAX_CONTEXT_ARTIFACT_BYTES:
            raise ValueError("launcher context baseline exceeds its bound")
        return LauncherFailureEvidenceBaseline.model_validate_json(content)
    except (OSError, ValueError):
        raise ValueError("launcher context baseline is invalid") from None


def _changed_since(
    current: LauncherEvidenceStamp | None,
    baseline: LauncherEvidenceStamp | None,
    *,
    started_at: datetime,
    observed_at: datetime,
) -> bool:
    if current is None:
        return False
    last_write = _parse_public_utc(current.last_write_utc)
    if last_write < started_at or last_write > observed_at:
        return False
    if baseline is None:
        return True
    return current.length != baseline.length or current.sha256 != baseline.sha256


def _read_current_bytes(path: Path, stamp: LauncherEvidenceStamp) -> bytes:
    try:
        content = path.read_bytes()
    except OSError:
        raise ValueError("launcher context artifact is unreadable") from None
    if (
        len(content) != stamp.length
        or hashlib.sha256(content).hexdigest().upper() != stamp.sha256
    ):
        raise ValueError("launcher context artifact changed after inventory")
    return content


def _baseline_case_map(
    baseline: LauncherFailureEvidenceBaseline,
) -> dict[str, LauncherEvidenceStamp]:
    return {entry.relative_name: entry for entry in baseline.cases}


def evaluate_launcher_failure_context(
    output_root: Path,
    baseline_path: Path,
    *,
    run_started_at: str,
    failure_observed_at: str,
) -> LauncherFailureContext:
    output_root = output_root.resolve()
    baseline = _read_baseline(output_root, baseline_path)
    started = _parse_public_utc(run_started_at)
    observed = _parse_public_utc(failure_observed_at)
    if observed < started:
        raise ValueError("launcher context invocation window is invalid")

    primary = LauncherFailurePrimary(
        code="launcher-validation-failed",
        stage="validator",
    )
    failure_sha256: str | None = None
    failure: FailureMarker | None = None
    failure_stamp = _artifact_stamp(output_root / "failure.json", "failure.json")
    if _changed_since(
        failure_stamp,
        baseline.failure,
        started_at=started,
        observed_at=observed,
    ):
        try:
            if failure_stamp is None:
                raise ValueError("launcher failure stamp is unavailable")
            failure = FailureMarker.model_validate_json(
                _read_current_bytes(output_root / "failure.json", failure_stamp)
            )
        except Exception:
            failure = None
        if failure is not None:
            primary = LauncherFailurePrimary(code=failure.code, stage=failure.stage)
            failure_sha256 = failure_stamp.sha256

    summary_commitment: LauncherFailureSummaryCommitment | None = None
    summary: ReliabilityRunSummary | None = None
    summary_stamp = _artifact_stamp(
        output_root / "reliability-summary.json",
        "reliability-summary.json",
    )
    if _changed_since(
        summary_stamp,
        baseline.summary,
        started_at=started,
        observed_at=observed,
    ):
        try:
            if summary_stamp is None:
                raise ValueError("launcher summary stamp is unavailable")
            summary = read_reliability_summary(
                _read_current_bytes(
                    output_root / "reliability-summary.json",
                    summary_stamp,
                )
            )
            if summary.status != "failed":
                raise ValueError("launcher context summary is not failed")
        except Exception:
            summary = None
        if summary is not None:
            summary_commitment = LauncherFailureSummaryCommitment(
                artifact_sha256=summary_stamp.sha256,
                completed_case_count=len(summary.cases),
            )

    candidates: list[LauncherFailureCaseCommitment] = []
    if failure is not None and summary is not None:
        baseline_cases = _baseline_case_map(baseline)
        for stamp in _case_stamps(output_root):
            if not _changed_since(
                stamp,
                baseline_cases.get(stamp.relative_name),
                started_at=started,
                observed_at=observed,
            ):
                continue
            case_path = output_root / Path(stamp.relative_name)
            try:
                failed_case = FailedCaseEvidence.model_validate_json(
                    _read_current_bytes(case_path, stamp)
                )
                case_id = case_path.stem
                if (
                    failed_case.status != "failed"
                    or failed_case.case_id != case_id
                    or _SAFE_CASE_ID.fullmatch(case_id) is None
                    or failed_case.failure != failure
                    or failed_case.host is None
                ):
                    continue
                case_started = _parse_public_utc(failed_case.host.started_at)
                case_finished = _parse_public_utc(failed_case.host.finished_at)
                if (
                    case_started < started
                    or case_finished < case_started
                    or case_finished > observed
                ):
                    continue
                candidates.append(
                    LauncherFailureCaseCommitment(
                        case_id_sha256=hashlib.sha256(case_id.encode("utf-8")).hexdigest().upper(),
                        artifact_sha256=stamp.sha256,
                        started_at=_public_utc(case_started),
                        finished_at=_public_utc(case_finished),
                    )
                )
            except Exception:
                continue

    case = candidates[0] if len(candidates) == 1 else None
    return LauncherFailureContext(
        schema_version=1,
        kind="launcher-failure-context",
        status="failed",
        primary=primary,
        failure_sha256=failure_sha256,
        summary=summary_commitment,
        case=case,
        case_context_secondary_sha256=(
            [] if case is not None else [_CASE_CONTEXT_UNBOUND_SHA256]
        ),
    )


def _canonical_run_model(model: Any) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _strict_run_model(
    output_root: Path,
    run_key: str,
    kind: str,
    model_type: Any,
    *,
    name: str | None = None,
    verification: reliability_evidence.RunVerification | None = None,
) -> tuple[Any, bytes]:
    raw = reliability_evidence.read_artifact(
        output_root,
        run_key,
        kind,
        name=name,
    )
    if model_type in {ReliabilityRunSummary, CaseEvidence}:
        model = model_type.model_validate_json(raw, strict=False)
    else:
        model = model_type.model_validate_json(raw)
    if raw != _canonical_run_model(model):
        raise ValueError("run artifact is not canonical")
    _assert_verified_commitment(
        verification,
        _run_relative_name(kind, name),
        raw,
    )
    return model, raw


def _run_relative_name(kind: str, name: str | None) -> str:
    fixed = {
        "failure": "failure.json",
        "summary": "reliability-summary.json",
        "preflight": "preflight.json",
    }
    if kind in fixed and name is None:
        return fixed[kind]
    suffixes = {"case": ("cases", ".json"), "audio": ("audio", ".wav")}
    if kind not in suffixes or name is None:
        raise ValueError("run artifact role is invalid")
    directory, suffix = suffixes[kind]
    return f"{directory}/{name}{suffix}"


def _assert_verified_commitment(
    verification: reliability_evidence.RunVerification | None,
    relative_name: str,
    payload: bytes,
) -> None:
    if verification is None:
        return
    terminal = verification.terminal
    commitments = [
        item
        for item in (terminal.preflight, terminal.failure, terminal.summary)
        if item is not None
    ] + list(terminal.cases) + list(terminal.artifacts)
    matches = [item for item in commitments if item.relative_name == relative_name]
    if len(matches) != 1:
        raise ValueError("verified run commitment is missing")
    commitment = matches[0]
    if (
        len(payload) != commitment.size_bytes
        or hashlib.sha256(payload).hexdigest() != commitment.sha256
    ):
        raise ValueError("verified run commitment changed")


def _verify_terminal_if_present(
    output_root: Path,
    run_key: str,
) -> reliability_evidence.RunVerification | None:
    terminal_path = Path(output_root).absolute() / "runs" / run_key / "terminal.json"
    if os.path.lexists(terminal_path):
        return reliability_evidence.verify_run(output_root, run_key)
    return None


def _evaluate_launcher_failure_context_for_run(
    output_root: Path,
    run_key: str,
    *,
    verification: reliability_evidence.RunVerification | None,
) -> LauncherFailureContext:
    failure_document, failure_raw = _strict_run_model(
        output_root,
        run_key,
        "failure",
        ReliabilityRunFailure,
        verification=verification,
    )
    if failure_document.run_key != run_key:
        raise ValueError("run failure binding mismatch")
    summary, summary_raw = _strict_run_model(
        output_root,
        run_key,
        "summary",
        ReliabilityRunSummary,
        verification=verification,
    )
    if summary.status != "failed":
        raise ValueError("run failure summary is not failed")

    completed_ids: set[str] = set()
    for expected_case in summary.cases:
        if expected_case.case_id in completed_ids:
            raise ValueError("run case binding is duplicated")
        completed_ids.add(expected_case.case_id)
        stored_case, _ = _strict_run_model(
            output_root,
            run_key,
            "case",
            CaseEvidence,
            name=expected_case.case_id,
            verification=verification,
        )
        if stored_case != expected_case:
            raise ValueError("run case binding mismatch")
        if stored_case.audio is not None:
            audio = reliability_evidence.read_artifact(
                output_root,
                run_key,
                "audio",
                name=stored_case.case_id,
            )
            _assert_verified_commitment(
                verification,
                f"audio/{stored_case.case_id}.wav",
                audio,
            )
            if (
                len(audio) != stored_case.audio.size_bytes
                or hashlib.sha256(audio).hexdigest() != stored_case.audio.sha256
            ):
                raise ValueError("run audio commitment mismatch")

    case_commitment: LauncherFailureCaseCommitment | None = None
    active_case_id = failure_document.active_case_id
    if active_case_id is not None:
        if active_case_id in completed_ids:
            raise ValueError("active run case is already completed")
        failed_case, failed_case_raw = _strict_run_model(
            output_root,
            run_key,
            "case",
            FailedCaseEvidence,
            name=active_case_id,
            verification=verification,
        )
        if (
            failed_case.status != "failed"
            or failed_case.case_id != active_case_id
            or failed_case.failure != failure_document.failure
            or failed_case.host is None
        ):
            raise ValueError("active run case binding mismatch")
        started_at = _parse_public_utc(failed_case.host.started_at)
        finished_at = _parse_public_utc(failed_case.host.finished_at)
        case_commitment = LauncherFailureCaseCommitment(
            case_id_sha256=hashlib.sha256(active_case_id.encode("utf-8")).hexdigest().upper(),
            artifact_sha256=hashlib.sha256(failed_case_raw).hexdigest().upper(),
            started_at=_public_utc(started_at),
            finished_at=_public_utc(finished_at),
        )

    context = LauncherFailureContext(
        schema_version=1,
        kind="launcher-failure-context",
        status="failed",
        primary=LauncherFailurePrimary(
            code=failure_document.failure.code,
            stage=failure_document.failure.stage,
        ),
        failure_sha256=hashlib.sha256(failure_raw).hexdigest().upper(),
        summary=LauncherFailureSummaryCommitment(
            artifact_sha256=hashlib.sha256(summary_raw).hexdigest().upper(),
            completed_case_count=len(summary.cases),
        ),
        case=case_commitment,
        case_context_secondary_sha256=(
            [] if case_commitment is not None else [_CASE_CONTEXT_UNBOUND_SHA256]
        ),
    )
    if verification is not None:
        observed = reliability_evidence.verify_run(output_root, run_key)
        if observed != verification:
            raise ValueError("run terminal commitment changed")
    return context


def evaluate_launcher_failure_context_for_run(
    output_root: Path,
    run_key: str,
) -> LauncherFailureContext:
    """Evaluate exactly one run without consulting mtimes, root markers, or current."""
    try:
        verification = _verify_terminal_if_present(Path(output_root), run_key)
        return _evaluate_launcher_failure_context_for_run(
            Path(output_root),
            run_key,
            verification=verification,
        )
    except Exception:
        raise ValueError("launcher run context is invalid") from None


def evaluate_current_launcher_failure_context(
    output_root: Path,
    *,
    baseline_path: Path | None = None,
    run_started_at: str | None = None,
    failure_observed_at: str | None = None,
) -> LauncherFailureContext:
    """Prefer a verified current run; legacy audit is pointer-absent only."""
    try:
        current = reliability_evidence.verify_current(Path(output_root))
    except Exception:
        raise ValueError("launcher current context is invalid") from None
    if isinstance(current, dict) and current["status"] == "absent":
        if baseline_path is None or run_started_at is None or failure_observed_at is None:
            raise ValueError("launcher legacy context is unavailable")
        return evaluate_launcher_failure_context(
            Path(output_root),
            baseline_path,
            run_started_at=run_started_at,
            failure_observed_at=failure_observed_at,
        )
    try:
        context = _evaluate_launcher_failure_context_for_run(
            Path(output_root),
            current.pointer.run_key,
            verification=current.run,
        )
        observed = reliability_evidence.verify_current(Path(output_root))
        if not isinstance(observed, reliability_evidence.CurrentVerification):
            raise ValueError("current pointer disappeared")
        if observed != current:
            raise ValueError("current pointer commitment changed")
        return context
    except Exception:
        raise ValueError("launcher current context is invalid") from None


def _parser() -> argparse.ArgumentParser:
    parser = PublicArgumentParser(description="Read launcher failure context safely.")
    subparsers = parser.add_subparsers(
        dest="mode",
        required=True,
        parser_class=PublicArgumentParser,
    )
    for mode in ("snapshot", "evaluate"):
        child = subparsers.add_parser(mode)
        child.add_argument("--output-root", type=Path, required=True)
        child.add_argument("--baseline-path", type=Path, required=True)
        if mode == "evaluate":
            child.add_argument("--run-started-at", required=True)
            child.add_argument("--failure-observed-at", required=True)
    run = subparsers.add_parser("evaluate-run")
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--run-key", type=_run_key_argument, required=True)
    current = subparsers.add_parser("evaluate-current")
    current.add_argument("--output-root", type=Path, required=True)
    current.add_argument("--baseline-path", type=Path)
    current.add_argument("--run-started-at")
    current.add_argument("--failure-observed-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except PublicArgumentError:
        if argv is not None:
            raise SystemExit(2) from None
        print('{"error":"invalid-arguments"}', file=sys.stderr)
        return 2
    try:
        if args.mode == "snapshot":
            write_launcher_failure_evidence_baseline(
                args.output_root,
                args.baseline_path,
            )
            return 0
        if args.mode == "evaluate-run":
            context = evaluate_launcher_failure_context_for_run(
                args.output_root,
                args.run_key,
            )
        elif args.mode == "evaluate-current":
            context = evaluate_current_launcher_failure_context(
                args.output_root,
                baseline_path=args.baseline_path,
                run_started_at=args.run_started_at,
                failure_observed_at=args.failure_observed_at,
            )
        else:
            context = evaluate_launcher_failure_context(
                args.output_root,
                args.baseline_path,
                run_started_at=args.run_started_at,
                failure_observed_at=args.failure_observed_at,
            )
        print(context.model_dump_json())
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
