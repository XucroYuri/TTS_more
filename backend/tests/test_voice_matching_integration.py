from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from app.comfyui.workflow_builder import build_workflow
from app.main import create_app
from app.models import Character, ProjectCharacter, ScriptLine, ScriptProject
from app.storage import ProjectStore
from app.voice_catalog import ReferenceLocation, VoiceCatalogService, VoiceCatalogStore
from app.voice_matching import rank_voice_candidates
from app.voice_matching_models import (
    CatalogSnapshot,
    ReferenceAssetRecord,
    VoiceMatchRequest,
    VoiceResourceRecord,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "voice_matching_labels.json"


def _write_wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\0\0" * 19_200)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _integration_catalog(data_root: Path, asset_root: Path) -> VoiceCatalogService:
    reference_path = asset_root / "references" / "九九-happy.wav"
    fingerprint = _write_wav(reference_path)
    reference = ReferenceAssetRecord(
        reference_asset_id="ref-九九-happy",
        character_id="诸葛九九",
        character_aliases=["九九"],
        language="zh",
        emotion="happy",
        prompt_text="参考文本",
        duration_seconds=1.2,
        confirmed=True,
        fingerprint=fingerprint,
        character_origin="confirmed",
        emotion_origin="confirmed",
        language_origin="confirmed",
    )
    resource = VoiceResourceRecord(
        resource_id="gpt-sovits-九九",
        character_id="诸葛九九",
        character_aliases=["九九"],
        reference_asset_ids=[reference.reference_asset_id],
        languages=["zh"],
        confirmed=True,
        state="ready",
        service_id="comfy-gpt",
        weight_artifact_ids=["gpt-weight", "sovits-weight"],
        mapping_origin="plugin",
        fingerprint="resource-fingerprint",
    )
    catalog_store = VoiceCatalogStore(data_root / "voice_matching")
    catalog_store.publish(
        CatalogSnapshot(
            version="catalog-integration-v1",
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
    return VoiceCatalogService(store=catalog_store, roots={"portable": asset_root})


def test_selected_line_reaches_matching_comfyui_workflow_identity(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "portable"
    store = ProjectStore(data_root)
    store.save_characters(
        [Character(id="library-九九", name="诸葛九九", aliases=["九九"], tags=["named"])]
    )
    store.save_project(
        "project-1",
        ScriptProject(
            title="integration",
            default_language="zh",
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
                    text="一条用于确定性验证的短句",
                    note="开心",
                    language="zh",
                )
            ],
        ),
    )
    service = _integration_catalog(data_root, asset_root)
    client = TestClient(create_app(data_root=data_root, voice_catalog_service=service))

    recommendation_response = client.post(
        "/api/projects/project-1/voice-recommendations",
        json={"line_ids": ["line-1"]},
    )
    assert recommendation_response.status_code == 200
    candidate = recommendation_response.json()["recommendations"][0]["candidates"][0]
    selection_response = client.put(
        "/api/projects/project-1/lines/line-1/voice-selection",
        json={"candidate_id": candidate["candidate_id"]},
    )
    assert selection_response.status_code == 200

    saved_line = client.get("/api/projects/project-1").json()["lines"][0]
    selection = saved_line["voice_selection"]
    parameters = {
        **saved_line["temporary_binding"]["config"],
        "text": "deterministic integration text",
        "asset_id": "uploaded-reference-asset",
    }
    workflow = build_workflow("gpt-sovits", parameters)

    assert selection["candidate_id"] == candidate["candidate_id"]
    assert workflow["1"]["inputs"]["resource_id"] == selection["resource_id"]
    assert workflow["1"]["inputs"]["speed"] == selection["speed_factor"]
    assert workflow["1"]["inputs"]["text_language"] == selection["text_language"]
    assert workflow["1"]["inputs"]["ref_language"] == selection["reference_language"]
    assert workflow["2"]["inputs"]["asset_id"] == "uploaded-reference-asset"
    assert workflow["3"]["inputs"]["opt_narrator"] == ["2", 0]


def test_labeled_fixture_meets_recall_and_named_identity_gates() -> None:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    references: list[ReferenceAssetRecord] = []
    resources: list[VoiceResourceRecord] = []
    resource_character: dict[str, str] = {}
    for character in payload["characters"]:
        reference_ids: list[str] = []
        for emotion in character["emotions"]:
            reference_id = f"ref-{character['id']}-{emotion}"
            reference_ids.append(reference_id)
            references.append(
                ReferenceAssetRecord(
                    reference_asset_id=reference_id,
                    character_id=character["id"],
                    character_aliases=character["aliases"],
                    language="zh",
                    emotion=emotion,
                    duration_seconds=1.5,
                    confirmed=True,
                    character_origin="confirmed",
                    emotion_origin="confirmed",
                    language_origin="confirmed",
                )
            )
        resource_id = f"resource-{character['id']}"
        resource_character[resource_id] = character["id"]
        resources.append(
            VoiceResourceRecord(
                resource_id=resource_id,
                character_id=character["id"],
                character_aliases=character["aliases"],
                reference_asset_ids=reference_ids,
                languages=["zh"],
                confirmed=True,
                state="ready",
                weight_artifact_ids=[f"weight-{character['id']}-gpt", f"weight-{character['id']}-sovits"],
                mapping_origin="manual",
            )
        )
    catalog = CatalogSnapshot(
        version=payload["catalog_version"],
        resources=resources,
        reference_assets=references,
    )

    recalled = 0
    named_identity_violations = 0
    for case in payload["cases"]:
        result = rank_voice_candidates(
            VoiceMatchRequest(
                line_id=case["line_id"],
                character_id=case["character_id"],
                character_aliases=case["aliases"],
                text="fixture",
                language="zh",
                emotion=case["emotion"],
                target_duration_seconds=1.5,
            ),
            catalog,
        )
        top_three = result.candidates[:3]
        if any(item.reference_asset_id == case["expected_reference_asset_id"] for item in top_three):
            recalled += 1
        accepted_identities = {case["character_id"], *case["aliases"]}
        named_identity_violations += sum(
            resource_character[item.resource_id] not in accepted_identities
            for item in top_three
        )

    assert recalled / len(payload["cases"]) >= 0.90
    assert named_identity_violations == 0
