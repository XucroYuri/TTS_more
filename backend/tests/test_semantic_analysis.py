from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from app.models import ScriptRevision
from app.semantic_analysis import LocatedCandidate, SemanticAnalysisService, chunk_source, locate_candidate
from app.semantic_models import (
    AnalysisRun,
    AnalysisRunQuality,
    AnalysisRunStatus,
    AnnotationKind,
    ReviewStatus,
    SemanticAnalysisDraft,
    UncertaintyCode,
    UnresolvedCandidate,
)
from app.semantic_provider import (
    AnalysisChunk,
    CharacterCandidatePayload,
    SemanticProviderContractError,
    SemanticProviderResponse,
    SemanticProviderTimeout,
    SemanticProviderUpstream,
    UtteranceCandidate,
)
from app.semantic_source import py_index_to_utf16, sha256_source, utf16_to_py_index, validate_source_span


class FakeSemanticProvider:
    name = "fake-semantic"
    model = "fake-model"

    def __init__(self, results: Iterable[SemanticProviderResponse | Exception]) -> None:
        self.results = iter(results)
        self.chunks: list[AnalysisChunk] = []

    def analyze_chunk(self, chunk: AnalysisChunk) -> SemanticProviderResponse:
        self.chunks.append(chunk)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result.model_copy(
            update={
                "chunk_id": chunk.chunk_id,
                "start_utf16": chunk.start_utf16,
                "end_utf16": chunk.end_utf16,
                "overlap_before": chunk.overlap_before,
                "overlap_after": chunk.overlap_after,
            }
        )


def _response(**values: object) -> SemanticProviderResponse:
    chunk = AnalysisChunk.single("")
    return SemanticProviderResponse(
        chunk_id=chunk.chunk_id,
        start_utf16=chunk.start_utf16,
        end_utf16=chunk.end_utf16,
        overlap_before=chunk.overlap_before,
        overlap_after=chunk.overlap_after,
        **values,
    )


def _source(text: str) -> ScriptRevision:
    return ScriptRevision(revision_id="script-r001", source_markdown=text, source_sha256=sha256_source(text))


def _run_and_draft(source: ScriptRevision) -> tuple[AnalysisRun, SemanticAnalysisDraft]:
    run = AnalysisRun(
        id="run-1",
        project_id="demo",
        source_revision_id=source.revision_id,
        draft_id="draft-1",
        status=AnalysisRunStatus.RUNNING,
    )
    draft = SemanticAnalysisDraft(
        id="draft-1",
        project_id="demo",
        source_revision_id=source.revision_id,
        version=3,
        confirmed_revision_id="confirmed-1",
        confirmed_parse_revision_id="parse-confirmed-1",
        confirmed_parse_fingerprint="a" * 64,
        confirm_idempotency_key="confirm-key",
    )
    return run, draft


def _candidate(dialogue: str, **changes: object) -> UtteranceCandidate:
    values: dict[str, object] = {
        "dialogue_excerpt": dialogue,
        "confidence": 0.95,
        "language": "zh",
        "anchor_before": "",
        "anchor_after": "",
        "occurrence_index": 0,
    }
    values.update(changes)
    return UtteranceCandidate(**values)


def test_paragraph_chunking_preserves_exact_chinese_emoji_crlf_and_global_utf16_metadata() -> None:
    text = "甲😀第一段\r\n仍是第一段\r\n\r\n乙第二段\r\n\r\n丙第三段"

    chunks = chunk_source(text, max_chars=20, overlap_chars=4)

    assert len(chunks) >= 2
    assert chunks[0].start_utf16 == 0
    assert chunks[-1].end_utf16 == py_index_to_utf16(text, len(text))
    assert all(chunk.text == text[utf16_to_py_index(text, chunk.start_utf16) : utf16_to_py_index(text, chunk.end_utf16)] for chunk in chunks)
    assert all(chunk.start_utf16 < chunk.end_utf16 for chunk in chunks)
    assert all(chunk.overlap_before == (0 if index == 0 else chunks[index - 1].end_utf16 - chunk.start_utf16) for index, chunk in enumerate(chunks))
    assert all(chunk.overlap_after == (0 if index == len(chunks) - 1 else chunk.end_utf16 - chunks[index + 1].start_utf16) for index, chunk in enumerate(chunks))
    assert all(utf16_to_py_index(text, offset) >= 0 for chunk in chunks for offset in (chunk.start_utf16, chunk.end_utf16))


