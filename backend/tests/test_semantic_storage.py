from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.models import ScriptProject, ScriptRevision
from app.semantic_models import (
    AnalysisRunQuality,
    AnalysisRunStatus,
    AnalysisWarning,
    AnnotationKind,
    AnnotationOrigin,
    CharacterCandidate,
    EmotionOrigin,
    ReviewStatus,
    SemanticAnnotation,
    SemanticUtterance,
    SourceSpan,
)
from app.semantic_source import sha256_source
from app.storage import ProjectStore

try:
    from app.semantic_storage import (
        CreateAnnotation,
        CreateUtterance,
        DeleteAnnotation,
        DismissWarning,
        DraftPatchRequest,
        MergeCharacters,
        ReplaceAnalysisResultError,
        ReplaceAnnotation,
        SemanticConflictError,
        SemanticNotFoundError,
        SemanticStore,
        SemanticValidationError,
        SetAnnotationStatus,
        SetCharacterStatus,
        SetUtteranceStatus,
        SplitAlias,
        UpdateUtterance,
        UpsertCharacter,
    )
except ImportError as error:
    _SEMANTIC_STORAGE_IMPORT_ERROR: ImportError | None = error
else:
    _SEMANTIC_STORAGE_IMPORT_ERROR = None


def _storage() -> None:
    if _SEMANTIC_STORAGE_IMPORT_ERROR is not None:
        pytest.fail(f"semantic storage module must be importable: {_SEMANTIC_STORAGE_IMPORT_ERROR}")


@pytest.fixture
def project_store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path)
    source = "小品：你好。\n[开心]"
    project = ScriptProject(
        title="Semantic demo",
        script_revisions=[
            ScriptRevision(
                revision_id="script-r001",
                source_markdown=source,
                source_sha256=sha256_source(source),
            )
        ],
        active_script_revision_id="script-r001",
    )
    store.save_project("demo", project)
    return store


def _span(text: str, start: int, end: int) -> SourceSpan:
    source = "小品：你好。\n[开心]"
    return SourceSpan(
        source_revision_id="script-r001",
        start_utf16=start,
        end_utf16=end,
        text=text,
        source_sha256=sha256_source(source),
    )


def _annotation(annotation_id: str, kind: AnnotationKind, text: str, start: int, end: int, *, status: ReviewStatus = ReviewStatus.ACCEPTED) -> SemanticAnnotation:
    return SemanticAnnotation(
        id=annotation_id,
        kind=kind,
        span=_span(text, start, end),
        origin=AnnotationOrigin.HUMAN,
        status=status,
    )


def _character(character_id: str = "character-1", *, aliases: list[str] | None = None, status: ReviewStatus = ReviewStatus.ACCEPTED) -> CharacterCandidate:
    return CharacterCandidate(
        id=character_id,
        canonical_name="小品",
        aliases=aliases or ["小品"],
        supporting_annotation_ids=["speaker-1"],
        origin=AnnotationOrigin.HUMAN,
        status=status,
    )


def _utterance(utterance_id: str = "utterance-1", *, status: ReviewStatus = ReviewStatus.ACCEPTED) -> SemanticUtterance:
    return SemanticUtterance(
        id=utterance_id,
        dialogue_annotation_id="dialogue-1",
        speaker_annotation_id="speaker-1",
        character_candidate_id="character-1",
        emotion_evidence_annotation_ids=["emotion-1"],
        normalized_emotion=None,
        emotion_origin=EmotionOrigin.NONE,
        confidence=1,
        status=status,
    )


def _seed_terminal(store: "SemanticStore", *, status: AnalysisRunStatus = AnalysisRunStatus.COMPLETED):
    run, draft = store.create_run_and_draft("demo", "script-r001", trace_id="trace-1")
    run.status = status
    if status is AnalysisRunStatus.COMPLETED:
        run.quality = AnalysisRunQuality.COMPLETE
    store.save_run(run)
    return run, draft


def _seed_draft(store: "SemanticStore"):
    run, draft = _seed_terminal(store)
    result = draft.model_copy(
        update={
            "annotations": [
                _annotation("speaker-1", AnnotationKind.SPEAKER, "小品", 0, 2),
                _annotation("dialogue-1", AnnotationKind.DIALOGUE, "你好。", 3, 6),
                _annotation("emotion-1", AnnotationKind.EMOTION_EVIDENCE, "开心", 8, 10),
            ],
            "characters": [_character()],
            "utterances": [_utterance()],
        }
    )
    store.replace_analysis_result(run.id, result)
    return run, store.load_draft(draft.id)


