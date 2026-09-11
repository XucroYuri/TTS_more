"""Server-authoritative voice catalog, recommendation, and selection routes."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .models import (
    Character,
    ProjectCharacter,
    ProjectCharacterMode,
    ProviderType,
    ScriptLine,
    ScriptProject,
    VoiceBinding,
)
from .role_library import (
    candidate_to_character,
    match_project_characters,
    resolve_project_characters,
    slugify_role_name,
)
from .role_mapping_document import RoleMappingRule, load_role_mapping_document, upsert_role_mapping_document
from .storage import ProjectStore
from .voice_catalog import (
    ReferenceMetadataOverride,
    ResourceMappingOverride,
    VoiceCatalogError,
    VoiceCatalogService,
)
from .voice_matching import (
    estimate_target_duration,
    rank_fuzzy_folder_candidates,
    rank_voice_candidates,
)
from .voice_matching_models import (
    CatalogSnapshot,
    VoiceCandidate,
    VoiceMatchRequest,
    VoiceRecommendation,
    VoiceSelectionSnapshot,
    WeightArtifactRecord,
)


_GENERIC_TAGS = {"generic", "通用", "通用角色", "路人"}
_EMOTION_ALIASES = {
    "开心": "happy",
    "高兴": "happy",
    "愤怒": "angry",
    "生气": "angry",
    "悲伤": "sad",
    "难过": "sad",
    "惊喜": "惊喜",
    "惊讶": "surprised",
    "恐惧": "fearful",
    "害怕": "fearful",
    "中性": "neutral",
    "平静": "neutral",
}


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class VoiceRecommendationRequest(_StrictRequest):
    line_ids: list[str] = Field(default_factory=list, max_length=500)
    apply_automatic: bool = False


class VoiceSelectionRequest(_StrictRequest):
    candidate_id: str = Field(min_length=1)


def _line_and_character(
    project: ScriptProject,
    line_id: str,
    store: ProjectStore,
):
    line = next((item for item in project.lines if item.id == line_id), None)
    if line is None:
        raise HTTPException(status_code=404, detail="line not found")
    library = store.load_characters()
    role_mappings = load_role_mapping_document(store.role_mapping_path())
    mappings = match_project_characters(
        project,
        library,
        role_mappings=role_mappings,
    )
    mapping = next(
        (item for item in mappings if item.project_character_id == line.character_id),
        None,
    )
    if mapping is None:
        return line, None
    has_snapshot = (
        mapping.mode == ProjectCharacterMode.SNAPSHOT
        and mapping.character_snapshot is not None
    )
    has_library_character = bool(
        mapping.library_character_id
        and any(item.id == mapping.library_character_id for item in library)
    )
    if not has_snapshot and not has_library_character:
        return line, None
    characters = resolve_project_characters(
        project,
        library,
        role_mappings=role_mappings,
    )
    character = next((item for item in characters if item.id == line.character_id), None)
    return line, character


def _normalized_emotion(note: str) -> str:
    value = note.strip().casefold()
    if not value:
        return "neutral"
    return _EMOTION_ALIASES.get(value, value)


def _recommend_line(
    project: ScriptProject,
    line_id: str,
    store: ProjectStore,
    service: VoiceCatalogService,
) -> VoiceRecommendation:
    snapshot = service.store.load_current()
    if snapshot is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "voice_assets_unavailable",
                "stage": "voice_recommendation",
            },
        )
    line, character = _line_and_character(project, line_id, store)
    project_character = next(
        (
            item
            for item in project.project_characters
            if item.project_character_id == line.character_id
        ),
        None,
    )
    language = line.language or project.default_language or "zh"
    emotion = _normalized_emotion(line.note)
    estimate = estimate_target_duration(line.text, language, emotion)
    aliases: list[str] = [project_character.name] if project_character is not None else []
    is_generic = False
    if character is not None:
        aliases = list(
            dict.fromkeys(
                [
                    *aliases,
                    character.name,
                    *character.aliases,
                    *character.nicknames,
                    *character.match_names,
                ]
            )
        )
        is_generic = any(tag.strip().casefold() in _GENERIC_TAGS for tag in character.tags)
    match_request = VoiceMatchRequest(
        line_id=line.id,
        character_id=line.character_id,
        character_aliases=aliases,
        is_generic=is_generic,
        text=line.text,
        language=language,
        emotion=emotion,
        target_duration_seconds=max(0.001, estimate.target_seconds),
    )
    recommendation = (
        rank_voice_candidates(match_request, snapshot)
        if character is not None
        else VoiceRecommendation(
            line_id=line.id,
            catalog_version=snapshot.version,
            blockers=["no_role_mapping"],
        )
    )
    if not recommendation.candidates:
        fuzzy = rank_fuzzy_folder_candidates(match_request, snapshot)
        if fuzzy.candidates:
            recommendation = fuzzy
    return recommendation.model_copy(
        update={
            "candidates": [
                candidate.model_copy(update={"catalog_version": snapshot.version})
                for candidate in recommendation.candidates
            ]
        }
    )


def _identity_key(value: object) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _find_character_for_confirmed_folder(
    characters: list[Character],
    training_task: str,
    asset_identities: list[str],
) -> Character | None:
    task_key = _identity_key(training_task)
    for character in characters:
        if task_key and task_key in {
            _identity_key(value) for value in character.match_names
        }:
            return character
    identity_keys = {_identity_key(value) for value in asset_identities}
    identity_keys.discard("")
    for character in characters:
        values = [
            character.id,
            character.name,
            *character.aliases,
            *character.nicknames,
            *character.match_names,
        ]
        if identity_keys.intersection(_identity_key(value) for value in values):
            return character
    return None


def _character_from_confirmed_folder(
    project_character: ProjectCharacter,
    candidate: VoiceCandidate,
    snapshot: CatalogSnapshot,
    service: VoiceCatalogService,
) -> Character:
    if (
        not candidate.training_task
        or not candidate.gpt_weight_artifact_id
        or not candidate.sovits_weight_artifact_id
    ):
        raise HTTPException(status_code=409, detail="fuzzy candidate has no complete training task")
    resource = next(
        (item for item in snapshot.resources if item.resource_id == candidate.resource_id),
        None,
    )
    selected_reference = next(
        (
            item
            for item in snapshot.reference_assets
            if item.reference_asset_id == candidate.reference_asset_id
        ),
        None,
    )
    if resource is None or selected_reference is None:
        raise HTTPException(status_code=409, detail="fuzzy candidate assets are unavailable")
    pair = _weight_pair_for_candidate(snapshot, resource, selected_reference, candidate)
    if pair is None:
        raise HTTPException(status_code=409, detail="fuzzy candidate weight pair is unavailable")
    gpt_path = service.resolve_weight(pair[0].artifact_id)
    sovits_path = service.resolve_weight(pair[1].artifact_id)
    reference_path = service.resolve_reference(selected_reference.reference_asset_id)
    task_references = [
        item
        for item in snapshot.reference_assets
        if item.reference_asset_id in resource.reference_asset_ids
        and item.training_task == candidate.training_task
    ]
    asset_identities = list(
        dict.fromkeys(
            [
                selected_reference.character_id,
                *selected_reference.character_aliases,
                *(item.character_id for item in task_references),
                *(alias for item in task_references for alias in item.character_aliases),
            ]
        )
    )
    role_id = slugify_role_name(candidate.training_task)
    character = candidate_to_character(
        {
            "id": role_id,
            "name": selected_reference.character_id or project_character.name,
            "aliases": list(
                dict.fromkeys([project_character.name, project_character.project_character_id, *asset_identities])
            ),
            "match_names": [candidate.training_task],
            "logs_id": role_id,
            "logs_name": candidate.training_task,
            "service_id": resource.service_id or "local-gpt-sovits",
            "gpt_weights": [str(gpt_path)],
            "sovits_weights": [str(sovits_path)],
            "recommended_gpt_weights_path": str(gpt_path),
            "recommended_sovits_weights_path": str(sovits_path),
            "reference_audio_groups": [
                {
                    "id": f"{role_id}-confirmed-folder",
                    "name": candidate.training_task,
                    "paths": [str(reference_path)],
                    "samples": [
                        {
                            "path": str(reference_path),
                            "text": selected_reference.prompt_text,
                            "text_source": "manual",
                            "duration_seconds": selected_reference.duration_seconds,
                        }
                    ],
                }
            ],
        }
    )
    mapping = {
        "training_task": candidate.training_task,
        "resource_id": candidate.resource_id,
        "root_id": pair[0].root_id,
        "gpt_weight_artifact_id": pair[0].artifact_id,
        "sovits_weight_artifact_id": pair[1].artifact_id,
        "reference_asset_id": selected_reference.reference_asset_id,
        "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "confirmed_for_project_role": project_character.name,
    }
    return character.model_copy(
        update={
            "source_assets": {
                **character.source_assets,
                "folder_mappings": [mapping],
            }
        }
    )


def _merge_confirmed_folder_character(
    existing: Character,
    candidate: Character,
) -> Character:
    existing_mappings = existing.source_assets.get("folder_mappings", [])
    candidate_mappings = candidate.source_assets.get("folder_mappings", [])
    return candidate.model_copy(
        update={
            "id": existing.id,
            "aliases": list(dict.fromkeys([*existing.aliases, *candidate.aliases])),
            "nicknames": list(dict.fromkeys([*existing.nicknames, *candidate.nicknames])),
            "match_names": list(dict.fromkeys([*existing.match_names, *candidate.match_names])),
            "avatar_path": existing.avatar_path or candidate.avatar_path,
            "source_assets": {
                **existing.source_assets,
                **candidate.source_assets,
                "folder_mappings": [*existing_mappings, *candidate_mappings],
            },
        }
    )


def _selection_for_candidate(
    line: ScriptLine,
    candidate: VoiceCandidate,
    source: Literal["automatic", "manual"],
    service: VoiceCatalogService,
) -> VoiceSelectionSnapshot:
    snapshot = service.store.load_current()
    if snapshot is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "voice_assets_unavailable",
                "stage": "voice_recommendation",
            },
        )
    resource = next(item for item in snapshot.resources if item.resource_id == candidate.resource_id)
    reference = next(
        item
        for item in snapshot.reference_assets
        if item.reference_asset_id == candidate.reference_asset_id
    )
    weight_pair = _weight_pair_for_candidate(snapshot, resource, reference, candidate)
    inference_parameters: dict[str, object] = {"engine": resource.engine_type}
    if weight_pair is not None:
        gpt_weight, sovits_weight = weight_pair
        inference_parameters.update(
            {
                "training_task": gpt_weight.training_task,
                "gpt_weight_artifact_id": gpt_weight.artifact_id,
                "sovits_weight_artifact_id": sovits_weight.artifact_id,
                "gpt_weight_fingerprint": gpt_weight.fingerprint,
                "sovits_weight_fingerprint": sovits_weight.fingerprint,
            }
        )
    return VoiceSelectionSnapshot(
        catalog_version=snapshot.version,
        candidate_id=candidate.candidate_id,
        resource_id=resource.resource_id,
        reference_asset_id=reference.reference_asset_id,
        score=candidate.score,
        speed_factor=candidate.speed_factor,
        line_id=line.id,
        source=source,
        resource_fingerprint=resource.fingerprint,
        reference_fingerprint=reference.fingerprint,
        emotion=reference.emotion,
        target_duration_seconds=candidate.target_duration_seconds,
        prompt_text=reference.prompt_text,
        reference_language=reference.language,
        text_language=line.language or "zh",
        inference_parameters=inference_parameters,
    )


def _weight_pair_for_candidate(
    snapshot: CatalogSnapshot,
    resource,
    reference,
    candidate: VoiceCandidate,
) -> tuple[WeightArtifactRecord, WeightArtifactRecord] | None:
    artifact_ids = (
        candidate.gpt_weight_artifact_id,
        candidate.sovits_weight_artifact_id,
    )
    if artifact_ids == (None, None):
        return None
    if None in artifact_ids or not resource.supports_dynamic_weights:
        raise HTTPException(status_code=409, detail="dynamic weight pair is incomplete")
    artifacts = {item.artifact_id: item for item in snapshot.weight_artifacts}
    gpt_weight = artifacts.get(artifact_ids[0])
    sovits_weight = artifacts.get(artifact_ids[1])
    if (
        gpt_weight is None
        or sovits_weight is None
        or gpt_weight.kind != "gpt"
        or sovits_weight.kind != "sovits"
    ):
        raise HTTPException(status_code=409, detail="dynamic weight pair is unavailable")
    if (
        gpt_weight.root_id != sovits_weight.root_id
        or gpt_weight.training_task != sovits_weight.training_task
        or candidate.training_task != gpt_weight.training_task
        or reference.reference_asset_id not in resource.reference_asset_ids
        or reference.training_task != gpt_weight.training_task
        or gpt_weight.root_id not in resource.compatible_root_ids
    ):
        raise HTTPException(status_code=409, detail="dynamic weight pair scope mismatch")
    return gpt_weight, sovits_weight


def _binding_for_selection(
    selection: VoiceSelectionSnapshot,
    service_id: str,
    staged_reference: str,
    weight_pair: tuple[WeightArtifactRecord, WeightArtifactRecord] | None = None,
) -> VoiceBinding:
    dynamic_config: dict[str, object] = {}
    if weight_pair is not None:
        gpt_weight, sovits_weight = weight_pair
        dynamic_config = {
            "training_task": gpt_weight.training_task,
            "voice_asset_root_id": gpt_weight.root_id,
            "gpt_weights_relative_path": gpt_weight.relative_path,
            "sovits_weights_relative_path": sovits_weight.relative_path,
        }
    return VoiceBinding(
        binding_id=f"voice-match-{selection.line_id}",
        provider_type=ProviderType.GPT_SOVITS,
        service_id=service_id,
        capabilities=["trained_weights_voice", "reference_audio_voice", "wav_output"],
        config={
            "resource_id": selection.resource_id,
            "reference_audio": staged_reference,
            "ref_audio_path": staged_reference,
            "prompt_text": selection.prompt_text,
            "prompt_lang": selection.reference_language,
            "text_lang": selection.text_language,
            "speed_factor": selection.speed_factor,
            "engine": selection.inference_parameters.get("engine", "gpt-sovits"),
            "_voice_catalog_version": selection.catalog_version,
            "_voice_resource_fingerprint": selection.resource_fingerprint,
            "_voice_reference_fingerprint": selection.reference_fingerprint,
            **dynamic_config,
        },
    )


def _replace_line(project: ScriptProject, line: ScriptLine) -> None:
    project.lines = [line if item.id == line.id else item for item in project.lines]
    for revision in project.parse_revisions:
        if revision.revision_id == project.active_parse_revision_id:
            revision.lines = [line if item.id == line.id else item for item in revision.lines]


def _save_candidate(
    project_id: str,
    project: ScriptProject,
    line: ScriptLine,
    candidate: VoiceCandidate,
    source: Literal["automatic", "manual"],
    store: ProjectStore,
    service: VoiceCatalogService,
) -> VoiceSelectionSnapshot:
    selection = _selection_for_candidate(line, candidate, source, service)
    snapshot = service.store.load_current()
    resource = next(item for item in snapshot.resources if item.resource_id == selection.resource_id)
    reference = next(
        item
        for item in snapshot.reference_assets
        if item.reference_asset_id == selection.reference_asset_id
    )
    weight_pair = _weight_pair_for_candidate(snapshot, resource, reference, candidate)
    staged = service.stage_reference(project_id, selection, store)
    binding = _binding_for_selection(
        selection,
        resource.service_id or "",
        str(staged),
        weight_pair,
    )
    _replace_line(
        project,
        line.model_copy(
            deep=True,
            update={"voice_selection": selection, "temporary_binding": binding},
        ),
    )
    return selection


def build_voice_matching_router(
    service: VoiceCatalogService,
    store: ProjectStore,
) -> APIRouter:
    router = APIRouter(tags=["voice-assets"])

    @router.post("/api/voice-assets/catalog/sync")
    def sync_catalog() -> dict[str, object]:
        return service.sync().model_dump(mode="json")

    @router.get("/api/voice-assets/catalog")
    def get_catalog() -> dict[str, object]:
        return service.public_view()

    @router.get("/api/voice-assets/references/{asset_id}/audio")
    def preview_reference(asset_id: str) -> FileResponse:
        try:
            path = service.resolve_reference(asset_id)
        except VoiceCatalogError as exc:
            status = 404 if exc.code == "voice_reference_not_found" else 409
            raise HTTPException(status_code=status, detail={"code": exc.code}) from exc
        media_type = "audio/wav" if path.suffix.casefold() == ".wav" else "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=path.name)

    @router.put("/api/voice-assets/references/{asset_id}/metadata")
    def confirm_reference_metadata(
        asset_id: str,
        request: ReferenceMetadataOverride,
    ) -> dict[str, object]:
        try:
            reference = service.set_reference_override(asset_id, request)
        except VoiceCatalogError as exc:
            status = 404 if exc.code == "voice_reference_not_found" else 409
            raise HTTPException(status_code=status, detail={"code": exc.code}) from exc
        return {"reference": reference.model_dump(mode="json")}

    @router.put("/api/voice-assets/resources/{resource_id}/mapping")
    def save_resource_mapping(
        resource_id: str,
        request: ResourceMappingOverride,
    ) -> dict[str, str]:
        try:
            service.set_resource_mapping(resource_id, request)
        except VoiceCatalogError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
        return {"status": "saved", "resource_id": resource_id}

    @router.post("/api/projects/{project_id}/voice-recommendations")
    def recommend(project_id: str, request: VoiceRecommendationRequest) -> dict[str, object]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        known_ids = [line.id for line in project.lines]
        line_ids = request.line_ids or known_ids
        if any(line_id not in known_ids for line_id in line_ids):
            raise HTTPException(status_code=404, detail="line not found")
        recommendations: list[VoiceRecommendation] = []
        changed = False
        for line_id in line_ids:
            recommendation = _recommend_line(project, line_id, store, service)
            recommendations.append(recommendation)
            if (
                request.apply_automatic
                and recommendation.candidates
                and recommendation.candidates[0].auto_fill_eligible
            ):
                line, _ = _line_and_character(project, line_id, store)
                if line.voice_selection is None or line.voice_selection.source == "automatic":
                    _save_candidate(
                        project_id,
                        project,
                        line,
                        recommendation.candidates[0],
                        "automatic",
                        store,
                        service,
                    )
                    changed = True
        if changed:
            store.save_project(project_id, project)
        return {
            "recommendations": [item.model_dump(mode="json") for item in recommendations]
        }

    @router.put("/api/projects/{project_id}/lines/{line_id}/voice-selection")
    def select(
        project_id: str,
        line_id: str,
        request: VoiceSelectionRequest,
    ) -> dict[str, object]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        recommendation = _recommend_line(project, line_id, store, service)
        candidate = next(
            (item for item in recommendation.candidates if item.candidate_id == request.candidate_id),
            None,
        )
        if candidate is None:
            raise HTTPException(status_code=409, detail="candidate is no longer eligible")
        if candidate.requires_identity_confirmation:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "voice_identity_confirmation_required",
                    "stage": "voice_selection",
                },
            )
        line, _ = _line_and_character(project, line_id, store)
        selection = _save_candidate(
            project_id,
            project,
            line,
            candidate,
            "manual",
            store,
            service,
        )
        store.save_project(project_id, project)
        return {"selection": selection.model_dump(mode="json")}

    @router.post(
        "/api/projects/{project_id}/lines/{line_id}/voice-identity-confirmation"
    )
    def confirm_fuzzy_identity(
        project_id: str,
        line_id: str,
        request: VoiceSelectionRequest,
    ) -> dict[str, object]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        recommendation = _recommend_line(project, line_id, store, service)
        candidate = next(
            (
                item
                for item in recommendation.candidates
                if item.candidate_id == request.candidate_id
            ),
            None,
        )
        if candidate is None or not candidate.requires_identity_confirmation:
            raise HTTPException(
                status_code=409,
                detail="candidate no longer requires identity confirmation",
            )
        project_character = next(
            (
                item
                for item in project.project_characters
                if item.project_character_id
                == next(line for line in project.lines if line.id == line_id).character_id
            ),
            None,
        )
        if project_character is None:
            raise HTTPException(status_code=404, detail="project character not found")
        snapshot = service.store.load_current()
        if snapshot is None or snapshot.version != candidate.catalog_version:
            raise HTTPException(status_code=409, detail="candidate catalog is stale")
        try:
            character = _character_from_confirmed_folder(
                project_character,
                candidate,
                snapshot,
                service,
            )
        except VoiceCatalogError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "field_path": exc.field_path},
            ) from exc
        existing_characters = store.load_characters()
        task_references = [
            item
            for item in snapshot.reference_assets
            if item.training_task == candidate.training_task
        ]
        existing = _find_character_for_confirmed_folder(
            existing_characters,
            candidate.training_task or "",
            [
                identity
                for item in task_references
                for identity in [item.character_id, *item.character_aliases]
            ],
        )
        if existing is not None:
            character = _merge_confirmed_folder_character(existing, character)
        characters = [item for item in existing_characters if item.id != character.id]
        characters.append(character)
        store.save_characters(characters)
        upsert_role_mapping_document(
            store.role_mapping_path(),
            RoleMappingRule(
                script_role_name=project_character.name,
                library_character_id=character.id,
                library_character_name=character.name,
                notes="在推荐音色确认时写入",
            ),
        )

        project.project_characters = [
            item.model_copy(
                update={
                    "library_character_id": character.id,
                    "mode": ProjectCharacterMode.REFERENCE,
                    "character_snapshot": None,
                    "project_binding": None,
                    "match_confidence": 1.0,
                    "match_status": "manual",
                }
            )
            if item.project_character_id == project_character.project_character_id
            else item
            for item in project.project_characters
        ]
        linked = next(
            item
            for item in project.project_characters
            if item.project_character_id == project_character.project_character_id
        )
        active_parse = next(
            (
                item
                for item in project.parse_revisions
                if item.revision_id == project.active_parse_revision_id
            ),
            None,
        )
        if active_parse is not None:
            active_parse.project_characters = project.project_characters
        store.save_project(project_id, project)
        refreshed = _recommend_line(project, line_id, store, service)
        return {
            "character": character.model_dump(mode="json"),
            "characters": [item.model_dump(mode="json") for item in characters],
            "project_character": linked.model_dump(mode="json"),
            "project": project.model_dump(mode="json"),
            "recommendation": refreshed.model_dump(mode="json"),
        }

    @router.delete("/api/projects/{project_id}/lines/{line_id}/voice-selection")
    def clear(project_id: str, line_id: str) -> dict[str, str]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        line, _ = _line_and_character(project, line_id, store)
        _replace_line(
            project,
            line.model_copy(
                deep=True,
                update={"voice_selection": None, "temporary_binding": None},
            ),
        )
        store.save_project(project_id, project)
        return {"status": "cleared"}

    return router
