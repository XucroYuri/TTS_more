"""Bounded, redacted JSONL audit logging for script parse attempts."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


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
        with _HANDLER_LOCK:
            logger.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            for handler in logger.handlers:
                if isinstance(handler, RotatingFileHandler):
                    handler.flush()
                    handler.close()
    except Exception:
        # Audit telemetry is best-effort and must never change endpoint behavior.
        return
