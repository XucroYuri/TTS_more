"""Bounded, redacted JSONL audit logging for script parse attempts."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from pydantic import BaseModel


_HANDLER_LOCK = threading.RLock()
_QUALITY_REASON_CODES = {
    "ambiguous_short_name_alias",
    "missing_dialogue_coverage",
    "missing_quoted_dialogue_coverage",
    "text_not_in_source_order",
    "source_text_mismatch",
    "source_excerpt_not_traceable",
    "source_excerpt_missing_source_text",
    "speaker_anchor_mismatch",
    "unknown_character",
    "non_dialogue_role",
    "quality_contract_violation",
}
_PARSE_REASON_CODES = {*_QUALITY_REASON_CODES, "provider_unavailable", "unexpected", "unknown"}
_PARSE_EVENT_STRING_LIMITS = {
    "attempt_id": 128,
    "project_id": 255,
    "script_revision_id": 128,
    "provider": 160,
    "model": 160,
    "timestamp": 64,
}
_PARSE_EVENTS = {"started", "failed", "succeeded"}
_FAILURE_CATEGORIES = {"provider_unavailable", "quality_rejected", "unexpected"}
_DIAGNOSTIC_STAGES = {"decode", "normalize", "verify"}
_DIAGNOSTIC_PHASES = {"initial", "repair"}
_DIAGNOSTIC_ISSUE_TYPES = {
    *_QUALITY_REASON_CODES,
    "missing_field",
    "invalid_type",
    "normalized_line_dropped",
    "partial_normalization_scan",
}
_DIAGNOSTIC_PATHS = {
    "provider.payload",
    "provider.payload.content",
    "provider.payload.lines",
    "provider.payload.lines.*",
    "provider.payload.lines.*.text",
    "provider.payload.lines.*.source_text",
    "provider.payload.lines.*.source_excerpt",
    "provider.payload.lines.*.speaker",
    "provider.payload.characters.*",
    "verifier.contract",
    "verifier.dialogue_coverage",
    "verifier.quoted_dialogue_coverage",
    "verifier.lines.*.text",
    "verifier.lines.*.source_text",
    "verifier.lines.*.source_excerpt",
    "verifier.lines.*.speaker",
    "verifier.lines.*.character_id",
    "verifier.characters",
    "verifier.characters.*",
}
_DIAGNOSTIC_COUNT_FIELDS = {
    "raw_line_item_count",
    "processed_line_item_count",
    "normalized_line_count",
    "dropped_non_object_count",
    "dropped_missing_speaker_count",
    "dropped_empty_text_count",
    "dropped_non_dialogue_role_count",
    "reference_candidate_count",
    "reference_colon_candidate_count",
    "reference_markdown_candidate_count",
}
_MAX_DIAGNOSTIC_RECORDS = 32
_MAX_DIAGNOSTIC_ISSUES = 24


def parse_attempt_logger(data_root: Path) -> logging.Logger:
    """Return the single bounded JSONL logger for one application data root."""
    path = (data_root / "logs" / "parse-attempts.jsonl").resolve()
    logger_name = f"app.parse_attempts.{hashlib.sha256(str(path).encode()).hexdigest()}"
    with _HANDLER_LOCK:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if any(getattr(handler, "_parse_attempt_path", None) == path for handler in logger.handlers):
            return logger
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path,
                maxBytes=1_048_576,
                backupCount=5,
                encoding="utf-8",
                delay=True,
            )
        except OSError:
            # Importing the module constructs the default app; a read-only
            # checkout must not prevent API startup. Explicit writable roots
            # still receive the required bounded file logger.
            handler = logging.NullHandler()
        handler._parse_attempt_path = path  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger


def safe_label(value: object) -> str:
    output: list[str] = []
    separator_pending = False
    for char in str(value or "unknown").strip():
        if char.isalnum() or char in "._-":
            output.append(char)
            separator_pending = False
        elif not separator_pending:
            output.append("-")
            separator_pending = True
    text = "".join(output).strip(".-_")
    return text[:80] or "unknown"


def quality_reason_codes(exc: BaseException) -> list[str]:
    values = getattr(exc, "reason_codes", ())
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ["unknown"]
    result = [safe_label(value) for value in values]
    known = [code for code in result if code in _QUALITY_REASON_CODES]
    return known or ["unknown"]


def _field(value: object, name: str, default: object = None) -> object:
    if type(value) is dict:
        return value.get(name, default)
    if isinstance(value, BaseModel):
        return getattr(value, name, default)
    return default


def _safe_diagnostic_records(values: object) -> list[dict[str, Any]]:
    if type(values) not in (list, tuple):
        return []
    output: list[dict[str, Any]] = []
    for value in values:
        stage = _field(value, "stage")
        phase = _field(value, "attempt_phase")
        if type(stage) is not str or stage not in _DIAGNOSTIC_STAGES:
            continue
        if phase is not None and (type(phase) is not str or phase not in _DIAGNOSTIC_PHASES):
            continue
        issues: list[dict[str, str]] = []
        raw_issues = _field(value, "issues", [])
        if type(raw_issues) in (list, tuple):
            for issue in raw_issues:
                issue_type = _field(issue, "type")
                path = _field(issue, "path")
                if (
                    type(issue_type) is str
                    and issue_type in _DIAGNOSTIC_ISSUE_TYPES
                    and type(path) is str
                    and path in _DIAGNOSTIC_PATHS
                ):
                    issues.append({"type": issue_type, "path": path})
                    if len(issues) >= _MAX_DIAGNOSTIC_ISSUES:
                        break
        if not issues:
            continue
        counts: dict[str, int] = {}
        raw_counts = _field(value, "counts", {})
        if type(raw_counts) is dict:
            for key in _DIAGNOSTIC_COUNT_FIELDS:
                count = raw_counts.get(key)
                if type(count) is int and count >= 0:
                    counts[key] = min(count, 10_000_000)
        record: dict[str, Any] = {"stage": stage, "attempt_phase": phase, "issues": issues, "counts": counts}
        provider_index = _field(value, "provider_index")
        if type(provider_index) is int and 0 <= provider_index <= 1_000_000:
            record["provider_index"] = provider_index
        output.append(record)
        if len(output) >= _MAX_DIAGNOSTIC_RECORDS:
            break
    return output


def safe_parser_diagnostics(exc: BaseException) -> list[dict[str, Any]]:
    return _safe_diagnostic_records(getattr(exc, "diagnostics", ()))


def _bounded_plain_string(value: object, max_length: int) -> str | None:
    if type(value) is not str or not value or len(value) > max_length:
        return None
    if not all(char.isprintable() for char in value):
        return None
    return value


def _bounded_nonnegative_int(value: object, maximum: int) -> int | None:
    if type(value) is not int or value < 0 or value > maximum:
        return None
    return value


def _safe_parse_event(event: dict[str, Any]) -> dict[str, Any]:
    safe_event: dict[str, Any] = {}
    for key, max_length in _PARSE_EVENT_STRING_LIMITS.items():
        value = _bounded_plain_string(event.get(key), max_length)
        if value is not None:
            safe_event[key] = value

    event_name = event.get("event")
    if type(event_name) is str and event_name in _PARSE_EVENTS:
        safe_event["event"] = event_name
    failure_category = event.get("failure_category")
    if type(failure_category) is str and failure_category in _FAILURE_CATEGORIES:
        safe_event["failure_category"] = failure_category

    source_length = _bounded_nonnegative_int(event.get("source_length"), 100_000_000)
    if source_length is not None:
        safe_event["source_length"] = source_length
    elapsed_ms = _bounded_nonnegative_int(event.get("elapsed_ms"), 604_800_000)
    if elapsed_ms is not None:
        safe_event["elapsed_ms"] = elapsed_ms
    http_status = event.get("http_status")
    if type(http_status) is int and 100 <= http_status <= 599:
        safe_event["http_status"] = http_status

    raw_reason_codes = event.get("reason_codes")
    if type(raw_reason_codes) in (list, tuple, set, frozenset):
        safe_event["reason_codes"] = [
            value
            for value in raw_reason_codes
            if type(value) is str and value in _PARSE_REASON_CODES
        ][:24]
    elif "reason_codes" in event:
        safe_event["reason_codes"] = []
    if "diagnostics" in event:
        safe_event["diagnostics"] = _safe_diagnostic_records(event["diagnostics"])
    return safe_event


def parser_metadata(parser: object, provider_name: object | None = None) -> dict[str, str]:
    """Extract public provider/model labels without inspecting credentials or URLs."""
    providers = getattr(parser, "providers", ())
    if not isinstance(providers, (list, tuple)):
        providers = ()
    enabled = [item for item in providers if getattr(getattr(item, "config", None), "enabled", True)]
    if provider_name is None and len(enabled) > 1:
        return {"provider": "multiple", "model": "multiple"}
    selected = next((item for item in enabled if getattr(item, "name", None) == provider_name), None)
    if provider_name is not None and selected is None:
        return {"provider": safe_label(provider_name), "model": "unknown"}
    selected = selected or (enabled[0] if enabled else None)
    config = getattr(selected, "config", None)
    return {
        "provider": safe_label(provider_name or getattr(selected, "name", getattr(parser, "name", "unknown"))),
        "model": safe_label(getattr(config, "model", getattr(parser, "model", "unknown"))),
    }


def log_parse_event(logger: logging.Logger, **event: Any) -> None:
    """Write only caller-supplied, non-content metadata as one JSON line."""
    try:
        safe_event = _safe_parse_event(event)
        with _HANDLER_LOCK:
            logger.info(json.dumps(safe_event, ensure_ascii=False, separators=(",", ":")))
            for handler in logger.handlers:
                if isinstance(handler, RotatingFileHandler):
                    handler.flush()
                    handler.close()
    except Exception:
        # Audit telemetry is best-effort and must never change endpoint behavior.
        return
