from __future__ import annotations

import json
import hashlib
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Character, ProjectCharacter, ScriptLine, ScriptProject
from app.storage import ProjectStore
from app.voice_catalog import (
    ReferenceLocation,
    VoiceCatalogError,
    VoiceCatalogService,
    VoiceCatalogStore,
)
from app.voice_matching_models import (
    CatalogSnapshot,
    ReferenceAssetRecord,
    VoiceResourceRecord,
    VoiceSelectionSnapshot,
    WeightArtifactRecord,
)


def _write_wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\0\0" * 19_200)
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _weight_fingerprint(root_id: str, relative_path: str, path: Path) -> str:
    stat = path.stat()
    value = f"{root_id}\0{relative_path}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ready_catalog(data_root: Path, asset_root: Path) -> VoiceCatalogService:
    reference_path = asset_root / "参考音频" / "九九-惊喜.wav"
    fingerprint = _write_wav(reference_path)
    reference = ReferenceAssetRecord(
        reference_asset_id="ref-九九",
        character_id="诸葛九九",
        character_aliases=["九九"],
        language="zh",
        emotion="惊喜",
        prompt_text="真的太好了",
        duration_seconds=1.2,
        confirmed=True,
        fingerprint=fingerprint,
        character_origin="confirmed",
        emotion_origin="confirmed",
        language_origin="confirmed",
    )
    resource = VoiceResourceRecord(
        resource_id="九九-v1",
        character_id="诸葛九九",
        character_aliases=["九九"],
        reference_asset_ids=[reference.reference_asset_id],
        languages=["zh"],
        confirmed=True,
        state="ready",
        service_id="comfy-gpt",
        weight_artifact_ids=["weight-gpt", "weight-sovits"],
        mapping_origin="plugin",
        fingerprint="resource-fingerprint-v1",
    )
    store = VoiceCatalogStore(data_root / "voice_matching")
    store.publish(
        CatalogSnapshot(
            version="catalog-v1",
            resources=[resource],
            reference_assets=[reference],
        ),
        reference_locations={
            reference.reference_asset_id: ReferenceLocation(
                root_id="portable",
                relative_path=reference_path.relative_to(asset_root).as_posix(),
                fingerprint=fingerprint,
            )
        },
    )
    return VoiceCatalogService(store=store, roots={"portable": asset_root})


def _project(data_root: Path) -> None:
    store = ProjectStore(data_root)
    store.save_characters(
        [
            Character(
                id="library-九九",
                name="诸葛九九",
                aliases=["九九"],
                tags=["named"],
            )
        ]
    )
    store.save_project(
        "project-1",
        ScriptProject(
            title="自动匹配测试",
            project_characters=[
                ProjectCharacter(
                    project_character_id="九九",
                    name="诸葛九九",
                    library_character_id="library-九九",
                )
            ],
            lines=[
                ScriptLine(
                    id="line-1",
                    character_id="九九",
                    text="真的太好了",
                    note="惊喜",
                    language="zh",
                )
            ],
        ),
    )


