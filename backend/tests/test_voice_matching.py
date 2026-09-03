import pytest
from pydantic import ValidationError

from app.voice_matching import estimate_target_duration, rank_voice_candidates
from app.voice_matching_models import (
    CatalogSnapshot,
    ReferenceAssetRecord,
    VoiceMatchPolicy,
    VoiceMatchRequest,
    VoiceResourceRecord,
    WeightArtifactRecord,
)


def match_request(
    *,
    character_id: str = "九九",
    aliases: list[str] | None = None,
    is_generic: bool = False,
    emotion: str | None = "neutral",
) -> VoiceMatchRequest:
    return VoiceMatchRequest(
        line_id="line-001",
        character_id=character_id,
        character_aliases=aliases or [],
        is_generic=is_generic,
        text="你好。",
        language="zh",
        emotion=emotion,
    )


def catalog_with(
    *,
    resource_character: str,
    reference_character: str,
    emotion: str = "neutral",
    generic_pool: bool = False,
) -> CatalogSnapshot:
    return CatalogSnapshot(
        version="catalog-v1",
        resources=[
            VoiceResourceRecord(
                resource_id="voice-001",
                character_id=resource_character,
                reference_asset_ids=["ref-001"],
                generic_pool=generic_pool,
                confirmed=True,
            )
        ],
        reference_assets=[
            ReferenceAssetRecord(
                reference_asset_id="ref-001",
                character_id=reference_character,
                language="zh",
                emotion=emotion,
                generic_pool=generic_pool,
                confirmed=True,
            )
        ],
    )


def test_named_character_never_cross_matches() -> None:
    request = match_request(character_id="九九", aliases=["诸葛九九"], is_generic=False)
    catalog = catalog_with(resource_character="可莉", reference_character="可莉")

    result = rank_voice_candidates(request, catalog)

    assert result.candidates == []
    assert result.blockers == ["no_eligible_voice_candidate"]


def test_explicit_emotion_neutral_fallback_is_not_auto_fill() -> None:
    request = match_request(character_id="九九", emotion="angry")
    catalog = catalog_with(resource_character="九九", reference_character="九九", emotion="neutral")

    result = rank_voice_candidates(request, catalog)

    assert result.candidates[0].score_breakdown.emotion == 12
    assert result.candidates[0].auto_fill_eligible is False


def test_unknown_emotion_metadata_can_use_deterministic_neutral_fallback() -> None:
    request = match_request(character_id="九九", emotion="calm").model_copy(
        update={"target_duration_seconds": 1.0}
    )
    catalog = catalog_with(
        resource_character="九九",
        reference_character="九九",
        emotion="neutral",
    ).model_copy(
        update={
            "reference_assets": [
                ReferenceAssetRecord(
                    reference_asset_id="ref-001",
                    character_id="九九",
                    language="zh",
                    emotion="neutral",
                    emotion_origin="unknown",
                    emotion_confidence=0,
                    duration_seconds=1.0,
                    metadata_score=5,
                    confirmed=True,
                )
            ]
        }
    )

    result = rank_voice_candidates(request, catalog)

    assert result.candidates[0].score == 82
    assert result.candidates[0].auto_fill_eligible is True
    assert "emotion_metadata_unavailable_fallback" in result.candidates[0].reasons


def test_generic_character_can_enter_confirmed_generic_pool() -> None:
    request = match_request(character_id="路人", is_generic=True)
    catalog = catalog_with(
        resource_character="通用男声",
        reference_character="通用男声",
        generic_pool=True,
    )

    result = rank_voice_candidates(request, catalog)

    assert result.candidates[0].score_breakdown.character == 15


def test_duration_estimator_uses_recent_median_after_five_samples() -> None:
    result = estimate_target_duration(
        text="一二三四五六七八",
        language="zh",
        emotion="neutral",
        history_rates=[4.0, 4.2, 4.4, 9.0, 3.8],
    )

    assert result.rate_source == "history_median"
    assert result.base_rate == 4.2


def test_unconfirmed_generic_resource_is_not_eligible() -> None:
    request = match_request(character_id="路人", is_generic=True)
    catalog = catalog_with(
        resource_character="通用男声",
        reference_character="通用男声",
        generic_pool=True,
    ).model_copy(
        update={
            "resources": [
                VoiceResourceRecord(
                    resource_id="voice-001",
                    character_id="通用男声",
                    reference_asset_ids=["ref-001"],
                    generic_pool=True,
                    confirmed=False,
                )
            ]
        }
    )

    assert rank_voice_candidates(request, catalog).candidates == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("auto_fill_threshold", 79.9), ("explicit_emotion_minimum", 21.9)],
)
def test_voice_match_policy_rejects_values_below_mandatory_auto_fill_floors(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValidationError):
        VoiceMatchPolicy(**{field: value})


def test_unready_comfyui_resource_is_never_ranked() -> None:
    request = match_request(character_id="九九", aliases=["诸葛九九"])
    catalog = catalog_with(resource_character="九九", reference_character="九九").model_copy(
        update={
            "resources": [
                VoiceResourceRecord(
                    resource_id="voice-001",
                    character_id="九九",
                    reference_asset_ids=["ref-001"],
                    state="reload_required",
                )
            ]
        }
    )

    result = rank_voice_candidates(request, catalog)

    assert result.candidates == []
    assert result.blockers == ["no_eligible_voice_candidate"]


