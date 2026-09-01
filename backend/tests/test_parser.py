import json
from pathlib import Path

import pytest

from app.parser import (
    AnthropicProvider,
    LineSourceEvidence,
    MultiProviderParser,
    OpenAICompatibleProvider,
    ParsedScriptDraft,
    ParserDiagnosticIssue,
    ParserDiagnosticRecord,
    ParserProviderConfig,
    ParserProviderUnavailable,
    ParserQualityError,
    ScriptParseVerifier,
    _decode_anthropic_tool_input,
    _draft_from_provider_payload,
    _tag_diagnostic_phase,
    chat_completions_url,
    parser_contract_probe_messages,
)
from app.parser_config import ParserProviderRecord, ParserProvidersUpdate, load_parser_providers, save_parser_providers
from app.models import Character, ScriptLine


def make_draft(
    *,
    characters: list[Character] | None = None,
    lines: list[ScriptLine] | None = None,
    source_evidence: dict[str, LineSourceEvidence] | None = None,
    provider: str = "llm-test",
) -> ParsedScriptDraft:
    return ParsedScriptDraft(
        provider=provider,
        characters=characters or [Character(id="narrator", name="NARRATOR")],
        lines=lines or [ScriptLine(id="l001", character_id="narrator", text="Hello.", language="en")],
        source_evidence=source_evidence or {},
    )


def test_script_parse_verifier_accepts_llm_draft_with_traceable_dialogue() -> None:
    source = "**NARRATOR**\n(calm)\nHello."
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", note="calm", text="Hello.", language="en")])

    ScriptParseVerifier().verify(source, draft)


def test_script_parse_verifier_rejects_non_dialogue_roles() -> None:
    draft = make_draft(
        characters=[Character(id="sfx", name="SFX")],
        lines=[ScriptLine(id="l001", character_id="sfx", text="Rain hits metal.", language="en")],
    )

    with pytest.raises(ParserQualityError, match="non-dialogue role SFX"):
        ScriptParseVerifier().verify("> **SFX**: Rain hits metal.", draft)