def test_exact_location_handles_unquoted_and_quoted_wrapper_exclusion_and_ignores_model_offsets() -> None:
    text = "胶布（惊喜，大喊）：真的！真的有加速效果！\r\n王说：“别碰它。”"
    chunk = AnalysisChunk.single(text)

    unquoted = locate_candidate(chunk, _candidate("真的！真的有加速效果！", model_start_utf16=999, model_end_utf16=1000))
    quoted = locate_candidate(chunk, _candidate("别碰它。", occurrence_index=0, model_start_utf16=0, model_end_utf16=1))

    assert isinstance(unquoted, LocatedCandidate)
    assert unquoted.text == "真的！真的有加速效果！"
    assert unquoted.start_utf16 == py_index_to_utf16(text, text.index("真的！"))
    assert isinstance(quoted, LocatedCandidate)
    assert quoted.text == "别碰它。"
    assert text[utf16_to_py_index(text, quoted.start_utf16) - 1] == "“"
    assert text[utf16_to_py_index(text, quoted.end_utf16)] == "”"


@pytest.mark.parametrize(
    ("changes", "wanted_index"),
    [
        ({"anchor_before": "甲："}, 0),
        ({"anchor_after": "，乙"}, 0),
        ({"occurrence_index": 1}, 1),
    ],
)
def test_duplicate_location_disambiguates_by_each_anchor_then_original_occurrence(
    changes: dict[str, object], wanted_index: int
) -> None:
    text = "甲：重复台词，乙。\n丙：重复台词，丁。"
    chunk = AnalysisChunk.single(text)
    candidate = _candidate("重复台词", **changes)

    located = locate_candidate(chunk, candidate)

    assert isinstance(located, LocatedCandidate)
    occurrences = [text.index("重复台词"), text.rindex("重复台词")]
    assert located.start_utf16 == py_index_to_utf16(text, occurrences[wanted_index])


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate("重复台词", anchor_before="不存在", occurrence_index=0),
        _candidate("重复台词", occurrence_index=8),
        _candidate("差一个字", occurrence_index=0),
    ],
)
def test_ambiguous_or_non_exact_candidate_is_unresolved(candidate: UtteranceCandidate) -> None:
    result = locate_candidate(AnalysisChunk.single("甲：重复台词。乙：重复台词。"), candidate)

    assert isinstance(result, UnresolvedCandidate)
    assert result.code is UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS
    assert "dialogue_excerpt" not in result.details