def _dynamic_catalog(
    data_root: Path,
    asset_root: Path,
    reference_asset_root: Path | None = None,
) -> VoiceCatalogService:
    gpt_relative = "GPT_weights_v2ProPlus/task-a-e50.ckpt"
    sovits_relative = "SoVITS_weights_v2ProPlus/task-a_e24_s360.pth"
    (asset_root / gpt_relative).parent.mkdir(parents=True)
    (asset_root / sovits_relative).parent.mkdir(parents=True)
    (asset_root / gpt_relative).write_bytes(b"gpt")
    (asset_root / sovits_relative).write_bytes(b"sovits")
    reference_root = reference_asset_root or asset_root
    reference_root_id = "reference-root" if reference_asset_root is not None else "portable"
    reference_path = reference_root / "logs" / "task-a" / "5-wav32k" / "九九-惊喜.wav"
    fingerprint = _write_wav(reference_path)
    reference = ReferenceAssetRecord(
        reference_asset_id="ref-task-a",
        character_id="诸葛九九",
        character_aliases=["九九"],
        language="zh",
        emotion="惊喜",
        prompt_text="真的太好了",
        duration_seconds=1.2,
        confirmed=True,
        fingerprint=fingerprint,
        character_origin="confirmed",
        emotion_origin="confirmed",
        language_origin="confirmed",
        training_task="task-a",
        root_id=reference_root_id,
    )
    resource = VoiceResourceRecord(
        resource_id="gpt-sovits-local",
        character_id="诸葛九九",
        character_aliases=["九九"],
        reference_asset_ids=[reference.reference_asset_id],
        languages=["zh"],
        confirmed=True,
        state="ready",
        service_id="comfy-gpt",
        mapping_origin="plugin",
        fingerprint="dynamic-resource-v1",
        supports_dynamic_weights=True,
        compatible_root_ids=["portable"],
    )
    gpt_path = asset_root / gpt_relative
    sovits_path = asset_root / sovits_relative
    weights = [
        WeightArtifactRecord(
            artifact_id="weight-gpt-a",
            root_id="portable",
            relative_path=gpt_relative,
            kind="gpt",
            character_id="诸葛九九",
            training_task="task-a",
            fingerprint=_weight_fingerprint("portable", gpt_relative, gpt_path),
        ),
        WeightArtifactRecord(
            artifact_id="weight-sovits-a",
            root_id="portable",
            relative_path=sovits_relative,
            kind="sovits",
            character_id="诸葛九九",
            training_task="task-a",
            fingerprint=_weight_fingerprint("portable", sovits_relative, sovits_path),
        ),
    ]
    catalog_store = VoiceCatalogStore(data_root / "voice_matching")
    catalog_store.publish(
        CatalogSnapshot(
            version="dynamic-catalog-v1",
            resources=[resource],
            reference_assets=[reference],
            weight_artifacts=weights,
        ),
        reference_locations={
            reference.reference_asset_id: ReferenceLocation(
                root_id=reference_root_id,
                relative_path=reference_path.relative_to(reference_root).as_posix(),
                fingerprint=fingerprint,
            )
        },
    )
    roots = {"portable": asset_root}
    if reference_asset_root is not None:
        roots[reference_root_id] = reference_root
    return VoiceCatalogService(store=catalog_store, roots=roots)


def test_catalog_routes_hide_paths_and_preview_by_asset_id(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    service = _ready_catalog(data_root, asset_root)
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    response = client.get("/api/voice-assets/catalog")

    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body, ensure_ascii=False)
    assert str(asset_root) not in serialized
    assert "relative_path" not in serialized
    preview = client.get("/api/voice-assets/references/ref-九九/audio")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "audio/wav"

    confirmed = client.put(
        "/api/voice-assets/references/ref-九九/metadata",
        json={
            "character_id": "诸葛九九",
            "character_aliases": ["九九"],
            "emotion": "surprised",
            "language": "zh",
            "prompt_text": "真的太好了",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["reference"]["emotion_origin"] == "confirmed"


def test_high_confidence_recommendation_is_read_only_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    service = _ready_catalog(data_root, asset_root)
    app = create_app(data_root=data_root, voice_catalog_service=service)
    client = TestClient(app)
    save_calls: list[str] = []
    original_save = app.state.store.save_project

    def tracked_save(project_id: str, project: ScriptProject) -> None:
        save_calls.append(project_id)
        original_save(project_id, project)

    monkeypatch.setattr(app.state.store, "save_project", tracked_save)

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"]},
    )

    assert response.status_code == 200
    recommendation = response.json()["recommendations"][0]
    assert recommendation["candidates"][0]["auto_fill_eligible"] is True
    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    assert saved_line["voice_selection"] is None
    assert save_calls == []
    assert client.get("/api/queue/status").json()["queued"] == 0


def test_high_confidence_recommendation_applies_once_when_explicitly_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    service = _ready_catalog(data_root, asset_root)
    app = create_app(data_root=data_root, voice_catalog_service=service)
    client = TestClient(app)
    save_calls: list[str] = []
    original_save = app.state.store.save_project

    def tracked_save(project_id: str, project: ScriptProject) -> None:
        save_calls.append(project_id)
        original_save(project_id, project)

    monkeypatch.setattr(app.state.store, "save_project", tracked_save)

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"], "apply_automatic": True},
    )

    assert response.status_code == 200
    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    assert saved_line["voice_selection"]["source"] == "automatic"
    assert saved_line["temporary_binding"]["config"]["resource_id"] == "九九-v1"
    assert saved_line["temporary_binding"]["config"]["prompt_text"] == "真的太好了"
    assert save_calls == ["project-1"]
    assert client.get("/api/queue/status").json()["queued"] == 0


