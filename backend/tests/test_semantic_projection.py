from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.models import ProjectCharacter, ScriptProject, ScriptRevision
from app.semantic_models import (
    AnalysisRunQuality,
    AnalysisRunStatus,
    AnalysisWarning,
    AnnotationKind,
    AnnotationOrigin,
    CharacterCandidate,
    EmotionOrigin,
    NormalizedEmotion,
    ReviewStatus,
    SemanticAnalysisDraft,
    SemanticAnnotation,
    SemanticUtterance,
    SourceSpan,
    UnresolvedCandidate,
)
from app.semantic_source import sha256_source
from app.semantic_storage import SemanticConflictError, SemanticStore, SemanticValidationError
from app.storage import ProjectStore

try:
    import app.semantic_projection as semantic_projection
    from app.semantic_projection import SemanticProjectionError, project_confirmed_draft, resolve_project_character
except ImportError as error:
    _PROJECTION_IMPORT_ERROR: ImportError | None = error
else:
    _PROJECTION_IMPORT_ERROR = None


SOURCE = "绑定者：后一句。\n新角色：第一句！\n复用者：默认语言。\n待定：保留。\n拒绝：保留。\n未分配：保留。"


def _projection() -> None:
    if _PROJECTION_IMPORT_ERROR is not None:
        pytest.fail(f"semantic projection module must be importable: {_PROJECTION_IMPORT_ERROR}")


def _span(text: str, source: str = SOURCE) -> SourceSpan:
    start = source.index(text)
    return SourceSpan(
        source_revision_id="script-r001",
        start_utf16=len(source[:start].encode("utf-16-le")) // 2,
        end_utf16=len(source[: start + len(text)].encode("utf-16-le")) // 2,
        text=text,
        source_sha256=sha256_source(source),
    )


def _annotation(annotation_id: str, text: str, status: ReviewStatus = ReviewStatus.ACCEPTED) -> SemanticAnnotation:
    return SemanticAnnotation(id=annotation_id, kind=AnnotationKind.DIALOGUE, span=_span(text), origin=AnnotationOrigin.HUMAN, status=status)


def _character(character_id: str, name: str, *, project_character_id: str | None = None, aliases: list[str] | None = None, status: ReviewStatus = ReviewStatus.ACCEPTED) -> CharacterCandidate:
    return CharacterCandidate(id=character_id, canonical_name=name, aliases=aliases or [], project_character_id=project_character_id, origin=AnnotationOrigin.HUMAN, status=status)


def _utterance(utterance_id: str, dialogue_id: str, character_id: str | None, *, status: ReviewStatus = ReviewStatus.ACCEPTED, language: str = "zh-CN", emotion: NormalizedEmotion | None = None) -> SemanticUtterance:
    return SemanticUtterance(
        id=utterance_id, dialogue_annotation_id=dialogue_id, character_candidate_id=character_id,
        normalized_emotion=emotion, emotion_intensity=0.75 if emotion is not None else None,
        emotion_origin=EmotionOrigin.INFERRED if emotion is not None else EmotionOrigin.NONE,
        language=language, confidence=1, status=status,
    )


