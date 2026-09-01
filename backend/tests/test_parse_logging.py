import json

from app.parse_logging import log_parse_event, parse_attempt_logger


def test_parse_logger_drops_non_allowlisted_content_and_diagnostics(tmp_path) -> None:
    logger = parse_attempt_logger(tmp_path)

    log_parse_event(
        logger,
        attempt_id="attempt-safe",
        project_id="project-safe",
        script_revision_id="script-r001",
        source_length=42,
        provider="provider-safe",
        model="model-safe",
        timestamp="2026-09-01T00:00:00+00:00",
        event="failed",
        elapsed_ms=10,
        http_status=422,
        failure_category="quality_rejected",
        reason_codes=["missing_dialogue_coverage", "SECRET-REASON"],
        diagnostics=[
            {
                "stage": "verify",
                "attempt_phase": "repair",
                "issues": [
                    {"type": "missing_dialogue_coverage", "path": "verifier.dialogue_coverage"},
                    {"type": "SECRET-ISSUE", "path": "SECRET-PATH"},
                ],
                "counts": {
                    "reference_candidate_count": 14,
                    "normalized_line_count": 9,
                    "SECRET-COUNT": 1,
                },
            }
        ],
        source_text="SECRET-SCRIPT",
        api_key="SECRET-API-KEY",
        prompt="SECRET-PROMPT",
        raw_response="SECRET-RESPONSE",
    )

    payload = json.loads((tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8"))
    assert payload == {
        "attempt_id": "attempt-safe",
        "project_id": "project-safe",
        "script_revision_id": "script-r001",
        "source_length": 42,
        "provider": "provider-safe",
        "model": "model-safe",
        "timestamp": "2026-09-01T00:00:00+00:00",
        "event": "failed",
        "elapsed_ms": 10,
        "http_status": 422,
        "failure_category": "quality_rejected",
        "reason_codes": ["missing_dialogue_coverage"],
        "diagnostics": [
            {
                "stage": "verify",
                "attempt_phase": "repair",
                "issues": [{"type": "missing_dialogue_coverage", "path": "verifier.dialogue_coverage"}],
                "counts": {"reference_candidate_count": 14, "normalized_line_count": 9},
            }
        ],
    }
    rendered = json.dumps(payload)
    for secret in (
        "SECRET-SCRIPT",
        "SECRET-API-KEY",
        "SECRET-PROMPT",
        "SECRET-RESPONSE",
        "SECRET-ISSUE",
        "SECRET-PATH",
        "SECRET-COUNT",
        "SECRET-REASON",
    ):
        assert secret not in rendered


def test_parse_logger_rejects_unsafe_values_even_for_allowlisted_fields(tmp_path) -> None:
    logger = parse_attempt_logger(tmp_path)

    log_parse_event(
        logger,
        attempt_id={"raw_response": "SECRET-RESPONSE"},
        project_id=["SECRET-SCRIPT"],
        script_revision_id=object(),
        source_length="SECRET-LENGTH",
        provider={"api_key": "SECRET-API-KEY"},
        model=["SECRET-MODEL"],
        timestamp="2026-09-01T00:00:00+00:00\nSECRET-TIMESTAMP",
        event="failed",
        elapsed_ms=True,
        http_status=422,
        failure_category="quality_rejected",
        reason_codes=[],
    )

    payload = json.loads((tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8"))
    assert payload == {
        "event": "failed",
        "http_status": 422,
        "failure_category": "quality_rejected",
        "reason_codes": [],
    }
    rendered = json.dumps(payload)
    assert "SECRET" not in rendered


def test_parse_logger_applies_record_limit_after_filtering_invalid_diagnostics(tmp_path) -> None:
    logger = parse_attempt_logger(tmp_path)

    log_parse_event(
        logger,
        event="failed",
        http_status=422,
        failure_category="quality_rejected",
        reason_codes=["missing_dialogue_coverage"],
        diagnostics=[
            *[{"stage": "SECRET-STAGE", "issues": []} for _index in range(65)],
            {
                "stage": "verify",
                "attempt_phase": "repair",
                "issues": [
                    *[{"type": "SECRET-ISSUE", "path": "SECRET-PATH"} for _index in range(25)],
                    {"type": "missing_dialogue_coverage", "path": "verifier.dialogue_coverage"}
                ],
                "counts": {"reference_candidate_count": 14, "normalized_line_count": 9},
            },
        ],
    )

    payload = json.loads((tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8"))
    assert payload["diagnostics"] == [
        {
            "stage": "verify",
            "attempt_phase": "repair",
            "issues": [{"type": "missing_dialogue_coverage", "path": "verifier.dialogue_coverage"}],
            "counts": {"reference_candidate_count": 14, "normalized_line_count": 9},
        }
    ]
