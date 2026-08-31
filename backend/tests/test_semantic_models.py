from __future__ import annotations

import hashlib

import pytest

from app.models import ScriptRevision

try:
    from app.semantic_models import (
        AnalysisRun,
        AnalysisRunQuality,
        AnalysisRunStatus,
        AnnotationKind,
        AnnotationOrigin,
        CharacterCandidate,
        EmotionOrigin,
        NormalizedEmotion,
        ReviewStatus,
        SemanticAnnotation,
        SemanticUtterance,
        SourceSpan,
        UncertaintyCode,
    )
    from app.semantic_source import (
        py_index_to_utf16,
        sha256_source,
        utf16_to_py_index,
        validate_source_span,
    )
except ImportError as error:
    _SEMANTIC_IMPORT_ERROR: ImportError | None = error
else:
    _SEMANTIC_IMPORT_ERROR = None


def _semantic() -> None:
    if _SEMANTIC_IMPORT_ERROR is not None:
        pytest.fail(f"semantic domain modules must be importable: {_SEMANTIC_IMPORT_ERROR}")


def test_utf16_round_trip_handles_chinese_emoji_and_crlf() -> None:
    _semantic()
    text = "甲😀\r\n台词"

    assert py_index_to_utf16(text, 0) == 0
    assert py_index_to_utf16(text, 2) == 3
    assert utf16_to_py_index(text, 3) == 2
    with pytest.raises(ValueError, match="surrogate"):
        utf16_to_py_index(text, 2)


@pytest.mark.parametrize("index", [-1, 4])
def test_python_index_to_utf16_rejects_out_of_bounds_indices(index: int) -> None:
    _semantic()
    with pytest.raises(ValueError, match="range"):
        py_index_to_utf16("甲😀", index)


@pytest.mark.parametrize("offset", [-1, 4])
def test_utf16_to_python_index_rejects_out_of_bounds_offsets(offset: int) -> None:
    _semantic()
    with pytest.raises(ValueError, match="range"):
        utf16_to_py_index("甲😀", offset)