def _draft(*, draft_id: str = "draft-confirm") -> SemanticAnalysisDraft:
    return SemanticAnalysisDraft(
        id=draft_id, project_id="demo", source_revision_id="script-r001",
        annotations=[
            _annotation("dialogue-later", "后一句。"),
            _annotation("dialogue-first", "第一句！"),
            _annotation("dialogue-default", "默认语言。"),
            _annotation("dialogue-pending", "保留。", ReviewStatus.PENDING),
            _annotation("dialogue-rejected", "拒绝：保留。", ReviewStatus.REJECTED),
            _annotation("dialogue-unassigned", "未分配：保留。", ReviewStatus.PENDING),
        ],
        characters=[
            _character("candidate-bound", "绑定候选", project_character_id="bound"),
            _character("candidate-new", "新角色", aliases=["新新", "小新"]),
            _character("candidate-reuse", "复用者"),
            _character("candidate-pending", "待定", status=ReviewStatus.PENDING),
            _character("candidate-rejected", "拒绝", status=ReviewStatus.REJECTED),
        ],
        utterances=[
            _utterance("utterance-later", "dialogue-later", "candidate-bound", emotion=NormalizedEmotion.SERIOUS),
            _utterance("utterance-default", "dialogue-default", "candidate-reuse", language=""),
            _utterance("utterance-first", "dialogue-first", "candidate-new", emotion=NormalizedEmotion.HAPPY),
            _utterance("utterance-pending", "dialogue-pending", "candidate-pending", status=ReviewStatus.PENDING),
            _utterance("utterance-rejected", "dialogue-rejected", "candidate-rejected", status=ReviewStatus.REJECTED),
            _utterance("utterance-unassigned", "dialogue-unassigned", None, status=ReviewStatus.PENDING),
        ],
        unresolved_candidates=[UnresolvedCandidate(id="unresolved-1", candidate_type="dialogue", message="kept")],
        warnings=[AnalysisWarning(id="warning-1", code="partial", message="kept warning")],
        provider="openai", model="semantic-model", prompt_version="prompt-v3", contract_version="contract-v2",
    )


def _project() -> ScriptProject:
    return ScriptProject(
        title="Projection demo", default_language="zh",
        script_revisions=[ScriptRevision(revision_id="script-r001", source_markdown=SOURCE, source_sha256=sha256_source(SOURCE))],
        active_script_revision_id="script-r001",
        project_characters=[ProjectCharacter(project_character_id="bound", name="显式绑定角色"), ProjectCharacter(project_character_id="reuse", name="复用者")],
    )


@pytest.fixture
def confirmable_store(tmp_path: Path) -> tuple[SemanticStore, str]:
    project_store = ProjectStore(tmp_path)
    project_store.save_project("demo", _project())
    store = SemanticStore(project_store)
    run, draft = store.create_run_and_draft("demo", "script-r001", trace_id="trace-confirm")
    run.status = AnalysisRunStatus.COMPLETED
    run.quality = AnalysisRunQuality.PARTIAL
    store.save_run(run)
    store.replace_analysis_result(run.id, _draft(draft_id=draft.id))
    return store, draft.id


def test_projection_keeps_full_snapshot_but_materializes_only_accepted_assigned_source_order() -> None:
    _projection()
    project = _project()
    parse_revision = project_confirmed_draft(project, _draft(), "revision-123")

    assert [line.id for line in parse_revision.lines] == ["utterance-later", "utterance-first", "utterance-default"]
    assert [line.text for line in parse_revision.lines] == ["后一句。", "第一句！", "默认语言。"]
    assert [line.note for line in parse_revision.lines] == ["serious", "happy", ""]
    assert [line.language for line in parse_revision.lines] == ["zh-CN", "zh-CN", "zh"]
    assert [line.line_uid for line in parse_revision.lines] == ["semantic-revision-123:utterance-later", "semantic-revision-123:utterance-first", "semantic-revision-123:utterance-default"]
    assert all(line.semantic_revision_id == "revision-123" and line.utterance_id == line.id for line in parse_revision.lines)
    assert parse_revision.provider == "semantic-confirmed"
    assert parse_revision.script_revision_id == "script-r001"
    assert parse_revision.parent_parse_revision_id == "parse-r001"
    assert project.active_parse_revision_id == parse_revision.revision_id
    assert project.lines == parse_revision.lines
    assert {character.name for character in project.project_characters} == {"显式绑定角色", "复用者", "新角色"}
    assert not {"新新", "小新"}.intersection(character.name for character in project.project_characters)