def test_service_builds_grounded_layers_controlled_aliases_and_acceptance_truth() -> None:
    text = Path(__file__).with_name("fixtures").joinpath("mixed_semantic_script.txt").read_text(encoding="utf-8")
    source = _source(text)
    run, draft = _run_and_draft(source)
    response = _response(
        character_candidates=[
            CharacterCandidatePayload(
                canonical_name="诸葛九九",
                aliases=["九九"],
                evidence_excerpts=["诸葛九九：胶布，踩蓝格！\n九九继续"],
                confidence=0.96,
            ),
            CharacterCandidatePayload(canonical_name="胶布", evidence_excerpts=["胶布（惊喜，大喊）"], confidence=0.94),
            CharacterCandidatePayload(canonical_name="王", evidence_excerpts=["王：“"], confidence=0.91),
            CharacterCandidatePayload(canonical_name="老王", evidence_excerpts=["老王回答"], confidence=0.92),
        ],
        utterance_candidates=[
            _candidate(
                "真的！真的有加速效果！跑得好快！！这下大笨蛙追不上了",
                speaker_name="胶布",
                speaker_evidence_excerpt="胶布（惊喜，大喊）",
                emotion_evidence_excerpts=["惊喜", "大喊"],
                normalized_emotion="excited",
                emotion_intensity=0.9,
                emotion_origin="source_grounded",
                source_excerpt="胶布（惊喜，大喊）：真的！真的有加速效果！跑得好快！！这下大笨蛙追不上了",
            ),
            _candidate(
                "胶布，踩蓝格！",
                speaker_name="九九",
                speaker_evidence_excerpt="诸葛九九",
                source_excerpt="不匹配但只能产生警告",
            ),
        ],
    )
    provider = FakeSemanticProvider([response])
    service = SemanticAnalysisService(provider, max_chunk_chars=10_000)

    result = service.analyze("demo", source, run, draft)

    assert [(item.canonical_name, item.aliases) for item in result.characters] == [
        ("胶布", []),
        ("诸葛九九", ["九九"]),
        ("王", []),
        ("老王", []),
    ]
    assert [item.status for item in result.characters] == [ReviewStatus.ACCEPTED] * 4
    assert len(result.utterances) == 2
    assert all(item.status is ReviewStatus.ACCEPTED for item in result.utterances)
    zhuge = next(item for item in result.characters if item.canonical_name == "诸葛九九")
    assert result.utterances[1].character_candidate_id == zhuge.id
    assert [item.kind for item in result.annotations].count(AnnotationKind.SPEAKER) == 2
    assert [item.kind for item in result.annotations].count(AnnotationKind.EMOTION_EVIDENCE) == 2
    assert [item.kind for item in result.annotations].count(AnnotationKind.DIALOGUE) == 2
    assert any(item.code == "source_excerpt_mismatch" for item in result.warnings)
    assert result.provider == "fake-semantic"
    assert result.model == "fake-model"
    assert result.prompt_version and result.contract_version
    assert result.version == draft.version
    assert result.created_at == draft.created_at
    assert result.confirmed_revision_id == draft.confirmed_revision_id
    for annotation in result.annotations:
        validate_source_span(annotation.span, source)


@pytest.mark.parametrize(
    ("text", "evidence_excerpts"),
    [
        ("诸葛九九登场。\n九九继续向前。", ["诸葛九九", "九九继续"]),
        ("诸葛九九登场。", ["诸葛九九"]),
        ("诸葛九九登场。\n另一场景里，九九继续。", ["诸葛九九登场。"]),
    ],
)
def test_alias_requires_one_unique_relationship_excerpt_with_independent_alias_occurrence(
    text: str, evidence_excerpts: list[str]
) -> None:
    source = _source(text)
    run, draft = _run_and_draft(source)
    response = _response(
        character_candidates=[
            CharacterCandidatePayload(
                canonical_name="诸葛九九",
                aliases=["九九"],
                evidence_excerpts=evidence_excerpts,
                confidence=0.95,
            )
        ]
    )

    result = SemanticAnalysisService(FakeSemanticProvider([response])).analyze("demo", source, run, draft)

    assert result.characters[0].aliases == []
    assert any(item.code == "alias_evidence_mismatch" for item in result.warnings)


def test_partially_overlapping_alias_evidence_does_not_map_speaker() -> None:
    source = _source("老王叔：台词。")
    run, draft = _run_and_draft(source)
    response = _response(
        character_candidates=[
            CharacterCandidatePayload(
                canonical_name="老王",
                aliases=["王叔"],
                evidence_excerpts=["老王叔"],
                confidence=0.95,
            )
        ],
        utterance_candidates=[
            _candidate(
                "台词。",
                speaker_name="王叔",
                speaker_evidence_excerpt="王叔",
            )
        ],
    )

    result = SemanticAnalysisService(FakeSemanticProvider([response])).analyze("demo", source, run, draft)

    assert result.characters[0].aliases == []
    assert any(item.code == "alias_evidence_mismatch" for item in result.warnings)
    assert result.utterances[0].character_candidate_id is None
    assert result.utterances[0].status is ReviewStatus.PENDING


