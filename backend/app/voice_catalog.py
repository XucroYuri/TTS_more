"""Bounded Portable discovery and immutable voice-catalog storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
import wave
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .storage import windows_path_is_within
from .voice_matching_models import (
    CatalogSnapshot,
    ReferenceAssetRecord,
    StrictVoiceModel,
    VoiceResourceRecord,
)
from .voice_metadata_inference import (
    VoiceMetadataInferenceItem,
    VoiceMetadataInferenceResult,
    VoiceMetadataInferrer,
    VoiceMetadataUnavailable,
)


_AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
_LANGUAGE_ALIASES = {
    "中文": "zh",
    "国语": "zh",
    "普通话": "zh",
    "粤语": "yue",
    "英文": "en",
    "英语": "en",
    "日文": "ja",
    "日语": "ja",
    "韩文": "ko",
    "韩语": "ko",
}


class VoiceCatalogError(RuntimeError):
    def __init__(self, code: str, field_path: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.field_path = field_path


class CatalogDiagnostic(StrictVoiceModel):
    code: str = Field(min_length=1)
    field_path: str = ""


class ReferenceLocation(StrictVoiceModel):
    root_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)


class ReferenceMetadataOverride(StrictVoiceModel):
    character_id: str | None = Field(default=None, min_length=1, max_length=200)
    character_aliases: list[str] = Field(default_factory=list, max_length=100)
    emotion: str | None = Field(default=None, min_length=1, max_length=100)
    language: str | None = Field(default=None, min_length=1, max_length=32)
    prompt_text: str | None = Field(default=None, max_length=1000)


class ResourceMappingOverride(StrictVoiceModel):
    character_id: str = Field(min_length=1, max_length=200)
    character_aliases: list[str] = Field(default_factory=list, max_length=100)
    weight_artifact_ids: list[str] = Field(min_length=1, max_length=100)
    reference_asset_ids: list[str] = Field(min_length=1, max_length=500)


class WeightArtifact(StrictVoiceModel):
    artifact_id: str = Field(min_length=1)
    root_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    kind: Literal["gpt", "sovits"]
    character_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)


class PortableAssetScan(StrictVoiceModel):
    root_id: str = Field(min_length=1)
    weight_artifacts: list[WeightArtifact] = Field(default_factory=list)
    reference_assets: list[ReferenceAssetRecord] = Field(default_factory=list)
    reference_locations: dict[str, ReferenceLocation] = Field(default_factory=dict)
    resource_records: list[VoiceResourceRecord] = Field(default_factory=list)
    diagnostics: list[CatalogDiagnostic] = Field(default_factory=list)


class VoiceCatalogEnvelope(StrictVoiceModel):
    schema_version: int = 1
    snapshot: CatalogSnapshot
    reference_locations: dict[str, ReferenceLocation] = Field(default_factory=dict)
    inference_cache: dict[str, VoiceMetadataInferenceResult] = Field(default_factory=dict)
    metadata_overrides: dict[str, ReferenceMetadataOverride] = Field(default_factory=dict)
    resource_mappings: dict[str, ResourceMappingOverride] = Field(default_factory=dict)


class VoiceCatalogStatus(StrictVoiceModel):
    state: Literal["ready", "partial", "failed"]
    catalog_version: str | None = None
    resource_count: int = 0
    reference_count: int = 0
    weight_count: int = 0
    diagnostics: list[CatalogDiagnostic] = Field(default_factory=list)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _file_stat_fingerprint(root_id: str, relative_path: str, path: Path) -> str:
    stat = path.stat()
    value = f"{root_id}\0{relative_path}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _measured_wav_duration(path: Path) -> float | None:
    if path.suffix.casefold() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (OSError, EOFError, wave.Error):
        return None
    if rate <= 0 or frames <= 0:
        return None
    return round(frames / rate, 4)


def _normalize_language(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "zh"
    return _LANGUAGE_ALIASES.get(text, text.casefold())


def _character_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    bracket = re.match(r"^\[([^_\]]+)", stem)
    if bracket:
        return bracket.group(1).strip()
    simplified = re.sub(r"(?:[-_]e\d+)(?:[-_]s\d+)?$", "", stem, flags=re.IGNORECASE)
    return simplified.strip(" _-") or "unknown"


def _prompt_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    if "]" in stem:
        return stem.split("]", 1)[1].strip()
    return stem.strip()


class PortableAssetScanner:
    """Discover configured assets without making executable weight pairings."""

    def scan(self, root_id: str, root: Path) -> PortableAssetScan:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise VoiceCatalogError("voice_asset_root_unavailable", root_id)
        character_map_raw = _read_json_object(resolved_root / "character_map.json")
        character_map = {
            str(key): str(value).strip()
            for key, value in character_map_raw.items()
            if isinstance(value, str) and value.strip()
        }
        artifacts = self._scan_weights(root_id, resolved_root, character_map)
        references, locations, diagnostics = self._scan_references(root_id, resolved_root)
        return PortableAssetScan(
            root_id=root_id,
            weight_artifacts=artifacts,
            reference_assets=references,
            reference_locations=locations,
            resource_records=[],
            diagnostics=diagnostics,
        )

    def _scan_weights(
        self,
        root_id: str,
        root: Path,
        character_map: dict[str, str],
    ) -> list[WeightArtifact]:
        artifacts: list[WeightArtifact] = []
        for directory in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            name = directory.name.casefold()
            if not directory.is_dir() or not (
                name.startswith("gpt_weights") or name.startswith("sovits_weights")
            ):
                continue
            kind: Literal["gpt", "sovits"] = "sovits" if name.startswith("sovits") else "gpt"
            extensions = {".pth", ".safetensors"} if kind == "sovits" else {".ckpt"}
            for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold()):
                if not path.is_file() or path.suffix.casefold() not in extensions:
                    continue
                relative = path.relative_to(root).as_posix()
                character = (
                    character_map.get(relative)
                    or character_map.get(path.name)
                    or _character_from_filename(path.name)
                )
                artifacts.append(
                    WeightArtifact(
                        artifact_id=_stable_id("weight", root_id, relative),
                        root_id=root_id,
                        relative_path=relative,
                        kind=kind,
                        character_id=character,
                        fingerprint=_file_stat_fingerprint(root_id, relative, path),
                    )
                )
        return artifacts

    def _scan_references(
        self,
        root_id: str,
        root: Path,
    ) -> tuple[
        list[ReferenceAssetRecord],
        dict[str, ReferenceLocation],
        list[CatalogDiagnostic],
    ]:
        logs_root = root / "logs"
        reference_roots: list[Path] = []
        if logs_root.is_dir():
            for task_dir in sorted(logs_root.iterdir(), key=lambda item: item.name.casefold()):
                candidate = task_dir / "5-wav32k"
                if task_dir.is_dir() and candidate.is_dir():
                    reference_roots.append(candidate)
        if not reference_roots:
            legacy_root = root / "参考音频"
            if legacy_root.is_dir():
                reference_roots = [legacy_root]
        if not reference_roots:
            return [], {}, [CatalogDiagnostic(code="reference_audio_root_missing", field_path=root_id)]
        assets: list[ReferenceAssetRecord] = []
        locations: dict[str, ReferenceLocation] = {}
        diagnostics: list[CatalogDiagnostic] = []
        for reference_root in reference_roots:
            metadata = _read_json_object(reference_root / "audio_metadata.json")
            for path in sorted(reference_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
                if not path.is_file() or path.suffix.casefold() not in _AUDIO_EXTENSIONS:
                    continue
                relative_to_reference = path.relative_to(reference_root).as_posix()
                relative_to_root = path.relative_to(root).as_posix()
                raw = metadata.get(relative_to_reference, metadata.get(path.name, {}))
                item = raw if isinstance(raw, dict) else {}
                character = str(item.get("character") or _character_from_filename(path.name)).strip()
                emotion = str(item.get("emotion") or "neutral").strip()
                prompt_text = str(item.get("text_override") or item.get("text") or _prompt_from_filename(path.name)).strip()
                fingerprint = _content_fingerprint(path)
                asset_id = _stable_id("reference", root_id, relative_to_root, fingerprint)
                duration = _measured_wav_duration(path)
                if duration is None:
                    diagnostics.append(
                        CatalogDiagnostic(code="reference_duration_unavailable", field_path=asset_id)
                    )
                assets.append(
                    ReferenceAssetRecord(
                        reference_asset_id=asset_id,
                        character_id=character or "unknown",
                        language=_normalize_language(item.get("lang") or item.get("language")),
                        emotion=emotion or "neutral",
                        prompt_text=prompt_text,
                        duration_seconds=duration,
                        confirmed=False,
                        metadata_score=5 if item else 2,
                        fingerprint=fingerprint,
                        character_origin="declared" if item.get("character") else "filename",
                        character_confidence=1 if item.get("character") else 0.5,
                        emotion_origin="declared" if item.get("emotion") else "unknown",
                        emotion_confidence=1 if item.get("emotion") else 0,
                        language_origin=(
                            "declared"
                            if item.get("lang") or item.get("language")
                            else "unknown"
                        ),
                    )
                )
                locations[asset_id] = ReferenceLocation(
                    root_id=root_id,
                    relative_path=relative_to_root,
                    fingerprint=fingerprint,
                )
        return assets, locations, diagnostics


class VoiceCatalogStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.catalog_path = root / "catalog.json"

    def publish(
        self,
        snapshot: CatalogSnapshot,
        *,
        reference_locations: dict[str, ReferenceLocation | dict[str, str]],
        inference_cache: dict[str, VoiceMetadataInferenceResult] | None = None,
        metadata_overrides: dict[str, ReferenceMetadataOverride] | None = None,
        resource_mappings: dict[str, ResourceMappingOverride] | None = None,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        envelope = VoiceCatalogEnvelope(
            snapshot=snapshot,
            reference_locations={
                asset_id: ReferenceLocation.model_validate(location)
                for asset_id, location in reference_locations.items()
            },
            inference_cache=inference_cache or {},
            metadata_overrides=metadata_overrides or {},
            resource_mappings=resource_mappings or {},
        )
        temporary = self.root / f"catalog.{uuid.uuid4().hex}.tmp"
        temporary.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.catalog_path)

    def _load_envelope(self) -> VoiceCatalogEnvelope | None:
        if not self.catalog_path.is_file():
            return None
        return VoiceCatalogEnvelope.model_validate_json(
            self.catalog_path.read_text(encoding="utf-8")
        )

    def load_current(self) -> CatalogSnapshot | None:
        envelope = self._load_envelope()
        return envelope.snapshot if envelope else None

    def load_reference_locations(self) -> dict[str, ReferenceLocation]:
        envelope = self._load_envelope()
        return envelope.reference_locations if envelope else {}

    def load_inference_cache(self) -> dict[str, VoiceMetadataInferenceResult]:
        envelope = self._load_envelope()
        return envelope.inference_cache if envelope else {}

    def load_metadata_overrides(self) -> dict[str, ReferenceMetadataOverride]:
        envelope = self._load_envelope()
        return envelope.metadata_overrides if envelope else {}

    def load_resource_mappings(self) -> dict[str, ResourceMappingOverride]:
        envelope = self._load_envelope()
        return envelope.resource_mappings if envelope else {}


class VoiceCatalogService:
    def __init__(
        self,
        *,
        store: VoiceCatalogStore,
        roots: dict[str, Path],
        scanner: PortableAssetScanner | None = None,
        metadata_inferrer: VoiceMetadataInferrer | None = None,
        registry: Any | None = None,
        clients: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.roots = dict(roots)
        self.scanner = scanner or PortableAssetScanner()
        self.metadata_inferrer = metadata_inferrer
        self.registry = registry
        self.clients = clients or {}
        self._sync_lock = threading.Lock()
        current = self.store.load_current()
        self._last_status = VoiceCatalogStatus(
            state="ready" if current else "failed",
            catalog_version=current.version if current else None,
            resource_count=len(current.resources) if current else 0,
            reference_count=len(current.reference_assets) if current else 0,
        )

    def sync(self) -> VoiceCatalogStatus:
        with self._sync_lock:
            diagnostics: list[CatalogDiagnostic] = []
            scans: list[PortableAssetScan] = []
            for root_id, root in sorted(self.roots.items()):
                try:
                    scans.append(self.scanner.scan(root_id, root))
                except (OSError, VoiceCatalogError):
                    diagnostics.append(
                        CatalogDiagnostic(
                            code="voice_asset_root_unavailable",
                            field_path=root_id,
                        )
                    )
            previous = self.store.load_current()
            if not scans:
                self._last_status = VoiceCatalogStatus(
                    state="failed",
                    catalog_version=previous.version if previous else None,
                    resource_count=len(previous.resources) if previous else 0,
                    reference_count=len(previous.reference_assets) if previous else 0,
                    diagnostics=diagnostics,
                )
                return self._last_status
            references = [asset for scan in scans for asset in scan.reference_assets]
            resources = [resource for scan in scans for resource in scan.resource_records]
            locations = {
                asset_id: location
                for scan in scans
                for asset_id, location in scan.reference_locations.items()
            }
            diagnostics.extend(item for scan in scans for item in scan.diagnostics)
            inference_cache = self.store.load_inference_cache()
            references, inference_cache, inference_diagnostics = self._apply_metadata_inference(
                references,
                locations,
                inference_cache,
            )
            diagnostics.extend(inference_diagnostics)
            metadata_overrides = self.store.load_metadata_overrides()
            references = self._apply_reference_overrides(references, metadata_overrides)
            resource_mappings = self.store.load_resource_mappings()
            resources, resource_diagnostics = self._merge_comfyui_resources(
                scans,
                references,
                locations,
                resource_mappings,
            )
            diagnostics.extend(resource_diagnostics)
            version_payload = {
                "weights": [
                    artifact.model_dump(mode="json")
                    for scan in scans
                    for artifact in scan.weight_artifacts
                ],
                "references": [asset.model_dump(mode="json") for asset in references],
                "resources": [resource.model_dump(mode="json") for resource in resources],
            }
            version = hashlib.sha256(
                json.dumps(
                    version_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            snapshot = CatalogSnapshot(
                version=version,
                resources=resources,
                reference_assets=references,
            )
            self.store.publish(
                snapshot,
                reference_locations=locations,
                inference_cache=inference_cache,
                metadata_overrides=metadata_overrides,
                resource_mappings=resource_mappings,
            )
            self._last_status = VoiceCatalogStatus(
                state="partial" if diagnostics else "ready",
                catalog_version=version,
                resource_count=len(resources),
                reference_count=len(references),
                weight_count=sum(len(scan.weight_artifacts) for scan in scans),
                diagnostics=diagnostics,
            )
            return self._last_status

    def public_view(self) -> dict[str, object]:
        snapshot = self.store.load_current()
        return {
            "state": self._last_status.state,
            "catalog_version": snapshot.version if snapshot else None,
            "resources": (
                [item.model_dump(mode="json") for item in snapshot.resources]
                if snapshot
                else []
            ),
            "references": (
                [item.model_dump(mode="json") for item in snapshot.reference_assets]
                if snapshot
                else []
            ),
            "diagnostics": [
                item.model_dump(mode="json") for item in self._last_status.diagnostics
            ],
            "counts": {
                "resources": len(snapshot.resources) if snapshot else 0,
                "references": len(snapshot.reference_assets) if snapshot else 0,
                "weights": self._last_status.weight_count,
            },
        }

    def validate_selection(self, selection: "VoiceSelectionSnapshot") -> None:
        snapshot = self.store.load_current()
        if snapshot is None or snapshot.version != selection.catalog_version:
            raise VoiceCatalogError("voice_asset_changed", "catalog_version")
        resource = next(
            (item for item in snapshot.resources if item.resource_id == selection.resource_id),
            None,
        )
        reference = next(
            (
                item
                for item in snapshot.reference_assets
                if item.reference_asset_id == selection.reference_asset_id
            ),
            None,
        )
        if (
            resource is None
            or reference is None
            or resource.state != "ready"
            or selection.reference_asset_id not in resource.reference_asset_ids
            or resource.fingerprint != selection.resource_fingerprint
            or reference.fingerprint != selection.reference_fingerprint
        ):
            raise VoiceCatalogError("voice_asset_changed", "selection")

    def stage_reference(
        self,
        project_id: str,
        selection: "VoiceSelectionSnapshot",
        project_store: "ProjectStore",
    ) -> Path:
        self.validate_selection(selection)
        source = self.resolve_reference(selection.reference_asset_id)
        target_dir = project_store.project_reference_audio_dir(project_id) / "matched"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.casefold() or ".wav"
        target = target_dir / f"{selection.reference_fingerprint}{suffix}"
        if not target.is_file():
            temporary = target_dir / f"{target.name}.{uuid.uuid4().hex}.tmp"
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        return target

    def _apply_metadata_inference(
        self,
        references: list[ReferenceAssetRecord],
        locations: dict[str, ReferenceLocation],
        cache: dict[str, VoiceMetadataInferenceResult],
    ) -> tuple[
        list[ReferenceAssetRecord],
        dict[str, VoiceMetadataInferenceResult],
        list[CatalogDiagnostic],
    ]:
        if self.metadata_inferrer is None:
            return references, cache, []
        pending: list[VoiceMetadataInferenceItem] = []
        cached_by_asset: dict[str, VoiceMetadataInferenceResult] = {}
        for asset in references:
            needs_inference = (
                asset.character_origin in {"filename", "unknown"}
                or asset.emotion_origin == "unknown"
                or asset.language_origin == "unknown"
            )
            if not needs_inference:
                continue
            location = locations.get(asset.reference_asset_id)
            if location is None:
                continue
            cache_key = _stable_id(
                "inference",
                asset.fingerprint,
                self.metadata_inferrer.provider_id,
            )
            cached = cache.get(cache_key)
            if cached is not None:
                cached_by_asset[asset.reference_asset_id] = cached
                continue
            pending.append(
                VoiceMetadataInferenceItem(
                    asset_id=asset.reference_asset_id,
                    filename=Path(location.relative_path).name,
                    prompt_text=asset.prompt_text,
                    declared_character=(
                        asset.character_id if asset.character_origin == "declared" else None
                    ),
                    declared_emotion=(
                        asset.emotion if asset.emotion_origin == "declared" else None
                    ),
                    declared_language=(
                        asset.language if asset.language_origin == "declared" else None
                    ),
                )
            )
        if pending:
            try:
                inferred = self.metadata_inferrer.infer(pending)
            except VoiceMetadataUnavailable:
                return references, cache, [
                    CatalogDiagnostic(
                        code="voice_metadata_inference_unavailable",
                        field_path="metadata",
                    )
                ]
            for result in inferred:
                asset = next(
                    item for item in references if item.reference_asset_id == result.asset_id
                )
                cache_key = _stable_id(
                    "inference",
                    asset.fingerprint,
                    self.metadata_inferrer.provider_id,
                )
                cache[cache_key] = result
                cached_by_asset[result.asset_id] = result
        output: list[ReferenceAssetRecord] = []
        for asset in references:
            result = cached_by_asset.get(asset.reference_asset_id)
            if result is None:
                output.append(asset)
                continue
            updates: dict[str, object] = {}
            if (
                asset.character_origin in {"filename", "unknown"}
                and result.character
                and result.character_confidence >= 0.90
            ):
                updates.update(
                    character_id=result.character,
                    character_origin="inferred",
                    character_confidence=result.character_confidence,
                )
            if (
                asset.emotion_origin == "unknown"
                and result.emotion
                and result.emotion_confidence >= 0.75
            ):
                updates.update(
                    emotion=result.emotion,
                    emotion_origin="inferred",
                    emotion_confidence=result.emotion_confidence,
                )
            if asset.language_origin == "unknown" and result.language:
                updates.update(language=result.language, language_origin="inferred")
            output.append(asset.model_copy(update=updates))
        return output, cache, []

    @staticmethod
    def _apply_reference_overrides(
        references: list[ReferenceAssetRecord],
        overrides: dict[str, ReferenceMetadataOverride],
    ) -> list[ReferenceAssetRecord]:
        output: list[ReferenceAssetRecord] = []
        for reference in references:
            override = overrides.get(reference.reference_asset_id)
            if override is None:
                output.append(reference)
                continue
            updates: dict[str, object] = {"confirmed": True, "metadata_score": 5}
            if override.character_id is not None:
                updates.update(
                    character_id=override.character_id,
                    character_aliases=override.character_aliases,
                    character_origin="confirmed",
                    character_confidence=1,
                )
            if override.emotion is not None:
                updates.update(
                    emotion=override.emotion,
                    emotion_origin="confirmed",
                    emotion_confidence=1,
                )
            if override.language is not None:
                updates.update(language=override.language, language_origin="confirmed")
            if override.prompt_text is not None:
                updates["prompt_text"] = override.prompt_text
            output.append(reference.model_copy(update=updates))
        return output

    def _merge_comfyui_resources(
        self,
        scans: list[PortableAssetScan],
        references: list[ReferenceAssetRecord],
        locations: dict[str, ReferenceLocation],
        resource_mappings: dict[str, ResourceMappingOverride],
    ) -> tuple[list[VoiceResourceRecord], list[CatalogDiagnostic]]:
        if self.registry is None:
            return [], []
        artifacts = [artifact for scan in scans for artifact in scan.weight_artifacts]
        artifacts_by_name: dict[str, list[WeightArtifact]] = {}
        for artifact in artifacts:
            artifacts_by_name.setdefault(Path(artifact.relative_path).name.casefold(), []).append(artifact)
        references_by_name: dict[str, list[ReferenceAssetRecord]] = {}
        for reference in references:
            location = locations.get(reference.reference_asset_id)
            if location is not None:
                references_by_name.setdefault(
                    Path(location.relative_path).name.casefold(), []
                ).append(reference)
        resources: list[VoiceResourceRecord] = []
        diagnostics: list[CatalogDiagnostic] = []
        mapped_artifact_ids: set[str] = set()
        for endpoint in sorted(self.registry.services, key=lambda item: item.service_id):
            if not endpoint.enabled or endpoint.api_contract != "comfyui-tts-audio-suite-v1":
                continue
            resource_id = str(endpoint.default_params.get("resource_id") or "").strip()
            if not resource_id:
                diagnostics.append(
                    CatalogDiagnostic(
                        code="comfyui_resource_mapping_missing",
                        field_path=endpoint.service_id,
                    )
                )
                continue
            client = self.clients.get(endpoint.service_id)
            if client is None or not hasattr(client, "capabilities"):
                diagnostics.append(
                    CatalogDiagnostic(
                        code="comfyui_capabilities_unavailable",
                        field_path=endpoint.service_id,
                    )
                )
                continue
            try:
                capabilities = client.capabilities()
            except Exception:
                diagnostics.append(
                    CatalogDiagnostic(
                        code="comfyui_capabilities_unavailable",
                        field_path=endpoint.service_id,
                    )
                )
                continue
            raw_resources = capabilities.get("resources", []) if isinstance(capabilities, dict) else []
            raw = next(
                (
                    item
                    for item in raw_resources
                    if isinstance(item, dict) and item.get("resource_id") == resource_id
                ),
                None,
            )
            if raw is None:
                diagnostics.append(
                    CatalogDiagnostic(
                        code="comfyui_resource_unregistered",
                        field_path=resource_id,
                    )
                )
                continue
            character = str(
                raw.get("character")
                or endpoint.default_params.get("character_id")
                or endpoint.default_params.get("character")
                or ""
            ).strip()
            aliases_value = raw.get("aliases", endpoint.default_params.get("character_aliases", []))
            aliases = (
                [str(value).strip() for value in aliases_value if str(value).strip()]
                if isinstance(aliases_value, list)
                else []
            )
            artifact_ids: list[str] = []
            for key in ("gpt_weight", "gpt_weights_path", "sovits_weight", "sovits_weights_path"):
                value = raw.get(key, endpoint.default_params.get(key))
                if not value:
                    continue
                matches = artifacts_by_name.get(Path(str(value)).name.casefold(), [])
                if len(matches) == 1:
                    artifact_ids.append(matches[0].artifact_id)
                    mapped_artifact_ids.add(matches[0].artifact_id)
            reference_ids: list[str] = []
            explicit_reference_ids = raw.get(
                "reference_asset_ids",
                endpoint.default_params.get("reference_asset_ids", []),
            )
            if isinstance(explicit_reference_ids, list):
                known_ids = {item.reference_asset_id for item in references}
                reference_ids.extend(
                    str(value) for value in explicit_reference_ids if str(value) in known_ids
                )
            for key in ("reference_audio", "ref_audio_path", "prompt_audio_path"):
                value = raw.get(key, endpoint.default_params.get(key))
                if not value:
                    continue
                matches = references_by_name.get(Path(str(value)).name.casefold(), [])
                if len(matches) == 1:
                    reference_ids.append(matches[0].reference_asset_id)
            reference_ids = list(dict.fromkeys(reference_ids))
            manual_mapping = resource_mappings.get(resource_id)
            if manual_mapping is not None:
                known_artifacts = {item.artifact_id for item in artifacts}
                known_references = {item.reference_asset_id for item in references}
                character = manual_mapping.character_id
                aliases = list(manual_mapping.character_aliases)
                artifact_ids = [
                    item
                    for item in manual_mapping.weight_artifact_ids
                    if item in known_artifacts
                ]
                mapped_artifact_ids.update(artifact_ids)
                reference_ids = [
                    item
                    for item in manual_mapping.reference_asset_ids
                    if item in known_references
                ]
            declared_ready = bool(raw.get("ready"))
            complete_mapping = bool(character and reference_ids and artifact_ids)
            state: Literal["ready", "unavailable", "incompatible", "reload_required"]
            if not declared_ready:
                state = "reload_required"
            elif not complete_mapping:
                state = "incompatible"
                diagnostics.append(
                    CatalogDiagnostic(
                        code="comfyui_resource_contract_incomplete",
                        field_path=resource_id,
                    )
                )
            else:
                state = "ready"
            safe_fingerprint_payload = {
                "resource_id": resource_id,
                "engine": raw.get("engine", "gpt-sovits"),
                "ready": declared_ready,
                "character": character,
                "aliases": aliases,
                "artifact_ids": sorted(set(artifact_ids)),
                "reference_ids": reference_ids,
                "contract_version": capabilities.get("contract_version"),
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    safe_fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            resources.append(
                VoiceResourceRecord(
                    resource_id=resource_id,
                    character_id=character or "unknown",
                    character_aliases=aliases,
                    reference_asset_ids=reference_ids,
                    languages=[
                        reference.language
                        for reference in references
                        if reference.reference_asset_id in reference_ids
                    ],
                    confirmed=complete_mapping,
                    metadata_score=5 if complete_mapping else 0,
                    engine_type=str(raw.get("engine") or "gpt-sovits"),
                    state=state,
                    service_id=endpoint.service_id,
                    weight_artifact_ids=sorted(set(artifact_ids)),
                    mapping_origin=(
                        "manual"
                        if manual_mapping is not None
                        else ("plugin" if raw.get("character") else "declared")
                    ),
                    fingerprint=fingerprint,
                )
            )
        for artifact in artifacts:
            if artifact.artifact_id not in mapped_artifact_ids:
                diagnostics.append(
                    CatalogDiagnostic(
                        code="voice_weight_unregistered",
                        field_path=artifact.artifact_id,
                    )
                )
        return resources, diagnostics

    def set_reference_override(
        self,
        asset_id: str,
        override: ReferenceMetadataOverride,
    ) -> ReferenceAssetRecord:
        snapshot = self.store.load_current()
        if snapshot is None:
            raise VoiceCatalogError("voice_catalog_unavailable", "catalog")
        if not any(item.reference_asset_id == asset_id for item in snapshot.reference_assets):
            raise VoiceCatalogError("voice_reference_not_found", "asset_id")
        overrides = self.store.load_metadata_overrides()
        overrides[asset_id] = override
        updated_references = self._apply_reference_overrides(snapshot.reference_assets, overrides)
        version = hashlib.sha256(
            json.dumps(
                {
                    "previous_version": snapshot.version,
                    "references": [
                        item.model_dump(mode="json") for item in updated_references
                    ],
                    "overrides": {
                        key: value.model_dump(mode="json")
                        for key, value in sorted(overrides.items())
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        updated_snapshot = snapshot.model_copy(
            update={"version": version, "reference_assets": updated_references}
        )
        self.store.publish(
            updated_snapshot,
            reference_locations=self.store.load_reference_locations(),
            inference_cache=self.store.load_inference_cache(),
            metadata_overrides=overrides,
            resource_mappings=self.store.load_resource_mappings(),
        )
        return next(item for item in updated_references if item.reference_asset_id == asset_id)

    def set_resource_mapping(
        self,
        resource_id: str,
        mapping: ResourceMappingOverride,
    ) -> None:
        snapshot = self.store.load_current()
        if snapshot is None:
            raise VoiceCatalogError("voice_catalog_unavailable", "catalog")
        mappings = self.store.load_resource_mappings()
        mappings[resource_id] = mapping
        self.store.publish(
            snapshot,
            reference_locations=self.store.load_reference_locations(),
            inference_cache=self.store.load_inference_cache(),
            metadata_overrides=self.store.load_metadata_overrides(),
            resource_mappings=mappings,
        )

    def resolve_reference(self, asset_id: str) -> Path:
        location = self.store.load_reference_locations().get(asset_id)
        if location is None:
            raise VoiceCatalogError("voice_reference_not_found", "asset_id")
        root = self.roots.get(location.root_id)
        if root is None:
            raise VoiceCatalogError("voice_asset_root_unavailable", location.root_id)
        try:
            resolved_root = root.resolve(strict=True)
            candidate = (resolved_root / location.relative_path).resolve(strict=False)
        except OSError as exc:
            raise VoiceCatalogError("voice_reference_not_found", "asset_id") from exc
        if not windows_path_is_within(candidate, resolved_root):
            raise VoiceCatalogError("voice_asset_path_unsafe", "relative_path")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise VoiceCatalogError("voice_reference_not_found", "asset_id") from exc
        if _content_fingerprint(resolved) != location.fingerprint:
            raise VoiceCatalogError("voice_asset_changed", "fingerprint")
        return resolved


def configured_voice_asset_roots(registry: Any) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    identities: set[str] = set()
    for endpoint in sorted(registry.services, key=lambda item: item.service_id):
        raw = str(endpoint.default_params.get("voice_asset_root") or "").strip()
        if not raw:
            continue
        path = Path(raw)
        identity = os.path.normcase(os.path.abspath(os.fspath(path)))
        if identity in identities:
            continue
        roots[f"service-{endpoint.service_id}"] = path
        identities.add(identity)
    fallback = os.environ.get("TTS_MORE_GPT_SOVITS_PORTABLE_ROOT", "").strip()
    if fallback:
        path = Path(fallback)
        identity = os.path.normcase(os.path.abspath(os.fspath(path)))
        if identity not in identities:
            roots["portable-env"] = path
    return roots