def test_character_resolution_prefers_explicit_then_unique_name_and_rejects_ambiguous_or_missing() -> None:
    _projection()
    project = _project()
    explicit = resolve_project_character(project, _character("explicit", "ignored", project_character_id="bound"))
    reused = resolve_project_character(project, _character("reuse-candidate", "复用者", aliases=["extra alias"]))
    created = resolve_project_character(project, _character("created", "只创建一次", aliases=["别名一", "别名二"]))
    created_again = resolve_project_character(project, _character("created-again", "只创建一次"))

    assert explicit.project_character_id == "bound"
    assert reused.project_character_id == "reuse"
    assert created_again.project_character_id == created.project_character_id
    assert [item.name for item in project.project_characters].count("只创建一次") == 1
    assert not {"别名一", "别名二"}.intersection(item.name for item in project.project_characters)
    with pytest.raises(SemanticProjectionError, match="missing_project_character"):
        resolve_project_character(project, _character("missing", "missing", project_character_id="does-not-exist"))
    project.project_characters.extend([ProjectCharacter(project_character_id="duplicate-one", name="重复"), ProjectCharacter(project_character_id="duplicate-two", name="重复")])
    with pytest.raises(SemanticProjectionError, match="ambiguous_project_character"):
        resolve_project_character(project, _character("ambiguous", "重复"))


def test_character_resolution_detects_deterministic_id_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    _projection()
    project = _project()
    project.project_characters.append(ProjectCharacter(project_character_id="forced-collision", name="别的名字"))
    monkeypatch.setattr(semantic_projection, "_deterministic_project_character_id", lambda _candidate: "forced-collision")
    with pytest.raises(SemanticProjectionError, match="project_character_id_collision"):
        resolve_project_character(project, _character("candidate", "新名字"))


def test_confirmation_persists_immutable_metadata_and_projection(confirmable_store: tuple[SemanticStore, str]) -> None:
    _projection()
    store, draft_id = confirmable_store
    result = store.confirm_draft(draft_id, 1, "confirm-key-1")

    assert result.semantic_revision.id.startswith("revision-")
    assert (len(result.semantic_revision.annotations), len(result.semantic_revision.characters), len(result.semantic_revision.utterances)) == (6, 5, 6)
    assert len(result.semantic_revision.unresolved_candidates) == len(result.semantic_revision.warnings) == 1
    assert (result.semantic_revision.provider, result.semantic_revision.model) == ("openai", "semantic-model")
    assert (result.semantic_revision.prompt_version, result.semantic_revision.contract_version) == ("prompt-v3", "contract-v2")
    pending_annotation = next(item for item in result.semantic_revision.annotations if item.id == "dialogue-pending")
    rejected_annotation = next(item for item in result.semantic_revision.annotations if item.id == "dialogue-rejected")
    pending_character = next(item for item in result.semantic_revision.characters if item.id == "candidate-pending")
    rejected_character = next(item for item in result.semantic_revision.characters if item.id == "candidate-rejected")
    pending_utterance = next(item for item in result.semantic_revision.utterances if item.id == "utterance-pending")
    rejected_utterance = next(item for item in result.semantic_revision.utterances if item.id == "utterance-rejected")
    assert (pending_annotation.status, pending_annotation.span.text) == (ReviewStatus.PENDING, "保留。")
    assert (rejected_annotation.status, rejected_annotation.span.text) == (ReviewStatus.REJECTED, "拒绝：保留。")
    assert (pending_character.status, pending_character.canonical_name) == (ReviewStatus.PENDING, "待定")
    assert (rejected_character.status, rejected_character.canonical_name) == (ReviewStatus.REJECTED, "拒绝")
    assert (pending_utterance.status, pending_utterance.dialogue_annotation_id) == (ReviewStatus.PENDING, "dialogue-pending")
    assert (rejected_utterance.status, rejected_utterance.dialogue_annotation_id) == (ReviewStatus.REJECTED, "dialogue-rejected")
    assert result.parse_revision.revision_id == f"semantic-{result.semantic_revision.id}"
    assert result.project.active_parse_revision_id == result.parse_revision.revision_id
    assert result.project.lines == result.parse_revision.lines
    stored = store.load_draft(draft_id)
    assert stored.version == 1
    assert stored.confirmed_revision_id == result.semantic_revision.id
    assert stored.confirmed_parse_revision_id == result.parse_revision.revision_id
    assert stored.confirm_idempotency_key == "confirm-key-1"