def test_evidence_mismatches_are_soft_and_block_only_missing_character_or_speaker_truth() -> None:
    text = "甲：保留这句。\n乙：另一句。"
    source = _source(text)
    run, draft = _run_and_draft(source)
    response = _response(
        character_candidates=[CharacterCandidatePayload(canonical_name="甲", confidence=0.95)],
        utterance_candidates=[
            _candidate(
                "保留这句。",
                speaker_name="甲",
                speaker_evidence_excerpt="不存在证据",
                normalized_emotion="happy",
                emotion_intensity=0.7,
                emotion_origin="source_grounded",
                emotion_evidence_excerpts=["也不存在"],
            ),
            _candidate("另一句。", speaker_name="不存在的角色", confidence=0.99),
        ],
    )

    result = SemanticAnalysisService(FakeSemanticProvider([response])).analyze("demo", source, run, draft)

    assert len(result.utterances) == 2
    assert all(item.status is ReviewStatus.PENDING for item in result.utterances)
    assert result.utterances[0].normalized_emotion is not None
    assert result.utterances[0].emotion_origin.value == "inferred"
    assert UncertaintyCode.EMOTION_INFERRED in result.utterances[0].uncertainty_codes
    assert {item.code for item in result.warnings} >= {"speaker_evidence_mismatch", "emotion_evidence_mismatch"}


@pytest.mark.parametrize(
    ("source_excerpt", "expects_warning"),
    [
        ("甲：重复台词。前文。", True),
        ("乙：重复台词。后文。", False),
    ],
)
def test_source_excerpt_must_cover_the_selected_duplicate_occurrence(
    source_excerpt: str, expects_warning: bool
) -> None:
    text = "甲：重复台词。前文。\n乙：重复台词。后文。"
    source = _source(text)
    run, draft = _run_and_draft(source)
    response = _response(
        utterance_candidates=[
            _candidate("重复台词。", occurrence_index=1, source_excerpt=source_excerpt)
        ]
    )

    result = SemanticAnalysisService(FakeSemanticProvider([response])).analyze("demo", source, run, draft)

    warnings = [item for item in result.warnings if item.code == "source_excerpt_mismatch"]
    assert bool(warnings) is expects_warning


def test_one_character_mutation_is_unresolved_while_other_candidate_succeeds() -> None:
    text = "甲：第一句。\n乙：第二句。"
    source = _source(text)
    run, draft = _run_and_draft(source)
    response = _response(
        utterance_candidates=[_candidate("第一句！"), _candidate("第二句。")]
    )

    result = SemanticAnalysisService(FakeSemanticProvider([response])).analyze("demo", source, run, draft)

    assert len(result.utterances) == 1
    assert next(item for item in result.annotations if item.kind is AnnotationKind.DIALOGUE).span.text == "第二句。"
    assert len(result.unresolved_candidates) == 1
    assert result.unresolved_candidates[0].code is UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS


def test_distinct_bad_candidates_remain_distinct_safe_unresolved_items() -> None:
    source = _source("甲：真实台词。")
    run, draft = _run_and_draft(source)
    response = _response(
        utterance_candidates=[_candidate("错误一"), _candidate("错误二")]
    )

    result = SemanticAnalysisService(FakeSemanticProvider([response])).analyze("demo", source, run, draft)

    assert len(result.unresolved_candidates) == 2
    assert len({item.id for item in result.unresolved_candidates}) == 2
    assert all("错误" not in str(item.details) for item in result.unresolved_candidates)


def test_overlap_deduplicates_by_span_keeps_highest_confidence_and_source_order() -> None:
    text = "甲：前句。\n\n乙：重叠句。\n\n丙：后句。"
    source = _source(text)
    run, draft = _run_and_draft(source)
    low = _response(utterance_candidates=[_candidate("重叠句。", confidence=0.81), _candidate("前句。")])
    high = _response(utterance_candidates=[_candidate("重叠句。", confidence=0.97), _candidate("后句。")])
    provider = FakeSemanticProvider([low, high])

    result = SemanticAnalysisService(provider, max_chunk_chars=18, overlap_chars=10).analyze("demo", source, run, draft)

    dialogue = [item for item in result.annotations if item.kind is AnnotationKind.DIALOGUE]
    assert [item.span.text for item in dialogue] == ["前句。", "重叠句。", "后句。"]
    overlap_annotation = next(item for item in dialogue if item.span.text == "重叠句。")
    assert overlap_annotation.confidence == 0.97
    assert len({item.dialogue_annotation_id for item in result.utterances}) == 3


