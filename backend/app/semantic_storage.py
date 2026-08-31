from __future__ import annotations

import copy
import json
import threading
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field

from app.models import ScriptRevision
from app.path_safety import validate_windows_component
from app.semantic_models import (
    AnalysisRun,
    AnalysisRunStatus,
    AnalysisWarning,
    AnnotationKind,
    CharacterCandidate,
    ReviewStatus,
    SemanticAnalysisDraft,
    SemanticAnnotation,
    SemanticRevision,
    SemanticUtterance,
)
from app.semantic_source import validate_source_span
from app.storage import ProjectStore, windows_filesystem_path, windows_path_identity


class SemanticStorageError(ValueError):
    code = "semantic_storage_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


class SemanticNotFoundError(SemanticStorageError):
    code = "semantic_not_found"


class SemanticConflictError(SemanticStorageError):
    code = "semantic_conflict"


class SemanticValidationError(SemanticStorageError):
    code = "semantic_draft_invalid"


class ReplaceAnalysisResultError(SemanticValidationError):
    pass


class CreateAnnotation(BaseModel):
    op: Literal["create_annotation"]
    annotation: SemanticAnnotation


class ReplaceAnnotation(BaseModel):
    op: Literal["replace_annotation"]
    annotation_id: str
    annotation: SemanticAnnotation


class DeleteAnnotation(BaseModel):
    op: Literal["delete_annotation"]
    annotation_id: str


class SetAnnotationStatus(BaseModel):
    op: Literal["set_annotation_status"]
    annotation_id: str
    status: ReviewStatus


class UpsertCharacter(BaseModel):
    op: Literal["upsert_character"]
    character: CharacterCandidate


class SetCharacterStatus(BaseModel):
    op: Literal["set_character_status"]
    character_id: str
    status: ReviewStatus


class MergeCharacters(BaseModel):
    op: Literal["merge_characters"]
    target_character_id: str
    source_character_ids: list[str]


class SplitAlias(BaseModel):
    op: Literal["split_alias"]
    character_id: str
    alias: str
    character: CharacterCandidate


class CreateUtterance(BaseModel):
    op: Literal["create_utterance"]
    utterance: SemanticUtterance


class UpdateUtterance(BaseModel):
    op: Literal["update_utterance"]
    utterance_id: str
    utterance: SemanticUtterance


class DeleteUtterance(BaseModel):
    op: Literal["delete_utterance"]
    utterance_id: str


class SetUtteranceStatus(BaseModel):
    op: Literal["set_utterance_status"]
    utterance_id: str
    status: ReviewStatus


class DismissWarning(BaseModel):
    op: Literal["dismiss_warning"]
    warning_id: str


DraftOperation: TypeAlias = Annotated[
    CreateAnnotation
    | ReplaceAnnotation
    | DeleteAnnotation
    | SetAnnotationStatus
    | UpsertCharacter
    | SetCharacterStatus
    | MergeCharacters
    | SplitAlias
    | CreateUtterance
    | UpdateUtterance
    | DeleteUtterance
    | SetUtteranceStatus
    | DismissWarning,
    Field(discriminator="op"),
]


class DraftPatchRequest(BaseModel):
    expected_version: int = Field(ge=1)
    operations: list[DraftOperation]