def test_same_key_retry_ignores_stale_version_and_never_duplicates_artifacts(confirmable_store: tuple[SemanticStore, str]) -> None:
    _projection()
    store, draft_id = confirmable_store
    first = store.confirm_draft(draft_id, 1, "confirm-key-1")
    second = store.confirm_draft(draft_id, 999, "confirm-key-1")

    assert second.semantic_revision == first.semantic_revision
    assert second.parse_revision == first.parse_revision
    assert len(second.project.parse_revisions) == len(first.project.parse_revisions)
    assert len(second.project.project_characters) == len(first.project.project_characters)
    assert len(second.project.lines) == len(first.project.lines)


def test_confirmed_retry_rejects_corrupt_revision_marker_without_creating_artifacts(confirmable_store: tuple[SemanticStore, str]) -> None:
    _projection()
    store, draft_id = confirmable_store
    store.confirm_draft(draft_id, 1, "confirm-key-1")
    draft_path = store.project_store.project_semantic_dir("demo") / "drafts" / f"{draft_id}.json"
    confirmed = store.load_draft(draft_id)
    confirmed.confirmed_revision_id = "revision-corrupt-marker"
    draft_path.write_text(confirmed.model_dump_json(indent=2), encoding="utf-8")
    revisions_dir = store.project_store.project_semantic_dir("demo") / "revisions"
    artifacts_before = {path.name: path.read_bytes() for path in revisions_dir.glob("*.json")}
    index_before = (store.project_store.root / "semantic" / "index.json").read_bytes()
    project_before = copy.deepcopy(store.project_store.load_project("demo"))

    with pytest.raises(SemanticValidationError, match="confirmed_artifact_mismatch"):
        store.confirm_draft(draft_id, 999, "confirm-key-1")

    assert {path.name: path.read_bytes() for path in revisions_dir.glob("*.json")} == artifacts_before
    assert (store.project_store.root / "semantic" / "index.json").read_bytes() == index_before
    assert store.project_store.load_project("demo") == project_before


def test_confirmed_retry_rejects_corrupt_parse_marker_before_recreating_missing_revision(confirmable_store: tuple[SemanticStore, str]) -> None:
    _projection()
    store, draft_id = confirmable_store
    result = store.confirm_draft(draft_id, 1, "confirm-key-1")
    draft_path = store.project_store.project_semantic_dir("demo") / "drafts" / f"{draft_id}.json"
    confirmed = store.load_draft(draft_id)
    confirmed.confirmed_parse_revision_id = "semantic-corrupt-marker"
    draft_path.write_text(confirmed.model_dump_json(indent=2), encoding="utf-8")
    revision_path = store.project_store.project_semantic_dir("demo") / "revisions" / f"{result.semantic_revision.id}.json"
    revision_path.unlink()
    index_path = store.project_store.root / "semantic" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["revisions"][result.semantic_revision.id]
    index_path.write_text(json.dumps(index), encoding="utf-8")
    index_before = index_path.read_bytes()
    project_before = copy.deepcopy(store.project_store.load_project("demo"))

    with pytest.raises(SemanticValidationError, match="confirmed_artifact_mismatch"):
        store.confirm_draft(draft_id, 999, "confirm-key-1")

    assert not revision_path.exists()
    assert index_path.read_bytes() == index_before
    assert store.project_store.load_project("demo") == project_before