def test_script_parse_verifier_rejects_empty_line_text() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", text="", language="en")])

    with pytest.raises(ParserQualityError, match="l001 has empty text"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.", draft)


def test_script_parse_verifier_rejects_duplicate_line_ids() -> None:
    draft = make_draft(
        lines=[
            ScriptLine(id="l001", character_id="narrator", text="Hello.", language="en"),
            ScriptLine(id="l001", character_id="narrator", text="Again.", language="en"),
        ]
    )

    with pytest.raises(ParserQualityError, match="duplicate normalized line id: l001"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.\nAgain.", draft)


def test_script_parse_verifier_rejects_unknown_character_reference() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="ghost", text="Hello.", language="en")])

    with pytest.raises(ParserQualityError, match="l001 references unknown character ghost"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.", draft)


def test_script_parse_verifier_rejects_untraceable_text() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", text="Invented line.", language="en")])

    with pytest.raises(ParserQualityError, match="l001 text is not an exact source match in source order"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.", draft)


def test_script_parse_verifier_rejects_dialogue_punctuation_mutation() -> None:
    draft = make_draft(
        characters=[Character(id="pang-bai", name="旁白")],
        lines=[ScriptLine(id="l001", character_id="pang-bai", text="不要动。", language="zh")],
    )

    with pytest.raises(ParserQualityError, match="l001 text is not an exact source match"):
        ScriptParseVerifier().verify("旁白: 不要动！", draft)


def test_script_parse_verifier_accepts_wrapping_quotes_removed_from_prose_dialogue() -> None:
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏")],
        lines=[ScriptLine(id="l001", character_id="lin-xia", text="我们马上出发。", language="zh")],
    )

    ScriptParseVerifier().verify("报道写道，林夏说：“我们马上出发。”随后离开。", draft)


def test_provider_payload_rejects_source_text_that_differs_from_dialogue() -> None:
    payload = {
        "characters": [{"id": "narrator", "name": "NARRATOR"}],
        "lines": [
            {
                "id": "l001",
                "character_id": "narrator",
                "text": "Hello.",
                "source_text": "Hello!",
                "source_excerpt": "NARRATOR: Hello!",
            }
        ],
    }

    with pytest.raises(ParserQualityError, match="source_text does not match text"):
        _draft_from_provider_payload("llm-test", payload)


def test_provider_payload_rejects_missing_source_text() -> None:
    payload = {
        "characters": [{"id": "narrator", "name": "NARRATOR"}],
        "lines": [
            {
                "id": "l001",
                "character_id": "narrator",
                "text": "Hello.",
                "source_excerpt": "NARRATOR: Hello.",
            }
        ],
    }

    with pytest.raises(ParserQualityError, match="line 1 missing source_text") as exc_info:
        _draft_from_provider_payload("llm-test", payload)

    assert [item.model_dump(mode="json") for item in exc_info.value.diagnostics] == [
        {
            "stage": "normalize",
            "attempt_phase": None,
            "issues": [
                {"type": "missing_field", "path": "provider.payload.lines.*.source_text"},
                {"type": "partial_normalization_scan", "path": "provider.payload.lines.*"},
            ],
            "counts": {
                "raw_line_item_count": 1,
                "processed_line_item_count": 1,
                "normalized_line_count": 0,
                "dropped_non_object_count": 0,
                "dropped_missing_speaker_count": 0,
                "dropped_empty_text_count": 0,
                "dropped_non_dialogue_role_count": 0,
            },
        }
    ]


def test_quality_error_normalizes_mapping_diagnostics_before_phase_tagging() -> None:
    error = ParserQualityError(
        "contract failed",
        diagnostics=[
            {
                "stage": "verify",
                "issues": [{"type": "quality_contract_violation", "path": "verifier.contract"}],
            }
        ],  # type: ignore[list-item]
    )

    _tag_diagnostic_phase(error, "repair")

    assert [item.model_dump(mode="json") for item in error.diagnostics] == [
        {
            "stage": "verify",
            "attempt_phase": "repair",
            "issues": [{"type": "quality_contract_violation", "path": "verifier.contract"}],
            "counts": {},
        }
    ]


def test_provider_payload_reports_empty_text_path_before_missing_source_failure() -> None:
    payload = {
        "characters": [{"id": "narrator", "name": "NARRATOR"}],
        "lines": [
            {
                "character_id": "narrator",
                "text": "",
                "source_text": "",
                "source_excerpt": "NARRATOR:",
            }
        ],
    }

    with pytest.raises(ParserQualityError, match="line 1 missing source_text") as exc_info:
        _draft_from_provider_payload("llm-test", payload)

    assert exc_info.value.diagnostics[0].issues == [
        ParserDiagnosticIssue(type="missing_field", path="provider.payload.lines.*.source_text"),
        ParserDiagnosticIssue(type="normalized_line_dropped", path="provider.payload.lines.*.text"),
        ParserDiagnosticIssue(type="partial_normalization_scan", path="provider.payload.lines.*"),
    ]


def test_provider_payload_marks_prefix_counts_when_later_line_fails() -> None:
    payload = {
        "characters": [{"id": "narrator", "name": "NARRATOR"}],
        "lines": [
            {
                "character_id": "narrator",
                "text": "First.",
                "source_text": "First.",
                "source_excerpt": "NARRATOR: First.",
            },
            {
                "character_id": "narrator",
                "text": "Second.",
                "source_excerpt": "NARRATOR: Second.",
            },
            "not-an-object",
        ],
    }

    with pytest.raises(ParserQualityError, match="line 2 missing source_text") as exc_info:
        _draft_from_provider_payload("llm-test", payload)

    diagnostic = exc_info.value.diagnostics[0].model_dump(mode="json")
    assert diagnostic["issues"] == [
        {"type": "missing_field", "path": "provider.payload.lines.*.source_text"},
        {"type": "partial_normalization_scan", "path": "provider.payload.lines.*"},
    ]
    assert diagnostic["counts"] == {
        "raw_line_item_count": 3,
        "processed_line_item_count": 2,
        "normalized_line_count": 1,
        "dropped_non_object_count": 0,
        "dropped_missing_speaker_count": 0,
        "dropped_empty_text_count": 0,
        "dropped_non_dialogue_role_count": 0,
    }


def test_provider_payload_rejects_missing_source_excerpt() -> None:
    payload = {
        "characters": [{"id": "narrator", "name": "NARRATOR"}],
        "lines": [
            {
                "id": "l001",
                "character_id": "narrator",
                "text": "Hello.",
                "source_text": "Hello.",
            }
        ],
    }

    with pytest.raises(ParserQualityError, match="line 1 missing source_excerpt"):
        _draft_from_provider_payload("llm-test", payload)


def test_provider_payload_keeps_source_evidence_out_of_serialized_draft() -> None:
    payload = {
        "characters": [{"id": "narrator", "name": "NARRATOR"}],
        "lines": [
            {
                "id": "l001",
                "character_id": "narrator",
                "text": "Hello.",
                "source_text": "Hello.",
                "source_excerpt": "NARRATOR: Hello.",
            }
        ],
    }

    draft = _draft_from_provider_payload("llm-test", payload)

    assert draft.source_evidence["l001"].source_text == "Hello."
    assert "source_evidence" not in draft.model_dump(mode="json")
    assert "diagnostic_counts" not in draft.model_dump(mode="json")
    assert "normalization_issues" not in draft.model_dump(mode="json")
    assert "diagnostic_counts" not in draft.model_dump_json()
    assert "normalization_issues" not in draft.model_dump_json()


def test_script_parse_verifier_rejects_out_of_order_dialogue() -> None:
    draft = make_draft(
        characters=[Character(id="alice", name="ALICE"), Character(id="bob", name="BOB")],
        lines=[
            ScriptLine(id="l001", character_id="bob", text="Second line.", language="en"),
            ScriptLine(id="l002", character_id="alice", text="First line.", language="en"),
        ],
    )

    with pytest.raises(ParserQualityError, match="l002 text is not an exact source match in source order"):
        ScriptParseVerifier().verify("ALICE: First line.\nBOB: Second line.", draft)


def test_script_parse_verifier_rejects_missing_recognizable_dialogue() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", text="First line.", language="en")])

    with pytest.raises(ParserQualityError, match="missing dialogue lines: expected at least 2, got 1") as exc_info:
        ScriptParseVerifier().verify("NARRATOR: First line.\nNARRATOR: Second line.", draft)

    diagnostic = exc_info.value.diagnostics[0].model_dump(mode="json")
    assert diagnostic["stage"] == "verify"
    assert diagnostic["attempt_phase"] is None
    assert {tuple(sorted(issue.items())) for issue in diagnostic["issues"]} >= {
        tuple(sorted({"type": "missing_dialogue_coverage", "path": "verifier.dialogue_coverage"}.items()))
    }
    assert diagnostic["counts"] == {
        "normalized_line_count": 1,
        "reference_candidate_count": 2,
        "reference_colon_candidate_count": 2,
        "reference_markdown_candidate_count": 0,
    }


def test_verifier_reports_provider_field_paths_for_normalized_line_drops() -> None:
    raw_lines: list[object] = [
        {
            "character_id": "narrator",
            "text": f"Line {index}.",
            "source_text": f"Line {index}.",
            "source_excerpt": f"NARRATOR: Line {index}.",
        }
        for index in range(1, 10)
    ]
    raw_lines.extend(
        [
            {
                "text": f"Line {index}.",
                "source_text": f"Line {index}.",
                "source_excerpt": f"NARRATOR: Line {index}.",
            }
            for index in range(10, 12)
        ]
    )
    raw_lines.extend(["not-an-object"] * 3)
    payload = {
        "characters": [{"id": "narrator", "name": "NARRATOR"}],
        "lines": raw_lines,
    }
    draft = _draft_from_provider_payload("llm-test", payload)
    source = "\n".join(f"NARRATOR: Line {index}." for index in range(1, 15))

    with pytest.raises(ParserQualityError, match="expected at least 14, got 9") as exc_info:
        ScriptParseVerifier().verify(source, draft)

    diagnostics = [item.model_dump(mode="json") for item in exc_info.value.diagnostics]
    assert [item["stage"] for item in diagnostics] == ["normalize", "verify"]
    assert diagnostics[0]["issues"] == [
        {"type": "normalized_line_dropped", "path": "provider.payload.lines.*.speaker"},
        {"type": "normalized_line_dropped", "path": "provider.payload.lines.*"},
    ]
    assert diagnostics[0]["counts"] == {
        "raw_line_item_count": 14,
        "processed_line_item_count": 14,
        "normalized_line_count": 9,
        "dropped_non_object_count": 3,
        "dropped_missing_speaker_count": 2,
        "dropped_empty_text_count": 0,
        "dropped_non_dialogue_role_count": 0,
    }
    assert diagnostics[1]["issues"] == [
        {"type": "missing_dialogue_coverage", "path": "verifier.dialogue_coverage"}
    ]


def test_script_parse_verifier_rejects_fabricated_source_excerpt_not_in_source() -> None:
    draft = make_draft(
        characters=[Character(id="alice", name="ALICE")],
        lines=[ScriptLine(id="l001", character_id="alice", text="Hello.", language="en")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="Hello.",
                source_excerpt="ALICE: A fabricated line.",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="l001 source_excerpt is not traceable in source"):
        ScriptParseVerifier().verify("ALICE: Hello.", draft)


def test_script_parse_verifier_rejects_source_excerpt_that_omits_dialogue() -> None:
    draft = make_draft(
        characters=[Character(id="alice", name="ALICE")],
        lines=[ScriptLine(id="l001", character_id="alice", text="Hello there.", language="en")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="Hello there.",
                source_excerpt="ALICE:",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="l001 source_excerpt does not contain source_text"):
        ScriptParseVerifier().verify("ALICE: Hello there.", draft)


def test_script_parse_verifier_rejects_missing_attributed_prose_quote() -> None:
    source = '记者会上，林夏说：“先撤离。” 随后，周明补充：“别回头。”'
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏")],
        lines=[ScriptLine(id="l001", character_id="lin-xia", text="先撤离。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“先撤离。”",
                source_excerpt="林夏说：“先撤离。”",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="missing quoted dialogue coverage: expected at least 2, got 1"):
        ScriptParseVerifier().verify(source, draft)


def test_script_parse_verifier_accepts_complete_attributed_prose_quotes() -> None:
    source = '记者会上，林夏说：“先撤离。” 随后，周明补充：“别回头。”'
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏"), Character(id="zhou-ming", name="周明")],
        lines=[
            ScriptLine(id="l001", character_id="lin-xia", text="先撤离。", language="zh"),
            ScriptLine(id="l002", character_id="zhou-ming", text="别回头。", language="zh"),
        ],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“先撤离。”",
                source_excerpt="林夏说：“先撤离。”",
            ),
            "l002": LineSourceEvidence(
                source_text="“别回头。”",
                source_excerpt="周明补充：“别回头。”",
            ),
        },
    )

    ScriptParseVerifier().verify(source, draft)


def test_script_parse_verifier_rejects_missing_chinese_quote_before_attribution() -> None:
    source = "“先撤离。”林夏说。随后，“别回头。”周明补充。"
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏")],
        lines=[ScriptLine(id="l001", character_id="lin-xia", text="先撤离。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“先撤离。”",
                source_excerpt="“先撤离。”林夏说。",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="missing quoted dialogue coverage: expected at least 2, got 1"):
        ScriptParseVerifier().verify(source, draft)


def test_script_parse_verifier_accepts_complete_chinese_quote_before_attribution() -> None:
    source = "“先撤离。”林夏说。随后，“别回头。”周明补充。"
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏"), Character(id="zhou-ming", name="周明")],
        lines=[
            ScriptLine(id="l001", character_id="lin-xia", text="先撤离。", language="zh"),
            ScriptLine(id="l002", character_id="zhou-ming", text="别回头。", language="zh"),
        ],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“先撤离。”",
                source_excerpt="“先撤离。”林夏说。",
            ),
            "l002": LineSourceEvidence(
                source_text="“别回头。”",
                source_excerpt="“别回头。”周明补充。",
            ),
        },
    )

    ScriptParseVerifier().verify(source, draft)


