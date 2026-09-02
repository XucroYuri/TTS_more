from __future__ import annotations

import json
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Character, ProjectCharacter, ScriptLine, ScriptProject
from app.storage import ProjectStore
from app.voice_catalog import ReferenceLocation, VoiceCatalogService, VoiceCatalogStore
from app.voice_matching_models import (
    CatalogSnapshot,
    ReferenceAssetRecord,
    VoiceResourceRecord,
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


def test_high_confidence_recommendation_autofills_without_generating(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    _project(data_root)
    service = _ready_catalog(data_root, asset_root)
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"]},
    )

    assert response.status_code == 200
    recommendation = response.json()["recommendations"][0]
    assert recommendation["candidates"][0]["auto_fill_eligible"] is True
    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    assert saved_line["voice_selection"]["source"] == "automatic"
    assert saved_line["temporary_binding"]["config"]["resource_id"] == "九九-v1"
    assert saved_line["temporary_binding"]["config"]["prompt_text"] == "真的太好了"
    assert client.get("/api/queue/status").json()["queued"] == 0


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
