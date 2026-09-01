"""Pure duration estimation and deterministic catalog ranking."""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from .voice_matching_models import (
    CatalogSnapshot,
    DEFAULT_POLICY,
    DurationEstimate,
    ReferenceAssetRecord,
    VoiceCandidate,
    VoiceMatchPolicy,
    VoiceMatchRequest,
    VoiceRecommendation,
    VoiceResourceRecord,
    VoiceScoreBreakdown,
)


_DEFAULT_RATES = {"zh": 4.5, "ja": 5.0, "en": 14.0}


def estimate_target_duration(
    text: str,
    language: str,
    emotion: str | None,
    history_rates: Sequence[float] = (),
) -> DurationEstimate:
    """Estimate seconds from a stable speech-rate baseline.

    Five usable historic samples are enough to replace language defaults;
    a median keeps an outlier from changing a recommendation unpredictably.
    """
    usable_rates = [rate for rate in history_rates if rate > 0]
    if len(usable_rates) >= 5:
        base_rate = round(float(statistics.median(usable_rates[-5:])), 4)
        source = "history_median"
    else:
        base_rate = _DEFAULT_RATES.get(language.strip().casefold(), 4.5)
        source = "language_default"
    units = len("".join(text.split()))
    return DurationEstimate(
        target_seconds=round(units / base_rate, 4) if units else 0,
        base_rate=base_rate,
        rate_source=source,
    )


def rank_voice_candidates(
    request: VoiceMatchRequest,
    catalog: CatalogSnapshot,
    policy: VoiceMatchPolicy = DEFAULT_POLICY,
) -> VoiceRecommendation:
    eligible = [pair for pair in catalog.candidate_pairs() if _passes_identity_gates(request, pair)]
    scored = [_score_candidate(request, pair, policy) for pair in eligible]
    scored.sort(key=lambda item: (-item.score, -item.score_breakdown.metadata, item.candidate_id))
    return VoiceRecommendation(
        line_id=request.line_id,
        catalog_version=catalog.version,
        candidates=scored[:3],
        blockers=[] if scored else ["no_eligible_voice_candidate"],
    )


def _passes_identity_gates(
    request: VoiceMatchRequest,
    pair: tuple[VoiceResourceRecord, ReferenceAssetRecord],
) -> bool:
    resource, asset = pair
    if request.is_generic:
        return (
            resource.generic_pool
            and asset.generic_pool
            and resource.confirmed
            and asset.confirmed
        )
    names = {request.character_id, *request.character_aliases}
    return resource.character_id in names and asset.character_id in names


def _score_candidate(
    request: VoiceMatchRequest,
    pair: tuple[VoiceResourceRecord, ReferenceAssetRecord],
    policy: VoiceMatchPolicy,
) -> VoiceCandidate:
    resource, asset = pair
    character = 15 if request.is_generic else 35
    emotion = _emotion_score(request.emotion, asset.emotion)
    duration, speed_factor = _duration_score(request.target_duration_seconds, asset.duration_seconds, policy)
    language = 10 if request.language.casefold() == asset.language.casefold() else 0
    metadata = min(resource.metadata_score, asset.metadata_score)
    breakdown = VoiceScoreBreakdown(
        character=character,
        emotion=emotion,
        duration=duration,
        language=language,
        metadata=metadata,
    )
    explicit_emotion = request.emotion not in (None, "neutral")
    auto_fill = breakdown.total >= policy.auto_fill_threshold and (
        not explicit_emotion or emotion >= policy.explicit_emotion_minimum
    )
    return VoiceCandidate(
        candidate_id=f"{resource.resource_id}:{asset.reference_asset_id}",
        resource_id=resource.resource_id,
        reference_asset_id=asset.reference_asset_id,
        score=breakdown.total,
        score_breakdown=breakdown,
        auto_fill_eligible=auto_fill,
        speed_factor=speed_factor,
    )


def _emotion_score(requested: str | None, available: str) -> float:
    if requested is None or requested == "neutral":
        return 30 if available == "neutral" else 0
    if available == requested:
        return 30
    if available == "neutral":
        return 12
    return 0


def _duration_score(
    target: float | None,
    reference: float | None,
    policy: VoiceMatchPolicy,
) -> tuple[float, float]:
    if target is None or reference is None:
        return 0, 1.0
    speed = max(policy.speed_min, min(policy.speed_max, reference / target))
    difference = abs(target - reference) / target
    return round(max(0, 20 * (1 - difference)), 4), round(speed, 4)
