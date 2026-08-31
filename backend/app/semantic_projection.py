from __future__ import annotations

import copy
import hashlib

from app.models import ParseRevision, ProjectCharacter, ScriptLine, ScriptProject, line_with_revision_uid
from app.semantic_models import CharacterCandidate, ReviewStatus, SemanticAnalysisDraft


class SemanticProjectionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _deterministic_project_character_id(candidate: CharacterCandidate) -> str:
    digest = hashlib.sha256(candidate.canonical_name.encode("utf-8")).hexdigest()[:24]
    return f"semantic-role-{digest}"


def resolve_project_character(project: ScriptProject, candidate: CharacterCandidate) -> ProjectCharacter:
    if candidate.project_character_id:
        match = next(
            (
                character
                for character in project.project_characters
                if character.project_character_id == candidate.project_character_id
            ),
            None,
        )
        if match is None:
            raise SemanticProjectionError("missing_project_character")
        return match

    matches = [character for character in project.project_characters if character.name == candidate.canonical_name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SemanticProjectionError("ambiguous_project_character")

    project_character_id = _deterministic_project_character_id(candidate)
    collision = next(
        (
            character
            for character in project.project_characters
            if character.project_character_id == project_character_id
        ),
        None,
    )
    if collision is not None:
        if collision.name == candidate.canonical_name:
            return collision
        raise SemanticProjectionError("project_character_id_collision")

    character = ProjectCharacter(project_character_id=project_character_id, name=candidate.canonical_name)
    project.project_characters.append(character)
    return character


def project_confirmed_draft(
    project: ScriptProject,
    draft: SemanticAnalysisDraft,
    semantic_revision_id: str,
) -> ParseRevision:
    parse_revision_id = f"semantic-{semantic_revision_id}"
    annotations = {annotation.id: annotation for annotation in draft.annotations}
    characters = {candidate.id: candidate for candidate in draft.characters}

    projected: list[tuple[int, int, str, ScriptLine]] = []
    for utterance in draft.utterances:
        if utterance.status is not ReviewStatus.ACCEPTED or not utterance.character_candidate_id:
            continue
        dialogue = annotations.get(utterance.dialogue_annotation_id)
        candidate = characters.get(utterance.character_candidate_id)
        if (
            dialogue is None
            or dialogue.status is not ReviewStatus.ACCEPTED
            or candidate is None
            or candidate.status is not ReviewStatus.ACCEPTED
        ):
            continue
        character = resolve_project_character(project, candidate)
        line = ScriptLine(
            id=utterance.id,
            character_id=character.project_character_id,
            text=dialogue.span.text,
            note=utterance.normalized_emotion.value if utterance.normalized_emotion is not None else "",
            language=utterance.language or project.default_language,
            semantic_revision_id=semantic_revision_id,
            utterance_id=utterance.id,
        )
        projected.append(
            (
                dialogue.span.start_utf16,
                dialogue.span.end_utf16,
                utterance.id,
                line_with_revision_uid(line, parse_revision_id),
            )
        )

    projected.sort(key=lambda item: item[:3])
    lines = [item[3] for item in projected]
    existing = next(
        (revision for revision in project.parse_revisions if revision.revision_id == parse_revision_id),
        None,
    )
    if existing is not None:
        _require_compatible_parse_revision(existing, draft, lines, project)
        project.active_parse_revision_id = existing.revision_id
        project.lines = copy.deepcopy(existing.lines)
        return existing

    revision = ParseRevision(
        revision_id=parse_revision_id,
        script_revision_id=draft.source_revision_id,
        parent_parse_revision_id=project.active_parse_revision_id,
        provider="semantic-confirmed",
        project_characters=copy.deepcopy(project.project_characters),
        lines=copy.deepcopy(lines),
    )
    project.parse_revisions.append(revision)
    project.active_parse_revision_id = revision.revision_id
    project.lines = copy.deepcopy(revision.lines)
    return revision


def validate_confirmed_parse_revision(
    project: ScriptProject,
    draft: SemanticAnalysisDraft,
    semantic_revision_id: str,
    parse_revision_id: str,
) -> ParseRevision:
    expected_parse_revision_id = f"semantic-{semantic_revision_id}"
    if parse_revision_id != expected_parse_revision_id:
        raise SemanticProjectionError("parse_revision_collision")
    existing = next(
        (revision for revision in project.parse_revisions if revision.revision_id == parse_revision_id),
        None,
    )
    if existing is None:
        raise SemanticProjectionError("confirmed_parse_revision_missing")

    validation_project = copy.deepcopy(project)
    validation_project.project_characters = copy.deepcopy(existing.project_characters)
    validation_project.active_parse_revision_id = existing.revision_id
    validation_project.lines = copy.deepcopy(existing.lines)
    validated = project_confirmed_draft(validation_project, draft, semantic_revision_id)
    if validated != existing:
        raise SemanticProjectionError("parse_revision_collision")
    return existing


def _require_compatible_parse_revision(
    revision: ParseRevision,
    draft: SemanticAnalysisDraft,
    lines: list[ScriptLine],
    project: ScriptProject,
) -> None:
    if (
        revision.provider != "semantic-confirmed"
        or revision.script_revision_id != draft.source_revision_id
        or revision.lines != lines
        or revision.project_characters != project.project_characters
    ):
        raise SemanticProjectionError("parse_revision_collision")
    parse_revision_ids = {item.revision_id for item in project.parse_revisions}
    if project.active_parse_revision_id == revision.revision_id:
        if (
            revision.parent_parse_revision_id is None
            or revision.parent_parse_revision_id == revision.revision_id
            or revision.parent_parse_revision_id not in parse_revision_ids
        ):
            raise SemanticProjectionError("parse_revision_collision")
    elif (
        project.active_parse_revision_id not in parse_revision_ids
        or revision.parent_parse_revision_id != project.active_parse_revision_id
    ):
        raise SemanticProjectionError("parse_revision_collision")