@pytest.mark.parametrize("conflict", ["speaker", "emotion"])
def test_incompatible_overlap_candidates_warn_and_force_dialogue_pending(conflict: str) -> None:
    text = "甲：前句。\n\n乙：重叠句。\n\n丙：后句。"
    source = _source(text)
    run, draft = _run_and_draft(source)
    first_values: dict[str, object] = {"speaker_name": "甲"}
    second_values: dict[str, object] = {"speaker_name": "乙"}
    if conflict == "emotion":
        first_values = {
            "speaker_name": "乙",
            "normalized_emotion": "happy",
            "emotion_intensity": 0.7,
            "emotion_origin": "inferred",
            "uncertainty_codes": ["emotion_inferred"],
        }
        second_values = {
            "speaker_name": "乙",
            "normalized_emotion": "angry",
            "emotion_intensity": 0.7,
            "emotion_origin": "inferred",
            "uncertainty_codes": ["emotion_inferred"],
        }
    characters = [
        CharacterCandidatePayload(canonical_name="甲", evidence_excerpts=["甲："], confidence=0.95),
        CharacterCandidatePayload(canonical_name="乙", evidence_excerpts=["乙："], confidence=0.95),
    ]
    first = _response(
        character_candidates=characters,
        utterance_candidates=[_candidate("重叠句。", confidence=0.90, **first_values)],
    )
    second = _response(
        character_candidates=characters,
        utterance_candidates=[_candidate("重叠句。", confidence=0.96, **second_values)],
    )

    result = SemanticAnalysisService(
        FakeSemanticProvider([first, second]), max_chunk_chars=18, overlap_chars=10
    ).analyze("demo", source, run, draft)

    assert len(result.utterances) == 1
    assert result.utterances[0].status is ReviewStatus.PENDING
    assert UncertaintyCode.DIALOGUE_AMBIGUOUS in result.utterances[0].uncertainty_codes
    dialogue = next(item for item in result.annotations if item.kind is AnnotationKind.DIALOGUE)
    assert dialogue.status is ReviewStatus.PENDING
    conflict_warning = next(item for item in result.warnings if item.code == "overlap_candidate_conflict")
    assert conflict_warning.details["dimensions"] == [conflict]
    assert "重叠句" not in str(conflict_warning.details)


def test_controlled_alias_speakers_are_compatible_overlap_identity() -> None:
    text = "诸葛九九：前句。\n九九继续。\n\n诸葛九九：重叠句。\n\n尾声很长很长很长很长。"
    source = _source(text)
    run, draft = _run_and_draft(source)
    character = CharacterCandidatePayload(
        canonical_name="诸葛九九",
        aliases=["九九"],
        evidence_excerpts=["诸葛九九：前句。\n九九继续"],
        confidence=0.95,
    )
    first = _response(
        character_candidates=[character],
        utterance_candidates=[_candidate("重叠句。", speaker_name="诸葛九九", confidence=0.90)],
    )
    second = _response(
        character_candidates=[character],
        utterance_candidates=[_candidate("重叠句。", speaker_name="九九", confidence=0.96)],
    )

    result = SemanticAnalysisService(
        FakeSemanticProvider([first, second]), max_chunk_chars=32, overlap_chars=14
    ).analyze("demo", source, run, draft)

    assert result.utterances[0].status is ReviewStatus.ACCEPTED
    assert UncertaintyCode.DIALOGUE_AMBIGUOUS not in result.utterances[0].uncertainty_codes
    assert not any(item.code == "overlap_candidate_conflict" for item in result.warnings)


def test_one_failed_chunk_returns_partial_draft_and_preserves_success() -> None:
    text = "甲：第一句。\n\n乙：第二句。"
    source = _source(text)
    run, draft = _run_and_draft(source)
    failure = SemanticProviderUpstream("safe upstream failure")
    success = _response(utterance_candidates=[_candidate("第二句。")])
    provider = FakeSemanticProvider([failure, success])

    result = SemanticAnalysisService(provider, max_chunk_chars=9, overlap_chars=0).analyze("demo", source, run, draft)

    assert len(result.utterances) == 1
    assert any(item.code == "chunk_failed" for item in result.warnings)


