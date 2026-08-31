from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from app.semantic_logging import semantic_event_logger
except ImportError as error:
    _SEMANTIC_LOGGING_IMPORT_ERROR: ImportError | None = error
else:
    _SEMANTIC_LOGGING_IMPORT_ERROR = None


def _logging() -> None:
    if _SEMANTIC_LOGGING_IMPORT_ERROR is not None:
        pytest.fail(f"semantic logging module must be importable: {_SEMANTIC_LOGGING_IMPORT_ERROR}")


def _rendered_logs(data_root: Path) -> str:
    paths = sorted((data_root / "logs").glob("semantic-analysis.jsonl*"))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_semantic_log_allows_only_metadata_and_redacts_exception_values(tmp_path: Path) -> None:
    _logging()
    logger = semantic_event_logger(tmp_path)

    logger.failed(
        run_id="run-1",
        trace_id="trace-1",
        project_id="demo",
        source_revision_id="script-r001",
        provider="fake-provider",
        model="fake-model",
        stage="analyze",
        error_code="semantic_provider_upstream",
        http_status=502,
        retryable=True,
        exception=RuntimeError(
            "SECRET SCRIPT sk-secret Authorization: Bearer token raw-provider-body"
        ),
        source_text="SECRET SCRIPT",
        api_key="sk-secret",
        Authorization="Bearer token",
        response_body="raw-provider-body",
        traceback="traceback secret",
        arbitrary_details={"private": "value"},
    )

    rendered = _rendered_logs(tmp_path)
    payload = json.loads(rendered)
    assert payload["event"] == "failed"
    assert payload["run_id"] == "run-1"
    assert payload["trace_id"] == "trace-1"
    assert payload["exception_type"] == "RuntimeError"
    assert payload["http_status"] == 502
    assert payload["retryable"] is True
    for forbidden in (
        "SECRET SCRIPT",
        "sk-secret",
        "Authorization",
        "Bearer token",
        "raw-provider-body",
        "traceback secret",
        "arbitrary_details",
        "source_text",
        "api_key",
        "response_body",
        "traceback",
    ):
        assert forbidden not in rendered


def test_semantic_logger_drops_unknown_fields_but_keeps_safe_chunk_metadata(tmp_path: Path) -> None:
    _logging()
    logger = semantic_event_logger(tmp_path)

    logger.started(
        run_id="run-safe",
        trace_id="trace-safe",
        project_id="demo",
        source_revision_id="script-r001",
        draft_id="draft-1",
        status="running",
        progress=0.25,
        chunk_id="chunk-0001",
        chunk_index=1,
        chunk_count=3,
        unknown="must disappear",
    )

    payload = json.loads(_rendered_logs(tmp_path))
    assert payload["event"] == "started"
    assert payload["chunk_id"] == "chunk-0001"
    assert payload["chunk_index"] == 1
    assert payload["chunk_count"] == 3
    assert payload["progress"] == 0.25
    assert "unknown" not in payload


def test_semantic_logger_is_idempotent_and_closes_handler_after_each_write(tmp_path: Path) -> None:
    _logging()
    first = semantic_event_logger(tmp_path)
    second = semantic_event_logger(tmp_path)

    assert first is second
    assert len(first.handlers) == 1
    first.queued(run_id="run-1", trace_id="trace-1", project_id="demo")

    lines = _rendered_logs(tmp_path).splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "run-1"
    assert first.handlers[0].stream is None


def test_semantic_logger_rotates_deterministically_and_honors_backup_count(tmp_path: Path) -> None:
    _logging()
    logger = semantic_event_logger(tmp_path, max_bytes=220, backup_count=2)

    for index in range(30):
        logger.queued(
            run_id=f"run-{index:03d}",
            trace_id=f"trace-{index:03d}",
            project_id="demo",
            source_revision_id="script-r001",
        )

    paths = sorted((tmp_path / "logs").glob("semantic-analysis.jsonl*"))
    assert 1 < len(paths) <= 3
    assert not (tmp_path / "logs" / "semantic-analysis.jsonl.3").exists()
    assert all(path.stat().st_size > 0 for path in paths)
    assert all(json.loads(line)["event"] == "queued" for path in paths for line in path.read_text(encoding="utf-8").splitlines())
    assert logger.handlers[0].stream is None