def test_script_parse_verifier_rejects_source_excerpt_with_wrong_colon_speaker() -> None:
    draft = make_draft(
        characters=[Character(id="alice", name="ALICE"), Character(id="bob", name="BOB")],
        lines=[ScriptLine(id="l001", character_id="bob", text="Yes.", language="en")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="Yes.",
                source_excerpt="ALICE: Yes.",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="l001 source_excerpt speaker ALICE does not match character BOB"):
        ScriptParseVerifier().verify("ALICE: Yes.", draft)


def test_script_parse_verifier_rejects_source_excerpt_with_wrong_markdown_speaker() -> None:
    draft = make_draft(
        characters=[Character(id="alice", name="ALICE"), Character(id="bob", name="BOB")],
        lines=[ScriptLine(id="l001", character_id="bob", text="Yes.", language="en")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="Yes.",
                source_excerpt="**ALICE**\nYes.",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="l001 source_excerpt speaker ALICE does not match character BOB"):
        ScriptParseVerifier().verify("**ALICE**\nYes.", draft)


def test_provider_payload_rejects_source_excerpt_with_wrong_markdown_speaker() -> None:
    payload = {
        "characters": [{"id": "alice", "name": "ALICE"}, {"id": "bob", "name": "BOB"}],
        "lines": [
            {
                "id": "l001",
                "character_id": "bob",
                "text": "Yes.",
                "source_text": "Yes.",
                "source_excerpt": "**ALICE**\nYes.",
            }
        ],
    }
    draft = _draft_from_provider_payload("llm-test", payload)

    with pytest.raises(ParserQualityError, match="l001 source_excerpt speaker ALICE does not match character BOB"):
        ScriptParseVerifier().verify("**ALICE**\nYes.", draft)


def test_script_parse_verifier_accepts_source_excerpt_with_matching_markdown_speaker() -> None:
    draft = make_draft(
        characters=[Character(id="alice", name="ALICE")],
        lines=[ScriptLine(id="l001", character_id="alice", text="Yes.", language="en")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="Yes.",
                source_excerpt="**ALICE**\nYes.",
            )
        },
    )

    ScriptParseVerifier().verify("**ALICE**\nYes.", draft)


def test_script_parse_verifier_rejects_source_excerpt_with_wrong_prose_speaker() -> None:
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏"), Character(id="zhou-ming", name="周明")],
        lines=[ScriptLine(id="l001", character_id="zhou-ming", text="先撤离。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“先撤离。”",
                source_excerpt="林夏说：“先撤离。”",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="l001 source_excerpt speaker 林夏 does not match character 周明"):
        ScriptParseVerifier().verify("林夏说：“先撤离。”", draft)


def test_script_parse_verifier_accepts_context_prefixed_chinese_prose_speaker() -> None:
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏")],
        lines=[ScriptLine(id="l001", character_id="lin-xia", text="先撤离。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="先撤离。",
                source_excerpt="记者会上，林夏说：“先撤离。”",
            )
        },
    )

    ScriptParseVerifier().verify("记者会上，林夏说：“先撤离。”", draft)


def test_script_parse_verifier_accepts_ambiguous_pronoun_attributed_quote() -> None:
    draft = make_draft(
        characters=[Character(id="lin-xia", name="林夏")],
        lines=[ScriptLine(id="l001", character_id="lin-xia", text="先撤离。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="先撤离。",
                source_excerpt="她说：“先撤离。”",
            )
        },
    )

    ScriptParseVerifier().verify("她说：“先撤离。”", draft)


def test_script_parse_verifier_accepts_context_prefixed_chinese_speaker_with_verb() -> None:
    draft = make_draft(
        characters=[Character(id="zhou-ming", name="周明")],
        lines=[ScriptLine(id="l001", character_id="zhou-ming", text="别回头。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="别回头。",
                source_excerpt="随后，周明补充：“别回头。”",
            )
        },
    )

    ScriptParseVerifier().verify("随后，周明补充：“别回头。”", draft)


def test_script_parse_verifier_does_not_treat_prose_action_prefix_as_speaker_anchor() -> None:
    draft = make_draft(
        characters=[Character(id="zhuge-jiujiu", name="诸葛九九")],
        lines=[ScriptLine(id="l001", character_id="zhuge-jiujiu", text="我来了。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“我来了。”",
                source_excerpt="风声掠过，诸葛九九沉默片刻，说：“我来了。”",
            )
        },
    )

    ScriptParseVerifier().verify("风声掠过，诸葛九九沉默片刻，说：“我来了。”", draft)


def test_script_parse_verifier_accepts_unique_terminal_short_name_alias() -> None:
    draft = make_draft(
        characters=[Character(id="zhuge-jiujiu", name="诸葛九九")],
        lines=[ScriptLine(id="l001", character_id="zhuge-jiujiu", text="我来了。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“我来了。”",
                source_excerpt="九九说：“我来了。”",
            )
        },
    )

    ScriptParseVerifier().verify("九九说：“我来了。”", draft)


def test_script_parse_verifier_rejects_ambiguous_short_name_alias() -> None:
    draft = make_draft(
        characters=[
            Character(id="zhuge-jiujiu", name="诸葛九九"),
            Character(id="wang-jiujiu", name="王九九"),
        ],
        lines=[ScriptLine(id="l001", character_id="zhuge-jiujiu", text="我来了。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“我来了。”",
                source_excerpt="九九说：“我来了。”",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="speaker 九九 does not match character 诸葛九九"):
        ScriptParseVerifier().verify("九九说：“我来了。”", draft)


def test_script_parse_verifier_rejects_wrong_single_character_colon_label() -> None:
    draft = make_draft(
        characters=[Character(id="yi", name="乙")],
        lines=[ScriptLine(id="l001", character_id="yi", text="台词。", language="zh")],
        source_evidence={"l001": LineSourceEvidence(source_text="台词。", source_excerpt="甲：台词。")},
    )

    with pytest.raises(ParserQualityError, match="speaker 甲 does not match character 乙"):
        ScriptParseVerifier().verify("甲：台词。", draft)


def test_script_parse_verifier_rejects_wrong_cjk_extension_colon_label() -> None:
    draft = make_draft(
        characters=[Character(id="yi", name="乙")],
        lines=[ScriptLine(id="l001", character_id="yi", text="台词。", language="zh")],
        source_evidence={"l001": LineSourceEvidence(source_text="台词。", source_excerpt="𠀀：台词。")},
    )

    with pytest.raises(ParserQualityError, match="speaker 𠀀 does not match character 乙"):
        ScriptParseVerifier().verify("𠀀：台词。", draft)


def test_script_parse_verifier_downgrades_prose_attribute_after_contextual_subject() -> None:
    draft = make_draft(
        characters=[Character(id="narrator", name="旁白")],
        lines=[ScriptLine(id="l001", character_id="narrator", text="别过来。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“别过来。”",
                source_excerpt="装备党面色惨白，声音颤抖，说：“别过来。”",
            )
        },
    )

    ScriptParseVerifier().verify("装备党面色惨白，声音颤抖，说：“别过来。”", draft)


def test_script_parse_verifier_downgrades_prose_attribute_after_narrative_object() -> None:
    draft = make_draft(
        characters=[Character(id="narrator", name="旁白")],
        lines=[ScriptLine(id="l001", character_id="narrator", text="别过来。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“别过来。”",
                source_excerpt="胶布被阴影遮住，声音颤抖，说：“别过来。”",
            )
        },
    )

    ScriptParseVerifier().verify("胶布被阴影遮住，声音颤抖，说：“别过来。”", draft)


def test_script_parse_verifier_rejects_contextual_named_prose_speaker_mismatch() -> None:
    draft = make_draft(
        characters=[Character(id="zhou-ming", name="周明")],
        lines=[ScriptLine(id="l001", character_id="zhou-ming", text="先撤离。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“先撤离。”",
                source_excerpt="记者会上，林夏说：“先撤离。”",
            )
        },
    )

    with pytest.raises(ParserQualityError, match="speaker 林夏 does not match character 周明"):
        ScriptParseVerifier().verify("记者会上，林夏说：“先撤离。”", draft)


def test_script_parse_verifier_downgrades_colon_label_with_known_character_action_suffix() -> None:
    draft = make_draft(
        characters=[Character(id="equipment", name="装备党")],
        lines=[ScriptLine(id="l001", character_id="equipment", text="别过来。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="“别过来。”",
                source_excerpt="**装备党**声音洪亮且带着一丝决绝：“别过来。”",
            )
        },
    )

    ScriptParseVerifier().verify("**装备党**声音洪亮且带着一丝决绝：“别过来。”", draft)


def test_provider_payload_unifies_unique_short_name_character_to_canonical_full_name() -> None:
    payload = {
        "characters": [
            {"id": "zhuge", "name": "诸葛九九"},
            {"id": "jiujiu", "name": "九九"},
        ],
        "lines": [
            {"id": "l001", "character_id": "zhuge", "text": "第一句。", "source_text": "第一句。", "source_excerpt": "诸葛九九：第一句。"},
            {"id": "l002", "character_id": "jiujiu", "text": "第二句。", "source_text": "第二句。", "source_excerpt": "九九：第二句。"},
        ],
    }

    draft = _draft_from_provider_payload("llm-test", payload)
    verified = ScriptParseVerifier().verify("诸葛九九：第一句。\n九九：第二句。", draft)

    assert [character.name for character in verified.characters] == ["诸葛九九"]
    assert {line.character_id for line in verified.lines} == {verified.characters[0].id}


def test_verifier_canonicalizes_short_character_from_unique_full_source_speaker_anchor() -> None:
    draft = make_draft(
        characters=[Character(id="jiujiu", name="九九")],
        lines=[ScriptLine(id="l001", character_id="jiujiu", text="我来了。", language="zh")],
        source_evidence={
            "l001": LineSourceEvidence(
                source_text="我来了。",
                source_excerpt="诸葛九九：我来了。",
            )
        },
    )

    verified = ScriptParseVerifier().verify("诸葛九九：我来了。", draft)

    assert [character.name for character in verified.characters] == ["诸葛九九"]
    assert verified.lines[0].character_id == verified.characters[0].id


def test_provider_payload_rejects_ambiguous_short_name_character_alias() -> None:
    payload = {
        "characters": [
            {"id": "zhuge", "name": "诸葛九九"},
            {"id": "wang", "name": "王九九"},
            {"id": "jiujiu", "name": "九九"},
        ],
        "lines": [
            {"id": "l001", "character_id": "jiujiu", "text": "台词。", "source_text": "台词。", "source_excerpt": "九九：台词。"},
        ],
    }

    with pytest.raises(ParserQualityError, match="ambiguous short-name character alias 九九") as exc_info:
        _draft_from_provider_payload("llm-test", payload)

    assert exc_info.value.reason_codes == ["ambiguous_short_name_alias"]


def test_provider_payload_does_not_unify_internal_substring_character_name() -> None:
    payload = {
        "characters": [
            {"id": "zhuge", "name": "诸葛九九"},
            {"id": "internal", "name": "葛九"},
        ],
        "lines": [
            {"id": "l001", "character_id": "internal", "text": "台词。", "source_text": "台词。", "source_excerpt": "葛九：台词。"},
        ],
    }

    draft = _draft_from_provider_payload("llm-test", payload)

    assert [character.name for character in draft.characters] == ["葛九"]


def test_parser_quality_error_preserves_message_and_exposes_safe_reason_codes() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", text="Invented.", language="en")])

    with pytest.raises(ParserQualityError) as exc_info:
        ScriptParseVerifier().verify("NARRATOR: Hello.\nNARRATOR: Again.", draft)

    assert "l001 text is not an exact source match" in str(exc_info.value)
    assert exc_info.value.reason_codes == ["missing_dialogue_coverage", "text_not_in_source_order"]
    assert all("Invented" not in reason for reason in exc_info.value.reason_codes)


def test_parser_contract_prompt_requires_agentic_source_fidelity_audit() -> None:
    messages = parser_contract_probe_messages()

    system_prompt = messages[0]["content"].lower()

    assert "internal audit" in system_prompt
    assert "source_text" in system_prompt
    assert "source_excerpt" in system_prompt
    assert "prose" in system_prompt
    assert "news" in system_prompt
    assert "do not reveal" in system_prompt
    assert "exact" in system_prompt


def test_repair_prompt_requires_exact_source_evidence() -> None:
    from app.parser import _REPAIR_PROMPT

    prompt = _REPAIR_PROMPT.lower()

    assert "source_text" in prompt
    assert "source_excerpt" in prompt
    assert "exact" in prompt
    assert "do not rewrite" in prompt


def test_parser_prompts_require_canonical_names_and_explicit_speaker_anchors() -> None:
    from app.parser import _REPAIR_PROMPT

    prompts = f"{parser_contract_probe_messages()[0]['content']}\n{_REPAIR_PROMPT}".lower()

    assert "canonical full name" in prompts
    assert "speaker anchor" in prompts
    assert "narrative" in prompts


def test_multi_provider_parser_requires_enabled_llm_provider_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(
            name="missing",
            base_url="https://example.invalid/v1",
            api_key_env="MISSING_KEY",
            model="fake",
        )
    )

    with pytest.raises(ParserProviderUnavailable, match="missing env MISSING_KEY"):
        MultiProviderParser([provider]).parse("旁白: 天亮了。")


def test_multi_provider_parser_requires_at_least_one_enabled_llm_provider() -> None:
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(
            name="disabled",
            base_url="https://example.invalid/v1",
            api_key_env="DISABLED_KEY",
            model="fake",
            enabled=False,
        )
    )

    with pytest.raises(ParserProviderUnavailable, match="no enabled parser providers"):
        MultiProviderParser([provider]).parse("旁白: 天亮了。")


def test_multi_provider_parser_does_not_fallback_after_quality_failure() -> None:
    class BadQualityProvider:
        name = "bad-quality"

        def parse(self, _text: str):
            raise ParserQualityError("non-dialogue role SFX is not allowed")

    with pytest.raises(ParserQualityError, match="SFX"):
        MultiProviderParser([BadQualityProvider()]).parse("旁白: 天亮了。")


def test_multi_provider_parser_preserves_structured_quality_diagnostics() -> None:
    error = ParserQualityError(
        "missing dialogue lines: expected at least 2, got 1",
        diagnostics=[
            ParserDiagnosticRecord(
                stage="verify",
                attempt_phase="repair",
                issues=[
                    ParserDiagnosticIssue(
                        type="missing_dialogue_coverage",
                        path="verifier.dialogue_coverage",
                    )
                ],
                counts={"reference_candidate_count": 2, "normalized_line_count": 1},
            )
        ],
    )

    class BadQualityProvider:
        name = "bad-quality"

        def parse(self, _text: str):
            raise error

    with pytest.raises(ParserQualityError, match="bad-quality: missing dialogue lines") as exc_info:
        MultiProviderParser([BadQualityProvider()]).parse("旁白: 天亮了。")

    assert exc_info.value.reason_codes == ["missing_dialogue_coverage"]
    assert len(exc_info.value.diagnostics) == 1
    assert exc_info.value.diagnostics[0].provider_index == 0
    assert exc_info.value.diagnostics[0].issues == [
        ParserDiagnosticIssue(
            type="missing_dialogue_coverage",
            path="verifier.dialogue_coverage",
        )
    ]
    assert exc_info.value.diagnostics[0].counts == {
        "reference_candidate_count": 2,
        "normalized_line_count": 1,
    }


def test_openai_provider_accepts_valid_llm_output_without_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                        "characters": [{"id": "narrator", "name": "NARRATOR"}],
                                        "lines": [
                                            {
                                                "id": "l001",
                                                "character_id": "narrator",
                                                "note": "calm",
                                                "text": "Hello.",
                                                "source_text": "Hello.",
                                                "source_excerpt": "**NARRATOR**\n(calm)\nHello.",
                                            }
                                        ],
                                    }
                                )
                            }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(name="openai-test", base_url="https://example.invalid", api_key_env="OPENAI_TEST_KEY", model="fake")
    )

    draft = provider.parse("**NARRATOR**\n(calm)\nHello.")

    assert len(calls) == 1
    assert draft.provider == "openai-test"
    assert draft.lines[0].text == "Hello."