def test_all_structural_failures_raise_422_and_all_upstream_or_timeout_propagate() -> None:
    text = "第一段\n\n第二段"
    source = _source(text)
    run, draft = _run_and_draft(source)

    with pytest.raises(SemanticProviderContractError) as structural:
        SemanticAnalysisService(
            FakeSemanticProvider(
                [SemanticProviderContractError("bad one"), SemanticProviderContractError("bad two")]
            ),
            max_chunk_chars=5,
            overlap_chars=0,
        ).analyze("demo", source, run, draft)
    assert structural.value.http_status == 422

    timeout = SemanticProviderTimeout("safe timeout")
    with pytest.raises(SemanticProviderTimeout) as propagated:
        SemanticAnalysisService(
            FakeSemanticProvider([timeout, SemanticProviderUpstream("safe failure")]),
            max_chunk_chars=5,
            overlap_chars=0,
        ).analyze("demo", source, run, draft)
    assert propagated.value is timeout


def test_missing_quoted_dialogue_ignores_unattributed_onomatopoeia() -> None:
    source = _source("门外一声“唰”，灯灭了。")
    run, draft = _run_and_draft(source)

    result = SemanticAnalysisService(FakeSemanticProvider([_response()])).analyze(
        "demo", source, run, draft
    )

    assert result.utterances == []
    assert result.annotations == []
    assert not any(item.code == "missing_quoted_dialogue" for item in result.warnings)


def test_missing_quoted_dialogue_warns_for_attributed_quote_with_utf16_span() -> None:
    source = _source("旁白😀写道：“别遗漏我。”")
    run, draft = _run_and_draft(source)

    result = SemanticAnalysisService(FakeSemanticProvider([_response()])).analyze(
        "demo", source, run, draft
    )

    warning = next(item for item in result.warnings if item.code == "missing_quoted_dialogue")

    assert warning.id == "warning-7507c1392006902536c2f9e9"
    assert warning.details == {"start_utf16": 8, "end_utf16": 13}


def test_missing_quoted_dialogue_warns_once_when_quote_matches_multiple_attribution_patterns() -> None:
    source = _source("旁白写道：“别遗漏我。”旁白说")
    run, draft = _run_and_draft(source)

    result = SemanticAnalysisService(FakeSemanticProvider([_response()])).analyze(
        "demo", source, run, draft
    )

    warnings = [item for item in result.warnings if item.code == "missing_quoted_dialogue"]

    assert len(warnings) == 1
    assert warnings[0].details == {"start_utf16": 6, "end_utf16": 11}


def test_missing_quoted_dialogue_does_not_warn_when_attributed_quote_is_covered() -> None:
    source = _source("旁白写道：“别遗漏我。”")
    run, draft = _run_and_draft(source)

    result = SemanticAnalysisService(
        FakeSemanticProvider([_response(utterance_candidates=[_candidate("别遗漏我。")])])
    ).analyze("demo", source, run, draft)

    assert [(item.kind, item.span.start_utf16, item.span.end_utf16) for item in result.annotations] == [
        (AnnotationKind.DIALOGUE, 6, 11)
    ]
    assert not any(item.code == "missing_quoted_dialogue" for item in result.warnings)
    assert run.status is AnalysisRunStatus.RUNNING
    assert run.quality is None


def test_attributed_quoted_dialogue_keeps_grounded_emotion_evidence() -> None:
    source = _source("旁白（平静）写道：“别遗漏我。”")
    run, draft = _run_and_draft(source)
    response = _response(
        utterance_candidates=[
            _candidate(
                "别遗漏我。",
                emotion_evidence_excerpts=["平静"],
                normalized_emotion="calm",
                emotion_intensity=0.7,
                emotion_origin="source_grounded",
            )
        ]
    )

    result = SemanticAnalysisService(FakeSemanticProvider([response])).analyze("demo", source, run, draft)

    assert result.utterances[0].emotion_origin.value == "source_grounded"
    assert len(result.utterances[0].emotion_evidence_annotation_ids) == 1
    assert [(item.kind, item.span.text) for item in result.annotations] == [
        (AnnotationKind.EMOTION_EVIDENCE, "平静"),
        (AnnotationKind.DIALOGUE, "别遗漏我。"),
    ]
    assert not any(item.code in {"emotion_evidence_mismatch", "missing_quoted_dialogue"} for item in result.warnings)
