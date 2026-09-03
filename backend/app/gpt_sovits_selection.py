"""Deterministic GPT-SoVITS training-task and weight selection helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .voice_matching_models import CatalogSnapshot, VoiceResourceRecord, WeightArtifactRecord


_GPT_PROGRESS_SUFFIX = re.compile(r"-e\d+$", re.IGNORECASE)
_SOVITS_PROGRESS_SUFFIX = re.compile(r"_e\d+_s\d+$", re.IGNORECASE)
_GPT_PROGRESS = re.compile(r"-e(?P<epoch>\d+)$", re.IGNORECASE)
_SOVITS_PROGRESS = re.compile(r"_e(?P<epoch>\d+)_s(?P<step>\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class DynamicWeightPair:
    root_id: str
    training_task: str
    gpt_weight_artifact_id: str
    sovits_weight_artifact_id: str


def normalize_training_task(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or normalized in {".", ".."}:
        return ""
    if "/" in normalized or "\\" in normalized:
        return ""
    return normalized.casefold()


def training_task_from_weight(
    path: Path,
    kind: Literal["gpt", "sovits"],
) -> str:
    stem = path.stem.strip()
    pattern = _GPT_PROGRESS_SUFFIX if kind == "gpt" else _SOVITS_PROGRESS_SUFFIX
    return normalize_training_task(pattern.sub("", stem))


def _progress_score(artifact: "WeightArtifactRecord") -> tuple[int, int, str]:
    stem = Path(artifact.relative_path).stem
    if artifact.kind == "gpt":
        match = _GPT_PROGRESS.search(stem)
        return (int(match.group("epoch")) if match else -1, -1, artifact.artifact_id)
    match = _SOVITS_PROGRESS.search(stem)
    return (
        int(match.group("epoch")) if match else -1,
        int(match.group("step")) if match else -1,
        artifact.artifact_id,
    )


def pair_dynamic_weights(
    snapshot: "CatalogSnapshot",
    resource: "VoiceResourceRecord",
) -> list[DynamicWeightPair]:
    if not resource.supports_dynamic_weights or not resource.compatible_root_ids:
        return []
    allowed_roots = set(resource.compatible_root_ids)
    grouped: dict[tuple[str, str], dict[str, list["WeightArtifactRecord"]]] = {}
    for artifact in snapshot.weight_artifacts:
        if artifact.root_id not in allowed_roots:
            continue
        key = (artifact.root_id, artifact.training_task)
        grouped.setdefault(key, {"gpt": [], "sovits": []})[artifact.kind].append(artifact)

    output: list[DynamicWeightPair] = []
    for (root_id, training_task), kinds in sorted(grouped.items()):
        if not kinds["gpt"] or not kinds["sovits"]:
            continue
        gpt = max(kinds["gpt"], key=_progress_score)
        sovits = max(kinds["sovits"], key=_progress_score)
        output.append(
            DynamicWeightPair(
                root_id=root_id,
                training_task=training_task,
                gpt_weight_artifact_id=gpt.artifact_id,
                sovits_weight_artifact_id=sovits.artifact_id,
            )
        )
    return output
