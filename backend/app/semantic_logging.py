"""Bounded, allowlisted JSONL events for persistent semantic analysis."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.net_guard import scrub_error


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

_LOGGER_LOCK = threading.RLock()
_LOGGERS: dict[Path, "SemanticEventLogger"] = {}
_ALLOWED_METADATA = {
    "project_id",
    "source_revision_id",
    "draft_id",
    "run_id",
    "trace_id",
    "provider",
    "model",
    "status",
    "quality",
    "progress",
    "stage",
    "duration_ms",
    "warning_count",
    "http_status",
    "error_code",
    "retryable",
    "exception_type",
    "error_summary",
    "chunk_id",
    "chunk_index",
    "chunk_count",
    "source_char_count",
    "source_sha256",
    "candidate_count",
    "accepted_count",
}


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _string_values(nested)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for nested in value for item in _string_values(nested)]
    return []


def _safe_metadata_value(value: object) -> object:
    if isinstance(value, str):
        return scrub_error(value).replace("\r", " ").replace("\n", " ")[:256]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return scrub_error(str(value)).replace("\r", " ").replace("\n", " ")[:256]


class SemanticEventLogger:
    def __init__(self, path: Path, *, max_bytes: int, backup_count: int) -> None:
        logger_name = f"app.semantic_analysis.{hashlib.sha256(str(path).encode()).hexdigest()}"
        self.path = path
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handler: logging.Handler = RotatingFileHandler(
                    path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                    delay=True,
                )
            except OSError:
                handler = logging.NullHandler()
            setattr(handler, "_semantic_analysis_path", path)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    @property
    def handlers(self) -> list[logging.Handler]:
        return self._logger.handlers

    def queued(self, **metadata: Any) -> None:
        self._emit("queued", **metadata)

    def started(self, **metadata: Any) -> None:
        self._emit("started", **metadata)

    def completed(self, **metadata: Any) -> None:
        self._emit("completed", **metadata)

    def failed(self, **metadata: Any) -> None:
        self._emit("failed", **metadata)

    def interrupted(self, **metadata: Any) -> None:
        self._emit("interrupted", **metadata)

    def _emit(self, event: str, **metadata: Any) -> None:
        try:
            exception = metadata.pop("exception", None)
            disallowed_values = [
                item
                for key, value in metadata.items()
                if key not in _ALLOWED_METADATA
                for item in _string_values(value)
            ]
            payload: dict[str, object] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "error" if event == "failed" else "info",
                "event": event,
            }
            for key in _ALLOWED_METADATA:
                if key in metadata:
                    payload[key] = _safe_metadata_value(metadata[key])
            if isinstance(exception, BaseException):
                summary = scrub_error(exception)
                for value in sorted(disallowed_values, key=len, reverse=True):
                    summary = summary.replace(value, "[REDACTED]")
                summary = re.sub(
                    r"(?i)authorization|api[_ -]?key|source[_ -]?text|prompt|response[_ -]?body|traceback",
                    "[REDACTED]",
                    summary,
                )
                payload["exception_type"] = type(exception).__name__[:128]
                payload["error_summary"] = summary.replace("\r", " ").replace("\n", " ")[:512]
            with _LOGGER_LOCK:
                self._logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                for handler in self._logger.handlers:
                    if isinstance(handler, RotatingFileHandler):
                        handler.flush()
                        handler.close()
        except Exception:
            # Telemetry is best effort and must never alter analysis behavior.
            return


def semantic_event_logger(
    data_root: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> SemanticEventLogger:
    """Return one Windows-safe semantic logger for each resolved data root."""
    root = Path(data_root).resolve(strict=False)
    path = (root / "logs" / "semantic-analysis.jsonl").resolve(strict=False)
    bounded_bytes = max(1, int(max_bytes))
    bounded_backups = max(0, int(backup_count))
    with _LOGGER_LOCK:
        existing = _LOGGERS.get(path)
        if existing is not None:
            return existing
        created = SemanticEventLogger(
            path,
            max_bytes=bounded_bytes,
            backup_count=bounded_backups,
        )
        _LOGGERS[path] = created
        return created
