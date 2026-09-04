"""Legacy voice-resource projection backed by the canonical voice catalog."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .voice_catalog import VoiceCatalogError, VoiceCatalogService
from .voice_matching_models import ReferenceAssetRecord, WeightArtifactRecord


def _empty_view() -> dict[str, Any]:
    return {
        "ready": False,
        "runtimes": {},
        "reference_audio": {
            "path": "",
            "exists": False,
            "is_dir": False,
            "groups": [],
        },
        "gpt_sovits": {
            "gpt_weights": [],
            "sovits_weights": [],
            "diagnostics": [],
        },
        "indextts": {
            "reference_audio": [],
            "model": {
                "path": "",
                "ready": False,
                "missing": ["catalog_resource"],
            },
            "diagnostics": [],
        },
    }


def _weight_options(
    service: VoiceCatalogService,
    artifacts: list[WeightArtifactRecord],
    *,
    limit: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    options: list[dict[str, str]] = []
    diagnostics: list[dict[str, str]] = []
    for artifact in sorted(
        artifacts,
        key=lambda item: (item.relative_path.casefold(), item.artifact_id),
    )[:limit]:
        try:
            resolved = service.resolve_weight(artifact.artifact_id)
        except VoiceCatalogError as exc:
            diagnostics.append(
                {
                    "status": exc.code,
                    "path": artifact.artifact_id,
                }
            )
            continue
        options.append({"name": resolved.name, "path": str(resolved)})
    return options, diagnostics


def _reference_groups(
    service: VoiceCatalogService,
    references: list[ReferenceAssetRecord],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    grouped: dict[tuple[str, str], list[tuple[ReferenceAssetRecord, Path]]] = (
        defaultdict(list)
    )
    diagnostics: list[dict[str, str]] = []
    for reference in sorted(
        references,
        key=lambda item: (
            (item.training_task or "").casefold(),
            item.reference_asset_id,
        ),
    ):
        try:
            resolved = service.resolve_reference(reference.reference_asset_id)
        except VoiceCatalogError as exc:
            diagnostics.append(
                {
                    "status": exc.code,
                    "path": reference.reference_asset_id,
                }
            )
            continue
        group_name = reference.training_task or resolved.parent.name
        grouped[(group_name, str(resolved.parent))].append((reference, resolved))

    groups: list[dict[str, Any]] = []
    for (group_name, group_path), items in sorted(
        grouped.items(),
        key=lambda item: (item[0][0].casefold(), item[0][1].casefold()),
    )[:limit]:
        groups.append(
            {
                "id": group_name,
                "name": group_name,
                "path": group_path,
                "audio_count": len(items),
                "samples": [str(path) for _, path in items[:5]],
                "sample_details": [
                    {
                        "path": str(path),
                        "text": reference.prompt_text,
                        "text_source": (
                            "catalog" if reference.prompt_text.strip() else "none"
                        ),
                    }
                    for reference, path in items[:8]
                ],
            }
        )
    return groups, diagnostics


def build_legacy_voice_candidates_view(
    service: VoiceCatalogService,
    limit: int,
) -> dict[str, Any]:
    """Return the legacy response shape without performing a second scan."""

    snapshot = service.store.load_current()
    if snapshot is None:
        return _empty_view()

    bounded_limit = max(1, min(limit, 500))
    gpt_weights, gpt_diagnostics = _weight_options(
        service,
        [item for item in snapshot.weight_artifacts if item.kind == "gpt"],
        limit=bounded_limit,
    )
    sovits_weights, sovits_diagnostics = _weight_options(
        service,
        [item for item in snapshot.weight_artifacts if item.kind == "sovits"],
        limit=bounded_limit,
    )
    groups, reference_diagnostics = _reference_groups(
        service,
        snapshot.reference_assets,
        limit=bounded_limit,
    )
    public_diagnostics = service.public_view()["diagnostics"]
    catalog_diagnostics = [
        {
            "status": str(item["code"]),
            "path": str(item["field_path"]),
        }
        for item in public_diagnostics
    ]
    gpt_sovits_ready = any(
        item.state == "ready" and item.engine_type == "gpt-sovits"
        for item in snapshot.resources
    )
    indextts_ready = any(
        item.state == "ready" and item.engine_type == "indextts"
        for item in snapshot.resources
    )
    any_ready = any(item.state == "ready" for item in snapshot.resources)
    reference_root = (
        str(next(iter(service.roots.values()))) if len(service.roots) == 1 else ""
    )
    return {
        "ready": any_ready,
        "runtimes": {},
        "reference_audio": {
            "path": reference_root,
            "exists": bool(groups),
            "is_dir": bool(groups),
            "groups": groups,
        },
        "gpt_sovits": {
            "gpt_weights": gpt_weights,
            "sovits_weights": sovits_weights,
            "diagnostics": [
                *catalog_diagnostics,
                *gpt_diagnostics,
                *sovits_diagnostics,
                *reference_diagnostics,
            ],
            "ready": gpt_sovits_ready,
        },
        "indextts": {
            "reference_audio": groups,
            "model": {
                "path": "",
                "ready": indextts_ready,
                "missing": [] if indextts_ready else ["catalog_resource"],
            },
            "diagnostics": catalog_diagnostics,
        },
    }
