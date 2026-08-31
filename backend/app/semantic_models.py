from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class AnnotationKind(str, Enum):
    SPEAKER = "speaker"
    EMOTION_EVIDENCE = "emotion_evidence"
    DIALOGUE = "dialogue"


class AnnotationOrigin(str, Enum):
    AI = "ai"
    HUMAN = "human"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EmotionOrigin(str, Enum):
    SOURCE_GROUNDED = "source_grounded"
    INFERRED = "inferred"
    NONE = "none"


class NormalizedEmotion(str, Enum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    EXCITED = "excited"
    SURPRISED = "surprised"
    SAD = "sad"
    ANGRY = "angry"
    FEARFUL = "fearful"
    DISGUSTED = "disgusted"
    ANXIOUS = "anxious"
    CALM = "calm"
    SERIOUS = "serious"
    GENTLE = "gentle"
    CONFUSED = "confused"
    OTHER = "other"


class UncertaintyCode(str, Enum):
    SPEAKER_UNKNOWN = "speaker_unknown"
    SPEAKER_AMBIGUOUS = "speaker_ambiguous"
    DIALOGUE_AMBIGUOUS = "dialogue_ambiguous"
    EMOTION_INFERRED = "emotion_inferred"
    EMOTION_AMBIGUOUS = "emotion_ambiguous"
    SOURCE_ANCHOR_AMBIGUOUS = "source_anchor_ambiguous"


class AnalysisRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AnalysisRunQuality(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class SourceSpan(BaseModel):
    source_revision_id: str
    start_utf16: int
    end_utf16: int
    text: str
    source_sha256: str

    @model_validator(mode="after")
    def validate_half_open_range(self) -> "SourceSpan":
        if self.start_utf16 < 0:
            raise ValueError("source span start_utf16 must be non-negative")
        if self.end_utf16 <= self.start_utf16:
            raise ValueError("source span must be a non-empty half-open range")
        if not self.text:
            raise ValueError("source span text must be non-empty")
        return self


class SemanticAnnotation(BaseModel):
    id: str
    kind: AnnotationKind
    span: SourceSpan
    origin: AnnotationOrigin
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: ReviewStatus = ReviewStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_origin_defaults(self) -> "SemanticAnnotation":
        if self.origin is AnnotationOrigin.AI and self.confidence is None:
            raise ValueError("AI annotations require confidence")
        if (
            self.origin is AnnotationOrigin.AI
            and self.status is ReviewStatus.ACCEPTED
            and self.confidence is not None
            and self.confidence < 0.8
        ):
            raise ValueError("accepted AI annotations require confidence >= 0.8")
        if self.origin is AnnotationOrigin.HUMAN and "status" not in self.model_fields_set:
            self.status = ReviewStatus.ACCEPTED
        return self


class CharacterCandidate(BaseModel):
    id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    supporting_annotation_ids: list[str] = Field(default_factory=list)
    project_character_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: ReviewStatus = ReviewStatus.PENDING
    origin: AnnotationOrigin

    @model_validator(mode="after")
    def validate_origin_defaults(self) -> "CharacterCandidate":
        if not self.canonical_name:
            raise ValueError("canonical_name must be non-empty")
        if self.origin is AnnotationOrigin.AI and self.confidence is None:
            raise ValueError("AI character candidates require confidence")
        if (
            self.origin is AnnotationOrigin.AI
            and self.status is ReviewStatus.ACCEPTED
            and self.confidence is not None
            and self.confidence < 0.8
        ):
            raise ValueError("accepted AI character candidates require confidence >= 0.8")
        if self.origin is AnnotationOrigin.HUMAN and "status" not in self.model_fields_set:
            self.status = ReviewStatus.ACCEPTED
        return self


class SemanticUtterance(BaseModel):
    id: str
    dialogue_annotation_id: str
    speaker_annotation_id: str | None = None
    character_candidate_id: str | None = None
    emotion_evidence_annotation_ids: list[str] = Field(default_factory=list)
    normalized_emotion: NormalizedEmotion | None = None
    custom_emotion: str | None = None
    emotion_intensity: float | None = Field(default=None, ge=0, le=1)
    emotion_origin: EmotionOrigin = EmotionOrigin.NONE
    language: str = "zh"
    confidence: float = Field(ge=0, le=1)
    uncertainty_codes: list[UncertaintyCode] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.PENDING

    @field_validator("uncertainty_codes")
    @classmethod
    def deduplicate_uncertainty_codes(cls, value: list[UncertaintyCode]) -> list[UncertaintyCode]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_emotion_fields(self) -> "SemanticUtterance":
        if UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS in self.uncertainty_codes:
            raise ValueError("source_anchor_ambiguous belongs to an unresolved candidate, not an utterance")
        if self.status is ReviewStatus.ACCEPTED and {
            UncertaintyCode.SPEAKER_UNKNOWN,
            UncertaintyCode.SPEAKER_AMBIGUOUS,
            UncertaintyCode.DIALOGUE_AMBIGUOUS,
        }.intersection(self.uncertainty_codes):
            raise ValueError("accepted utterance cannot include unresolved speaker or dialogue uncertainty")
        if self.emotion_origin is EmotionOrigin.SOURCE_GROUNDED and not self.emotion_evidence_annotation_ids:
            raise ValueError("source_grounded emotion requires an emotion evidence annotation")
        if self.normalized_emotion is None:
            if self.custom_emotion is not None or self.emotion_intensity is not None:
                raise ValueError("emotion fields require normalized_emotion")
            if self.emotion_origin is not EmotionOrigin.NONE:
                raise ValueError("emotion_origin must be none without normalized_emotion")
        else:
            if self.emotion_intensity is None:
                raise ValueError("emotion_intensity is required with normalized_emotion")
            if self.emotion_origin is EmotionOrigin.NONE:
                raise ValueError("emotion_origin must describe normalized_emotion")
            if self.normalized_emotion is NormalizedEmotion.OTHER:
                if not self.custom_emotion or len(self.custom_emotion) > 32:
                    raise ValueError("other emotion requires custom_emotion of at most 32 characters")
            elif self.custom_emotion is not None:
                raise ValueError("custom_emotion is only allowed for other emotion")
        return self


class AnalysisWarning(BaseModel):
    id: str
    code: str
    message: str
    annotation_id: str | None = None
    utterance_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class UnresolvedCandidate(BaseModel):
    id: str
    code: UncertaintyCode = UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS
    candidate_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class AnalysisError(BaseModel):
    code: str
    http_status: int
    stage: str
    message: str
    retryable: bool
    run_id: str | None = None
    trace_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = Field(default_factory=dict)


class AnalysisRun(BaseModel):
    id: str
    project_id: str
    source_revision_id: str
    draft_id: str
    status: AnalysisRunStatus = AnalysisRunStatus.QUEUED
    quality: AnalysisRunQuality | None = None
    progress: float = Field(default=0, ge=0, le=1)
    warnings: list[AnalysisWarning] = Field(default_factory=list)
    error: AnalysisError | None = None
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_status_quality(self) -> "AnalysisRun":
        if self.status is AnalysisRunStatus.COMPLETED and self.quality is None:
            raise ValueError("completed analysis runs require quality")
        if self.status is not AnalysisRunStatus.COMPLETED and self.quality is not None:
            raise ValueError("non-completed analysis runs must not have quality")
        return self


class SemanticAnalysisDraft(BaseModel):
    id: str
    project_id: str
    source_revision_id: str
    version: int = Field(default=1, ge=1)
    annotations: list[SemanticAnnotation] = Field(default_factory=list)
    characters: list[CharacterCandidate] = Field(default_factory=list)
    utterances: list[SemanticUtterance] = Field(default_factory=list)
    unresolved_candidates: list[UnresolvedCandidate] = Field(default_factory=list)
    warnings: list[AnalysisWarning] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    contract_version: str | None = None
    confirmed_revision_id: str | None = None
    confirmed_parse_revision_id: str | None = None
    confirm_idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SemanticRevision(BaseModel):
    id: str
    project_id: str
    source_revision_id: str
    annotations: list[SemanticAnnotation] = Field(default_factory=list)
    characters: list[CharacterCandidate] = Field(default_factory=list)
    utterances: list[SemanticUtterance] = Field(default_factory=list)
    unresolved_candidates: list[UnresolvedCandidate] = Field(default_factory=list)
    warnings: list[AnalysisWarning] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    contract_version: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
