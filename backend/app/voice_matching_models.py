"""Strict, path-free contracts for deterministic voice matching."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictVoiceModel(BaseModel):
    """Base contract for persisted matching data and API payloads."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _normalized_emotion(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    if not normalized:
        raise ValueError("emotion must not be empty")
    return normalized


class ReferenceAssetRecord(StrictVoiceModel):
    reference_asset_id: str = Field(min_length=1)
    character_id: str = Field(min_length=1)
    language: str = Field(default="zh", min_length=1)
    emotion: str = "neutral"
    duration_seconds: float | None = Field(default=None, gt=0)
    generic_pool: bool = False
    confirmed: bool = False
    metadata_score: float = Field(default=5, ge=0, le=5)

    @field_validator("emotion")
    @classmethod
    def normalize_emotion(cls, value: str) -> str:
        return _normalized_emotion(value)


class VoiceResourceRecord(StrictVoiceModel):
    resource_id: str = Field(min_length=1)
    character_id: str = Field(min_length=1)
    reference_asset_ids: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    generic_pool: bool = False
    confirmed: bool = False
    metadata_score: float = Field(default=5, ge=0, le=5)


class CatalogSnapshot(StrictVoiceModel):
    version: str = Field(min_length=1)
    resources: list[VoiceResourceRecord] = Field(default_factory=list)
    reference_assets: list[ReferenceAssetRecord] = Field(default_factory=list)

    def candidate_pairs(self) -> list[tuple[VoiceResourceRecord, ReferenceAssetRecord]]:
        assets_by_id = {asset.reference_asset_id: asset for asset in self.reference_assets}
        return [
            (resource, assets_by_id[asset_id])
            for resource in self.resources
            for asset_id in resource.reference_asset_ids
            if asset_id in assets_by_id
        ]


class DurationEstimate(StrictVoiceModel):
    target_seconds: float = Field(ge=0)
    base_rate: float = Field(gt=0)
    rate_source: Literal["language_default", "history_median"]


class VoiceMatchRequest(StrictVoiceModel):
    line_id: str = Field(min_length=1)
    character_id: str = Field(min_length=1)
    character_aliases: list[str] = Field(default_factory=list)
    is_generic: bool = False
    text: str = ""
    language: str = Field(default="zh", min_length=1)
    emotion: str | None = "neutral"
    target_duration_seconds: float | None = Field(default=None, gt=0)

    @field_validator("emotion")
    @classmethod
    def normalize_optional_emotion(cls, value: str | None) -> str | None:
        return None if value is None else _normalized_emotion(value)


class VoiceScoreBreakdown(StrictVoiceModel):
    character: float = Field(ge=0, le=35)
    emotion: float = Field(ge=0, le=30)
    duration: float = Field(ge=0, le=20)
    language: float = Field(ge=0, le=10)
    metadata: float = Field(ge=0, le=5)

    @property
    def total(self) -> float:
        return round(self.character + self.emotion + self.duration + self.language + self.metadata, 4)


class VoiceCandidate(StrictVoiceModel):
    candidate_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    reference_asset_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=100)
    score_breakdown: VoiceScoreBreakdown
    auto_fill_eligible: bool
    speed_factor: float = Field(ge=0.85, le=1.20)


class VoiceRecommendation(StrictVoiceModel):
    line_id: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    candidates: list[VoiceCandidate] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class VoiceSelectionSnapshot(StrictVoiceModel):
    catalog_version: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    reference_asset_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=100)
    speed_factor: float = Field(ge=0.85, le=1.20)


class VoiceMatchPolicy(StrictVoiceModel):
    auto_fill_threshold: float = Field(default=80, ge=80, le=100)
    explicit_emotion_minimum: float = Field(default=22, ge=22, le=30)
    speed_min: float = Field(default=0.85, ge=0.85, le=1.20)
    speed_max: float = Field(default=1.20, ge=0.85, le=1.20)


DEFAULT_POLICY = VoiceMatchPolicy()