def test_openai_provider_repairs_invalid_quality_output_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")
    calls: list[dict[str, object]] = []
    responses = [
        {
            "characters": [{"id": "sfx", "name": "SFX"}],
            "lines": [
                {
                    "id": "scene-1",
                    "character_id": "sfx",
                    "text": "Rain hits metal.",
                    "source_text": "Rain hits metal.",
                    "source_excerpt": "SFX: Rain hits metal.",
                }
            ],
        },
        {
            "characters": [{"id": "narrator", "name": "NARRATOR"}],
            "lines": [
                {
                    "id": "bad-id",
                    "character_id": "narrator",
                    "note": "(calm)",
                    "text": "**Hello.**",
                    "source_text": "Hello.",
                    "source_excerpt": "**NARRATOR**\n(calm)\nHello.",
                }
            ],
        },
    ]

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": json.dumps(self.payload)}}]}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(responses[len(calls) - 1])

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(name="openai-test", base_url="https://example.invalid", api_key_env="OPENAI_TEST_KEY", model="fake")
    )

    draft = provider.parse("**NARRATOR**\n(calm)\nHello.")

    assert len(calls) == 2
    assert "repair" in str(calls[1]["json"]).lower()
    assert draft.provider == "openai-test"
    assert [character.model_dump(include={"id", "name"}) for character in draft.characters] == [
        {"id": "narrator", "name": "NARRATOR"}
    ]
    assert draft.lines[0].model_dump(include={"id", "character_id", "note", "text", "language"}) == {
        "id": "l001",
        "character_id": "narrator",
        "note": "calm",
        "text": "Hello.",
        "language": "en",
    }
    assert any("repaired" in warning for warning in draft.warnings)


