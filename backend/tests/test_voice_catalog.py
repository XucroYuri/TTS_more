from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import pytest

from app.models import EngineName, ProviderType, TTSServiceEndpoint
from app.services import ServiceRegistry
from app.voice_catalog import (
    PortableAssetScanner,
    ReferenceMetadataOverride,
    ResourceMappingOverride,
    VoiceCatalogError,
    VoiceCatalogService,
    VoiceCatalogStore,
    configured_voice_asset_roots,
)
from app.voice_metadata_inference import (
    VoiceMetadataInferenceResult,
    VoiceMetadataUnavailable,
)
from app.voice_matching_models import CatalogSnapshot


def _write_silent_wav(path: Path, *, duration_ms: int = 1200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 16_000
    frame_count = sample_rate * duration_ms // 1000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\0\0" * frame_count)


def _portable_fixture(root: Path) -> Path:
    (root / "GPT_weights_v2ProPlus").mkdir(parents=True)
    (root / "SoVITS_weights_v2ProPlus").mkdir(parents=True)
    (root / "GPT_weights_v2ProPlus" / "九九-e10.ckpt").write_bytes(b"gpt")
    (root / "SoVITS_weights_v2ProPlus" / "九九_e8_s120.pth").write_bytes(b"sovits")
    (root / "character_map.json").write_text(
        json.dumps(
            {
                "九九-e10.ckpt": "诸葛九九",
                "九九_e8_s120.pth": "诸葛九九",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reference = root / "参考音频" / "[九九惊喜_中文]真的太好了.wav"
    _write_silent_wav(reference)
    (root / "参考音频" / "audio_metadata.json").write_text(
        json.dumps(
            {
                reference.name: {
                    "character": "诸葛九九",
                    "emotion": "惊喜",
                    "lang": "中文",
                    "text_override": "真的太好了",
                    "duration_ms": 9999,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def test_portable_scanner_discovers_assets_without_pairing_weights(tmp_path: Path) -> None:
    scan = PortableAssetScanner().scan("portable", _portable_fixture(tmp_path / "portable"))

    assert {artifact.kind for artifact in scan.weight_artifacts} == {"gpt", "sovits"}
    assert {artifact.character_id for artifact in scan.weight_artifacts} == {"诸葛九九"}
    assert scan.resource_records == []
    assert len(scan.reference_assets) == 1
    reference = scan.reference_assets[0]
    assert reference.character_id == "诸葛九九"
    assert reference.emotion == "惊喜"
    assert reference.language == "zh"
    assert reference.prompt_text == "真的太好了"
    assert reference.duration_seconds == pytest.approx(1.2)


def test_catalog_service_restores_weight_count_from_persisted_snapshot(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    scan = PortableAssetScanner().scan("portable", root)
    store = VoiceCatalogStore(tmp_path / "catalog")
    store.publish(
        CatalogSnapshot(
            version="persisted-v1",
            resources=scan.resource_records,
            reference_assets=scan.reference_assets,
            weight_artifacts=scan.weight_artifacts,
        ),
        reference_locations=scan.reference_locations,
    )

    restarted = VoiceCatalogService(store=store, roots={"portable": root})

    assert restarted.public_view()["counts"]["weights"] == 2


def test_resolve_weight_rejects_artifact_changed_after_sync(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
    )
    service.sync()
    snapshot = service.store.load_current()
    assert snapshot is not None
    artifact = snapshot.weight_artifacts[0]
    path = service.resolve_weight(artifact.artifact_id)
    path.write_bytes(b"changed-after-sync")

    with pytest.raises(VoiceCatalogError) as captured:
        service.resolve_weight(artifact.artifact_id)

    assert captured.value.code == "voice_asset_changed"
    assert captured.value.field_path == "weight_artifacts"


def test_portable_scanner_extracts_exact_training_task_from_each_weight_kind(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    (root / "GPT_weights_v2ProPlus").mkdir(parents=True)
    (root / "SoVITS_weights_v2ProPlus").mkdir(parents=True)
    (root / "参考音频").mkdir()
    (root / "GPT_weights_v2ProPlus" / "角色-A-e50.ckpt").write_bytes(b"gpt")
    (root / "SoVITS_weights_v2ProPlus" / "角色-A_e24_s360.pth").write_bytes(b"sovits")
    _write_silent_wav(root / "参考音频" / "legacy.wav")

    scan = PortableAssetScanner().scan("portable", root)

    assert {item.training_task for item in scan.weight_artifacts} == {"角色-a"}
    assert scan.reference_assets[0].training_task is None


def test_portable_scanner_normalizes_logs_task_like_weight_task(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    (root / "GPT_weights_v2ProPlus").mkdir(parents=True)
    (root / "SoVITS_weights_v2ProPlus").mkdir(parents=True)
    (root / "GPT_weights_v2ProPlus" / "胶布TTS新-20260611-e50.ckpt").write_bytes(b"gpt")
    (root / "SoVITS_weights_v2ProPlus" / "胶布TTS新-20260611_e24_s240.pth").write_bytes(b"sovits")
    reference = root / "logs" / "胶布TTS新-20260611" / "5-wav32k" / "胶布TTS新_01.wav"
    _write_silent_wav(reference)

    scan = PortableAssetScanner().scan("portable", root)

    assert {item.training_task for item in scan.weight_artifacts} == {"胶布tts新-20260611"}
    assert {item.training_task for item in scan.reference_assets} == {"胶布tts新-20260611"}


def test_portable_scanner_prefers_wav32k_from_variable_training_task_folders(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    first = root / "logs" / "task-alpha" / "5-wav32k" / "[九九开心_中文]第一句.wav"
    second = root / "logs" / "任意训练任务-2026" / "5-wav32k" / "[九九平静_中文]第二句.wav"
    ignored = root / "logs" / "task-alpha" / "eval" / "not-a-reference.wav"
    _write_silent_wav(first)
    _write_silent_wav(second)
    _write_silent_wav(ignored)
    (first.parents[1] / "2-name2text.txt").write_text(
        f"{first.name}\tphones\t[2, 2, 1]\t第一句原文\n",
        encoding="utf-8",
    )

    scan = PortableAssetScanner().scan("portable", root)

    assert len(scan.reference_assets) == 2
    assert set(scan.reference_locations) == {
        item.reference_asset_id for item in scan.reference_assets
    }
    assert {
        location.relative_path for location in scan.reference_locations.values()
    } == {
        "logs/task-alpha/5-wav32k/[九九开心_中文]第一句.wav",
        "logs/任意训练任务-2026/5-wav32k/[九九平静_中文]第二句.wav",
    }
    assert {item.training_task for item in scan.reference_assets} == {
        "task-alpha",
        "任意训练任务-2026",
    }
    first_record = next(item for item in scan.reference_assets if item.training_task == "task-alpha")
    assert first_record.character_id == "task"
    assert first_record.prompt_text == "第一句原文"
    assert first_record.language == "zh"
    assert first_record.language_origin == "inferred"


def test_catalog_snapshot_persists_discovered_weight_artifacts(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
    )

    service.sync()

    snapshot = service.store.load_current()
    assert snapshot is not None
    assert {item.training_task for item in snapshot.weight_artifacts} == {"九九"}


def test_catalog_store_publishes_one_complete_snapshot_atomically(tmp_path: Path) -> None:
    store = VoiceCatalogStore(tmp_path / "voice_matching")
    snapshot = CatalogSnapshot(version="v1")

    store.publish(snapshot, reference_locations={})

    assert store.load_current() == snapshot
    assert store.load_reference_locations() == {}
    assert list((tmp_path / "voice_matching").glob("*.tmp")) == []


def test_failed_sync_preserves_last_good_snapshot(tmp_path: Path) -> None:
    store = VoiceCatalogStore(tmp_path / "voice_matching")
    store.publish(CatalogSnapshot(version="v1"), reference_locations={})
    service = VoiceCatalogService(
        store=store,
        roots={"portable": tmp_path / "missing"},
    )

    status = service.sync()

    assert status.state == "failed"
    assert status.catalog_version == "v1"
    assert status.diagnostics[0].code == "voice_asset_root_unavailable"
    assert store.load_current().version == "v1"


def test_reference_resolution_rechecks_root_confinement(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    store = VoiceCatalogStore(tmp_path / "voice_matching")
    service = VoiceCatalogService(store=store, roots={"portable": root})
    assert service.sync().state == "ready"
    reference_id = store.load_current().reference_assets[0].reference_asset_id
    resolved = service.resolve_reference(reference_id)
    assert resolved.is_file()
    assert resolved.is_relative_to(root)

    envelope = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    envelope["reference_locations"][reference_id]["relative_path"] = "../outside.wav"
    store.catalog_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(VoiceCatalogError) as exc_info:
        service.resolve_reference(reference_id)
    assert exc_info.value.code == "voice_asset_path_unsafe"


class _RecordingInferrer:
    provider_id = "test-provider:test-model:voice-metadata-v1"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def infer(self, items):
        self.calls += 1
        if self.fail:
            raise VoiceMetadataUnavailable("provider unavailable")
        return [
            VoiceMetadataInferenceResult(
                asset_id=item.asset_id,
                character="诸葛九九",
                character_confidence=0.96,
                emotion="happy",
                emotion_confidence=0.88,
                language="zh",
            )
            for item in items
        ]


def test_missing_metadata_is_inferred_once_and_cached_by_fingerprint(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    metadata_path = root / "参考音频" / "audio_metadata.json"
    metadata_path.write_text("{}", encoding="utf-8")
    inferrer = _RecordingInferrer()
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
        metadata_inferrer=inferrer,
    )

    assert service.sync().state == "ready"
    assert service.sync().state == "ready"

    reference = service.store.load_current().reference_assets[0]
    assert inferrer.calls == 1
    assert reference.character_id == "诸葛九九"
    assert reference.character_origin == "inferred"
    assert reference.character_confidence == pytest.approx(0.96)
    assert reference.emotion == "happy"
    assert reference.emotion_origin == "inferred"
    assert reference.emotion_confidence == pytest.approx(0.88)


def test_inference_failure_keeps_objective_assets_and_safe_diagnostic(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    (root / "参考音频" / "audio_metadata.json").write_text("{}", encoding="utf-8")
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
        metadata_inferrer=_RecordingInferrer(fail=True),
    )

    status = service.sync()

    assert status.state == "partial"
    assert status.reference_count == 1
    assert [item.code for item in status.diagnostics] == ["voice_metadata_inference_unavailable"]
    assert status.diagnostics[0].field_path == "metadata"


class _CapabilityClient:
    def capabilities(self) -> dict[str, object]:
        return {
            "contract_version": "tts-audio-suite-v1",
            "resources": [
                {
                    "resource_id": "九九-v1",
                    "engine": "gpt-sovits",
                    "ready": True,
                    "character": "诸葛九九",
                    "aliases": ["九九"],
                    "reference_audio": "[九九惊喜_中文]真的太好了.wav",
                    "gpt_weight": "九九-e10.ckpt",
                    "sovits_weight": "九九_e8_s120.pth",
                }
            ],
        }


class _DynamicCapabilityClient:
    def capabilities(self) -> dict[str, object]:
        return {
            "contract_version": "tts-audio-suite-v1",
            "resources": [
                {
                    "resource_id": "gpt-sovits-local",
                    "engine": "gpt-sovits",
                    "ready": True,
                }
            ],
        }


def test_dynamic_comfyui_resource_exposes_only_exact_root_task_pools(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    logs_reference = root / "logs" / "九九" / "5-wav32k" / "【配音员】九九-惊喜.wav"
    _write_silent_wav(logs_reference)
    (logs_reference.parents[1] / "2-name2text.txt").write_text(
        f"{logs_reference.name}\tphones\tzh\t真的太好了\n",
        encoding="utf-8",
    )
    endpoint = TTSServiceEndpoint(
        service_id="comfy-gpt",
        display_name="Comfy GPT-SoVITS",
        engine=EngineName.GPT_SOVITS,
        provider_type=ProviderType.GPT_SOVITS,
        api_contract="comfyui-tts-audio-suite-v1",
        base_url="http://127.0.0.1:8188",
        mode="external",
        managed=False,
        enabled=True,
        capabilities=["tts", "trained_weights_voice", "reference_audio_voice"],
        default_params={
            "resource_id": "gpt-sovits-local",
            "voice_asset_root": str(root),
            "dynamic_weights": True,
        },
    )
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
        registry=ServiceRegistry([endpoint]),
        clients={"comfy-gpt": _DynamicCapabilityClient()},
    )

    status = service.sync()
    snapshot = service.store.load_current()

    assert status.state == "ready"
    resource = snapshot.resources[0]
    assert resource.state == "ready"
    assert resource.supports_dynamic_weights is True
    assert resource.compatible_root_ids == ["portable"]
    assert set(resource.weight_artifact_ids) == {
        item.artifact_id for item in snapshot.weight_artifacts
    }
    assert resource.reference_asset_ids == [
        next(
            item.reference_asset_id
            for item in snapshot.reference_assets
            if item.training_task == "九九"
        )
    ]


def test_dynamic_comfyui_resource_supports_separate_weight_and_logs_roots(
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "model"
    portable_root = tmp_path / "portable"
    (model_root / "GPT_weights_v2ProPlus").mkdir(parents=True)
    (model_root / "SoVITS_weights_v2ProPlus").mkdir(parents=True)
    (model_root / "GPT_weights_v2ProPlus" / "task-a-e50.ckpt").write_bytes(b"gpt")
    (model_root / "SoVITS_weights_v2ProPlus" / "task-a_e24_s360.pth").write_bytes(
        b"sovits"
    )
    reference = portable_root / "logs" / "task-a" / "5-wav32k" / "九九-惊喜.wav"
    _write_silent_wav(reference)
    (reference.parents[1] / "2-name2text.txt").write_text(
        f"{reference.name}\tphones\tzh\t真的太好了\n",
        encoding="utf-8",
    )
    linked_reference = model_root / "logs" / "task-a" / "5-wav32k" / reference.name
    linked_reference.parent.mkdir(parents=True)
    os.link(reference, linked_reference)
    (linked_reference.parents[1] / "2-name2text.txt").write_text(
        f"{linked_reference.name}\tphones\tzh\t真的太好了\n",
        encoding="utf-8",
    )
    endpoint = TTSServiceEndpoint(
        service_id="comfy-gpt",
        display_name="Comfy GPT-SoVITS",
        engine=EngineName.GPT_SOVITS,
        provider_type=ProviderType.GPT_SOVITS,
        api_contract="comfyui-tts-audio-suite-v1",
        base_url="http://127.0.0.1:8188",
        mode="external",
        managed=False,
        enabled=True,
        capabilities=["tts", "trained_weights_voice", "reference_audio_voice"],
        default_params={
            "resource_id": "gpt-sovits-local",
            "voice_asset_root": str(model_root),
            "logs_root": str(portable_root / "logs"),
            "dynamic_weights": True,
        },
    )
    registry = ServiceRegistry([endpoint])
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots=configured_voice_asset_roots(registry),
        registry=registry,
        clients={"comfy-gpt": _DynamicCapabilityClient()},
    )

    assert set(service.roots.values()) == {model_root, portable_root / "logs"}

    status = service.sync()
    snapshot = service.store.load_current()

    assert status.state in {"ready", "partial"}
    assert snapshot is not None
    assert len(snapshot.reference_assets) == 1
    resource = snapshot.resources[0]
    assert resource.state == "ready"
    assert len(resource.weight_artifact_ids) == 2
    assert len(resource.reference_asset_ids) == 1
    reference_record = next(
        item
        for item in snapshot.reference_assets
        if item.reference_asset_id == resource.reference_asset_ids[0]
    )
    weight_root_ids = {
        item.root_id
        for item in snapshot.weight_artifacts
        if item.artifact_id in resource.weight_artifact_ids
    }
    assert reference_record.root_id not in weight_root_ids
    assert service.resolve_reference(reference_record.reference_asset_id) == reference


def test_only_ready_explicit_comfyui_resource_becomes_candidate(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    endpoint = TTSServiceEndpoint(
        service_id="comfy-gpt",
        display_name="Comfy GPT-SoVITS",
        engine=EngineName.GPT_SOVITS,
        provider_type=ProviderType.GPT_SOVITS,
        api_contract="comfyui-tts-audio-suite-v1",
        base_url="http://127.0.0.1:8188",
        mode="external",
        managed=False,
        enabled=True,
        capabilities=["tts", "trained_weights_voice", "reference_audio_voice"],
        default_params={"resource_id": "九九-v1", "voice_asset_root": str(root)},
    )
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
        registry=ServiceRegistry([endpoint]),
        clients={"comfy-gpt": _CapabilityClient()},
    )

    status = service.sync()
    catalog = service.store.load_current()

    assert status.state == "ready"
    assert len(catalog.resources) == 1
    resource = catalog.resources[0]
    assert resource.resource_id == "九九-v1"
    assert resource.state == "ready"
    assert resource.character_id == "诸葛九九"
    assert resource.character_aliases == ["九九"]
    assert len(resource.weight_artifact_ids) == 2
    assert resource.reference_asset_ids == [catalog.reference_assets[0].reference_asset_id]
    serialized = json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False)
    assert str(root) not in serialized


def test_confirmed_reference_override_persists_across_rescan(tmp_path: Path) -> None:
    root = _portable_fixture(tmp_path / "portable")
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
    )
    assert service.sync().state == "ready"
    asset_id = service.store.load_current().reference_assets[0].reference_asset_id
    original_version = service.store.load_current().version

    updated = service.set_reference_override(
        asset_id,
        ReferenceMetadataOverride(
            character_id="诸葛九九",
            character_aliases=["九九"],
            emotion="surprised",
            language="zh",
            prompt_text="真的太好了",
        ),
    )
    assert service.store.load_current().version != original_version
    assert service.sync().state == "ready"

    persisted = service.store.load_current().reference_assets[0]
    assert updated.character_origin == "confirmed"
    assert persisted.character_id == "诸葛九九"
    assert persisted.character_aliases == ["九九"]
    assert persisted.emotion == "surprised"
    assert persisted.emotion_origin == "confirmed"


class _UnmappedCapabilityClient:
    def capabilities(self) -> dict[str, object]:
        return {
            "contract_version": "tts-audio-suite-v1",
            "resources": [
                {
                    "resource_id": "九九-v1",
                    "engine": "gpt-sovits",
                    "ready": True,
                }
            ],
        }


def test_manual_resource_mapping_cannot_override_bridge_readiness_but_completes_contract(
    tmp_path: Path,
) -> None:
    root = _portable_fixture(tmp_path / "portable")
    endpoint = TTSServiceEndpoint(
        service_id="comfy-gpt",
        display_name="Comfy GPT-SoVITS",
        engine=EngineName.GPT_SOVITS,
        provider_type=ProviderType.GPT_SOVITS,
        api_contract="comfyui-tts-audio-suite-v1",
        base_url="http://127.0.0.1:8188",
        mode="external",
        managed=False,
        enabled=True,
        capabilities=["tts", "trained_weights_voice", "reference_audio_voice"],
        default_params={"resource_id": "九九-v1", "voice_asset_root": str(root)},
    )
    scanner = PortableAssetScanner()
    discovered = scanner.scan("portable", root)
    service = VoiceCatalogService(
        store=VoiceCatalogStore(tmp_path / "voice_matching"),
        roots={"portable": root},
        scanner=scanner,
        registry=ServiceRegistry([endpoint]),
        clients={"comfy-gpt": _UnmappedCapabilityClient()},
    )
    assert service.sync().state == "partial"
    reference_id = service.store.load_current().reference_assets[0].reference_asset_id

    service.set_resource_mapping(
        "九九-v1",
        ResourceMappingOverride(
            character_id="诸葛九九",
            character_aliases=["九九"],
            weight_artifact_ids=[item.artifact_id for item in discovered.weight_artifacts],
            reference_asset_ids=[reference_id],
        ),
    )
    status = service.sync()

    resource = service.store.load_current().resources[0]
    assert status.state == "ready"
    assert resource.state == "ready"
    assert resource.mapping_origin == "manual"
    assert resource.confirmed is True