def test_existing_parse_revision_rejects_wrong_parent_on_first_projection_collision() -> None:
    _projection()
    project = _project()
    revision = project_confirmed_draft(project, _draft(), "revision-parent")
    project.active_parse_revision_id = "parse-r001"
    project.lines = copy.deepcopy(project.parse_revisions[0].lines)
    revision.parent_parse_revision_id = None

    with pytest.raises(SemanticProjectionError, match="parse_revision_collision"):
        project_confirmed_draft(project, _draft(), "revision-parent")


def test_existing_active_parse_revision_rejects_self_parent_during_marker_window_recovery() -> None:
    _projection()
    project = _project()
    revision = project_confirmed_draft(project, _draft(), "revision-self-parent")
    revision.parent_parse_revision_id = revision.revision_id

    with pytest.raises(SemanticProjectionError, match="parse_revision_collision"):
        project_confirmed_draft(project, _draft(), "revision-self-parent")


@pytest.mark.parametrize("snapshot_damage", ["extra", "missing", "mutated"])
def test_existing_parse_revision_requires_complete_character_snapshot(snapshot_damage: str) -> None:
    _projection()
    project = _project()
    project.project_characters.append(ProjectCharacter(project_character_id="unused", name="未使用角色"))
    revision = project_confirmed_draft(project, _draft(), "revision-snapshot")
    project.active_parse_revision_id = "parse-r001"
    project.lines = copy.deepcopy(project.parse_revisions[0].lines)
    if snapshot_damage == "extra":
        revision.project_characters.append(ProjectCharacter(project_character_id="extra", name="额外角色"))
    elif snapshot_damage == "missing":
        revision.project_characters = [item for item in revision.project_characters if item.project_character_id != "unused"]
    else:
        unused = next(item for item in revision.project_characters if item.project_character_id == "unused")
        unused.name = "被篡改角色"

    with pytest.raises(SemanticProjectionError, match="parse_revision_collision"):
        project_confirmed_draft(project, _draft(), "revision-snapshot")


def test_confirmation_conflicts_for_different_key_or_initial_stale_version(confirmable_store: tuple[SemanticStore, str]) -> None:
    _projection()
    store, draft_id = confirmable_store
    with pytest.raises(SemanticConflictError, match="draft_version_conflict"):
        store.confirm_draft(draft_id, 2, "confirm-key-1")
    store.confirm_draft(draft_id, 1, "confirm-key-1")
    with pytest.raises(SemanticConflictError, match="draft_confirmed"):
        store.confirm_draft(draft_id, 1, "confirm-key-2")


@pytest.mark.parametrize("status", [AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING])
def test_confirmation_rejects_nonterminal_run(tmp_path: Path, status: AnalysisRunStatus) -> None:
    _projection()
    project_store = ProjectStore(tmp_path)
    project_store.save_project("demo", _project())
    store = SemanticStore(project_store)
    run, draft = store.create_run_and_draft("demo", "script-r001", trace_id="trace")
    if status is AnalysisRunStatus.RUNNING:
        run.status = status
        store.save_run(run)
    with pytest.raises(SemanticConflictError, match="semantic_run_not_terminal"):
        store.confirm_draft(draft.id, draft.version, "confirm-key")


@pytest.mark.parametrize("key", ["", "   ", "x" * 513])
def test_confirmation_rejects_invalid_idempotency_key(confirmable_store: tuple[SemanticStore, str], key: str) -> None:
    _projection()
    store, draft_id = confirmable_store
    with pytest.raises(SemanticValidationError, match="invalid_idempotency_key"):
        store.confirm_draft(draft_id, 1, key)