def test_openai_provider_raises_quality_error_when_repair_is_still_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                    {
                                        "characters": [{"id": "sfx", "name": "SFX"}],
                                        "lines": [
                                            {
                                                "id": "l001",
                                                "character_id": "sfx",
                                                "text": "Rain hits metal.",
                                                "source_text": "Rain hits metal.",
                                                "source_excerpt": "SFX: Rain hits metal.",
                                            }
                                        ],
                                    }
                                )
                            }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(name="openai-test", base_url="https://example.invalid", api_key_env="OPENAI_TEST_KEY", model="fake")
    )

    with pytest.raises(ParserQualityError, match="SFX") as exc_info:
        provider.parse("> **SFX**: Rain hits metal.")

    diagnostics = [item.model_dump(mode="json") for item in exc_info.value.diagnostics]
    assert [item["attempt_phase"] for item in diagnostics] == ["initial", "repair"]
    assert all(item["stage"] == "normalize" for item in diagnostics)
    assert all(
        item["issues"] == [{"type": "non_dialogue_role", "path": "provider.payload.characters.*"}]
        for item in diagnostics
    )
    assert all(item["counts"]["raw_line_item_count"] == 1 for item in diagnostics)
    assert all(item["counts"]["normalized_line_count"] == 0 for item in diagnostics)
    assert all(item["counts"]["dropped_non_dialogue_role_count"] == 1 for item in diagnostics)


