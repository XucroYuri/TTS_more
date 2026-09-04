"""Pure duration estimation and deterministic catalog ranking."""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from .gpt_sovits_selection import DynamicWeightPair, pair_dynamic_weights

from .voice_matching_models import (
    CatalogSnapshot,
    DEFAULT_POLICY,
    DurationEstimate,
    ReferenceAssetRecord,
    VoiceCandidate,
    VoiceBlockerCode,
    VoiceMatchPolicy,
    VoiceMatchRequest,
    VoiceRecommendation,
    VoiceResourceRecord,
    VoiceScoreBreakdown,
)


_DEFAULT_RATES = {"zh": 4.5, "ja": 5.0, "en": 14.0}


def classify_empty_recommendation(
    request: VoiceMatchRequest,
    catalog: CatalogSnapshot,
) -> VoiceBlockerCode:
    """Explain an empty ranking without weakening identity eligibility gates."""
    if not catalog.resources or not catalog.reference_assets:
        return "voice_assets_unavailable"
    identity_names = {request.character_id, *request.character_aliases}
    matching_resources = [
        item
        for item in catalog.resources
        if item.character_id in identity_names
        or bool(identity_names & set(item.character_aliases))
    ]
    if matching_resources and all(item.state != "ready" for item in matching_resources):
        return "service_offline"
    return "no_eligible_voice_candidate"


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
    ambiguous_names = _ambiguous_identity_names(catalog)
    eligible: list[
        tuple[VoiceResourceRecord, ReferenceAssetRecord, DynamicWeightPair | None]
    ] = []
    for resource, asset in catalog.candidate_pairs():
        if resource.supports_dynamic_weights:
            continue
        pair = (resource, asset)
        if _passes_identity_gates(request, pair, ambiguous_names):
            eligible.append((resource, asset, None))
    for resource in catalog.resources:
        if not resource.supports_dynamic_weights:
            continue
        for weight_pair in pair_dynamic_weights(catalog, resource):
            for asset in catalog.reference_assets:
                if (
                    asset.reference_asset_id not in resource.reference_asset_ids
                    or asset.training_task != weight_pair.training_task
                    or not asset.prompt_text.strip()
                ):
                    continue
                pair = (resource, asset)
                if _passes_identity_gates(request, pair, ambiguous_names):
                    eligible.append((resource, asset, weight_pair))
    scored = [
        _score_candidate(request, (resource, asset), policy, weight_pair).model_copy(
            update={"catalog_version": catalog.version}
        )
        for resource, asset, weight_pair in eligible
    ]
    scored.sort(key=lambda item: (-item.score, -item.score_breakdown.metadata, item.candidate_id))
    return VoiceRecommendation(
        line_id=request.line_id,
        catalog_version=catalog.version,
        candidates=scored[:3],
        blockers=[] if scored else [classify_empty_recommendation(request, catalog)],
    )


def _passes_identity_gates(
    request: VoiceMatchRequest,
    pair: tuple[VoiceResourceRecord, ReferenceAssetRecord],
    ambiguous_names: set[str],
) -> bool:
    resource, asset = pair
    if resource.state != "ready":
        return False
    if asset.character_origin == "inferred" and asset.character_confidence < 0.90:
        return False
    if asset.emotion_origin == "inferred" and asset.emotion_confidence < 0.75:
        return False
    if request.is_generic:
        return (
            resource.generic_pool
            and asset.generic_pool
            and resource.confirmed
            and asset.confirmed
        )
    names = {request.character_id, *request.character_aliases}
    resource_matches = resource.supports_dynamic_weights or _record_identity_matches(
        request.character_id,
        names,
        resource.character_id,
        set(resource.character_aliases),
        ambiguous_names,
    )
    return resource_matches and _record_identity_matches(
        request.character_id,
        names,
        asset.character_id,
        set(asset.character_aliases),
        ambiguous_names,
    )


def _ambiguous_identity_names(catalog: CatalogSnapshot) -> set[str]:
    owners: dict[str, set[str]] = {}
    records = [*catalog.resources, *catalog.reference_assets]
    for record in records:
        for name in {record.character_id, *record.character_aliases}:
            owners.setdefault(name, set()).add(record.character_id)
    return {name for name, identities in owners.items() if len(identities) > 1}


def _record_identity_matches(
    request_character_id: str,
    request_names: set[str],
    record_character_id: str,
    record_aliases: set[str],
    ambiguous_names: set[str],
) -> bool:
    if request_character_id == record_character_id:
        return True
    matches = request_names & {record_character_id, *record_aliases}
    return any(name not in ambiguous_names for name in matches)


def _score_candidate(
    request: VoiceMatchRequest,
    pair: tuple[VoiceResourceRecord, ReferenceAssetRecord],
    policy: VoiceMatchPolicy,
    weight_pair: DynamicWeightPair | None = None,
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
    unknown_emotion_fallback = bool(
        explicit_emotion
        and asset.emotion_origin == "unknown"
        and asset.emotion == "neutral"
    )
    auto_fill = breakdown.total >= policy.auto_fill_threshold and (
        not explicit_emotion
        or emotion >= policy.explicit_emotion_minimum
        or unknown_emotion_fallback
    )
    reasons = [
        "character_identity_eligible",
        f"emotion_score:{emotion:g}",
        f"duration_score:{duration:g}",
        f"language_score:{language:g}",
    ]
    if unknown_emotion_fallback:
        reasons.append("emotion_metadata_unavailable_fallback")
    dynamic_identity = (
        f":{weight_pair.gpt_weight_artifact_id}:{weight_pair.sovits_weight_artifact_id}"
        if weight_pair
        else ""
    )
    return VoiceCandidate(
        candidate_id=(
            f"{resource.resource_id}{dynamic_identity}:{asset.reference_asset_id}"
        ),
        resource_id=resource.resource_id,
        reference_asset_id=asset.reference_asset_id,
        score=breakdown.total,
        score_breakdown=breakdown,
        auto_fill_eligible=auto_fill,
        speed_factor=speed_factor,
        engine_type=resource.engine_type,
        target_duration_seconds=request.target_duration_seconds,
        reasons=reasons,
        catalog_version="",
        training_task=weight_pair.training_task if weight_pair else None,
        gpt_weight_artifact_id=(
            weight_pair.gpt_weight_artifact_id if weight_pair else None
        ),
        sovits_weight_artifact_id=(
            weight_pair.sovits_weight_artifact_id if weight_pair else None
        ),
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