def _persist_draft_for_test(project_store: ProjectStore, draft) -> None:
    """Build an on-disk legacy/confirmed fixture without exposing a write API."""
    path = project_store.project_semantic_dir(draft.project_id) / "drafts" / f"{draft.id}.json"
    path.write_text(draft.model_dump_json(indent=2), encoding="utf-8")


def test_create_run_and_draft_persists_constrained_sidecars_and_reverse_index(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)

    run, draft = store.create_run_and_draft("demo", "script-r001", trace_id="trace-1")

    semantic_dir = project_store.project_semantic_dir("demo")
    assert (semantic_dir / "runs" / f"{run.id}.json").is_file()
    assert (semantic_dir / "drafts" / f"{draft.id}.json").is_file()
    assert store.load_run(run.id).draft_id == draft.id
    assert store.load_draft(draft.id).version == 1
    index = json.loads((project_store.root / "semantic" / "index.json").read_text(encoding="utf-8"))
    assert index["runs"][run.id]["project_id"] == "demo"
    assert index["drafts"][draft.id] == {"project_id": "demo", "run_id": run.id}


def test_create_run_and_draft_rejects_legacy_oversized_source_without_writes(tmp_path: Path) -> None:
    _storage()
    project_store = ProjectStore(tmp_path)
    source = ScriptRevision(revision_id="script-r001", source_markdown="甲\r\n😀乙")
    project_store.save_project(
        "demo",
        ScriptProject(
            title="Oversized legacy source",
            script_revisions=[source],
            active_script_revision_id=source.revision_id,
        ),
    )
    store = SemanticStore(project_store, max_source_codepoints=4)

    with pytest.raises(SemanticValidationError, match="source_too_large"):
        store.create_run_and_draft("demo", source.revision_id, trace_id="trace-oversized")

    assert not project_store.project_semantic_dir("demo").exists()
    assert not (project_store.root / "semantic" / "index.json").exists()


def test_create_run_and_draft_accepts_source_at_exact_codepoint_limit(tmp_path: Path) -> None:
    _storage()
    project_store = ProjectStore(tmp_path)
    source = ScriptRevision(revision_id="script-r001", source_markdown="甲\r\n😀")
    project_store.save_project(
        "demo",
        ScriptProject(
            title="Exact-limit source",
            script_revisions=[source],
            active_script_revision_id=source.revision_id,
        ),
    )
    store = SemanticStore(project_store, max_source_codepoints=4)

    run, draft = store.create_run_and_draft("demo", source.revision_id, trace_id="trace-exact")

    assert store.load_run(run.id).source_revision_id == source.revision_id
    assert store.load_draft(draft.id).source_revision_id == source.revision_id