def test_sha256_source_hashes_exact_utf8_bytes_without_normalization() -> None:
    _semantic()
    text = "\ufeff甲\r\nCafe\u0301"

    assert sha256_source(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert sha256_source(text) != sha256_source(text.removeprefix("\ufeff").replace("\r\n", "\n"))


def test_validate_source_span_accepts_exact_slice_and_rejects_source_mismatches() -> None:
    _semantic()
    source = ScriptRevision(
        revision_id="script-r001",
        source_markdown="甲😀\r\n台词",
        source_sha256=sha256_source("甲😀\r\n台词"),
    )
    span = SourceSpan(
        source_revision_id="script-r001",
        start_utf16=3,
        end_utf16=7,
        text="\r\n台词",
        source_sha256=source.source_sha256,
    )

    validate_source_span(span, source)

    for changed in (
        span.model_copy(update={"source_revision_id": "script-r002"}),
        span.model_copy(update={"source_sha256": sha256_source("other")}),
        span.model_copy(update={"text": "台词"}),
        span.model_copy(update={"start_utf16": 2}),
    ):
        with pytest.raises(ValueError):
            validate_source_span(changed, source)


@pytest.mark.parametrize(
    ("start_utf16", "end_utf16", "text"),
    [(-1, 1, "x"), (1, 1, "x"), (2, 1, "x"), (0, 1, "")],
)
def test_source_span_requires_a_non_empty_half_open_range(
    start_utf16: int, end_utf16: int, text: str
) -> None:
    _semantic()
    with pytest.raises(ValueError):
        SourceSpan(
            source_revision_id="script-r001",
            start_utf16=start_utf16,
            end_utf16=end_utf16,
            text=text,
            source_sha256="a" * 64,
        )


def test_semantic_models_expose_exact_enums_and_model_invariants() -> None:
    _semantic()
    span = SourceSpan(
        source_revision_id="script-r001",
        start_utf16=0,
        end_utf16=2,
        text="你好",
        source_sha256="a" * 64,
    )
    annotation = SemanticAnnotation(
        id="dialogue-1",
        kind=AnnotationKind.DIALOGUE,
        span=span,
        origin=AnnotationOrigin.HUMAN,
    )
    character = CharacterCandidate(
        id="character-1",
        canonical_name="小品",
        origin=AnnotationOrigin.HUMAN,
    )
    utterance = SemanticUtterance(
        id="utterance-1",
        dialogue_annotation_id=annotation.id,
        character_candidate_id=character.id,
        normalized_emotion=NormalizedEmotion.EXCITED,
        emotion_intensity=0.8,
        emotion_origin=EmotionOrigin.INFERRED,
        confidence=0.9,
        uncertainty_codes=[UncertaintyCode.EMOTION_INFERRED, UncertaintyCode.EMOTION_INFERRED],
        status=ReviewStatus.ACCEPTED,
    )

    assert annotation.status == ReviewStatus.ACCEPTED
    assert character.status == ReviewStatus.ACCEPTED
    assert utterance.uncertainty_codes == [UncertaintyCode.EMOTION_INFERRED]
    assert NormalizedEmotion.OTHER.value == "other"
    assert UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS.value == "source_anchor_ambiguous"
    assert AnalysisRunStatus.COMPLETED.value == "completed"
    assert AnalysisRunQuality.PARTIAL.value == "partial"

    with pytest.raises(ValueError, match="custom_emotion"):
        SemanticUtterance(
            id="utterance-2",
            dialogue_annotation_id=annotation.id,
            normalized_emotion=NormalizedEmotion.OTHER,
            emotion_intensity=0.5,
            emotion_origin=EmotionOrigin.INFERRED,
            confidence=0.5,
        )
    with pytest.raises(ValueError, match="custom_emotion"):
        SemanticUtterance(
            id="utterance-3",
            dialogue_annotation_id=annotation.id,
            normalized_emotion=NormalizedEmotion.HAPPY,
            custom_emotion="joyful",
            emotion_intensity=0.5,
            emotion_origin=EmotionOrigin.INFERRED,
            confidence=0.5,
        )

    run = AnalysisRun(
        id="run-1",
        project_id="demo",
        source_revision_id="script-r001",
        draft_id="draft-1",
        status=AnalysisRunStatus.COMPLETED,
        quality=AnalysisRunQuality.COMPLETE,
    )
    assert run.status == AnalysisRunStatus.COMPLETED


@pytest.mark.parametrize(
    "uncertainty_code",
    [
        UncertaintyCode.SPEAKER_UNKNOWN,
        UncertaintyCode.SPEAKER_AMBIGUOUS,
        UncertaintyCode.DIALOGUE_AMBIGUOUS,
    ],
)
def test_accepted_utterance_rejects_unresolved_speaker_or_dialogue_uncertainty(
    uncertainty_code: UncertaintyCode,
) -> None:
    _semantic()
    with pytest.raises(ValueError, match="accepted utterance"):
        SemanticUtterance(
            id="utterance-accepted-uncertain",
            dialogue_annotation_id="dialogue-1",
            character_candidate_id="character-1",
            confidence=0.9,
            uncertainty_codes=[uncertainty_code],
            status=ReviewStatus.ACCEPTED,
        )


def test_utterance_rejects_source_anchor_ambiguity_in_all_review_states() -> None:
    _semantic()
    with pytest.raises(ValueError, match="source_anchor_ambiguous"):
        SemanticUtterance(
            id="utterance-anchor-ambiguous",
            dialogue_annotation_id="dialogue-1",
            confidence=0.5,
            uncertainty_codes=[UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS],
            status=ReviewStatus.PENDING,
        )


def test_source_grounded_emotion_requires_an_emotion_evidence_annotation() -> None:
    _semantic()
    with pytest.raises(ValueError, match="source_grounded"):
        SemanticUtterance(
            id="utterance-grounded-without-evidence",
            dialogue_annotation_id="dialogue-1",
            normalized_emotion=NormalizedEmotion.HAPPY,
            emotion_intensity=0.7,
            emotion_origin=EmotionOrigin.SOURCE_GROUNDED,
            confidence=0.8,
        )


def test_ai_annotation_acceptance_requires_point_eight_confidence() -> None:
    _semantic()
    span = SourceSpan(
        source_revision_id="script-r001",
        start_utf16=0,
        end_utf16=2,
        text="你好",
        source_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="0.8"):
        SemanticAnnotation(
            id="annotation-low-confidence",
            kind=AnnotationKind.DIALOGUE,
            span=span,
            origin=AnnotationOrigin.AI,
            confidence=0.79,
            status=ReviewStatus.ACCEPTED,
        )

    pending = SemanticAnnotation(
        id="annotation-pending",
        kind=AnnotationKind.DIALOGUE,
        span=span,
        origin=AnnotationOrigin.AI,
        confidence=0.79,
    )
    assert pending.status == ReviewStatus.PENDING


def test_ai_character_acceptance_requires_point_eight_confidence() -> None:
    _semantic()
    with pytest.raises(ValueError, match="0.8"):
        CharacterCandidate(
            id="character-low-confidence",
            canonical_name="小品",
            origin=AnnotationOrigin.AI,
            confidence=0.79,
            status=ReviewStatus.ACCEPTED,
        )

    pending = CharacterCandidate(
        id="character-pending",
        canonical_name="小品",
        origin=AnnotationOrigin.AI,
        confidence=0.79,
    )
    assert pending.status == ReviewStatus.PENDING


def test_completed_analysis_run_requires_a_quality() -> None:
    _semantic()
    with pytest.raises(ValueError, match="completed.*quality"):
        AnalysisRun(
            id="run-completed-without-quality",
            project_id="demo",
            source_revision_id="script-r001",
            draft_id="draft-1",
            status=AnalysisRunStatus.COMPLETED,
        )


@pytest.mark.parametrize(
    "status",
    [
        AnalysisRunStatus.QUEUED,
        AnalysisRunStatus.RUNNING,
        AnalysisRunStatus.FAILED,
        AnalysisRunStatus.INTERRUPTED,
    ],
)
def test_non_completed_analysis_run_rejects_quality(status: AnalysisRunStatus) -> None:
    _semantic()
    with pytest.raises(ValueError, match="non-completed.*quality"):
        AnalysisRun(
            id="run-non-completed-with-quality",
            project_id="demo",
            source_revision_id="script-r001",
            draft_id="draft-1",
            status=status,
            quality=AnalysisRunQuality.PARTIAL,
        )