@pytest.mark.parametrize(
    ("response_payload", "expected_message"),
    [
        ({"choices": [{"message": {"content": "[]"}}]}, "parser response must be a JSON object"),
        ({"choices": [{"message": {"content": "not-json"}}]}, "parser response was not valid JSON"),
        ({}, "parser response envelope was invalid"),
    ],
)
def test_openai_provider_tags_initial_decode_response_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    response_payload: dict[str, object],
    expected_message: str,
) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")
    calls = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return response_payload

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            nonlocal calls
            calls += 1
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(
            name="openai-test",
            base_url="https://example.invalid",
            api_key_env="OPENAI_TEST_KEY",
            model="fake",
        )
    )

    with pytest.raises(ParserQualityError, match=expected_message) as exc_info:
        provider.parse("旁白: 天亮了。")

    assert calls == 1
    assert [item.model_dump(mode="json") for item in exc_info.value.diagnostics] == [
        {
            "stage": "decode",
            "attempt_phase": "initial",
            "issues": [{"type": "invalid_type", "path": "provider.payload"}],
            "counts": {},
        }
    ]


@pytest.mark.parametrize(
    ("repair_payload", "expected_message"),
    [
        ({"choices": [{"message": {"content": "[]"}}]}, "parser response must be a JSON object"),
        ({"choices": [{"message": {"content": "not-json"}}]}, "parser response was not valid JSON"),
        ({}, "parser response envelope was invalid"),
    ],
)
def test_openai_provider_keeps_repair_decode_response_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    repair_payload: dict[str, object],
    expected_message: str,
) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")
    response_payloads = [
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "characters": [{"id": "sfx", "name": "SFX"}],
                                "lines": [
                                    {
                                        "character_id": "sfx",
                                        "text": "Rain hits metal.",
                                        "source_text": "Rain hits metal.",
                                        "source_excerpt": "SFX: Rain hits metal.",
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        },
        repair_payload,
    ]

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.calls = 0

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            response = FakeResponse(response_payloads[self.calls])
            self.calls += 1
            return response

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(
            name="openai-test",
            base_url="https://example.invalid",
            api_key_env="OPENAI_TEST_KEY",
            model="fake",
        )
    )

    with pytest.raises(ParserQualityError, match=expected_message) as exc_info:
        provider.parse("SFX: Rain hits metal.")

    diagnostics = [item.model_dump(mode="json") for item in exc_info.value.diagnostics]
    assert [item["attempt_phase"] for item in diagnostics] == ["initial", "repair"]
    assert diagnostics[-1] == {
        "stage": "decode",
        "attempt_phase": "repair",
        "issues": [{"type": "invalid_type", "path": "provider.payload"}],
        "counts": {},
    }