def test_recommendation_reports_no_role_mapping_before_ranking(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    store = ProjectStore(data_root)
    project = store.load_project("project-1")
    project.project_characters[0].library_character_id = "missing-character"
    store.save_project("project-1", project)
    service = _ready_catalog(data_root, asset_root)
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"]},
    )

    assert response.status_code == 200
    recommendation = response.json()["recommendations"][0]
    assert recommendation["candidates"] == []
    assert recommendation["blockers"] == ["no_role_mapping"]


def test_fuzzy_folder_candidate_requires_confirmation_then_persists_role_mapping(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    store = ProjectStore(data_root)
    store.save_characters([])
    project = store.load_project("project-1")
    project.project_characters = [
        ProjectCharacter(
            project_character_id="jiao-bu",
            name="胶布",
            library_character_id=None,
            match_status="unmatched",
        )
    ]
    project.lines = [
        ScriptLine(
            id="line-1",
            character_id="jiao-bu",
            text="真的太好了",
            note="惊喜",
            language="zh",
        )
    ]
    store.save_project("project-1", project)
    service = _dynamic_catalog(data_root, asset_root)
    current = service.store.load_current()
    task_name = "xxx-胶布tts"
    service.store.publish(
        current.model_copy(
            update={
                "reference_assets": [
                    item.model_copy(
                        update={
                            "character_id": "训练音色",
                            "character_aliases": [],
                            "training_task": task_name,
                        }
                    )
                    for item in current.reference_assets
                ],
                "weight_artifacts": [
                    item.model_copy(
                        update={
                            "character_id": "训练音色",
                            "training_task": task_name,
                        }
                    )
                    for item in current.weight_artifacts
                ],
            }
        ),
        reference_locations=service.store.load_reference_locations(),
    )
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"], "apply_automatic": True},
    )

    assert response.status_code == 200
    candidate = response.json()["recommendations"][0]["candidates"][0]
    assert candidate["score_breakdown"]["character"] == 35
    assert candidate["auto_fill_eligible"] is False
    assert candidate["identity_match"] == "folder_fuzzy"
    assert candidate["requires_identity_confirmation"] is True
    assert client.get("/api/projects/project-1").json()["lines"][0]["voice_selection"] is None
    blocked_selection = client.put(
        "/api/projects/project-1/lines/line-1/voice-selection",
        json={"candidate_id": candidate["candidate_id"]},
    )
    assert blocked_selection.status_code == 409
    assert blocked_selection.json()["detail"]["code"] == "voice_identity_confirmation_required"

    confirmed = client.post(
        "/api/projects/project-1/lines/line-1/voice-identity-confirmation",
        json={"candidate_id": candidate["candidate_id"]},
    )

    assert confirmed.status_code == 200
    payload = confirmed.json()
    assert payload["project_character"]["library_character_id"] == payload["character"]["id"]
    assert payload["project_character"]["match_status"] == "manual"
    assert task_name in payload["character"]["match_names"]
    assert "胶布" in payload["character"]["aliases"]
    mapping = payload["character"]["source_assets"]["folder_mappings"][0]
    assert mapping["training_task"] == task_name
    refreshed = payload["recommendation"]["candidates"][0]
    assert refreshed["identity_match"] == "strict"
    assert refreshed["requires_identity_confirmation"] is False
    assert refreshed["score_breakdown"]["character"] == 35


def test_recommendation_returns_structured_catalog_unavailable_error(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _project(data_root)
    service = VoiceCatalogService(
        store=VoiceCatalogStore(data_root / "voice_matching"),
        roots={},
    )
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "voice_assets_unavailable",
        "stage": "voice_recommendation",
    }