def test_retry_recovers_revision_written_before_project_save(confirmable_store: tuple[SemanticStore, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _projection()
    store, draft_id = confirmable_store
    original_save = store.project_store.save_project
    monkeypatch.setattr(store.project_store, "save_project", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("project crash")))
    with pytest.raises(RuntimeError, match="project crash"):
        store.confirm_draft(draft_id, 1, "recover-revision")

    revision_files = list((store.project_store.project_semantic_dir("demo") / "revisions").glob("*.json"))
    assert len(revision_files) == 1
    assert store.load_draft(draft_id).confirmed_revision_id is None
    assert len(store.project_store.load_project("demo").parse_revisions) == 1
    revision_bytes = revision_files[0].read_bytes()
    monkeypatch.setattr(store.project_store, "save_project", original_save)
    recovered = store.confirm_draft(draft_id, 1, "recover-revision")
    assert revision_files[0].read_bytes() == revision_bytes
    assert len(recovered.project.parse_revisions) == 2


def test_retry_rejects_revision_index_collision_with_another_draft(confirmable_store: tuple[SemanticStore, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _projection()
    store, draft_id = confirmable_store
    original_save = store.project_store.save_project
    monkeypatch.setattr(store.project_store, "save_project", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("project crash")))
    with pytest.raises(RuntimeError, match="project crash"):
        store.confirm_draft(draft_id, 1, "collision-key")

    index_path = store.project_store.root / "semantic" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    revision_id = next(iter(index["revisions"]))
    index["revisions"][revision_id]["draft_id"] = "another-draft"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr(store.project_store, "save_project", original_save)

    with pytest.raises(SemanticValidationError, match="semantic_revision_collision"):
        store.confirm_draft(draft_id, 1, "collision-key")


def test_retry_recovers_project_materialized_before_draft_marker(confirmable_store: tuple[SemanticStore, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _projection()
    store, draft_id = confirmable_store
    original_write_model = store._write_model
    failed_once = False

    def fail_confirmation_marker(path: Path, model: object) -> None:
        nonlocal failed_once
        if path.name == f"{draft_id}.json" and getattr(model, "confirmed_revision_id", None) and not failed_once:
            failed_once = True
            raise RuntimeError("marker crash")
        original_write_model(path, model)

    monkeypatch.setattr(store, "_write_model", fail_confirmation_marker)
    with pytest.raises(RuntimeError, match="marker crash"):
        store.confirm_draft(draft_id, 1, "recover-project")
    materialized = store.project_store.load_project("demo")
    assert len(materialized.parse_revisions) == 2
    assert store.load_draft(draft_id).confirmed_revision_id is None
    recovered = store.confirm_draft(draft_id, 1, "recover-project")
    assert len(recovered.project.parse_revisions) == 2
    assert len({line.line_uid for line in recovered.project.lines}) == len(recovered.project.lines)
    assert store.load_draft(draft_id).confirmed_parse_revision_id == recovered.parse_revision.revision_id


def test_invalid_source_span_causes_no_project_or_confirmation_mutation(confirmable_store: tuple[SemanticStore, str]) -> None:
    _projection()
    store, draft_id = confirmable_store
    draft_path = store.project_store.project_semantic_dir("demo") / "drafts" / f"{draft_id}.json"
    invalid = store.load_draft(draft_id)
    invalid.annotations[0].span.text = "错误文本"
    draft_path.write_text(invalid.model_dump_json(indent=2), encoding="utf-8")
    project_before = copy.deepcopy(store.project_store.load_project("demo"))
    draft_before = draft_path.read_bytes()
    with pytest.raises(SemanticValidationError, match="semantic_draft_invalid"):
        store.confirm_draft(draft_id, 1, "invalid-draft")
    assert store.project_store.load_project("demo") == project_before
    assert draft_path.read_bytes() == draft_before
    assert not (store.project_store.project_semantic_dir("demo") / "revisions").exists()


def test_deterministic_semantic_revision_id_does_not_embed_raw_key(confirmable_store: tuple[SemanticStore, str]) -> None:
    _projection()
    store, draft_id = confirmable_store
    raw_key = "secret/path-like:key"
    result = store.confirm_draft(draft_id, 1, raw_key)
    expected_digest = hashlib.sha256(f"demo\0{draft_id}\0{raw_key}".encode("utf-8")).hexdigest()
    assert result.semantic_revision.id == f"revision-{expected_digest}"
    assert raw_key not in result.semantic_revision.id