def test_chat_completions_url_normalizes_kwjm_root_and_legacy_v1_base() -> None:
    assert chat_completions_url("https://kwjm.com") == "https://kwjm.com/v1/chat/completions"
    assert chat_completions_url("https://example.invalid/v1") == "https://example.invalid/v1/chat/completions"
    assert chat_completions_url("https://example.invalid/v1/chat/completions") == "https://example.invalid/v1/chat/completions"


def test_parser_provider_config_defaults_to_openai_compatible_adapter() -> None:
    config = ParserProviderConfig(
        name="legacy-compatible",
        base_url="https://example.invalid/v1",
        api_key_env="LEGACY_API_KEY",
        model="legacy-model",
    )

    assert config.adapter == "openai-compatible"


def test_env_example_documents_supported_parser_provider_keys() -> None:
    content = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    parser_block = content.split("# Multi-provider parser configuration.", 1)[1]
    provider_line = next(
        line for line in parser_block.splitlines() if line.startswith("# TTS_MORE_PARSER_PROVIDERS=")
    )
    providers = json.loads(provider_line.removeprefix("# TTS_MORE_PARSER_PROVIDERS="))

    assert "ANTHROPIC_API_KEY" in parser_block
    assert "GEMINI_API_KEY" in parser_block
    assert "OPENROUTER_API_KEY" in parser_block
    assert "AIHUBMIX_API_KEY" in parser_block
    assert "QIANFAN_API_KEY" not in parser_block
    assert "MISTRAL_API_KEY" not in parser_block
    assert providers == [
        {
            "name": "OpenAI",
            "adapter": "openai-compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-5.5",
            "enabled": True,
        }
    ]


def test_parser_provider_record_persists_adapter(tmp_path: Path) -> None:
    config_path = tmp_path / "parser_providers.json"
    env_path = tmp_path / ".env.local"

    save_parser_providers(
        config_path,
        env_path,
        ParserProvidersUpdate(
            providers=[
                {
                    "name": "Anthropic",
                    "base_url": "https://api.anthropic.com",
                    "api_key_env": "ANTHROPIC_API_KEY",
                    "model": "claude-fable-5",
                    "enabled": True,
                    "timeout_seconds": 60,
                    "priority": 20,
                    "adapter": "anthropic",
                }
            ]
        ),
    )

    loaded = load_parser_providers(config_path)

    assert loaded[0].adapter == "anthropic"
    assert '"adapter": "anthropic"' in config_path.read_text(encoding="utf-8")


def test_build_parser_provider_uses_anthropic_adapter() -> None:
    from app.parser import AnthropicProvider, build_parser_provider

    provider = build_parser_provider(
        ParserProviderConfig(
            name="Anthropic",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-fable-5",
            adapter="anthropic",
        )
    )

    assert isinstance(provider, AnthropicProvider)


def test_anthropic_provider_posts_messages_tool_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-ant-test")
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "emit_tts_parse",
                        "input": {
                            "characters": [{"id": "narrator", "name": "NARRATOR"}],
                            "lines": [
                                {
                                    "id": "l001",
                                    "character_id": "narrator",
                                    "text": "Hello.",
                                    "note": "calm",
                                    "source_text": "Hello.",
                                    "source_excerpt": "**NARRATOR**\n(calm)\nHello.",
                                }
                            ],
                        },
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = AnthropicProvider(
        ParserProviderConfig(
            name="anthropic-test",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_TEST_KEY",
            model="claude-fable-5",
            adapter="anthropic",
        )
    )

    draft = provider.parse("**NARRATOR**\n(calm)\nHello.")

    assert draft.lines[0].text == "Hello."
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-test"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    payload = calls[0]["json"]
    assert payload["model"] == "claude-fable-5"
    assert payload["tool_choice"] == {"type": "tool", "name": "emit_tts_parse"}
    assert payload["tools"][0]["name"] == "emit_tts_parse"


