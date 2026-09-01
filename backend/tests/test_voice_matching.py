import pytest
from pydantic import ValidationError

from app.voice_matching import estimate_target_duration, rank_voice_candidates
from app.voice_matching_models import (
    CatalogSnapshot,
    ReferenceAssetRecord,
    VoiceMatchPolicy,
    VoiceMatchRequest,
    VoiceResourceRecord,
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
