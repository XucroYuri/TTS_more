from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from app.models import ScriptRevision
from app.net_guard import scrub_error
from app.semantic_models import (
    AnalysisRun,
    AnalysisWarning,
    AnnotationKind,
    AnnotationOrigin,
    CharacterCandidate,
    EmotionOrigin,
    ReviewStatus,
    SemanticAnalysisDraft,
    SemanticAnnotation,
    SemanticUtterance,
    SourceSpan,
    UncertaintyCode,
    UnresolvedCandidate,
)
from app.semantic_provider import (
    CONTRACT_VERSION,
    PROMPT_VERSION,
    AnalysisChunk,
    SemanticProvider,
    SemanticProviderContractError,
    SemanticProviderError,
    SemanticProviderResponse,
    UtteranceCandidate,
)
from app.semantic_source import py_index_to_utf16, sha256_source, utf16_to_py_index, validate_source_span


AI_ACCEPTANCE_THRESHOLD = 0.8


class LocatedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    candidate: UtteranceCandidate
    text: str
    local_start_utf16: int
    local_end_utf16: int
    start_utf16: int
    end_utf16: int
    occurrence_index: int


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _unresolved(
    chunk: AnalysisChunk, candidate: UtteranceCandidate, occurrence_count: int
) -> UnresolvedCandidate:
    return UnresolvedCandidate(
        id=_digest_id(
            "unresolved",
            chunk.chunk_id,
            candidate.dialogue_excerpt,
            candidate.anchor_before,
            candidate.anchor_after,
            candidate.occurrence_index,
        ),
        code=UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS,
        candidate_type="utterance",
        message="Dialogue candidate could not be uniquely grounded in the source chunk.",
        details={"chunk_id": chunk.chunk_id, "occurrence_count": occurrence_count},
    )


def _all_occurrences(text: str, excerpt: str) -> list[int]:
    starts: list[int] = []
    cursor = 0
    while True:
        found = text.find(excerpt, cursor)
        if found < 0:
            return starts
        starts.append(found)
        cursor = found + 1


def locate_candidate(
    chunk: AnalysisChunk, candidate: UtteranceCandidate
) -> LocatedCandidate | UnresolvedCandidate:
    starts = _all_occurrences(chunk.text, candidate.dialogue_excerpt)
    if len(starts) == 1:
        selected = starts[0]
    elif not starts:
        return _unresolved(chunk, candidate, 0)
    else:
        remaining = list(starts)
        if candidate.anchor_before:
            remaining = [
                start
                for start in remaining
                if chunk.text[:start].endswith(candidate.anchor_before)
            ]
        if candidate.anchor_after:
            remaining = [
                start
                for start in remaining
                if chunk.text[start + len(candidate.dialogue_excerpt) :].startswith(candidate.anchor_after)
            ]
        if len(remaining) == 1:
            selected = remaining[0]
        elif candidate.occurrence_index < len(starts) and starts[candidate.occurrence_index] in remaining:
            selected = starts[candidate.occurrence_index]
        else:
            return _unresolved(chunk, candidate, len(starts))

    local_start = py_index_to_utf16(chunk.text, selected)
    local_end = py_index_to_utf16(chunk.text, selected + len(candidate.dialogue_excerpt))
    return LocatedCandidate(
        chunk_id=chunk.chunk_id,
        candidate=candidate,
        text=candidate.dialogue_excerpt,
        local_start_utf16=local_start,
        local_end_utf16=local_end,
        start_utf16=chunk.start_utf16 + local_start,
        end_utf16=chunk.start_utf16 + local_end,
        occurrence_index=starts.index(selected),
    )


def _paragraph_boundaries(text: str) -> list[int]:
    return [match.end() for match in re.finditer(r"(?:(?:\r\n)|\r|\n){2,}", text)]