class SemanticStore:
    _index_locks_guard = threading.Lock()
    _index_locks: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()

    def __init__(self, project_store: ProjectStore) -> None:
        self.project_store = project_store

    def create_run_and_draft(
        self, project_id: str, source_revision_id: str, *, trace_id: str
    ) -> tuple[AnalysisRun, SemanticAnalysisDraft]:
        safe_project_id = self._safe_project_id(project_id)
        with self.project_store.project_lock(safe_project_id):
            self._source_revision(safe_project_id, source_revision_id)
            run_id = f"run-{uuid.uuid4().hex}"
            draft_id = f"draft-{uuid.uuid4().hex}"
            run = AnalysisRun(
                id=run_id,
                project_id=safe_project_id,
                source_revision_id=source_revision_id,
                draft_id=draft_id,
                trace_id=trace_id,
            )
            draft = SemanticAnalysisDraft(
                id=draft_id,
                project_id=safe_project_id,
                source_revision_id=source_revision_id,
            )
            self._write_model(self._run_path(safe_project_id, run_id), run)
            self._write_model(self._draft_path(safe_project_id, draft_id), draft)
            with self._index_lock():
                index = self._load_index()
                index["runs"][run_id] = {"project_id": safe_project_id, "draft_id": draft_id}
                index["drafts"][draft_id] = {"project_id": safe_project_id, "run_id": run_id}
                self._write_index(index)
            return run, draft

    def load_run(self, run_id: str) -> AnalysisRun:
        project_id = self._project_for("runs", run_id)
        return self._read_model(self._run_path(project_id, run_id), AnalysisRun)

    def load_draft(self, draft_id: str) -> SemanticAnalysisDraft:
        project_id = self._project_for("drafts", draft_id)
        return self._read_model(self._draft_path(project_id, draft_id), SemanticAnalysisDraft)

    def save_run(self, run: AnalysisRun) -> None:
        project_id = self._project_for("runs", run.id)
        if project_id != run.project_id:
            raise SemanticValidationError()
        with self.project_store.project_lock(project_id):
            self._write_model(self._run_path(project_id, run.id), self._touch(run))

    def replace_analysis_result(self, run_id: str, replacement: SemanticAnalysisDraft) -> SemanticAnalysisDraft:
        project_id = self._project_for("runs", run_id)
        with self.project_store.project_lock(project_id):
            run = self._read_model(self._run_path(project_id, run_id), AnalysisRun)
            draft = self._read_model(self._draft_path(project_id, run.draft_id), SemanticAnalysisDraft)
            if draft.confirmed_revision_id is not None:
                raise ReplaceAnalysisResultError("draft_confirmed")
            if draft.version != 1:
                raise ReplaceAnalysisResultError()
            saved = replacement.model_copy(
                update={
                    "id": draft.id,
                    "project_id": draft.project_id,
                    "source_revision_id": draft.source_revision_id,
                    "version": draft.version,
                    "created_at": draft.created_at,
                    "updated_at": _now(),
                    "confirmed_revision_id": None,
                    "confirmed_parse_revision_id": None,
                    "confirm_idempotency_key": None,
                },
                deep=True,
            )
            self._validate_draft(saved, self._source_revision(project_id, saved.source_revision_id))
            self._write_model(self._draft_path(project_id, saved.id), saved)
            return saved

    def patch_draft(
        self, draft_id: str, expected_version: int, operations: list[DraftOperation]
    ) -> SemanticAnalysisDraft:
        project_id = self._project_for("drafts", draft_id)
        with self.project_store.project_lock(project_id):
            draft = self._read_model(self._draft_path(project_id, draft_id), SemanticAnalysisDraft)
            if draft.version != expected_version:
                raise SemanticConflictError("draft_version_conflict")
            run = self._read_model(self._run_path(project_id, self._run_for_draft(draft_id)), AnalysisRun)
            if run.status in {AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING}:
                raise SemanticConflictError("semantic_run_not_terminal")
            if draft.confirmed_revision_id is not None:
                raise SemanticConflictError("draft_confirmed")
            candidate = copy.deepcopy(draft)
            try:
                for operation in operations:
                    self._apply(candidate, operation)
                self._validate_draft(candidate, self._source_revision(project_id, candidate.source_revision_id))
            except SemanticStorageError:
                raise
            except (ValueError, KeyError) as error:
                raise SemanticValidationError() from error
            saved = candidate.model_copy(update={"version": draft.version + 1, "updated_at": _now()}, deep=True)
            self._write_model(self._draft_path(project_id, draft_id), saved)
            return saved

    def load_revision(self, revision_id: str) -> SemanticRevision:
        project_id = self._project_for("revisions", revision_id)
        return self._read_model(self._revision_path(project_id, revision_id), SemanticRevision)

    def interrupt_incomplete_runs(self) -> int:
        index = self._load_index()
        changed = 0
        for run_id, metadata in list(index["runs"].items()):
            project_id = metadata["project_id"]
            with self.project_store.project_lock(project_id):
                run = self._read_model(self._run_path(project_id, run_id), AnalysisRun)
                if run.status in {AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING}:
                    run = self._touch(run.model_copy(update={"status": AnalysisRunStatus.INTERRUPTED}))
                    self._write_model(self._run_path(project_id, run_id), run)
                    changed += 1
        return changed

    def _apply(self, draft: SemanticAnalysisDraft, operation: DraftOperation) -> None:
        if isinstance(operation, CreateAnnotation):
            self._ensure_missing(draft.annotations, operation.annotation.id)
            draft.annotations.append(operation.annotation)
        elif isinstance(operation, ReplaceAnnotation):
            self._ensure_id_matches(operation.annotation_id, operation.annotation.id)
            self._replace(draft.annotations, operation.annotation_id, operation.annotation)
        elif isinstance(operation, DeleteAnnotation):
            annotation = self._find(draft.annotations, operation.annotation_id)
            draft.annotations = [item for item in draft.annotations if item.id != annotation.id]
            if annotation.kind is AnnotationKind.DIALOGUE:
                draft.utterances = [item for item in draft.utterances if item.dialogue_annotation_id != annotation.id]
            elif annotation.kind is AnnotationKind.SPEAKER:
                draft.utterances = [
                    item.model_copy(update={"speaker_annotation_id": None}) if item.speaker_annotation_id == annotation.id else item
                    for item in draft.utterances
                ]
                draft.characters = [
                    item.model_copy(update={"supporting_annotation_ids": [support for support in item.supporting_annotation_ids if support != annotation.id]})
                    for item in draft.characters
                ]
            else:
                draft.utterances = [
                    item.model_copy(update={"emotion_evidence_annotation_ids": [evidence for evidence in item.emotion_evidence_annotation_ids if evidence != annotation.id]})
                    for item in draft.utterances
                ]
        elif isinstance(operation, SetAnnotationStatus):
            annotation = self._find(draft.annotations, operation.annotation_id)
            self._replace(draft.annotations, annotation.id, self._touch(annotation.model_copy(update={"status": operation.status})))
            if annotation.kind is AnnotationKind.DIALOGUE:
                if operation.status is ReviewStatus.REJECTED:
                    draft.utterances = [
                        item.model_copy(update={"status": ReviewStatus.REJECTED}) if item.dialogue_annotation_id == annotation.id else item
                        for item in draft.utterances
                    ]
                elif operation.status is ReviewStatus.PENDING:
                    draft.utterances = [
                        item.model_copy(update={"status": ReviewStatus.PENDING}) if item.dialogue_annotation_id == annotation.id and item.status is ReviewStatus.ACCEPTED else item
                        for item in draft.utterances
                    ]
        elif isinstance(operation, UpsertCharacter):
            if any(item.id == operation.character.id for item in draft.characters):
                self._replace(draft.characters, operation.character.id, operation.character)
            else:
                draft.characters.append(operation.character)
        elif isinstance(operation, SetCharacterStatus):
            character = self._find(draft.characters, operation.character_id)
            self._replace(draft.characters, character.id, character.model_copy(update={"status": operation.status}))
            if operation.status is ReviewStatus.REJECTED:
                draft.utterances = [
                    item.model_copy(update={"status": ReviewStatus.REJECTED}) if item.character_candidate_id == character.id else item
                    for item in draft.utterances
                ]
            elif operation.status is ReviewStatus.PENDING:
                draft.utterances = [
                    item.model_copy(update={"status": ReviewStatus.PENDING}) if item.character_candidate_id == character.id and item.status is ReviewStatus.ACCEPTED else item
                    for item in draft.utterances
                ]
        elif isinstance(operation, MergeCharacters):
            if operation.target_character_id in operation.source_character_ids or len(set(operation.source_character_ids)) != len(operation.source_character_ids):
                raise SemanticValidationError()
            target = self._find(draft.characters, operation.target_character_id)
            sources = [self._find(draft.characters, item) for item in operation.source_character_ids]
            aliases = list(dict.fromkeys(target.aliases + [alias for item in sources for alias in item.aliases]))
            supports = list(dict.fromkeys(target.supporting_annotation_ids + [support for item in sources for support in item.supporting_annotation_ids]))
            self._replace(draft.characters, target.id, target.model_copy(update={"aliases": aliases, "supporting_annotation_ids": supports}))
            source_ids = {item.id for item in sources}
            draft.characters = [item for item in draft.characters if item.id not in source_ids]
            draft.utterances = [
                item.model_copy(update={"character_candidate_id": target.id}) if item.character_candidate_id in source_ids else item
                for item in draft.utterances
            ]
        elif isinstance(operation, SplitAlias):
            source = self._find(draft.characters, operation.character_id)
            if operation.alias not in source.aliases:
                raise SemanticValidationError()
            self._ensure_missing(draft.characters, operation.character.id)
            self._replace(source_list := draft.characters, source.id, source.model_copy(update={"aliases": [item for item in source.aliases if item != operation.alias]}))
            source_list.append(operation.character)
        elif isinstance(operation, CreateUtterance):
            self._ensure_missing(draft.utterances, operation.utterance.id)
            draft.utterances.append(operation.utterance)
        elif isinstance(operation, UpdateUtterance):
            self._ensure_id_matches(operation.utterance_id, operation.utterance.id)
            self._replace(draft.utterances, operation.utterance_id, operation.utterance)
        elif isinstance(operation, DeleteUtterance):
            self._find(draft.utterances, operation.utterance_id)
            draft.utterances = [item for item in draft.utterances if item.id != operation.utterance_id]
        elif isinstance(operation, SetUtteranceStatus):
            utterance = self._find(draft.utterances, operation.utterance_id)
            self._replace(draft.utterances, utterance.id, utterance.model_copy(update={"status": operation.status}))
        elif isinstance(operation, DismissWarning):
            self._find(draft.warnings, operation.warning_id)
            draft.warnings = [item for item in draft.warnings if item.id != operation.warning_id]
        else:  # pragma: no cover - discriminated union makes this unreachable
            raise SemanticValidationError()

    def _validate_draft(self, draft: SemanticAnalysisDraft, source: ScriptRevision) -> None:
        self._unique(draft.annotations)
        self._unique(draft.characters)
        self._unique(draft.utterances)
        annotations = {item.id: item for item in draft.annotations}
        characters = {item.id: item for item in draft.characters}
        try:
            for annotation in draft.annotations:
                validate_source_span(annotation.span, source)
            for character in draft.characters:
                for support_id in character.supporting_annotation_ids:
                    if annotations.get(support_id) is None or annotations[support_id].kind is not AnnotationKind.SPEAKER:
                        raise ValueError("invalid character support")
            for utterance in draft.utterances:
                dialogue = annotations.get(utterance.dialogue_annotation_id)
                if dialogue is None or dialogue.kind is not AnnotationKind.DIALOGUE:
                    raise ValueError("invalid dialogue reference")
                if utterance.speaker_annotation_id is not None:
                    speaker = annotations.get(utterance.speaker_annotation_id)
                    if speaker is None or speaker.kind is not AnnotationKind.SPEAKER:
                        raise ValueError("invalid speaker reference")
                for evidence_id in utterance.emotion_evidence_annotation_ids:
                    evidence = annotations.get(evidence_id)
                    if evidence is None or evidence.kind is not AnnotationKind.EMOTION_EVIDENCE:
                        raise ValueError("invalid emotion reference")
                character = characters.get(utterance.character_candidate_id) if utterance.character_candidate_id else None
                if utterance.character_candidate_id is not None and character is None:
                    raise ValueError("invalid character reference")
                if utterance.status is ReviewStatus.ACCEPTED:
                    if dialogue.status is not ReviewStatus.ACCEPTED or character is None or character.status is not ReviewStatus.ACCEPTED:
                        raise ValueError("accepted utterance dependencies")
                if dialogue.status is ReviewStatus.PENDING and utterance.status is not ReviewStatus.PENDING:
                    raise ValueError("pending dialogue requires pending utterance")
                if dialogue.status is ReviewStatus.REJECTED and utterance.status is not ReviewStatus.REJECTED:
                    raise ValueError("rejected dialogue requires rejected utterance")
                if character is not None and character.status is not ReviewStatus.ACCEPTED and utterance.status is ReviewStatus.ACCEPTED:
                    raise ValueError("accepted utterance character dependency")
        except ValueError as error:
            raise SemanticValidationError() from error

    def _source_revision(self, project_id: str, source_revision_id: str) -> ScriptRevision:
        project = self.project_store.load_project(project_id)
        for revision in project.script_revisions:
            if revision.revision_id == source_revision_id:
                return revision
        raise SemanticNotFoundError("source_revision_not_found")

    def _project_for(self, kind: str, identifier: str) -> str:
        safe_identifier = self._safe_id(identifier)
        metadata = self._load_index().get(kind, {}).get(safe_identifier)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("project_id"), str):
            raise SemanticNotFoundError(f"{kind[:-1]}_not_found")
        return self._safe_project_id(metadata["project_id"])

    def _run_for_draft(self, draft_id: str) -> str:
        metadata = self._load_index()["drafts"].get(self._safe_id(draft_id))
        if not isinstance(metadata, dict) or not isinstance(metadata.get("run_id"), str):
            raise SemanticNotFoundError("draft_not_found")
        return self._safe_id(metadata["run_id"])

    def _run_path(self, project_id: str, run_id: str) -> Path:
        return self._semantic_dir(project_id) / "runs" / f"{self._safe_id(run_id)}.json"

    def _draft_path(self, project_id: str, draft_id: str) -> Path:
        return self._semantic_dir(project_id) / "drafts" / f"{self._safe_id(draft_id)}.json"

    def _revision_path(self, project_id: str, revision_id: str) -> Path:
        return self._semantic_dir(project_id) / "revisions" / f"{self._safe_id(revision_id)}.json"

    def _semantic_dir(self, project_id: str) -> Path:
        return self.project_store.project_semantic_dir(self._safe_project_id(project_id))

    def _index_path(self) -> Path:
        return self.project_store.root / "semantic" / "index.json"

    def _load_index(self) -> dict[str, dict[str, object]]:
        path = self._index_path()
        if not path.exists():
            return {"runs": {}, "drafts": {}, "revisions": {}}
        payload = self._read_json(path)
        if not isinstance(payload, dict):
            raise SemanticValidationError()
        return {kind: dict(payload.get(kind, {})) for kind in ("runs", "drafts", "revisions")}

    def _write_index(self, index: dict[str, dict[str, object]]) -> None:
        self._write_json(self._index_path(), index)

    def _index_lock(self) -> threading.RLock:
        root_key = windows_path_identity(windows_filesystem_path(self.project_store.root).resolve(strict=False))
        with self._index_locks_guard:
            lock = self._index_locks.get(root_key)
            if lock is None:
                lock = threading.RLock()
                self._index_locks[root_key] = lock
            return lock

    @staticmethod
    def _unique(items: list[object]) -> None:
        ids = [getattr(item, "id") for item in items]
        if len(ids) != len(set(ids)):
            raise SemanticValidationError()

    @staticmethod
    def _find(items: list[object], identifier: str):
        for item in items:
            if getattr(item, "id") == identifier:
                return item
        raise SemanticValidationError()

    @staticmethod
    def _ensure_missing(items: list[object], identifier: str) -> None:
        if any(getattr(item, "id") == identifier for item in items):
            raise SemanticValidationError()

    @staticmethod
    def _replace(items: list[object], identifier: str, replacement: object) -> None:
        for index, item in enumerate(items):
            if getattr(item, "id") == identifier:
                items[index] = replacement
                return
        raise SemanticValidationError()

    @staticmethod
    def _ensure_id_matches(expected: str, actual: str) -> None:
        if expected != actual:
            raise SemanticValidationError()

    @staticmethod
    def _touch(model: BaseModel):
        if "updated_at" in type(model).model_fields:
            return model.model_copy(update={"updated_at": _now()})
        return model

    @staticmethod
    def _read_json(path: Path) -> object:
        return json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def _read_model(cls, path: Path, model_type: type[BaseModel]):
        try:
            return model_type.model_validate(cls._read_json(path))
        except FileNotFoundError as error:
            raise SemanticNotFoundError() from error

    @staticmethod
    def _write_model(path: Path, model: BaseModel) -> None:
        SemanticStore._write_json(path, model.model_dump(mode="json"))

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _safe_project_id(project_id: str) -> str:
        return validate_windows_component(project_id, label="project id", max_units=255)

    @staticmethod
    def _safe_id(identifier: str) -> str:
        return validate_windows_component(identifier, label="semantic id", max_units=255)


def _now() -> datetime:
    return datetime.now(timezone.utc)