def test_low_confidence_inferred_character_cannot_drive_candidate() -> None:
    request = match_request(character_id="九九", aliases=["诸葛九九"])
    catalog = catalog_with(resource_character="九九", reference_character="九九").model_copy(
        update={
            "reference_assets": [
                ReferenceAssetRecord(
                    reference_asset_id="ref-001",
                    character_id="九九",
                    character_origin="inferred",
                    character_confidence=0.89,
                )
            ]
        }
    )

    assert rank_voice_candidates(request, catalog).candidates == []


def test_controlled_resource_and_reference_aliases_match_named_character() -> None:
    request = match_request(character_id="诸葛九九", aliases=[])
    catalog = CatalogSnapshot(
        version="catalog-v1",
        resources=[
            VoiceResourceRecord(
                resource_id="voice-001",
                character_id="九九",
                character_aliases=["诸葛九九"],
                reference_asset_ids=["ref-001"],
            )
        ],
        reference_assets=[
            ReferenceAssetRecord(
                reference_asset_id="ref-001",
                character_id="九九",
                character_aliases=["诸葛九九"],
            )
        ],
    )

    result = rank_voice_candidates(request, catalog)

    assert result.candidates[0].resource_id == "voice-001"


def test_ambiguous_alias_across_two_characters_blocks_automatic_matching() -> None:
    request = match_request(character_id="小九", aliases=[])
    catalog = CatalogSnapshot(
        version="catalog-v1",
        resources=[
            VoiceResourceRecord(
                resource_id="voice-a",
                character_id="诸葛九九",
                character_aliases=["小九"],
                reference_asset_ids=["ref-a"],
            ),
            VoiceResourceRecord(
                resource_id="voice-b",
                character_id="王九九",
                character_aliases=["小九"],
                reference_asset_ids=["ref-b"],
            ),
        ],
        reference_assets=[
            ReferenceAssetRecord(
                reference_asset_id="ref-a",
                character_id="诸葛九九",
                character_aliases=["小九"],
            ),
            ReferenceAssetRecord(
                reference_asset_id="ref-b",
                character_id="王九九",
                character_aliases=["小九"],
            ),
        ],
    )

    result = rank_voice_candidates(request, catalog)

    assert result.candidates == []


def test_dynamic_candidates_never_cross_training_task_reference_pools() -> None:
    request = match_request(character_id="九九", emotion="happy")
    request = request.model_copy(update={"target_duration_seconds": 2.0})
    catalog = CatalogSnapshot(
        version="catalog-v1",
        resources=[
            VoiceResourceRecord(
                resource_id="gpt-sovits-local",
                character_id="九九",
                state="ready",
                confirmed=True,
                supports_dynamic_weights=True,
                compatible_root_ids=["portable"],
            )
        ],
        weight_artifacts=[
            WeightArtifactRecord(
                artifact_id="gpt-a",
                root_id="portable",
                relative_path="GPT_weights/task-a-e50.ckpt",
                kind="gpt",
                character_id="九九",
                training_task="task-a",
                fingerprint="gpt-a-fingerprint",
            ),
            WeightArtifactRecord(
                artifact_id="sovits-a",
                root_id="portable",
                relative_path="SoVITS_weights/task-a_e24_s360.pth",
                kind="sovits",
                character_id="九九",
                training_task="task-a",
                fingerprint="sovits-a-fingerprint",
            ),
            WeightArtifactRecord(
                artifact_id="gpt-b",
                root_id="portable",
                relative_path="GPT_weights/task-b-e50.ckpt",
                kind="gpt",
                character_id="九九",
                training_task="task-b",
                fingerprint="gpt-b-fingerprint",
            ),
            WeightArtifactRecord(
                artifact_id="sovits-b",
                root_id="portable",
                relative_path="SoVITS_weights/task-b_e24_s360.pth",
                kind="sovits",
                character_id="九九",
                training_task="task-b",
                fingerprint="sovits-b-fingerprint",
            ),
        ],
        reference_assets=[
            ReferenceAssetRecord(
                reference_asset_id="ref-a-happy",
                character_id="九九",
                language="zh",
                emotion="happy",
                prompt_text="参考原文甲",
                duration_seconds=2.0,
                confirmed=True,
                training_task="task-a",
                root_id="portable",
            ),
            ReferenceAssetRecord(
                reference_asset_id="ref-b-neutral",
                character_id="九九",
                language="zh",
                emotion="neutral",
                prompt_text="参考原文乙",
                duration_seconds=4.0,
                confirmed=True,
                training_task="task-b",
                root_id="portable",
            ),
        ],
    )

    result = rank_voice_candidates(request, catalog)

    assert result.candidates[0].reference_asset_id == "ref-a-happy"
    assert result.candidates[0].training_task == "task-a"
    assert result.candidates[0].gpt_weight_artifact_id == "gpt-a"
    assert result.candidates[0].sovits_weight_artifact_id == "sovits-a"
    assert {
        (item.training_task, item.reference_asset_id)
        for item in result.candidates
    } == {
        ("task-a", "ref-a-happy"),
        ("task-b", "ref-b-neutral"),
    }