def test_dynamic_recommendation_persists_weight_pair_and_same_task_reference(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    service = _dynamic_catalog(data_root, asset_root)
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"], "apply_automatic": True},
    )

    assert response.status_code == 200
    candidate = response.json()["recommendations"][0]["candidates"][0]
    assert candidate["training_task"] == "task-a"
    assert candidate["gpt_weight_artifact_id"] == "weight-gpt-a"
    assert candidate["sovits_weight_artifact_id"] == "weight-sovits-a"
    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    selection = saved_line["voice_selection"]
    assert selection["inference_parameters"]["engine"] == "gpt-sovits"
    assert selection["inference_parameters"]["training_task"] == "task-a"
    assert selection["inference_parameters"]["gpt_weight_artifact_id"] == "weight-gpt-a"
    assert selection["inference_parameters"]["sovits_weight_artifact_id"] == "weight-sovits-a"
    assert selection["inference_parameters"]["gpt_weight_fingerprint"]
    assert selection["inference_parameters"]["sovits_weight_fingerprint"]
    config = saved_line["temporary_binding"]["config"]
    assert config["training_task"] == "task-a"
    assert config["voice_asset_root_id"] == "portable"
    assert config["gpt_weights_relative_path"] == "GPT_weights_v2ProPlus/task-a-e50.ckpt"
    assert config["sovits_weights_relative_path"] == "SoVITS_weights_v2ProPlus/task-a_e24_s360.pth"


def test_dynamic_recommendation_stages_reference_from_separate_logs_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    _project(data_root)
    service = _dynamic_catalog(
        data_root,
        tmp_path / "model",
        reference_asset_root=tmp_path / "portable",
    )
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"], "apply_automatic": True},
    )

    assert response.status_code == 200
    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    assert saved_line["voice_selection"]["source"] == "automatic"
    staged_reference = Path(saved_line["temporary_binding"]["config"]["ref_audio_path"])
    assert staged_reference.is_file()
    assert staged_reference.is_relative_to(tmp_path / "Project")


def test_dynamic_selection_rejects_weight_replaced_after_selection(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    service = _dynamic_catalog(data_root, asset_root)
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))
    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"], "apply_automatic": True},
    )
    assert response.status_code == 200
    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    selection = VoiceSelectionSnapshot.model_validate(saved_line["voice_selection"])
    service.validate_selection(selection)

    (asset_root / "GPT_weights_v2ProPlus/task-a-e50.ckpt").write_bytes(
        b"replacement-weight"
    )

    with pytest.raises(VoiceCatalogError) as captured:
        service.validate_selection(selection)
    assert captured.value.code == "voice_asset_changed"
    assert captured.value.field_path == "weight_artifacts.gpt"


def test_manual_selection_uses_server_candidate_and_can_be_cleared(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    service = _ready_catalog(data_root, asset_root)
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    selected = client.put(
        "/api/projects/project-1/lines/line-1/voice-selection",
        json={"candidate_id": "九九-v1:ref-九九"},
    )

    assert selected.status_code == 200
    assert selected.json()["selection"]["source"] == "manual"
    cleared = client.delete(
        "/api/projects/project-1/lines/line-1/voice-selection"
    )
    assert cleared.status_code == 200
    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    assert saved_line["voice_selection"] is None
    assert saved_line["temporary_binding"] is None


def test_stale_voice_selection_fails_async_job_without_fallback(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "comfy-gpt",
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "api_contract": "comfyui-tts-audio-suite-v1",
                    "base_url": "mock://comfy-gpt",
                    "capabilities": [
                        "tts",
                        "trained_weights_voice",
                        "reference_audio_voice",
                    ],
                    "default_params": {
                        "engine": "gpt-sovits",
                        "resource_id": "九九-v1",
                    },
                    "setup_state": "ready",
                }
            ]
        ),
        encoding="utf-8",
    )
    _project(data_root)
    service = _ready_catalog(data_root, asset_root)
    client = TestClient(
        create_app(
            data_root=data_root,
            services_path=services_path,
            voice_catalog_service=service,
        )
    )
    assert client.put(
        "/api/projects/project-1/lines/line-1/voice-selection",
        json={"candidate_id": "九九-v1:ref-九九"},
    ).status_code == 200

    current = service.store.load_current()
    changed_resource = current.resources[0].model_copy(
        update={"fingerprint": "resource-fingerprint-v2"}
    )
    service.store.publish(
        current.model_copy(update={"resources": [changed_resource]}),
        reference_locations=service.store.load_reference_locations(),
    )
    request = {
        "project_id": "project-1",
        "tasks": [
            {
                "line": {
                    "id": "line-1",
                    "character_id": "九九",
                    "text": "真的太好了",
                },
                "engine": "gpt-sovits",
                "profile": "default",
                "parameters": {},
            }
        ],
    }

    response = client.post("/api/jobs/generation", json=request)

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "failed"
    assert "voice_asset_changed" in response.json()["items"][0]["error"]