@pytest.mark.parametrize(
    ("response_payload", "expected_message", "issue_type", "issue_path"),
    [
        ({"content": []}, "did not include emit_tts_parse", "missing_field", "provider.payload"),
        ([], "anthropic response envelope was invalid", "invalid_type", "provider.payload"),
        (
            {"content": None},
            "anthropic response content must be a list",
            "invalid_type",
            "provider.payload.content",
        ),
    ],
)
def test_anthropic_provider_tags_initial_decode_error(
    monkeypatch: pytest.MonkeyPatch,
    response_payload: object,
    expected_message: str,
    issue_type: str,
    issue_path: str,
) -> None:
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-ant-test")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return response_payload

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = AnthropicProvider(
        ParserProviderConfig(
            name="anthropic-test",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_TEST_KEY",
            model="claude-fable-5",
            adapter="anthropic",
        )
    )

    with pytest.raises(ParserQualityError, match=expected_message) as exc_info:
        provider.parse("旁白: 天亮了。")

    assert [item.model_dump(mode="json") for item in exc_info.value.diagnostics] == [
        {
            "stage": "decode",
            "attempt_phase": "initial",
            "issues": [{"type": issue_type, "path": issue_path}],
            "counts": {},
        }
    ]


def test_anthropic_provider_keeps_invalid_repair_content_diagnostic() -> None:
    provider = AnthropicProvider(
        ParserProviderConfig(
            name="anthropic-test",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_TEST_KEY",
            model="claude-fable-5",
            adapter="anthropic",
        )
    )
    responses: list[dict[str, object]] = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_tts_parse",
                    "input": {
                        "characters": [{"id": "sfx", "name": "SFX"}],
                        "lines": [
                            {
                                "character_id": "sfx",
                                "text": "Rain hits metal.",
                                "source_text": "Rain hits metal.",
                                "source_excerpt": "SFX: Rain hits metal.",
                            }
                        ],
                    },
                }
            ]
        },
        {"content": None},
    ]

    def fake_post_json(_api_key: str, _messages: list[dict[str, str]]) -> dict[str, object]:
        return _decode_anthropic_tool_input(responses.pop(0))

    provider._post_json = fake_post_json  # type: ignore[method-assign]

    with pytest.raises(ParserQualityError, match="content must be a list") as exc_info:
        provider._parse_with_key("SFX: Rain hits metal.", "sk-ant-test")

    diagnostics = exc_info.value.diagnostics
    assert [item.attempt_phase for item in diagnostics] == ["initial", "repair"]
    assert diagnostics[-1].issues == [
        ParserDiagnosticIssue(type="invalid_type", path="provider.payload.content")
    ]


def test_anthropic_provider_repairs_with_explicit_repair_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-ant-test")
    calls: list[dict[str, object]] = []
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_tts_parse",
                    "input": {
                        "characters": [{"id": "sfx", "name": "SFX"}],
                        "lines": [
                            {
                                "id": "l001",
                                "character_id": "sfx",
                                "text": "Rain hits metal.",
                                "source_text": "Rain hits metal.",
                                "source_excerpt": "SFX: Rain hits metal.",
                            }
                        ],
                    },
                }
            ]
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_tts_parse",
                    "input": {
                        "characters": [{"id": "narrator", "name": "NARRATOR"}],
                        "lines": [
                            {
                                "id": "l001",
                                "character_id": "narrator",
                                "text": "Hello.",
                                "note": "calm",
                                "source_text": "Hello.",
                                "source_excerpt": "**NARRATOR**\n(calm)\nHello.",
                            }
                        ],
                    },
                }
            ]
        },
    ]

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(responses[len(calls) - 1])

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = AnthropicProvider(
        ParserProviderConfig(
            name="anthropic-test",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_TEST_KEY",
            model="claude-fable-5",
            adapter="anthropic",
        )
    )

    draft = provider.parse("**NARRATOR**\n(calm)\nHello.")

    assert draft.lines[0].text == "Hello."
    assert len(calls) == 2
    assert calls[1]["json"]["system"] == calls[0]["json"]["system"]
    assert calls[1]["json"]["tools"] == calls[0]["json"]["tools"]
    assert calls[1]["json"]["tool_choice"] == calls[0]["json"]["tool_choice"]
    repair_message = calls[1]["json"]["messages"][0]["content"]
    assert not repair_message.startswith("Script:\n```text\n")
    assert "Repair the previous JSON" in repair_message
    assert "Quality errors:" in repair_message
    assert "Previous JSON:" in repair_message
    assert '"name": "SFX"' in repair_message
    assert "Original script:" in repair_message
    assert "**NARRATOR**" in repair_message


def test_anthropic_provider_probe_preview_keeps_raw_payload_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "emit_tts_parse",
                        "input": {
                            "characters": [{"id": "n", "name": "N"}],
                            "lines": [
                                {
                                    "id": "l1",
                                    "character_id": "n",
                                    "text": "Hello from the contract test.",
                                    "source_text": "Hello from the contract test.",
                                    "source_excerpt": "Hello from the contract test.",
                                }
                            ],
                        },
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = AnthropicProvider(
        ParserProviderConfig(
            name="anthropic-test",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_TEST_KEY",
            model="claude-fable-5",
            adapter="anthropic",
        )
    )

    result = provider.probe("sk-ant-test")
    expected_preview = json.dumps(
        {
            "characters": [{"id": "n", "name": "N"}],
            "lines": [
                {
                    "id": "l1",
                    "character_id": "n",
                    "text": "Hello from the contract test.",
                    "source_text": "Hello from the contract test.",
                    "source_excerpt": "Hello from the contract test.",
                }
            ],
        },
        ensure_ascii=False,
    )[:120]

    assert result.draft.lines[0].text == "Hello from the contract test."
    assert result.content_preview == expected_preview
    assert '"provider":' not in result.content_preview
    assert len(calls) == 1


def test_default_parser_providers_include_agentic_presets_and_exclude_removed_providers() -> None:
    from app.parser_config import default_parser_providers

    providers = default_parser_providers()
    names = [provider.name for provider in providers]
    by_name = {provider.name: provider for provider in providers}

    assert "百度千帆" not in names
    assert "Mistral" not in names
    assert by_name["OpenAI"].adapter == "openai-compatible"
    assert by_name["OpenAI"].model == "gpt-5.5"
    assert by_name["Anthropic"].adapter == "anthropic"
    assert by_name["Anthropic"].base_url == "https://api.anthropic.com"
    assert by_name["Gemini"].base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert by_name["OpenRouter"].api_key_env == "OPENROUTER_API_KEY"
    assert by_name["Aihubmix"].base_url == "https://aihubmix.com/v1"
    assert names[-1] == "开物基模"
    assert all(provider.enabled is False for provider in providers)