def chunk_source(text: str, *, max_chars: int = 6000, overlap_chars: int = 256) -> list[AnalysisChunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than max_chars")
    if not text:
        return [AnalysisChunk.single("")]

    boundaries = _paragraph_boundaries(text)
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        limit = min(len(text), start + max_chars)
        if limit == len(text):
            end = len(text)
        else:
            candidates = [boundary for boundary in boundaries if start < boundary <= limit]
            end = candidates[-1] if candidates else limit
        ranges.append((start, end))
        if end == len(text):
            break
        target = max(start + 1, end - overlap_chars)
        paragraph_starts = [boundary for boundary in boundaries if target <= boundary < end]
        next_start = paragraph_starts[0] if paragraph_starts else target
        if next_start <= start:
            next_start = end
        start = next_start

    chunks: list[AnalysisChunk] = []
    for index, (start, end) in enumerate(ranges):
        start_utf16 = py_index_to_utf16(text, start)
        end_utf16 = py_index_to_utf16(text, end)
        previous_end_utf16 = py_index_to_utf16(text, ranges[index - 1][1]) if index else start_utf16
        next_start_utf16 = (
            py_index_to_utf16(text, ranges[index + 1][0]) if index + 1 < len(ranges) else end_utf16
        )
        chunks.append(
            AnalysisChunk(
                chunk_id=f"chunk-{index:04d}",
                text=text[start:end],
                start_utf16=start_utf16,
                end_utf16=end_utf16,
                overlap_before=max(0, previous_end_utf16 - start_utf16),
                overlap_after=max(0, end_utf16 - next_start_utf16),
            )
        )
    return chunks


def _name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _unique_occurrence(text: str, excerpt: str) -> tuple[int, int] | None:
    starts = _all_occurrences(text, excerpt)
    if len(starts) != 1:
        return None
    return starts[0], starts[0] + len(excerpt)


def _unique_chunk_occurrence(chunk: AnalysisChunk, excerpt: str) -> tuple[int, int] | None:
    located = _unique_occurrence(chunk.text, excerpt)
    if located is None:
        return None
    local_start = py_index_to_utf16(chunk.text, located[0])
    local_end = py_index_to_utf16(chunk.text, located[1])
    return chunk.start_utf16 + local_start, chunk.start_utf16 + local_end


def _has_alias_relationship_evidence(
    source_text: str,
    canonical_name: str,
    alias: str,
    evidence_excerpts: Iterable[str],
) -> bool:
    for evidence in evidence_excerpts:
        if _unique_occurrence(source_text, evidence) is None:
            continue
        canonical_ranges = [
            (start, start + len(canonical_name))
            for start in _all_occurrences(evidence, canonical_name)
        ]
        if not canonical_ranges:
            continue
        for alias_start in _all_occurrences(evidence, alias):
            alias_end = alias_start + len(alias)
            if any(
                alias_end <= canonical_start or alias_start >= canonical_end
                for canonical_start, canonical_end in canonical_ranges
            ):
                return True
    return False


def _candidate_claims(
    candidate: UtteranceCandidate,
    speaker_identities: dict[str, str] | None = None,
) -> tuple[object, ...]:
    speaker_key = _name_key(candidate.speaker_name) if candidate.speaker_name else None
    if speaker_key is not None and speaker_identities is not None:
        speaker_key = speaker_identities.get(speaker_key, speaker_key)
    return (
        speaker_key,
        candidate.normalized_emotion.value if candidate.normalized_emotion else None,
        candidate.custom_emotion,
        candidate.emotion_intensity,
        candidate.emotion_origin.value,
        candidate.language,
        tuple(sorted(code.value for code in candidate.uncertainty_codes)),
    )


def _conflicting_claim_dimensions(
    candidates: Iterable[UtteranceCandidate],
    speaker_identities: dict[str, str] | None = None,
) -> list[str]:
    claims = [
        _candidate_claims(candidate, speaker_identities)
        for candidate in candidates
    ]
    if len(claims) < 2:
        return []
    dimensions: list[str] = []
    if len({claim[0] for claim in claims}) > 1:
        dimensions.append("speaker")
    if len({claim[1:5] for claim in claims}) > 1:
        dimensions.append("emotion")
    if len({claim[5] for claim in claims}) > 1:
        dimensions.append("language")
    if len({claim[6] for claim in claims}) > 1:
        dimensions.append("uncertainty")
    return dimensions


def _source_span(source: ScriptRevision, start_utf16: int, end_utf16: int) -> SourceSpan:
    start = utf16_to_py_index(source.source_markdown, start_utf16)
    end = utf16_to_py_index(source.source_markdown, end_utf16)
    return SourceSpan(
        source_revision_id=source.revision_id,
        start_utf16=start_utf16,
        end_utf16=end_utf16,
        text=source.source_markdown[start:end],
        source_sha256=source.source_sha256 or sha256_source(source.source_markdown),
    )


def _warning(
    code: str,
    *,
    identity: object,
    annotation_id: str | None = None,
    utterance_id: str | None = None,
    details: dict[str, object] | None = None,
) -> AnalysisWarning:
    messages = {
        "chunk_failed": "One semantic analysis chunk failed; grounded results from other chunks were kept.",
        "source_anchor_ambiguous": "A dialogue candidate could not be uniquely grounded.",
        "speaker_evidence_mismatch": "Speaker evidence did not resolve uniquely in the source chunk.",
        "emotion_evidence_mismatch": "Emotion evidence did not resolve uniquely in the source chunk.",
        "source_excerpt_mismatch": "Diagnostic source_excerpt did not match the grounded dialogue context.",
        "character_evidence_mismatch": "Character evidence did not resolve uniquely in the source.",
        "alias_evidence_mismatch": "An explicit alias lacked unambiguous contextual source evidence.",
        "overlap_candidate_conflict": "Overlapping candidates disagreed on semantic claims; the grounded dialogue remains pending.",
        "missing_quoted_dialogue": "Quoted source text was not covered by a grounded dialogue candidate.",
    }
    return AnalysisWarning(
        id=_digest_id("warning", code, identity),
        code=code,
        message=scrub_error(messages[code]),
        annotation_id=annotation_id,
        utterance_id=utterance_id,
        details=details or {},
    )


@dataclass
class _CharacterRecord:
    canonical_name: str
    confidence: float
    aliases: list[str] = field(default_factory=list)
    evidence_excerpts: list[str] = field(default_factory=list)
    supporting_annotation_ids: list[str] = field(default_factory=list)
    evidence_grounded: bool = False
    first_source_index: int = 1 << 60


@dataclass
class _GroundedUtterance:
    located: LocatedCandidate
    chunk: AnalysisChunk
    dialogue_annotation: SemanticAnnotation
    speaker_annotation_id: str | None
    speaker_key: str | None
    emotion_annotation_ids: list[str]
    emotion_origin: EmotionOrigin
    uncertainty_codes: list[UncertaintyCode]


class SemanticAnalysisService:
    def __init__(
        self,
        provider: SemanticProvider,
        *,
        max_chunk_chars: int = 6000,
        overlap_chars: int = 256,
        acceptance_threshold: float = AI_ACCEPTANCE_THRESHOLD,
    ) -> None:
        self.provider = provider
        self.max_chunk_chars = max_chunk_chars
        self.overlap_chars = overlap_chars
        self.acceptance_threshold = acceptance_threshold

    def analyze(
        self,
        project_id: str,
        source_revision: ScriptRevision,
        run: AnalysisRun,
        draft: SemanticAnalysisDraft,
    ) -> SemanticAnalysisDraft:
        if project_id != run.project_id or project_id != draft.project_id:
            raise ValueError("semantic analysis project identity mismatch")
        if source_revision.revision_id != run.source_revision_id or source_revision.revision_id != draft.source_revision_id:
            raise ValueError("semantic analysis source identity mismatch")

        chunks = chunk_source(
            source_revision.source_markdown,
            max_chars=self.max_chunk_chars,
            overlap_chars=self.overlap_chars,
        )
        successes: list[tuple[AnalysisChunk, SemanticProviderResponse]] = []
        failures: list[tuple[AnalysisChunk, SemanticProviderError]] = []
        for chunk in chunks:
            try:
                successes.append((chunk, self.provider.analyze_chunk(chunk)))
            except SemanticProviderError as exc:
                failures.append((chunk, exc))

        if not successes:
            if failures and all(isinstance(error, SemanticProviderContractError) for _, error in failures):
                raise SemanticProviderContractError("all semantic chunks failed structural validation")
            if failures:
                raise failures[0][1]
            raise SemanticProviderContractError("semantic analysis produced no chunk result")

        warnings: list[AnalysisWarning] = [
            _warning("chunk_failed", identity=chunk.chunk_id, details={"chunk_id": chunk.chunk_id})
            for chunk, _error in failures
        ]
        unresolved: list[UnresolvedCandidate] = []

        character_records = self._merge_characters(source_revision, successes, warnings)
        alias_map = self._alias_map(character_records)

        located_groups: dict[tuple[int, int], list[tuple[AnalysisChunk, LocatedCandidate]]] = {}
        for chunk, response in successes:
            for candidate in response.utterance_candidates:
                if UncertaintyCode.SOURCE_ANCHOR_AMBIGUOUS in candidate.uncertainty_codes:
                    result: LocatedCandidate | UnresolvedCandidate = _unresolved(chunk, candidate, 0)
                else:
                    result = locate_candidate(chunk, candidate)
                if isinstance(result, UnresolvedCandidate):
                    unresolved.append(result)
                    warnings.append(
                        _warning(
                            "source_anchor_ambiguous",
                            identity=result.id,
                            details={"chunk_id": chunk.chunk_id},
                        )
                    )
                    continue
                key = (result.start_utf16, result.end_utf16)
                located_groups.setdefault(key, []).append((chunk, result))

        located_by_span: dict[tuple[int, int], tuple[AnalysisChunk, LocatedCandidate]] = {}
        for key, group in located_groups.items():
            selected_chunk, selected = sorted(
                group,
                key=lambda item: (
                    -item[1].candidate.confidence,
                    item[0].chunk_id,
                    repr(_candidate_claims(item[1].candidate, alias_map)),
                ),
            )[0]
            dimensions = _conflicting_claim_dimensions(
                (item[1].candidate for item in group), alias_map
            )
            if dimensions:
                uncertainty = list(selected.candidate.uncertainty_codes)
                if UncertaintyCode.DIALOGUE_AMBIGUOUS not in uncertainty:
                    uncertainty.append(UncertaintyCode.DIALOGUE_AMBIGUOUS)
                selected = selected.model_copy(
                    update={
                        "candidate": selected.candidate.model_copy(
                            update={"uncertainty_codes": uncertainty}
                        )
                    }
                )
                warnings.append(
                    _warning(
                        "overlap_candidate_conflict",
                        identity=(key, tuple(dimensions)),
                        details={
                            "start_utf16": key[0],
                            "end_utf16": key[1],
                            "candidate_count": len(group),
                            "dimensions": dimensions,
                            "chunk_ids": sorted({item[0].chunk_id for item in group}),
                        },
                    )
                )
            located_by_span[key] = (selected_chunk, selected)

        annotations_by_key: dict[tuple[AnnotationKind, int, int], SemanticAnnotation] = {}
        grounded: list[_GroundedUtterance] = []
        for chunk, located in sorted(located_by_span.values(), key=lambda item: item[1].start_utf16):
            candidate = located.candidate
            dialogue_status = (
                ReviewStatus.ACCEPTED
                if candidate.confidence >= self.acceptance_threshold
                and UncertaintyCode.DIALOGUE_AMBIGUOUS not in candidate.uncertainty_codes
                else ReviewStatus.PENDING
            )
            dialogue = self._annotation(
                source_revision,
                AnnotationKind.DIALOGUE,
                located.start_utf16,
                located.end_utf16,
                candidate.confidence,
                dialogue_status,
            )
            dialogue = self._keep_annotation(annotations_by_key, dialogue)

            speaker_key = _name_key(candidate.speaker_name) if candidate.speaker_name else None
            speaker_annotation_id: str | None = None
            if candidate.speaker_evidence_excerpt:
                speaker_span = _unique_chunk_occurrence(chunk, candidate.speaker_evidence_excerpt)
                if speaker_span is None:
                    warnings.append(
                        _warning(
                            "speaker_evidence_mismatch",
                            identity=(chunk.chunk_id, located.start_utf16),
                            utterance_id=_digest_id("utterance", located.start_utf16, located.end_utf16),
                        )
                    )
                else:
                    speaker_status = (
                        ReviewStatus.ACCEPTED
                        if candidate.confidence >= self.acceptance_threshold
                        and not {
                            UncertaintyCode.SPEAKER_UNKNOWN,
                            UncertaintyCode.SPEAKER_AMBIGUOUS,
                        }.intersection(candidate.uncertainty_codes)
                        else ReviewStatus.PENDING
                    )
                    speaker = self._annotation(
                        source_revision,
                        AnnotationKind.SPEAKER,
                        speaker_span[0],
                        speaker_span[1],
                        candidate.confidence,
                        speaker_status,
                    )
                    speaker = self._keep_annotation(annotations_by_key, speaker)
                    speaker_annotation_id = speaker.id
                    canonical_key = alias_map.get(speaker_key or "")
                    if canonical_key in character_records:
                        record = character_records[canonical_key]
                        record.evidence_grounded = True
                        if speaker.id not in record.supporting_annotation_ids:
                            record.supporting_annotation_ids.append(speaker.id)

            emotion_annotation_ids: list[str] = []
            for evidence_index, excerpt in enumerate(candidate.emotion_evidence_excerpts):
                emotion_span = _unique_chunk_occurrence(chunk, excerpt)
                if emotion_span is None:
                    warnings.append(
                        _warning(
                            "emotion_evidence_mismatch",
                            identity=(chunk.chunk_id, located.start_utf16, evidence_index),
                        )
                    )
                    continue
                emotion = self._annotation(
                    source_revision,
                    AnnotationKind.EMOTION_EVIDENCE,
                    emotion_span[0],
                    emotion_span[1],
                    candidate.confidence,
                    ReviewStatus.ACCEPTED
                    if candidate.confidence >= self.acceptance_threshold
                    else ReviewStatus.PENDING,
                )
                emotion = self._keep_annotation(annotations_by_key, emotion)
                if emotion.id not in emotion_annotation_ids:
                    emotion_annotation_ids.append(emotion.id)

            if candidate.source_excerpt:
                diagnostic = _unique_chunk_occurrence(chunk, candidate.source_excerpt)
                if diagnostic is None or not (
                    diagnostic[0] <= located.start_utf16
                    and located.end_utf16 <= diagnostic[1]
                ):
                    warnings.append(
                        _warning(
                            "source_excerpt_mismatch",
                            identity=(chunk.chunk_id, located.start_utf16),
                        )
                    )

            emotion_origin = candidate.emotion_origin
            uncertainty = list(candidate.uncertainty_codes)
            if (
                candidate.normalized_emotion is not None
                and emotion_origin is EmotionOrigin.SOURCE_GROUNDED
                and not emotion_annotation_ids
            ):
                emotion_origin = EmotionOrigin.INFERRED
                if UncertaintyCode.EMOTION_INFERRED not in uncertainty:
                    uncertainty.append(UncertaintyCode.EMOTION_INFERRED)

            grounded.append(
                _GroundedUtterance(
                    located=located,
                    chunk=chunk,
                    dialogue_annotation=dialogue,
                    speaker_annotation_id=speaker_annotation_id,
                    speaker_key=speaker_key,
                    emotion_annotation_ids=emotion_annotation_ids,
                    emotion_origin=emotion_origin,
                    uncertainty_codes=uncertainty,
                )
            )

        characters = self._materialize_characters(character_records)
        characters_by_key = {_name_key(item.canonical_name): item for item in characters}
        utterances: list[SemanticUtterance] = []
        for item in grounded:
            candidate = item.located.candidate
            canonical_key = alias_map.get(item.speaker_key or "")
            character = characters_by_key.get(canonical_key or "")
            blocking = {
                UncertaintyCode.SPEAKER_UNKNOWN,
                UncertaintyCode.SPEAKER_AMBIGUOUS,
                UncertaintyCode.DIALOGUE_AMBIGUOUS,
            }.intersection(item.uncertainty_codes)
            status = (
                ReviewStatus.ACCEPTED
                if item.dialogue_annotation.status is ReviewStatus.ACCEPTED
                and character is not None
                and character.status is ReviewStatus.ACCEPTED
                and not blocking
                else ReviewStatus.PENDING
            )
            uncertainty = list(item.uncertainty_codes)
            if not candidate.speaker_name and UncertaintyCode.SPEAKER_UNKNOWN not in uncertainty:
                uncertainty.append(UncertaintyCode.SPEAKER_UNKNOWN)
            utterances.append(
                SemanticUtterance(
                    id=_digest_id("utterance", item.located.start_utf16, item.located.end_utf16),
                    dialogue_annotation_id=item.dialogue_annotation.id,
                    speaker_annotation_id=item.speaker_annotation_id,
                    character_candidate_id=character.id if character else None,
                    emotion_evidence_annotation_ids=item.emotion_annotation_ids,
                    normalized_emotion=candidate.normalized_emotion,
                    custom_emotion=candidate.custom_emotion,
                    emotion_intensity=candidate.emotion_intensity,
                    emotion_origin=item.emotion_origin,
                    language=candidate.language,
                    confidence=candidate.confidence,
                    uncertainty_codes=uncertainty,
                    status=status,
                )
            )

        annotations = sorted(
            annotations_by_key.values(),
            key=lambda item: (item.span.start_utf16, item.span.end_utf16, item.kind.value, item.id),
        )
        warnings.extend(self._quoted_coverage_warnings(source_revision, annotations))
        warnings = self._deduplicate_models(warnings)
        unresolved = self._deduplicate_models(unresolved)
        for annotation in annotations:
            validate_source_span(annotation.span, source_revision)

        return draft.model_copy(
            update={
                "annotations": annotations,
                "characters": characters,
                "utterances": utterances,
                "unresolved_candidates": unresolved,
                "warnings": warnings,
                "provider": self.provider.name,
                "model": self.provider.model,
                "prompt_version": PROMPT_VERSION,
                "contract_version": CONTRACT_VERSION,
            }
        )

    def _merge_characters(
        self,
        source: ScriptRevision,
        successes: Iterable[tuple[AnalysisChunk, SemanticProviderResponse]],
        warnings: list[AnalysisWarning],
    ) -> dict[str, _CharacterRecord]:
        records: dict[str, _CharacterRecord] = {}
        raw_alias_owners: dict[str, set[str]] = {}
        for _chunk, response in successes:
            for candidate in response.character_candidates:
                key = _name_key(candidate.canonical_name)
                record = records.get(key)
                if record is None:
                    record = _CharacterRecord(candidate.canonical_name, candidate.confidence)
                    records[key] = record
                elif candidate.confidence > record.confidence:
                    record.canonical_name = candidate.canonical_name
                    record.confidence = candidate.confidence
                for evidence in candidate.evidence_excerpts:
                    if evidence not in record.evidence_excerpts:
                        record.evidence_excerpts.append(evidence)
                    located = _unique_occurrence(source.source_markdown, evidence)
                    if located is not None:
                        record.evidence_grounded = True
                        record.first_source_index = min(record.first_source_index, located[0])
                    else:
                        warnings.append(
                            _warning("character_evidence_mismatch", identity=(key, evidence))
                        )
                for alias in candidate.aliases:
                    alias_key = _name_key(alias)
                    raw_alias_owners.setdefault(alias_key, set()).add(key)
                    if alias not in record.aliases:
                        record.aliases.append(alias)

        for key, record in records.items():
            visible: list[str] = []
            for alias in record.aliases:
                alias_key = _name_key(alias)
                if (
                    raw_alias_owners.get(alias_key) == {key}
                    and alias_key not in records
                    and _has_alias_relationship_evidence(
                        source.source_markdown,
                        record.canonical_name,
                        alias,
                        record.evidence_excerpts,
                    )
                ):
                    visible.append(alias)
                else:
                    warnings.append(_warning("alias_evidence_mismatch", identity=(key, alias_key)))
            record.aliases = visible
            if record.first_source_index == 1 << 60:
                name_occurrences = _all_occurrences(source.source_markdown, record.canonical_name)
                if name_occurrences:
                    record.first_source_index = name_occurrences[0]
        return records

    @staticmethod
    def _alias_map(records: dict[str, _CharacterRecord]) -> dict[str, str]:
        mapping = {key: key for key in records}
        for key, record in records.items():
            for alias in record.aliases:
                mapping[_name_key(alias)] = key
        return mapping

    def _materialize_characters(
        self, records: dict[str, _CharacterRecord]
    ) -> list[CharacterCandidate]:
        ordered = sorted(records.items(), key=lambda item: (item[1].first_source_index, item[0]))
        return [
            CharacterCandidate(
                id=_digest_id("character", key),
                canonical_name=record.canonical_name,
                aliases=record.aliases,
                supporting_annotation_ids=record.supporting_annotation_ids,
                confidence=record.confidence,
                status=ReviewStatus.ACCEPTED
                if record.confidence >= self.acceptance_threshold and record.evidence_grounded
                else ReviewStatus.PENDING,
                origin=AnnotationOrigin.AI,
            )
            for key, record in ordered
        ]

    @staticmethod
    def _keep_annotation(
        annotations: dict[tuple[AnnotationKind, int, int], SemanticAnnotation],
        annotation: SemanticAnnotation,
    ) -> SemanticAnnotation:
        key = (annotation.kind, annotation.span.start_utf16, annotation.span.end_utf16)
        existing = annotations.get(key)
        if existing is None or (annotation.confidence or 0) > (existing.confidence or 0):
            annotations[key] = annotation
            return annotation
        return existing

    @staticmethod
    def _annotation(
        source: ScriptRevision,
        kind: AnnotationKind,
        start_utf16: int,
        end_utf16: int,
        confidence: float,
        status: ReviewStatus,
    ) -> SemanticAnnotation:
        return SemanticAnnotation(
            id=_digest_id("annotation", kind.value, start_utf16, end_utf16),
            kind=kind,
            span=_source_span(source, start_utf16, end_utf16),
            origin=AnnotationOrigin.AI,
            confidence=confidence,
            status=status,
        )

    @staticmethod
    def _deduplicate_models(items: list) -> list:
        by_id = {item.id: item for item in items}
        return [by_id[key] for key in sorted(by_id)]

    @staticmethod
    def _quoted_coverage_warnings(
        source: ScriptRevision, annotations: list[SemanticAnnotation]
    ) -> list[AnalysisWarning]:
        covered = {
            (item.span.start_utf16, item.span.end_utf16)
            for item in annotations
            if item.kind is AnnotationKind.DIALOGUE
        }
        warnings: list[AnalysisWarning] = []
        patterns = [r"“([^”\r\n]+)”", r'"([^"\r\n]+)"', r"「([^」\r\n]+)」", r"『([^』\r\n]+)』", r"‘([^’\r\n]+)’"]
        for pattern in patterns:
            for match in re.finditer(pattern, source.source_markdown):
                start_utf16 = py_index_to_utf16(source.source_markdown, match.start(1))
                end_utf16 = py_index_to_utf16(source.source_markdown, match.end(1))
                if (start_utf16, end_utf16) not in covered:
                    warnings.append(
                        _warning(
                            "missing_quoted_dialogue",
                            identity=(start_utf16, end_utf16),
                            details={"start_utf16": start_utf16, "end_utf16": end_utf16},
                        )
                    )
        return warnings