def test_cross_project_run_creation_keeps_both_reverse_index_mappings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _storage()
    project_store = ProjectStore(tmp_path)
    for project_id in ("one", "two"):
        source = ScriptRevision(revision_id="script-r001", source_markdown="台词", source_sha256=sha256_source("台词"))
        project_store.save_project(project_id, ScriptProject(title=project_id, script_revisions=[source], active_script_revision_id=source.revision_id))
    first = SemanticStore(project_store)
    second = SemanticStore(ProjectStore(tmp_path))
    barrier = threading.Barrier(2)
    original_first = first._load_index
    original_second = second._load_index

    def synchronize_index_load(load):
        def synchronized():
            index = load()
            try:
                barrier.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
            return index
        return synchronized

    monkeypatch.setattr(first, "_load_index", synchronize_index_load(original_first))
    monkeypatch.setattr(second, "_load_index", synchronize_index_load(original_second))
    created: list[tuple[str, object, object]] = []

    def create(store: SemanticStore, project_id: str) -> None:
        run, draft = store.create_run_and_draft(project_id, "script-r001", trace_id=project_id)
        created.append((project_id, run, draft))

    threads = [threading.Thread(target=create, args=(first, "one")), threading.Thread(target=create, args=(second, "two"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()

    index = json.loads((tmp_path / "semantic" / "index.json").read_text(encoding="utf-8"))
    assert len(created) == 2
    for project_id, run, draft in created:
        assert index["runs"][run.id]["project_id"] == project_id
        assert index["drafts"][draft.id] == {"project_id": project_id, "run_id": run.id}


@pytest.mark.parametrize("project_id", ["../escape", "..", "CON", "demo/../../escape"])
def test_hostile_project_ids_cannot_escape_sidecar_root(tmp_path: Path, project_id: str) -> None:
    _storage()
    store = SemanticStore(ProjectStore(tmp_path))

    with pytest.raises(ValueError):
        store.create_run_and_draft(project_id, "script-r001", trace_id="trace")

    assert not (tmp_path.parent / "escape").exists()


def test_sidecar_entity_ids_are_safe_components_and_unknown_ids_do_not_escape(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    run, _draft = store.create_run_and_draft("demo", "script-r001", trace_id="trace")

    with pytest.raises((SemanticNotFoundError, ValueError)):
        store.load_run("../" + run.id)
    with pytest.raises((SemanticNotFoundError, ValueError)):
        store.load_draft("..\\draft.json")


def test_patch_is_atomic_for_stale_version_and_invalid_batches(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_draft(store)
    original_path = project_store.project_semantic_dir("demo") / "drafts" / f"{draft.id}.json"
    original_bytes = original_path.read_bytes()

    changed = store.patch_draft(
        draft.id,
        1,
        [SetAnnotationStatus(op="set_annotation_status", annotation_id="dialogue-1", status=ReviewStatus.PENDING)],
    )
    assert changed.version == 2
    version_two_bytes = original_path.read_bytes()
    with pytest.raises(SemanticConflictError, match="draft_version_conflict"):
        store.patch_draft(draft.id, 1, [])
    assert original_path.read_bytes() == version_two_bytes
    with pytest.raises(SemanticValidationError, match="semantic_draft_invalid"):
        store.patch_draft(
            draft.id,
            2,
            [UpdateUtterance(op="update_utterance", utterance_id="utterance-1", utterance=_utterance().model_copy(update={"dialogue_annotation_id": "missing"}))],
        )
    assert store.load_draft(draft.id).version == 2
    assert original_bytes != version_two_bytes
    assert original_path.read_bytes() == version_two_bytes


@pytest.mark.parametrize("status", [AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING])
def test_queued_and_running_drafts_are_read_only(project_store: ProjectStore, status: AnalysisRunStatus) -> None:
    _storage()
    store = SemanticStore(project_store)
    run, draft = store.create_run_and_draft("demo", "script-r001", trace_id="trace")
    run.status = status
    store.save_run(run)

    with pytest.raises(SemanticConflictError, match="semantic_run_not_terminal"):
        store.patch_draft(draft.id, 1, [])


@pytest.mark.parametrize("status", [AnalysisRunStatus.COMPLETED, AnalysisRunStatus.FAILED, AnalysisRunStatus.INTERRUPTED])
def test_all_terminal_drafts_are_editable_and_empty_drafts_are_valid(project_store: ProjectStore, status: AnalysisRunStatus) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_terminal(store, status=status)

    patched = store.patch_draft(draft.id, 1, [])

    assert patched.version == 2


def test_confirmed_draft_rejects_patch(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_terminal(store)
    confirmed = draft.model_copy(update={"confirmed_revision_id": "semantic-1"})
    _persist_draft_for_test(project_store, confirmed)

    with pytest.raises(SemanticConflictError, match="draft_confirmed"):
        store.patch_draft(draft.id, 1, [])


def test_annotation_delete_cascades_dialogue_speaker_and_emotion_references(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_draft(store)

    dialogue_deleted = store.patch_draft(draft.id, 1, [DeleteAnnotation(op="delete_annotation", annotation_id="dialogue-1")])
    assert dialogue_deleted.utterances == []
    draft = store.load_draft(draft.id)
    restored = _seed_draft(store)[1]
    speaker_deleted = store.patch_draft(restored.id, restored.version, [DeleteAnnotation(op="delete_annotation", annotation_id="speaker-1")])
    assert speaker_deleted.utterances[0].speaker_annotation_id is None
    assert speaker_deleted.characters[0].supporting_annotation_ids == []
    emotion_deleted = store.patch_draft(speaker_deleted.id, speaker_deleted.version, [DeleteAnnotation(op="delete_annotation", annotation_id="emotion-1")])
    assert emotion_deleted.utterances[0].emotion_evidence_annotation_ids == []


def test_status_cascades_preserve_draft_invariants(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_draft(store)

    rejected = store.patch_draft(draft.id, 1, [SetAnnotationStatus(op="set_annotation_status", annotation_id="dialogue-1", status=ReviewStatus.REJECTED)])
    assert rejected.utterances[0].status is ReviewStatus.REJECTED
    reset = _seed_draft(store)[1]
    pending = store.patch_draft(reset.id, reset.version, [SetAnnotationStatus(op="set_annotation_status", annotation_id="dialogue-1", status=ReviewStatus.PENDING)])
    assert pending.utterances[0].status is ReviewStatus.PENDING
    reset = _seed_draft(store)[1]
    character_pending = store.patch_draft(reset.id, reset.version, [SetCharacterStatus(op="set_character_status", character_id="character-1", status=ReviewStatus.PENDING)])
    assert character_pending.utterances[0].status is ReviewStatus.PENDING
    character_rejected = store.patch_draft(character_pending.id, character_pending.version, [SetCharacterStatus(op="set_character_status", character_id="character-1", status=ReviewStatus.REJECTED)])
    assert character_rejected.utterances[0].status is ReviewStatus.REJECTED


def test_create_replace_upsert_update_merge_split_and_dismiss_commands(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_draft(store)
    new_annotation = _annotation("dialogue-2", AnnotationKind.DIALOGUE, "你好。", 3, 6, status=ReviewStatus.PENDING)
    new_character = _character("character-2", aliases=["小品", "阿品"], status=ReviewStatus.PENDING)
    new_utterance = _utterance("utterance-2", status=ReviewStatus.PENDING).model_copy(update={"dialogue_annotation_id": "dialogue-2", "character_candidate_id": "character-2"})
    draft = draft.model_copy(update={"warnings": [AnalysisWarning(id="warning-1", code="warning", message="review")]})
    _persist_draft_for_test(project_store, draft)

    patched = store.patch_draft(
        draft.id,
        draft.version,
        [
            CreateAnnotation(op="create_annotation", annotation=new_annotation),
            ReplaceAnnotation(op="replace_annotation", annotation_id="dialogue-2", annotation=new_annotation.model_copy(update={"status": ReviewStatus.ACCEPTED})),
            UpsertCharacter(op="upsert_character", character=new_character),
            CreateUtterance(op="create_utterance", utterance=new_utterance),
            UpdateUtterance(op="update_utterance", utterance_id="utterance-2", utterance=new_utterance.model_copy(update={"confidence": 0.5})),
            DismissWarning(op="dismiss_warning", warning_id="warning-1"),
        ],
    )
    assert {item.id for item in patched.annotations} >= {"dialogue-1", "dialogue-2"}
    assert next(item for item in patched.utterances if item.id == "utterance-2").confidence == 0.5
    assert patched.warnings == []
    merged = store.patch_draft(
        patched.id,
        patched.version,
        [MergeCharacters(op="merge_characters", target_character_id="character-1", source_character_ids=["character-2"])],
    )
    assert [item.id for item in merged.characters] == ["character-1"]
    assert next(item for item in merged.utterances if item.id == "utterance-2").character_candidate_id == "character-1"
    split_character = _character("character-3", aliases=["阿品"], status=ReviewStatus.PENDING)
    split = store.patch_draft(
        merged.id,
        merged.version,
        [SplitAlias(op="split_alias", character_id="character-1", alias="阿品", character=split_character)],
    )
    assert "阿品" not in split.characters[0].aliases
    assert any(item.id == "character-3" for item in split.characters)


def test_draft_patch_request_uses_discriminated_operations() -> None:
    _storage()
    request = DraftPatchRequest.model_validate(
        {"expected_version": 7, "operations": [{"op": "dismiss_warning", "warning_id": "warning-1"}]}
    )

    assert request.operations[0].op == "dismiss_warning"
    with pytest.raises(ValueError):
        DraftPatchRequest.model_validate({"expected_version": 1, "operations": [{"op": "unknown"}]})


def test_full_reference_validation_rejects_wrong_kinds_and_duplicates(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_draft(store)
    duplicate = _annotation("dialogue-1", AnnotationKind.DIALOGUE, "你好。", 3, 6)

    with pytest.raises(SemanticValidationError, match="semantic_draft_invalid"):
        store.patch_draft(draft.id, 1, [CreateAnnotation(op="create_annotation", annotation=duplicate)])
    with pytest.raises(SemanticValidationError, match="semantic_draft_invalid"):
        store.patch_draft(
            draft.id,
            1,
            [UpdateUtterance(op="update_utterance", utterance_id="utterance-1", utterance=_utterance().model_copy(update={"speaker_annotation_id": "dialogue-1"}))],
        )


@pytest.mark.parametrize("status", [ReviewStatus.PENDING, ReviewStatus.ACCEPTED, ReviewStatus.REJECTED])
def test_all_utterance_statuses_reject_nonempty_dangling_character_references(project_store: ProjectStore, status: ReviewStatus) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_draft(store)
    dangling = _utterance(status=status).model_copy(update={"character_candidate_id": "missing-character"})

    with pytest.raises(SemanticValidationError, match="semantic_draft_invalid"):
        store.patch_draft(draft.id, draft.version, [UpdateUtterance(op="update_utterance", utterance_id="utterance-1", utterance=dangling)])


def test_worker_replacement_preserves_identity_and_rejects_confirmed_or_user_edited_drafts(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    run, draft = _seed_terminal(store)
    replacement = draft.model_copy(update={"annotations": [_annotation("dialogue-1", AnnotationKind.DIALOGUE, "你好。", 3, 6)]})

    saved = store.replace_analysis_result(run.id, replacement)
    assert saved.id == draft.id
    assert saved.project_id == "demo"
    assert saved.source_revision_id == "script-r001"
    edited = store.patch_draft(draft.id, saved.version, [])
    with pytest.raises(ReplaceAnalysisResultError, match="semantic_draft_invalid"):
        store.replace_analysis_result(run.id, replacement)
    _persist_draft_for_test(project_store, edited.model_copy(update={"confirmed_revision_id": "semantic-1"}))
    with pytest.raises(ReplaceAnalysisResultError, match="draft_confirmed"):
        store.replace_analysis_result(run.id, replacement)


def test_interrupt_incomplete_runs_is_idempotent_and_keeps_terminal_records(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    queued, _ = store.create_run_and_draft("demo", "script-r001", trace_id="queued")
    running, _ = store.create_run_and_draft("demo", "script-r001", trace_id="running")
    running.status = AnalysisRunStatus.RUNNING
    store.save_run(running)
    completed, _ = _seed_terminal(store, status=AnalysisRunStatus.COMPLETED)
    failed, _ = _seed_terminal(store, status=AnalysisRunStatus.FAILED)

    assert store.interrupt_incomplete_runs() == 2
    assert store.load_run(queued.id).status is AnalysisRunStatus.INTERRUPTED
    assert store.load_run(running.id).status is AnalysisRunStatus.INTERRUPTED
    assert store.load_run(completed.id).status is AnalysisRunStatus.COMPLETED
    assert store.load_run(failed.id).status is AnalysisRunStatus.FAILED
    assert store.interrupt_incomplete_runs() == 0


def test_project_store_update_project_materializes_mutation_once_under_project_lock(project_store: ProjectStore) -> None:
    _storage()
    seen: list[str] = []

    saved, result = project_store.update_project(
        "demo",
        lambda project: (seen.append(project.title), setattr(project, "title", "Updated"), "ok")[-1],
    )

    assert result == "ok"
    assert seen == ["Semantic demo"]
    assert saved.title == "Updated"
    assert project_store.load_project("demo").title == "Updated"
    assert project_store.project_semantic_dir("demo").parent == project_store.project_script_dir("demo")


def test_project_lock_serializes_same_project_mutations(project_store: ProjectStore) -> None:
    _storage()
    entered = threading.Event()
    release = threading.Event()
    acquired: list[str] = []

    def first() -> None:
        with project_store.project_lock("demo"):
            entered.set()
            assert release.wait(2)

    def second() -> None:
        assert entered.wait(2)
        with project_store.project_lock("demo"):
            acquired.append("second")

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert entered.wait(2)
    assert acquired == []
    release.set()
    first_thread.join(2)
    second_thread.join(2)
    assert acquired == ["second"]


def test_confirmation_uses_dedicated_marker_write_without_incrementing_edit_version(project_store: ProjectStore) -> None:
    _storage()
    store = SemanticStore(project_store)
    _run, draft = _seed_draft(store)

    result = store.confirm_draft(draft.id, draft.version, "storage-confirm-key")

    confirmed = store.load_draft(draft.id)
    assert confirmed.version == draft.version
    assert confirmed.confirmed_revision_id == result.semantic_revision.id
    assert confirmed.confirmed_parse_revision_id == result.parse_revision.revision_id
    assert confirmed.confirm_idempotency_key == "storage-confirm-key"
