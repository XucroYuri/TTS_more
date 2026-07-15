from __future__ import annotations

import json
import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel

from app.hardware import collect_local_hardware_status
from app.auth import auth_status_endpoint, install_token_middleware
from app.local_control import install_local_control
from app.models import Character, EngineName, GenerationManifest, GenerationTask, PROVIDER_ENGINE_DEFAULTS, ParseRevision, ProjectCharacter, ProjectCharacterMode, ReferenceAudioGroup, ReferenceAudioSample, ScriptProject, ScriptRevision
from app.net_guard import EgressError, scrub_error, validate_egress_url
from app.open_source_tts import OpenSourceTTSConfigureRequest, OpenSourceTTSDetectRequest, configure_open_source_tts, detect_open_source_tts, open_source_catalog
from app.portable_discovery import PortablePackageDiscoverRequest, PortablePackageRegisterRequest, discover_portable_packages, endpoint_from_portable_package, read_portable_package
from app.parser import MultiProviderParser, OpenAICompatibleProvider, ParserProviderConfig, ParserProviderUnavailable, ParserQualityError, build_parser_provider
from app.parser_config import ParserProviderUpdate, ParserProvidersUpdate, load_parser_providers, public_parser_providers, save_parser_providers
from app.queue import GenerationJobManager, ServiceGenerationQueue, build_cluster_key
from app.resources import AUDIO_SUFFIXES, collect_voice_candidates, scan_reference_audio_groups
from app.role_library import candidate_to_character, common_logs_presets, freeze_project_character, match_project_characters, referenced_projects, resolve_project_characters, scan_gpt_sovits_model_catalog_candidates, scan_logs_index_candidates, scan_logs_reference_audio_samples, scan_role_library_candidates
from app.service_config import ServiceSettingsUpdate, public_service_settings, save_service_settings
from app.services import ServiceRegistry, ServiceRouter, build_load_signature, require_remote_artifact_transfer
from app.storage import ProjectStore
from app.supervisor import ServiceSupervisor

DEFAULT_REFERENCE_AUDIO_ROOT = Path("data") / "local" / "reference-audio"
DEFAULT_DATA_ROOT = Path("data")
DEFAULT_RUNTIME_ROOT = Path("data") / ".runtime"


def _resolve_repo_lock_path(module_file: Path = Path(__file__)) -> Path:
    project_root = Path(module_file).resolve().parents[2]
    package_root = project_root.parent
    if (package_root / "package" / "tts-more-package.json").is_file():
        return package_root / "package" / "repo.lock.json"
    return project_root / "repo.lock.json"


REPO_LOCK_PATH = _resolve_repo_lock_path()
AUDIO_UPLOAD_SUFFIXES = AUDIO_SUFFIXES | {".webm", ".aac", ".opus"}
# Maximum accepted upload size for avatar / reference-audio endpoints.
MAX_UPLOAD_BYTES = int(os.environ.get("TTS_MORE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

load_dotenv(".env.local")
load_dotenv(".env")


class ParseScriptRequest(BaseModel):
    text: str


class ParserProviderTestRequest(BaseModel):
    provider: ParserProviderUpdate


class GenerateRequest(BaseModel):
    project_id: str
    tasks: list[GenerationTask]


class RoleLibraryScanRequest(BaseModel):
    limit: int = 80


class RoleLibraryImportRequest(BaseModel):
    candidate: dict[str, Any]


class ProjectCharactersUpdate(BaseModel):
    project_characters: list[ProjectCharacter]


class ScriptRevisionCreate(BaseModel):
    source_markdown: str
    summary: str = ""


class ParseRevisionCreate(BaseModel):
    script_revision_id: str


class ActivateRevisionRequest(BaseModel):
    script_revision_id: str | None = None
    parse_revision_id: str | None = None


def create_app(
    data_root: Path | str = DEFAULT_DATA_ROOT,
    reference_audio_root: Path | str = DEFAULT_REFERENCE_AUDIO_ROOT,
    services_path: Path | str | None = None,
    runtime_root: Path | str | None = None,
    parser_config_path: Path | str | None = None,
    env_path: Path | str | None = None,
    static_root: Path | str | None = None,
    controller_root: Path | str | None = None,
) -> FastAPI:
    app = FastAPI(title="TTS More Orchestrator", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Optional shared bearer token. No-op (all requests pass through) when
    # TTS_MORE_API_TOKEN is unset; enforces Authorization: Bearer <token> on
    # mutating/egress endpoints when it is set.
    install_token_middleware(app)

    store = ProjectStore(Path(data_root))
    project_root = Path(__file__).resolve().parents[2]
    parser_config_file = Path(parser_config_path) if parser_config_path else store.root / "parser_providers.json"
    env_file = Path(env_path) if env_path else project_root / ".env.local"
    load_dotenv(env_file, override=False)
    parser = _build_parser(parser_config_file)
    services_file, writable_services_file = _resolve_service_settings_paths(store.root, Path(services_path) if services_path else None)
    service_registry = _load_service_registry(services_file)
    service_router = ServiceRouter(service_registry)
    queue = ServiceGenerationQueue(service_router)
    job_manager = GenerationJobManager(queue, store)
    ref_root = Path(reference_audio_root)
    portable_controller_root = Path(controller_root) if controller_root is not None else _portable_controller_root(Path(data_root), project_root)
    supervisor = ServiceSupervisor(
        project_root=project_root,
        portable_controller_root=portable_controller_root,
        runtime_root=Path(runtime_root) if runtime_root else Path(data_root) / ".runtime",
    )

    app.state.store = store
    app.state.parser = parser
    app.state.service_registry = service_registry
    app.state.service_router = service_router
    app.state.queue = queue
    app.state.job_manager = job_manager
    app.state.reference_audio_root = ref_root
    app.state.supervisor = supervisor
    app.state.services_path = services_file
    app.state.writable_services_path = writable_services_file
    app.state.parser_config_path = parser_config_file
    app.state.env_path = env_file
    # Read at app-creation time so tests/processes can override via env.
    app.state.max_upload_bytes = int(os.environ.get("TTS_MORE_MAX_UPLOAD_BYTES", str(MAX_UPLOAD_BYTES)))

    install_local_control(
        app,
        controller_root=portable_controller_root,
        refresh_services=lambda endpoints: _apply_registry(app, ServiceRegistry(list(endpoints)), store),
    )

    @app.get("/api/auth/status")
    def auth_status() -> dict[str, Any]:
        return auth_status_endpoint()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "repos": _read_repo_lock(),
            "workers": app.state.service_router.health(),
            "reference_audio_root": str(ref_root),
        }

    @app.get("/api/services")
    def services() -> dict[str, Any]:
        return {"services": _service_health_with_supervisor(app.state.service_router, supervisor)}

    @app.get("/api/settings/services")
    def get_service_settings() -> dict[str, Any]:
        return public_service_settings(app.state.service_registry, env_file)

    @app.put("/api/settings/services")
    def put_service_settings(request: ServiceSettingsUpdate) -> dict[str, Any]:
        registry = save_service_settings(app.state.writable_services_path, env_file, request)
        if _service_mode() == "mock":
            registry = _mocked_registry(registry)
        _apply_registry(app, registry, store)
        app.state.services_path = app.state.writable_services_path
        return public_service_settings(registry, env_file)

    @app.post("/api/settings/services/reload")
    def reload_service_settings() -> dict[str, Any]:
        try:
            if services_path is None:
                app.state.services_path, app.state.writable_services_path = _resolve_service_settings_paths(store.root, None)
            registry = _load_service_registry(app.state.services_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"failed to reload services: {exc}") from exc
        _apply_registry(app, registry, store)
        return public_service_settings(registry, env_file)

    @app.get("/api/open-source-tts/catalog")
    def open_source_tts_catalog() -> dict[str, Any]:
        return {"providers": open_source_catalog(project_root)}

    @app.post("/api/open-source-tts/detect")
    def open_source_tts_detect(request: OpenSourceTTSDetectRequest) -> dict[str, Any]:
        return detect_open_source_tts(request, project_root)

    @app.post("/api/open-source-tts/configure")
    def open_source_tts_configure(request: OpenSourceTTSConfigureRequest) -> dict[str, Any]:
        registry, endpoint, detect_payload = configure_open_source_tts(
            request,
            app.state.service_registry,
            app.state.writable_services_path,
            project_root,
        )
        _apply_registry(app, registry, store)
        app.state.services_path = app.state.writable_services_path
        return {
            "service": endpoint.model_dump(mode="json"),
            "detect": detect_payload,
            "settings": public_service_settings(registry, env_file),
        }

    @app.post("/api/portable-packages/discover")
    def portable_packages_discover(request: PortablePackageDiscoverRequest) -> dict[str, Any]:
        packages = discover_portable_packages(
            portable_controller_root,
            request.roots,
            include_siblings=request.include_siblings,
        )
        return {"packages": [package.model_dump(mode="json") for package in packages]}

    @app.post("/api/portable-packages/register")
    def portable_package_register(request: PortablePackageRegisterRequest) -> dict[str, Any]:
        try:
            descriptor = read_portable_package(Path(request.package_root))
            endpoint = endpoint_from_portable_package(descriptor, request)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        services = [service for service in app.state.service_registry.services if service.service_id != endpoint.service_id]
        services.append(endpoint)
        services.sort(key=lambda service: (service.priority, service.service_id))
        registry = app.state.service_registry.with_services(services)
        registry.save(app.state.writable_services_path)
        _apply_registry(app, registry, store)
        app.state.services_path = app.state.writable_services_path
        return {
            "package": descriptor.model_dump(mode="json"),
            "service": endpoint.model_dump(mode="json"),
            "settings": public_service_settings(registry, env_file),
        }

    @app.post("/api/services/{service_id}/test")
    def test_service(service_id: str) -> dict[str, Any]:
        try:
            endpoint = app.state.service_registry.get(service_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="service not found") from exc
        payload = app.state.service_router._service_health(endpoint)
        return {"service_id": service_id, "ready": payload["ready"], "health": payload["health"]}

    @app.get("/api/services/status")
    def services_status() -> dict[str, Any]:
        return {
            "services": _service_health_with_supervisor(app.state.service_router, supervisor, app.state.queue),
            "hardware": collect_local_hardware_status(),
        }

    @app.get("/api/startup/checks")
    def startup_checks() -> dict[str, Any]:
        resources = collect_voice_candidates(
            reference_audio_root=ref_root,
            gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root", app.state.service_registry),
            sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root", app.state.service_registry),
            indextts_model_dir=_indextts_model_dir(),
            runtime_checks=_runtime_checks(app.state.service_registry),
            limit=20,
        )
        return {
            "service_mode": _service_mode(),
            "config": {
                "services_path": str(app.state.services_path),
                "services_exists": app.state.services_path.exists(),
                "env_path": str(env_file),
                "env_exists": env_file.exists(),
            },
            "services": _service_health_with_supervisor(app.state.service_router, supervisor),
            "resources": resources,
            "hardware": collect_local_hardware_status(),
        }

    @app.get("/api/runtime/mode")
    def runtime_mode() -> dict[str, Any]:
        return {
            "service_mode": _service_mode(),
            "data_root": str(store.root),
            "runtime_root": str(supervisor.runtime_root),
            "services": [supervisor.status(endpoint) for endpoint in app.state.service_registry.services],
        }

    @app.get("/api/resources/diagnose")
    def diagnose_resources() -> dict[str, Any]:
        return {
            "reference_audio_root": _path_report(ref_root),
            "services": _service_health_with_supervisor(app.state.service_router, supervisor),
            "repositories": _repo_path_reports(),
        }

    @app.get("/api/resources/voice-candidates")
    def voice_candidates(limit: int = 80) -> dict[str, Any]:
        return collect_voice_candidates(
            reference_audio_root=ref_root,
            gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root", app.state.service_registry),
            sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root", app.state.service_registry),
            indextts_model_dir=_indextts_model_dir(),
            runtime_checks=_runtime_checks(app.state.service_registry),
            limit=limit,
        )

    @app.post("/api/services/{service_id}/start")
    def start_service(service_id: str) -> dict[str, Any]:
        try:
            endpoint = app.state.service_registry.get(service_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="service not found") from exc
        return supervisor.start(endpoint)

    @app.post("/api/services/{service_id}/start-and-wait")
    def start_and_wait_service(service_id: str, timeout_seconds: float = 30.0) -> dict[str, Any]:
        try:
            endpoint = app.state.service_registry.get(service_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="service not found") from exc
        started = supervisor.start(endpoint)
        if started.get("status") == "not manageable":
            raise HTTPException(status_code=409, detail=started.get("reason", "service is not manageable"))
        deadline = datetime.now(timezone.utc).timestamp() + max(1.0, min(timeout_seconds, 180.0))
        last_health: dict[str, Any] = {}
        while datetime.now(timezone.utc).timestamp() < deadline:
            last_health = app.state.service_router._service_health(endpoint)
            if last_health.get("ready"):
                return {"status": "ready", "service_id": service_id, "start": started, "health": last_health}
            time.sleep(1.0)
        return {"status": "timeout", "service_id": service_id, "start": started, "health": last_health}

    @app.post("/api/services/{service_id}/stop")
    def stop_service(service_id: str) -> dict[str, Any]:
        return supervisor.stop(service_id)

    @app.get("/api/services/{service_id}/logs")
    def service_logs(service_id: str, lines: int = 120) -> dict[str, Any]:
        return supervisor.logs(service_id, lines=lines)

    @app.get("/api/services/{service_id}/load-state")
    def service_load_state(service_id: str) -> dict[str, Any]:
        try:
            app.state.service_registry.get(service_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="service not found") from exc
        return app.state.queue.load_state(service_id)

    @app.get("/api/services/{service_id}/gradio/index")
    def service_gradio_index(service_id: str) -> dict[str, Any]:
        try:
            endpoint = app.state.service_registry.get(service_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="service not found") from exc
        if not endpoint.api_contract.startswith("gradio-"):
            raise HTTPException(status_code=400, detail="service is not a Gradio endpoint")
        client = app.state.service_router.clients.get(service_id)
        if client is None or not hasattr(client, "gradio_index"):
            raise HTTPException(status_code=400, detail="Gradio index is not available for this service")
        try:
            return client.gradio_index()  # type: ignore[attr-defined]
        except Exception as exc:
            raise HTTPException(status_code=502, detail=scrub_error(exc, getattr(endpoint, "base_url", None))) from exc

    @app.get("/api/repos")
    def repos() -> dict[str, Any]:
        return _read_repo_lock()

    @app.post("/api/parse-script")
    def parse_script(request: ParseScriptRequest) -> dict[str, Any]:
        if not request.text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        try:
            return app.state.parser.parse(request.text).model_dump(mode="json")
        except ParserProviderUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ParserQualityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/parser/providers")
    def get_parser_providers() -> dict[str, Any]:
        return public_parser_providers(parser_config_file, env_file)

    @app.put("/api/parser/providers")
    def put_parser_providers(request: ParserProvidersUpdate) -> dict[str, Any]:
        try:
            save_parser_providers(parser_config_file, env_file, request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        app.state.parser = _build_parser(parser_config_file)
        return public_parser_providers(parser_config_file, env_file)

    @app.post("/api/parser/providers/test")
    def test_parser_provider(request: ParserProviderTestRequest) -> dict[str, Any]:
        provider = request.provider
        if not provider.enabled:
            return {
                "ok": False,
                "state": "disabled",
                "message": "provider is disabled",
                "provider": provider.name,
            }
        if not provider.base_url.strip() or not provider.model.strip():
            return {
                "ok": False,
                "state": "blocked",
                "message": "base_url and model are required",
                "provider": provider.name,
            }
        # SSRF guard: the parser provider base_url is user-supplied and the
        # server will POST to it. Loopback is allowed (local LLM gateways),
        # but private/LAN/link-local/metadata targets are blocked by default.
        try:
            validate_egress_url(provider.base_url, allow_loopback=True)
        except EgressError as exc:
            return {
                "ok": False,
                "state": "blocked",
                "message": f"base_url is not allowed: {exc}",
                "provider": provider.name,
            }
        api_key = provider.api_key or os.environ.get(provider.api_key_env)
        if not api_key:
            return {
                "ok": False,
                "state": "needs_key",
                "message": f"missing env {provider.api_key_env}",
                "provider": provider.name,
            }
        started = time.time()
        try:
            parser_provider = build_parser_provider(ParserProviderConfig.model_validate(provider.model_dump(mode="python")))
            probe = parser_provider.probe(api_key)
            return {
                "ok": True,
                "state": "ready",
                "message": "parser contract request succeeded",
                "provider": provider.name,
                "model": provider.model,
                "latency_ms": round((time.time() - started) * 1000),
                "content_preview": probe.content_preview,
            }
        except Exception as exc:
            return {
                "ok": False,
                "state": "blocked",
                "message": scrub_error(exc, provider.base_url),
                "provider": provider.name,
                "model": provider.model,
                "latency_ms": round((time.time() - started) * 1000),
            }

    @app.get("/api/characters")
    def get_characters() -> list[dict[str, Any]]:
        return [character.model_dump(mode="json") for character in store.load_characters()]

    @app.put("/api/characters")
    def put_characters(characters: list[Character]) -> dict[str, str]:
        store.save_characters(characters)
        return {"status": "saved"}

    @app.post("/api/characters/{character_id}/avatar/upload")
    async def upload_character_avatar(character_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(status_code=400, detail="unsupported avatar image")
        characters = store.load_characters()
        character = next((item for item in characters if item.id == character_id), None)
        if character is None:
            raise HTTPException(status_code=404, detail="character not found")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="avatar image is empty")
        if len(content) > app.state.max_upload_bytes:
            raise HTTPException(status_code=413, detail=f"avatar image exceeds upload limit")

        output_dir = Path(app.state.store.root) / "character_avatars"
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_id = _safe_upload_name(character_id).removesuffix(Path(_safe_upload_name(character_id)).suffix)
        output_path = output_dir / f"{safe_id}{suffix}"
        counter = 1
        while output_path.exists():
            output_path = output_dir / f"{safe_id}-{counter}{suffix}"
            counter += 1
        output_path.write_bytes(content)
        character.avatar_path = str(output_path)
        character.updated_at = datetime.now(timezone.utc)
        store.save_characters(characters)
        return {"character": character.model_dump(mode="json")}

    @app.post("/api/characters/{character_id}/reference-audio/upload")
    async def upload_character_reference_audio(character_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in AUDIO_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=400, detail="unsupported audio file")
        characters = store.load_characters()
        character = next((item for item in characters if item.id == character_id), None)
        if character is None:
            raise HTTPException(status_code=404, detail="character not found")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="audio file is empty")
        if len(content) > app.state.max_upload_bytes:
            raise HTTPException(status_code=413, detail=f"audio file exceeds upload limit")

        safe_character_id = _safe_upload_name(character_id).removesuffix(Path(_safe_upload_name(character_id)).suffix)
        output_dir = Path(app.state.store.root) / "character_reference_audio" / safe_character_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / _safe_upload_name(file.filename or f"reference{suffix}")
        counter = 1
        while output_path.exists():
            output_path = output_dir / f"{output_path.stem}-{counter}{suffix}"
            counter += 1
        output_path.write_bytes(content)

        sample = ReferenceAudioSample(path=str(output_path), text="", text_source="manual")
        group_id = "manual-uploads"
        group = next((item for item in character.reference_audio_groups if item.id == group_id), None)
        if group is None:
            character.reference_audio_groups.append(
                ReferenceAudioGroup(
                    id=group_id,
                    name="手动上传",
                    paths=[str(output_dir)],
                    copied_paths=[str(output_path)],
                    samples=[sample],
                )
            )
        else:
            if str(output_dir) not in group.paths:
                group.paths.append(str(output_dir))
            if str(output_path) not in group.copied_paths:
                group.copied_paths.append(str(output_path))
            group.samples.append(sample)
        character.updated_at = datetime.now(timezone.utc)
        store.save_characters(characters)
        return {"character": character.model_dump(mode="json"), "sample": sample.model_dump(mode="json")}

    @app.get("/api/character-library")
    def character_library() -> dict[str, Any]:
        return {"characters": [character.model_dump(mode="json") for character in store.load_characters()]}

    @app.post("/api/character-library/scan")
    def scan_character_library(request: RoleLibraryScanRequest) -> dict[str, Any]:
        return {
            "candidates": scan_role_library_candidates(
                reference_audio_root=ref_root,
                gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root", app.state.service_registry),
                sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root", app.state.service_registry),
                logs_roots=[
                    *_configured_weight_roots(store.load_characters(), "logs_root", app.state.service_registry),
                    *_configured_weight_roots(store.load_characters(), "logs_roots", app.state.service_registry),
                ],
                limit=request.limit,
            )
        }

    @app.get("/api/character-library/logs-candidates")
    def character_library_logs_candidates(service_id: str | None = None, include_gradio: bool = True, limit: int = 80) -> dict[str, Any]:
        gradio_candidates: list[dict[str, Any]] = []
        diagnostics: list[dict[str, str]] = []
        if include_gradio:
            services = [
                service
                for service in app.state.service_registry.services
                if service.enabled
                and service.api_contract == "gradio-gpt-sovits-webui"
                and (service_id is None or service.service_id == service_id)
            ]
            for service in services:
                client = app.state.service_router.clients.get(service.service_id)
                if client is None or not hasattr(client, "gradio_index"):
                    diagnostics.append({"service_id": service.service_id, "status": "unsupported", "detail": "Gradio index is not available"})
                    continue
                try:
                    index = client.gradio_index()  # type: ignore[attr-defined]
                    gradio_candidates.extend(index.get("candidates", []))
                except Exception as exc:
                    diagnostics.append({"service_id": service.service_id, "status": "unreachable", "detail": str(exc)})
        return {
            "candidates": scan_logs_index_candidates(
                reference_audio_root=ref_root,
                gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root", app.state.service_registry),
                sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root", app.state.service_registry),
                logs_roots=[
                    *_configured_weight_roots(store.load_characters(), "logs_root", app.state.service_registry),
                    *_configured_weight_roots(store.load_characters(), "logs_roots", app.state.service_registry),
                ],
                service_id=service_id,
                gradio_candidates=gradio_candidates,
                limit=limit,
            ),
            "diagnostics": diagnostics,
        }

    @app.get("/api/model-catalog/gpt-sovits")
    def gpt_sovits_model_catalog(
        service_id: str | None = None,
        include_gradio: bool = True,
        include_api: bool = True,
        limit: int = 120,
    ) -> dict[str, Any]:
        diagnostics: list[dict[str, str]] = []
        gradio_candidates, api_candidates = _collect_gpt_sovits_catalog_candidates(
            app,
            service_id=service_id,
            include_gradio=include_gradio,
            include_api=include_api,
            diagnostics=diagnostics,
            limit=limit,
        )
        characters = store.load_characters()
        if service_id:
            gpt_roots = _configured_weight_roots_for_service(characters, "gpt_weights_root", app.state.service_registry, service_id)
            sovits_roots = _configured_weight_roots_for_service(characters, "sovits_weights_root", app.state.service_registry, service_id)
            logs_roots = [
                *_configured_weight_roots_for_service(characters, "logs_root", app.state.service_registry, service_id),
                *_configured_weight_roots_for_service(characters, "logs_roots", app.state.service_registry, service_id),
            ]
        else:
            gpt_roots = _configured_weight_roots(characters, "gpt_weights_root", app.state.service_registry)
            sovits_roots = _configured_weight_roots(characters, "sovits_weights_root", app.state.service_registry)
            logs_roots = [
                *_configured_weight_roots(characters, "logs_root", app.state.service_registry),
                *_configured_weight_roots(characters, "logs_roots", app.state.service_registry),
            ]
        return {
            "models": scan_gpt_sovits_model_catalog_candidates(
                reference_audio_root=ref_root,
                gpt_weights_roots=gpt_roots,
                sovits_weights_roots=sovits_roots,
                logs_roots=logs_roots,
                service_id=service_id,
                gradio_candidates=gradio_candidates,
                api_candidates=api_candidates,
                limit=limit,
            ),
            "diagnostics": diagnostics,
        }

    @app.get("/api/model-catalog/gpt-sovits/samples")
    def gpt_sovits_model_catalog_samples(service_id: str | None = None, logs_name: str = "", limit: int = 120) -> dict[str, Any]:
        diagnostics: list[dict[str, str]] = []
        if service_id:
            client = app.state.service_router.clients.get(service_id)
            if client is not None and hasattr(client, "model_samples"):
                try:
                    return client.model_samples(logs_name, limit=limit)  # type: ignore[attr-defined]
                except Exception as exc:
                    diagnostics.append({"service_id": service_id, "status": "api_v2_unavailable", "detail": str(exc)})
            characters = store.load_characters()
            logs_roots = [
                *_configured_weight_roots_for_service(characters, "logs_root", app.state.service_registry, service_id),
                *_configured_weight_roots_for_service(characters, "logs_roots", app.state.service_registry, service_id),
            ]
        else:
            characters = store.load_characters()
            logs_roots = [
                *_configured_weight_roots(characters, "logs_root", app.state.service_registry),
                *_configured_weight_roots(characters, "logs_roots", app.state.service_registry),
            ]
        payload = scan_logs_reference_audio_samples(logs_roots=logs_roots, logs_name=logs_name, limit=limit)
        return {
            **payload,
            "service_id": service_id,
            "diagnostics": [*diagnostics, *(payload.get("diagnostics") or [])],
        }

    @app.get("/api/character-library/logs-reference-audio")
    def character_library_logs_reference_audio(
        service_id: str | None = None,
        logs_name: str = "",
        gpt_weights_path: str | None = None,
        sovits_weights_path: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        del gpt_weights_path, sovits_weights_path
        characters = store.load_characters()
        if service_id:
            logs_roots = [
                *_configured_weight_roots_for_service(characters, "logs_root", app.state.service_registry, service_id),
                *_configured_weight_roots_for_service(characters, "logs_roots", app.state.service_registry, service_id),
            ]
            if not logs_roots:
                return {
                    "service_id": service_id,
                    "logs_name": logs_name,
                    "samples": [],
                    "diagnostics": [{
                        "status": "service_logs_roots_missing",
                        "detail": f"service {service_id!r} has no configured logs_roots; reference audio samples are service-scoped",
                    }],
                }
        else:
            logs_roots = [
                *_configured_weight_roots(characters, "logs_root", app.state.service_registry),
                *_configured_weight_roots(characters, "logs_roots", app.state.service_registry),
            ]
        payload = scan_logs_reference_audio_samples(logs_roots=logs_roots, logs_name=logs_name, limit=limit)
        return {
            **payload,
            "service_id": service_id,
        }

    @app.get("/api/character-library/common-logs-presets")
    def character_library_common_logs_presets() -> dict[str, Any]:
        return {"presets": common_logs_presets()}

    @app.post("/api/character-library/import")
    def import_character_library_candidate(request: RoleLibraryImportRequest) -> dict[str, Any]:
        character = candidate_to_character(request.candidate)
        characters = [item for item in store.load_characters() if item.id != character.id]
        characters.append(character)
        store.save_characters(characters)
        return {"character": character.model_dump(mode="json")}

    @app.post("/api/character-library/import-common-presets")
    def import_common_logs_preset_characters(service_id: str | None = None, replace_existing: bool = False) -> dict[str, Any]:
        candidate_payload = character_library_logs_candidates(service_id=service_id, include_gradio=True, limit=200)
        preset_names = {item["name"] for item in common_logs_presets()}
        preset_candidates = [item for item in candidate_payload["candidates"] if item.get("name") in preset_names]
        existing = store.load_characters()
        existing_ids = {character.id for character in existing}
        imported: list[Character] = []
        updated: list[Character] = []
        skipped: list[str] = []
        for candidate in preset_candidates:
            character = candidate_to_character(candidate)
            existing_match = _find_existing_character_for_candidate(existing, candidate, character)
            if character.id in existing_ids or existing_match is not None:
                matched_id = existing_match.id if existing_match else character.id
                if replace_existing:
                    for index, item in enumerate(existing):
                        if item.id == matched_id:
                            avatar_path = item.avatar_path if item.avatar_path and not character.avatar_path else character.avatar_path
                            existing[index] = character.model_copy(update={"id": item.id, "avatar_path": avatar_path})
                            updated.append(existing[index])
                            break
                else:
                    skipped.append(matched_id)
                continue
            imported.append(character)
            existing_ids.add(character.id)
        if imported or updated:
            store.save_characters([*existing, *imported])
        return {
            "imported": [character.model_dump(mode="json") for character in imported],
            "updated": [character.model_dump(mode="json") for character in updated],
            "skipped": skipped,
        }

    @app.delete("/api/character-library/{character_id}")
    def delete_character_library_item(character_id: str) -> dict[str, str]:
        references = referenced_projects(_load_all_projects(store), character_id)
        if references:
            raise HTTPException(status_code=409, detail=f"character is referenced by project(s): {', '.join(references)}")
        characters = store.load_characters()
        next_characters = [character for character in characters if character.id != character_id]
        if len(next_characters) == len(characters):
            raise HTTPException(status_code=404, detail="character not found")
        store.save_characters(next_characters)
        return {"status": "deleted"}

    @app.get("/api/projects")
    def list_projects() -> dict[str, Any]:
        return {"projects": store.list_projects()}

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        try:
            return store.load_project(project_id).model_dump(mode="json")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc

    @app.put("/api/projects/{project_id}")
    def put_project(project_id: str, project: ScriptProject) -> dict[str, str]:
        store.save_project(project_id, project)
        return {"status": "saved"}

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, str]:
        try:
            trashed_path = store.delete_project(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "deleted", "project_id": project_id, "trashed_path": str(trashed_path)}

    @app.get("/api/projects/{project_id}/script-revisions")
    def get_script_revisions(project_id: str) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        return {
            "active_script_revision_id": project.active_script_revision_id,
            "active_parse_revision_id": project.active_parse_revision_id,
            "script_revisions": [revision.model_dump(mode="json") for revision in project.script_revisions],
            "parse_revisions": [revision.model_dump(mode="json") for revision in project.parse_revisions],
        }

    @app.post("/api/projects/{project_id}/script-revisions")
    def create_script_revision(project_id: str, request: ScriptRevisionCreate) -> dict[str, Any]:
        if not request.source_markdown.strip():
            raise HTTPException(status_code=400, detail="source_markdown is required")
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        revision = ScriptRevision(
            revision_id=_next_revision_id("script", [item.revision_id for item in project.script_revisions]),
            source_markdown=request.source_markdown,
            parent_revision_id=project.active_script_revision_id,
            summary=request.summary,
        )
        project.script_revisions.append(revision)
        project.active_script_revision_id = revision.revision_id
        store.save_project(project_id, project)
        revision_payload = revision.model_dump(mode="json")
        return {"revision": revision_payload, "script_revision": revision_payload, "project": project.model_dump(mode="json")}

    @app.post("/api/projects/{project_id}/parse-revisions")
    def create_parse_revision(project_id: str, request: ParseRevisionCreate) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        script_revision = next((item for item in project.script_revisions if item.revision_id == request.script_revision_id), None)
        if script_revision is None:
            raise HTTPException(status_code=404, detail="script revision not found")
        try:
            draft = app.state.parser.parse(script_revision.source_markdown)
        except ParserProviderUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ParserQualityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        revision_id = _next_revision_id("parse", [item.revision_id for item in project.parse_revisions])
        project_characters = [
            ProjectCharacter(
                project_character_id=character.id,
                name=character.name,
                library_character_id=None,
                match_status="unmatched",
            )
            for character in draft.characters
        ]
        lines = [line.model_copy(update={"line_uid": f"{revision_id}:{line.id}"}) for line in draft.lines]
        draft_project = project.model_copy(
            deep=True,
            update={"project_characters": project_characters, "lines": lines},
        )
        project_characters = match_project_characters(draft_project, store.load_characters(), force=True)
        revision = ParseRevision(
            revision_id=revision_id,
            script_revision_id=script_revision.revision_id,
            parent_parse_revision_id=project.active_parse_revision_id,
            provider=draft.provider,
            warnings=draft.warnings,
            project_characters=project_characters,
            lines=lines,
        )
        project.parse_revisions.append(revision)
        project.active_script_revision_id = script_revision.revision_id
        project.active_parse_revision_id = revision.revision_id
        project.project_characters = project_characters
        project.lines = lines
        store.save_project(project_id, project)
        revision_payload = revision.model_dump(mode="json")
        return {"revision": revision_payload, "parse_revision": revision_payload, "project": project.model_dump(mode="json")}

    @app.post("/api/projects/{project_id}/activate-revision")
    def activate_revision(project_id: str, request: ActivateRevisionRequest) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        if request.script_revision_id:
            if not any(item.revision_id == request.script_revision_id for item in project.script_revisions):
                raise HTTPException(status_code=404, detail="script revision not found")
            project.active_script_revision_id = request.script_revision_id
        if request.parse_revision_id:
            parse_revision = next((item for item in project.parse_revisions if item.revision_id == request.parse_revision_id), None)
            if parse_revision is None:
                raise HTTPException(status_code=404, detail="parse revision not found")
            project.active_parse_revision_id = parse_revision.revision_id
            project.active_script_revision_id = parse_revision.script_revision_id
            project.project_characters = parse_revision.project_characters
            project.lines = parse_revision.lines
        store.save_project(project_id, project)
        return {"project": project.model_dump(mode="json")}

    @app.post("/api/projects/{project_id}/reference-audio/upload")
    async def upload_project_reference_audio(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in AUDIO_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=400, detail="unsupported audio file")
        output_dir = store.project_reference_audio_dir(project_id) / "temporary"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / _safe_upload_name(file.filename or f"reference{suffix}")
        counter = 1
        while output_path.exists():
            output_path = output_dir / f"{output_path.stem}-{counter}{suffix}"
            counter += 1
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="audio file is empty")
        if len(content) > app.state.max_upload_bytes:
            raise HTTPException(status_code=413, detail=f"audio file exceeds upload limit")
        output_path.write_bytes(content)
        return {"sample": {"path": str(output_path), "text": "", "text_source": "manual"}}

    @app.get("/api/projects/{project_id}/characters")
    def get_project_characters(project_id: str) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        library = store.load_characters()
        project_characters = match_project_characters(project, library)
        if project.project_characters != project_characters:
            project.project_characters = project_characters
            store.save_project(project_id, project)
        return {
            "project_characters": [item.model_dump(mode="json") for item in project.project_characters],
            "characters": [character.model_dump(mode="json") for character in resolve_project_characters(project, library)],
        }

    @app.put("/api/projects/{project_id}/characters")
    def put_project_characters(project_id: str, request: ProjectCharactersUpdate) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        project.project_characters = request.project_characters
        active_parse = next((item for item in project.parse_revisions if item.revision_id == project.active_parse_revision_id), None)
        if active_parse is not None:
            active_parse.project_characters = project.project_characters
        store.save_project(project_id, project)
        return {"project_characters": [item.model_dump(mode="json") for item in project.project_characters]}

    @app.post("/api/projects/{project_id}/characters/rematch")
    def rematch_project_characters(project_id: str) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        library = store.load_characters()
        project.project_characters = match_project_characters(project, library, force=True)
        active_parse = next((item for item in project.parse_revisions if item.revision_id == project.active_parse_revision_id), None)
        if active_parse is not None:
            active_parse.project_characters = project.project_characters
        store.save_project(project_id, project)
        return {
            "project_characters": [item.model_dump(mode="json") for item in project.project_characters],
            "characters": [character.model_dump(mode="json") for character in resolve_project_characters(project, library)],
        }

    @app.post("/api/projects/{project_id}/characters/{project_character_id}/freeze")
    def freeze_character(project_id: str, project_character_id: str) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
            project_character = freeze_project_character(project, project_character_id, store.load_characters())
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project character not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store.save_project(project_id, project)
        return {"project_character": project_character.model_dump(mode="json")}

    @app.post("/api/projects/{project_id}/characters/{project_character_id}/unfreeze")
    def unfreeze_character(project_id: str, project_character_id: str) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        for index, project_character in enumerate(project.project_characters):
            if project_character.project_character_id == project_character_id:
                next_character = project_character.model_copy(update={"mode": ProjectCharacterMode.REFERENCE, "character_snapshot": None})
                project.project_characters[index] = next_character
                store.save_project(project_id, project)
                return {"project_character": next_character.model_dump(mode="json")}
        raise HTTPException(status_code=404, detail="project character not found")

    @app.get("/api/projects/{project_id}/manifest")
    def get_manifest(project_id: str) -> dict[str, Any]:
        return store.load_manifest(project_id).model_dump(mode="json")

    @app.delete("/api/projects/{project_id}/manifest/lines/{line_key}/versions/{version_id}")
    def delete_generation_version(project_id: str, line_key: str, version_id: str) -> dict[str, Any]:
        manifest = store.load_manifest(project_id)
        resolved_line_key, history = _resolve_manifest_history_for_version(manifest, line_key, version_id)
        if history is None:
            raise HTTPException(status_code=404, detail="line history not found")
        target = next((version for version in history.versions if version.version_id == version_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="generation version not found")
        history.versions = [version for version in history.versions if version.version_id != version_id]
        audio_deleted = False
        warning: str | None = None
        if target.audio_path:
            resolved = _resolve_project_audio_file(store.project_audio_dir(project_id), Path(app.state.store.root), target.audio_path)
            if resolved is None:
                warning = "audio path is outside project audio directory"
            elif resolved.exists():
                resolved.unlink()
                audio_deleted = True
            else:
                warning = "audio file not found"
        store.save_manifest(manifest)
        return {
            "status": "deleted",
            "project_id": project_id,
            "line_key": resolved_line_key,
            "version_id": version_id,
            "audio_deleted": audio_deleted,
            "warning": warning,
        }

    @app.post("/api/generate")
    def generate(request: GenerateRequest) -> dict[str, Any]:
        try:
            tasks = _enrich_tasks_for_project(store, request.project_id, request.tasks)
            _validate_generation_tasks(tasks)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        manifest = store.load_manifest(request.project_id)
        output_dir = store.project_audio_dir(request.project_id)
        app.state.queue.run(tasks, manifest, output_dir=output_dir)
        store.save_manifest(manifest)
        return manifest.model_dump(mode="json")

    @app.post("/api/jobs/generation")
    def create_generation_job(request: GenerateRequest) -> dict[str, Any]:
        tasks = _prepare_tasks_for_async_job(store, request.project_id, request.tasks)
        job = app.state.job_manager.submit(request.project_id, tasks)
        return job.model_dump(mode="json")

    @app.post("/api/generation/preflight")
    def generation_preflight(request: GenerateRequest) -> dict[str, Any]:
        try:
            tasks = _enrich_tasks_for_project(store, request.project_id, request.tasks)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        items = [_preflight_task(app.state.service_router, supervisor, app.state.queue, task) for task in tasks]
        if all(item["status"] == "ready" for item in items):
            status = "ready"
        elif any(item["status"] == "needs_user_action" for item in items):
            status = "needs_user_action"
        else:
            status = "blocked"
        return {"status": status, "items": items}

    @app.get("/api/jobs/{job_id}")
    def get_generation_job(job_id: str) -> dict[str, Any]:
        try:
            return app.state.job_manager.get(job_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_generation_job(job_id: str) -> dict[str, Any]:
        try:
            return app.state.job_manager.cancel(job_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/api/queue/status")
    def queue_status() -> dict[str, Any]:
        return app.state.job_manager.status()

    @app.post("/api/validation/real-tts/run")
    def run_real_tts_validation(request: GenerateRequest) -> dict[str, Any]:
        try:
            tasks = _enrich_tasks_for_project(store, request.project_id, request.tasks)
            _validate_generation_tasks(tasks)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if _service_mode() == "real":
            _reject_mock_validation_services(tasks, app.state.service_registry)
        manifest = store.load_manifest(request.project_id)
        output_dir = store.project_audio_dir(request.project_id)
        app.state.queue.run(tasks, manifest, output_dir=output_dir)
        store.save_manifest(manifest)
        return {"summary": _manifest_summary(manifest), "manifest": manifest.model_dump(mode="json")}

    @app.get("/api/validation/demo-plan")
    def demo_validation_plan(project_id: str = "demo", limit: int = 30, repeats: int = 1) -> dict[str, Any]:
        try:
            project = store.load_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        line_limit = max(1, min(limit, 200))
        repeat_count = max(1, min(repeats, 10))
        runnable: list[GenerationTask] = []
        blocked: list[dict[str, Any]] = []
        for line in project.lines[:line_limit]:
            base_task = GenerationTask(
                line=line,
                engine=line.engine_override or EngineName.GPT_SOVITS,
                profile=line.profile_override or "default",
                service_id=line.service_override,
                parameters={},
            )
            try:
                runnable.append(_enrich_tasks_for_project(store, project_id, [base_task])[0])
            except ValueError as exc:
                blocked.append({"line_id": line.id, "line_uid": line.line_uid or line.id, "character_id": line.character_id, "reason": str(exc)})
        tasks = [task for _ in range(repeat_count) for task in runnable]
        preflight_items = [_preflight_task(app.state.service_router, supervisor, app.state.queue, task) for task in tasks]
        if not preflight_items:
            preflight_status = "blocked"
        elif all(item["status"] == "ready" for item in preflight_items):
            preflight_status = "ready"
        elif any(item["status"] == "needs_user_action" for item in preflight_items):
            preflight_status = "needs_user_action"
        else:
            preflight_status = "blocked"
        clusters: dict[str, dict[str, Any]] = {}
        for task in tasks:
            try:
                route = app.state.service_router.resolve_task(task)
                key = build_cluster_key(task, route)
            except Exception:
                key = "unresolved"
            item = clusters.setdefault(key, {"cluster_key": key, "count": 0, "line_ids": []})
            item["count"] += 1
            item["line_ids"].append(task.line.id)
        return {
            "project_id": project_id,
            "title": project.title,
            "summary": {
                "line_count": len(project.lines),
                "considered_line_count": min(line_limit, len(project.lines)),
                "runnable_line_count": len(runnable),
                "blocked_line_count": len(blocked),
                "task_count": len(tasks),
                "repeats": repeat_count,
            },
            "blocked_lines": blocked,
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "preflight": {"status": preflight_status, "items": preflight_items},
            "clusters": sorted(clusters.values(), key=lambda item: (-item["count"], item["cluster_key"])),
        }

    @app.get("/api/reference-audio/scan")
    def scan_reference_audio(limit: int = 80) -> dict[str, Any]:
        return {"root": str(ref_root), "groups": scan_reference_audio_groups(ref_root, limit=limit)}

    @app.get("/api/audio")
    def get_audio(path: str) -> FileResponse:
        data_root = Path(app.state.store.root)
        extra_safe = [data_root, ref_root, *store.read_project_roots()]
        audio_path = _resolve_data_audio_path(
            data_root,
            path,
            allowed_roots=[
                *extra_safe,
                *_confined_weight_roots(store.load_characters(), "logs_root", app.state.service_registry, project_root, extra_safe),
                *_confined_weight_roots(store.load_characters(), "logs_roots", app.state.service_registry, project_root, extra_safe),
            ],
        )
        if not audio_path.is_file():
            raise HTTPException(status_code=404, detail="audio not found")
        return FileResponse(audio_path, media_type=_audio_media_type(audio_path))

    @app.get("/api/assets/image")
    def get_image_asset(path: str) -> FileResponse:
        data_root = Path(app.state.store.root)
        extra_safe = [data_root, ref_root, *store.read_project_roots()]
        image_path = _resolve_data_asset_path(
            data_root,
            path,
            allowed_roots=[
                *extra_safe,
                *_confined_weight_roots(store.load_characters(), "logs_root", app.state.service_registry, project_root, extra_safe),
                *_confined_weight_roots(store.load_characters(), "logs_roots", app.state.service_registry, project_root, extra_safe),
            ],
        )
        if not image_path.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        media_type = _image_media_type(image_path)
        return FileResponse(image_path, media_type=media_type)

    configured_static_root = static_root or os.environ.get("TTS_MORE_STATIC_ROOT")
    if configured_static_root:
        frontend_root = Path(configured_static_root).resolve(strict=False)
        index_path = frontend_root / "index.html"
        if not index_path.is_file():
            raise RuntimeError(f"frontend static assets are missing: {index_path}")
        assets_root = frontend_root / "assets"
        if assets_root.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_root), name="frontend-assets")

        @app.get("/", include_in_schema=False)
        def frontend_index() -> FileResponse:
            return FileResponse(index_path)

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend_spa(frontend_path: str) -> FileResponse:
            if frontend_path == "api" or frontend_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found")
            candidate = (frontend_root / frontend_path).resolve(strict=False)
            try:
                candidate.relative_to(frontend_root)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="frontend asset not found") from exc
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_path)

    return app


def _build_parser(config_path: Path | None = None) -> MultiProviderParser:
    providers = []
    if config_path is not None:
        for item in load_parser_providers(config_path):
            providers.append(build_parser_provider(ParserProviderConfig.model_validate(item.model_dump(mode="python"))))
    else:
        raw = os.environ.get("TTS_MORE_PARSER_PROVIDERS")
        if raw:
            for item in json.loads(raw):
                providers.append(build_parser_provider(ParserProviderConfig.model_validate(item)))
    return MultiProviderParser(providers)


def _load_service_registry(path: Path) -> ServiceRegistry:
    registry = ServiceRegistry.load(path)
    if _service_mode() == "mock":
        return _mocked_registry(registry)
    return registry


def _apply_registry(app: FastAPI, registry: ServiceRegistry, store: ProjectStore) -> None:
    """Rebuild router/queue/job_manager from a (possibly updated) registry.

    Called after every mutation of the service registry (save settings, reload,
    open-source configure) to keep the routing layer in sync.
    """
    router = ServiceRouter(registry)
    queue = ServiceGenerationQueue(router)
    app.state.service_registry = registry
    app.state.service_router = router
    app.state.queue = queue
    app.state.job_manager = GenerationJobManager(queue, store)


def _resolve_service_settings_paths(data_root: Path, explicit_path: Path | None) -> tuple[Path, Path]:
    if explicit_path is not None:
        return explicit_path, explicit_path
    env_path = os.environ.get("TTS_MORE_SERVICES_PATH")
    if env_path:
        resolved_env_path = Path(env_path)
        return resolved_env_path, resolved_env_path
    local_path = data_root / "local" / "services.json"
    committed_path = data_root / "services.json"
    template_path = data_root / "templates" / "services.example.json"
    for candidate in (local_path, committed_path, template_path):
        if candidate.exists():
            return candidate, local_path
    return local_path, local_path


def _portable_controller_root(data_root: Path, project_root: Path) -> Path:
    del data_root  # The executable/module layout, not process environment or cwd, is authoritative.
    packaged_root = project_root.parent
    if (
        (packaged_root / "package" / "tts-more-package.json").is_file()
        and (packaged_root / "scripts" / "select-portable-folder.ps1").is_file()
    ):
        return packaged_root
    return project_root


def _resolve_manifest_history_for_version(manifest: GenerationManifest, line_key: str, version_id: str) -> tuple[str, Any | None]:
    history = manifest.lines.get(line_key)
    if history is not None:
        return line_key, history
    if ":" in line_key:
        legacy_key = line_key.rsplit(":", 1)[-1]
        legacy_history = manifest.lines.get(legacy_key)
        if legacy_history is not None and any(version.version_id == version_id for version in legacy_history.versions):
            return legacy_key, legacy_history
    return line_key, None


def _mocked_registry(registry: ServiceRegistry) -> ServiceRegistry:
    return ServiceRegistry(
        [
            service.model_copy(update={"base_url": f"mock://{service.service_id}", "health_url": None})
            if "paid_provider" not in service.capabilities
            else service
            for service in registry.services
        ]
    )


def _service_mode() -> str:
    return os.environ.get("TTS_MORE_SERVICE_MODE", os.environ.get("TTS_MORE_ADAPTER_MODE", "real"))


def _service_health_with_supervisor(
    router: ServiceRouter,
    supervisor: ServiceSupervisor,
    queue: ServiceGenerationQueue | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for service in router.health():
        endpoint = router.registry.get(service["service_id"])
        supervisor_state = supervisor.status(endpoint)
        load_state = queue.load_state(endpoint.service_id) if queue is not None else {}
        output.append(_layer_service_status({**service, "supervisor": supervisor_state, "load_state": load_state}, supervisor_state))
    return output


def _layer_service_status(service: dict[str, Any], supervisor_state: dict[str, Any]) -> dict[str, Any]:
    health = service.get("health") or {}
    setup_state = _effective_setup_state(service, health)
    reported_running = supervisor_state.get("running")
    portable_health_running = bool(
        service.get("control_kind") == "portable-package"
        and reported_running is None
        and service.get("ready")
    )
    effective_running = bool(reported_running is True or portable_health_running)
    managed_local_stopped = bool(
        supervisor_state.get("manageable")
        and not effective_running
        and service.get("network_scope") == "localhost"
    )
    hard_blocked_setup = setup_state in {"not_configured", "repo_missing", "env_missing", "endpoint_unreachable"}
    if service.get("enabled") is False:
        state = "disabled"
        severity = "neutral"
    elif hard_blocked_setup:
        state = "blocked"
        severity = "danger"
    elif managed_local_stopped and health.get("state") == "ready":
        state = "partial"
        severity = "attention"
    elif health.get("state"):
        state = str(health["state"])
        severity = str(health.get("severity") or ("ready" if state == "ready" else "attention"))
    elif managed_local_stopped:
        state = "blocked"
        severity = "danger"
    elif service.get("ready"):
        state = "ready"
        severity = "ready"
    elif health.get("reachable") or health.get("status") == "needs key":
        state = "partial"
        severity = "attention"
    else:
        state = "blocked"
        severity = "danger"
    load_state = service.get("load_state") or {}
    repo_path = service.get("repo_path")
    repo_found = _repo_path_exists(repo_path)
    endpoint_reachable = bool(health.get("reachable") or health.get("port_reachable") or service.get("ready"))
    api_contract_ok = bool(health.get("config_ok") or health.get("required_api_ok") or service.get("ready"))
    return {
        **service,
        "ready": bool(service.get("ready")) and state in {"ready", "running"},
        "state": state,
        "severity": severity,
        "port_reachable": bool(health.get("port_reachable", service.get("ready", False))),
        "config_ok": bool(health.get("config_ok", service.get("ready", False))),
        "required_api_ok": bool(health.get("required_api_ok", service.get("ready", False))),
        "auth_ok": bool(health.get("auth_ok", True)),
        "can_start": bool(supervisor_state.get("manageable") and not effective_running),
        "supervisor_state": "running" if effective_running else "stopped",
        "source_profile": service.get("source_profile"),
        "catalog_provider": service.get("catalog_provider"),
        "setup_state": setup_state,
        "repo_found": repo_found,
        "repo_path_exists": repo_found,
        "endpoint_reachable": endpoint_reachable,
        "api_contract_ok": api_contract_ok,
        "loaded_signature": load_state.get("loaded_signature"),
        "verification_level": load_state.get("verification_level"),
        "last_load_error": load_state.get("last_error"),
    }


def _effective_setup_state(service: dict[str, Any], health: dict[str, Any]) -> str:
    explicit = service.get("setup_state")
    repo_path = service.get("repo_path")
    source_profile = service.get("source_profile")
    if repo_path and source_profile == "local_repo" and not _repo_path_exists(repo_path):
        return "repo_missing"
    if explicit:
        return str(explicit)
    if service.get("enabled") is False:
        return "not_configured"
    if service.get("ready") or health.get("state") == "ready":
        return "ready"
    if health.get("reachable") or health.get("port_reachable") or health.get("status") == "needs key":
        return "partial"
    if service.get("base_url"):
        return "endpoint_unreachable"
    return "not_configured"


def _repo_path_exists(raw_path: Any) -> bool | None:
    if not raw_path:
        return None
    try:
        return Path(str(raw_path)).exists()
    except OSError:
        return False


def _preflight_task(router: ServiceRouter, supervisor: ServiceSupervisor, queue: ServiceGenerationQueue, task: GenerationTask) -> dict[str, Any]:
    try:
        _assert_generation_inputs(task)
    except ValueError as exc:
        return {
            "line_id": task.line.id,
            "line_uid": task.line.line_uid or task.line.id,
            "status": "blocked",
            "selected_service_id": None,
            "load_signature": None,
            "current_loaded_signature": None,
            "load_state": "unresolved",
            "load_match": False,
            "verification_level": None,
            "last_load_error": None,
            "fallback_action": None,
            "reason": str(exc),
        }
    try:
        route = router.resolve_task(task)
        require_remote_artifact_transfer(route.endpoint)
        load_signature = build_load_signature(route.endpoint, task.parameters)
        load_state = queue.load_state(route.endpoint.service_id)
        current_loaded_signature = load_state.get("loaded_signature")
        load_match = bool(current_loaded_signature and current_loaded_signature == load_signature)
        return {
            "line_id": task.line.id,
            "line_uid": task.line.line_uid or task.line.id,
            "status": "ready",
            "selected_service_id": route.endpoint.service_id,
            "load_signature": load_signature,
            "current_loaded_signature": current_loaded_signature,
            "load_state": "loaded" if load_match else ("switch_required" if current_loaded_signature else "not_loaded"),
            "load_match": load_match,
            "verification_level": load_state.get("verification_level"),
            "last_load_error": load_state.get("last_error"),
            "fallback_action": None,
            "reason": None,
        }
    except Exception as exc:
        fallback_action = _local_fallback_action(router, supervisor, task)
        return {
            "line_id": task.line.id,
            "line_uid": task.line.line_uid or task.line.id,
            "status": "needs_user_action" if fallback_action else "blocked",
            "selected_service_id": None,
            "load_signature": None,
            "current_loaded_signature": None,
            "load_state": "unresolved",
            "load_match": False,
            "verification_level": None,
            "last_load_error": None,
            "fallback_action": fallback_action,
            "reason": str(exc),
        }


def _local_fallback_action(router: ServiceRouter, supervisor: ServiceSupervisor, task: GenerationTask) -> dict[str, str] | None:
    candidate_ids = [*task.fallback_service_ids]
    services = router.registry.by_provider(task.provider_type) if task.provider_type else router.registry.by_engine(task.engine)
    for service in services:
        candidate_ids.append(service.service_id)
    seen: set[str] = set()
    for service_id in candidate_ids:
        if not service_id or service_id in seen:
            continue
        seen.add(service_id)
        try:
            endpoint = router.registry.get(service_id)
        except KeyError:
            continue
        supervisor_state = supervisor.status(endpoint)
        if endpoint.network_scope == "localhost" and supervisor_state.get("manageable") and not supervisor_state.get("running"):
            return {"type": "start_service", "service_id": endpoint.service_id}
    return None


def _next_revision_id(prefix: str, existing_ids: list[str]) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}-r(?P<num>\d+)$")
    highest = 0
    for revision_id in existing_ids:
        match = pattern.match(revision_id)
        if match:
            highest = max(highest, int(match.group("num")))
    return f"{prefix}-r{highest + 1:03d}"


def _read_repo_lock() -> dict[str, Any]:
    if not REPO_LOCK_PATH.exists():
        return {"repositories": []}
    return json.loads(REPO_LOCK_PATH.read_text(encoding="utf-8"))


def _repo_path_reports() -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for item in _read_repo_lock().get("repositories", []):
        path = Path(item.get("path", ""))
        reports.append({**item, "exists": path.exists(), "absolute_path": str(path.resolve(strict=False))})
    return reports


def _repo_path(name: str) -> Path:
    for item in _read_repo_lock().get("repositories", []):
        if item.get("name") == name:
            return Path(item.get("path", ""))
    return Path("repo") / name


def _indextts_model_dir() -> Path:
    return Path(os.environ.get("TTS_MORE_INDEXTTS_MODEL_DIR", str(_repo_path("index-tts") / "checkpoints")))


def _runtime_checks(service_registry: ServiceRegistry) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for endpoint in service_registry.services:
        if endpoint.provider_type is None:
            continue
        if endpoint.provider_type.value == "gpt-sovits":
            checks[endpoint.service_id] = {"python": endpoint.start_command[0] if endpoint.start_command else "python", "modules": ["numpy", "torch"]}
        if endpoint.provider_type.value == "indextts":
            checks[endpoint.service_id] = {"python": endpoint.env.get("TTS_MORE_INDEXTTS_PYTHON", os.environ.get("TTS_MORE_INDEXTTS_PYTHON", os.environ.get("TTS_MORE_PYTHON_EXE", "python"))), "modules": ["torch", "indextts"]}
    return checks


def _collect_gpt_sovits_catalog_candidates(
    app: FastAPI,
    service_id: str | None,
    include_gradio: bool,
    include_api: bool,
    diagnostics: list[dict[str, str]],
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gradio_candidates: list[dict[str, Any]] = []
    api_candidates: list[dict[str, Any]] = []
    for service in app.state.service_registry.services:
        if not service.enabled or service.provider_type is None or service.provider_type.value != "gpt-sovits":
            continue
        if service_id is not None and service.service_id != service_id:
            continue
        client = app.state.service_router.clients.get(service.service_id)
        if include_gradio and service.api_contract == "gradio-gpt-sovits-webui":
            if client is None or not hasattr(client, "gradio_index"):
                diagnostics.append({"service_id": service.service_id, "status": "unsupported", "detail": "Gradio index is not available"})
            else:
                try:
                    gradio_candidates.extend((client.gradio_index()).get("candidates", [])[:limit])  # type: ignore[attr-defined]
                except Exception as exc:
                    diagnostics.append({"service_id": service.service_id, "status": "unreachable", "detail": scrub_error(exc, getattr(service, "base_url", None))})
        if include_api and (service.api_contract == "gpt-sovits-api-v2" or "gpt-sovits-api-v2" in service.capabilities):
            if client is None or not hasattr(client, "model_catalog"):
                diagnostics.append({"service_id": service.service_id, "status": "unsupported", "detail": "GPT-SoVITS API v2 model catalog is not available"})
            else:
                try:
                    api_candidates.extend((client.model_catalog(limit=limit)).get("candidates", []))  # type: ignore[attr-defined]
                except Exception as exc:
                    diagnostics.append({"service_id": service.service_id, "status": "api_v2_unreachable", "detail": scrub_error(exc, getattr(service, "base_url", None))})
    return gradio_candidates, api_candidates


def _allowed_data_roots() -> list[Path]:
    """Extra filesystem roots the operator explicitly permits the audio/image
    read endpoints to serve from (os.pathsep-separated, env
    TTS_MORE_ALLOWED_DATA_ROOTS). Used for legitimate model weight dirs that
    live outside the project tree (e.g. a shared NAS mount)."""
    raw = os.environ.get("TTS_MORE_ALLOWED_DATA_ROOTS", "")
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item).resolve(strict=False))
    return roots


def _is_safe_data_root(candidate: Path, project_root: Path, allowed: list[Path]) -> bool:
    """A configured weight/logs root is safe to serve files from if it is
    inside the project root or explicitly allowlisted by the operator."""
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, ValueError):
        return False
    try:
        resolved.relative_to(project_root)
        return True
    except ValueError:
        pass
    for root in allowed:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _configured_weight_roots(characters: list[Character], key: str, service_registry: ServiceRegistry | None = None) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        if value and str(value) not in seen:
            roots.append(Path(str(value)))
            seen.add(str(value))

    for character in characters:
        for profile in character.profiles:
            values = [profile.config.get(key)]
            values.extend(binding.config.get(key) for binding in profile.bindings)
            for value in values:
                add(value)
    if service_registry is not None:
        for service in service_registry.services:
            add(service.default_params.get(key))
    return roots


def _confined_weight_roots(characters: list[Character], key: str, service_registry: ServiceRegistry | None, project_root: Path, extra_safe_roots: list[Path]) -> list[Path]:
    """Like _configured_weight_roots but confines user-writable (character)
    roots to the project / data root / operator allowlist. Service-config roots
    (from services.json default_params) are operator-trusted and pass through.
    Used only by the file-READ endpoints (/api/audio, /api/assets/image)."""
    roots: list[Path] = []
    seen: set[str] = set()
    allowed = _allowed_data_roots()

    def add(value: Any, *, trusted: bool) -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item, trusted=trusted)
            return
        if not value:
            return
        candidate = Path(str(value))
        if not trusted:
            if not _is_safe_data_root(candidate, project_root, allowed) and not _is_safe_data_root(candidate, project_root, extra_safe_roots):
                return
        key_str = str(candidate.resolve(strict=False))
        if key_str not in seen:
            roots.append(candidate)
            seen.add(key_str)

    for character in characters:
        for profile in character.profiles:
            values = [profile.config.get(key)]
            values.extend(binding.config.get(key) for binding in profile.bindings)
            for value in values:
                add(value, trusted=False)
    if service_registry is not None:
        for service in service_registry.services:
            add(service.default_params.get(key), trusted=True)
    return roots


def _configured_weight_roots_for_service(characters: list[Character], key: str, service_registry: ServiceRegistry | None, service_id: str) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        if value and str(value) not in seen:
            roots.append(Path(str(value)))
            seen.add(str(value))

    for character in characters:
        for profile in character.profiles:
            if profile.service_id == service_id:
                add(profile.config.get(key))
            for binding in profile.bindings:
                if binding.service_id == service_id:
                    add(binding.config.get(key))
    if service_registry is not None:
        try:
            service = service_registry.get(service_id)
        except KeyError:
            service = None
        if service is not None:
            add(service.default_params.get(key))
    return roots


def _safe_upload_name(filename: str) -> str:
    path = Path(filename)
    stem = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", path.stem).strip("._-") or "reference"
    stem = stem[:64]  # bound stem length to avoid pathological filenames
    suffix = path.suffix.lower()[:8]  # bound suffix (e.g. .jpeg is 5)
    return f"{stem}{suffix}"


def _load_all_projects(store: ProjectStore) -> list[tuple[str, ScriptProject]]:
    projects: list[tuple[str, ScriptProject]] = []
    for item in store.list_projects():
        project_id = str(item["project_id"])
        projects.append((project_id, store.load_project(project_id)))
    return projects


def _enrich_tasks_for_project(store: ProjectStore, project_id: str, tasks: list[GenerationTask]) -> list[GenerationTask]:
    try:
        project = store.load_project(project_id)
    except FileNotFoundError:
        return tasks
    characters = resolve_project_characters(project, store.load_characters())
    by_id = {character.id: character for character in characters}
    project_lines = {line.id: line for line in project.lines}
    output: list[GenerationTask] = []
    for task in tasks:
        stored_line = project_lines.get(task.line.id)
        line = task.line
        if stored_line is not None:
            line_updates: dict[str, Any] = {"line_uid": stored_line.line_uid}
            if not line.language and stored_line.language:
                line_updates["language"] = stored_line.language
            if line.temporary_binding is None and stored_line.temporary_binding:
                line_updates["temporary_binding"] = stored_line.temporary_binding
            if line_updates:
                line = line.model_copy(update=line_updates)
                task = task.model_copy(update={"line": line})
        if line.temporary_binding is None and stored_line and stored_line.temporary_binding:
            line = line.model_copy(update={"temporary_binding": stored_line.temporary_binding})
            task = task.model_copy(update={"line": line})
        revision_parameters = {
            "_script_revision_id": project.active_script_revision_id,
            "_parse_revision_id": project.active_parse_revision_id,
        }
        if line.temporary_binding is not None:
            binding = line.temporary_binding
            parameters = {**binding.config, **task.parameters, **revision_parameters, "binding_source": "temporary"}
            enriched = task.model_copy(
                update={
                    "engine": PROVIDER_ENGINE_DEFAULTS[binding.provider_type],
                    "profile": binding.binding_id,
                    "service_id": task.service_id or line.service_override or binding.service_id,
                    "fallback_service_ids": task.fallback_service_ids or binding.fallback_services,
                    "parameters": parameters,
                    "binding_id": binding.binding_id,
                    "provider_type": binding.provider_type,
                    "required_capabilities": task.required_capabilities or binding.capabilities,
                }
            )
            _assert_generation_inputs(enriched)
            output.append(enriched)
            continue
        character = by_id.get(line.character_id)
        if character is None:
            output.append(task)
            continue
        profile_id = line.profile_override or (task.profile if task.profile and task.profile != "default" else None) or character.default_profile
        profile = next((item for item in character.profiles if item.id == profile_id), None) or (character.profiles[0] if character.profiles else None)
        if profile is None:
            if task.provider_type and task.binding_id:
                output.append(task)
                continue
            raise ValueError(f"line {line.id} character {line.character_id!r} needs a voice binding before generation")
        binding = None
        if task.binding_id:
            binding = next((item for item in profile.bindings if item.binding_id == task.binding_id), None)
        if binding is None and line.binding_override:
            binding = next((item for item in profile.bindings if item.binding_id == line.binding_override), None)
        if binding is None and profile.bindings:
            binding = profile.bindings[0]
        parameters = {**profile.config, **(binding.config if binding else {}), **task.parameters, **revision_parameters}
        enriched = task.model_copy(
            update={
                "engine": profile.engine,
                "profile": profile.id,
                "service_id": task.service_id or (binding.service_id if binding else None) or profile.service_id,
                "fallback_service_ids": task.fallback_service_ids or (binding.fallback_services if binding else []) or profile.fallback_services,
                "parameters": parameters,
                "binding_id": task.binding_id or (binding.binding_id if binding else None),
                "provider_type": task.provider_type or (binding.provider_type if binding else None),
                "required_capabilities": task.required_capabilities or (binding.capabilities if binding else []),
            }
        )
        _assert_generation_inputs(enriched)
        output.append(enriched)
    return output


def _prepare_tasks_for_async_job(store: ProjectStore, project_id: str, tasks: list[GenerationTask]) -> list[GenerationTask]:
    output: list[GenerationTask] = []
    for task in tasks:
        try:
            enriched_tasks = _enrich_tasks_for_project(store, project_id, [task])
        except ValueError as exc:
            output.append(_prefailed_task(task, str(exc)))
            continue
        for enriched in enriched_tasks:
            try:
                _assert_generation_inputs(enriched)
            except ValueError as exc:
                output.append(_prefailed_task(enriched, str(exc)))
            else:
                output.append(enriched)
    return output


def _prefailed_task(task: GenerationTask, error: str) -> GenerationTask:
    return task.model_copy(update={"parameters": {**task.parameters, "_prefail_error": error}})


def _validate_generation_tasks(tasks: list[GenerationTask]) -> None:
    for task in tasks:
        _assert_generation_inputs(task)


def _assert_generation_inputs(task: GenerationTask) -> None:
    provider = task.provider_type.value if task.provider_type is not None else task.engine.value
    params = task.parameters
    if provider == "gpt-sovits":
        missing = []
        if not _has_text(params.get("gpt_weights_path") or params.get("gpt_weights")):
            missing.append("gpt_weights_path")
        if not _has_text(params.get("sovits_weights_path") or params.get("sovits_weights")):
            missing.append("sovits_weights_path")
        if not _has_text(params.get("ref_audio_path") or params.get("reference_audio") or params.get("voice")):
            missing.append("ref_audio_path")
        if not _has_text(params.get("prompt_text")):
            missing.append("prompt_text")
        if missing:
            raise ValueError(f"line {task.line.id} GPT-SoVITS binding is incomplete: missing {', '.join(missing)}")
    if provider == "indextts" and not _has_text(params.get("voice") or params.get("ref_audio_path") or params.get("reference_audio")):
        raise ValueError(f"line {task.line.id} IndexTTS binding is incomplete: missing voice reference audio")


def _has_text(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _resolve_data_audio_path(data_root: Path, raw_path: str, allowed_roots: list[Path] | None = None) -> Path:
    path = _resolve_data_asset_path(data_root, raw_path, outside_detail="audio path is outside data root", allowed_roots=allowed_roots)
    _audio_media_type(path)
    return path


def _resolve_project_audio_file(project_audio_root: Path, data_root: Path, raw_path: str) -> Path | None:
    root = project_audio_root.resolve()
    requested = Path(raw_path)
    candidates = [requested] if requested.is_absolute() else [Path.cwd() / requested, data_root / requested, root / requested]
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    return None


def _resolve_data_asset_path(data_root: Path, raw_path: str, outside_detail: str = "asset path is outside data root", allowed_roots: list[Path] | None = None) -> Path:
    roots = [data_root.resolve()]
    for root in allowed_roots or []:
        if root.exists():
            roots.append(root.resolve())
    requested = Path(raw_path)
    candidates = [requested] if requested.is_absolute() else [Path.cwd() / requested, *[root / requested for root in roots]]
    for candidate in candidates:
        resolved = candidate.resolve()
        for root in roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return resolved
    raise HTTPException(status_code=400, detail=outside_detail)


def _audio_media_type(path: Path) -> str:
    suffix_media_types = {
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }
    media_type = suffix_media_types.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or ""
    if not media_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="asset is not an audio file")
    return media_type


# Magic-byte signatures for image types the avatar endpoint accepts. Checking
# the file header (not just the extension) stops a non-image file renamed to
# .png from being served as an image.
_IMAGE_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _image_media_type(path: Path) -> str:
    """Determine the image media type from the file's magic bytes. The
    extension is only used to pick among ambiguous signatures; a file whose
    bytes do not match any known image signature is rejected even if its
    name ends in .png/.jpg. This stops a renamed non-image from being served
    as an image."""
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        header = b""
    for signature, media_type in _IMAGE_SIGNATURES:
        if header.startswith(signature):
            # WebP needs the WEBP marker at offset 8.
            if media_type == "image/webp" and header[8:12] != b"WEBP":
                continue
            return media_type
    # Bytes did not match any image signature. If the file is empty, treat it
    # as a missing image; otherwise reject it as a non-image regardless of the
    # extension (prevents serving renamed payloads).
    if not header:
        raise HTTPException(status_code=400, detail="image is empty or unreadable")
    raise HTTPException(status_code=400, detail="asset is not an image")


def _manifest_summary(manifest: GenerationManifest) -> dict[str, int]:
    completed = 0
    failed = 0
    for history in manifest.lines.values():
        for version in history.versions:
            if version.status == "completed":
                completed += 1
            if version.status == "failed":
                failed += 1
    return {"completed": completed, "failed": failed, "total": completed + failed}


def _reject_mock_validation_services(tasks: list[GenerationTask], service_registry: ServiceRegistry) -> None:
    mock_services: list[str] = []
    for task in tasks:
        candidate_ids = [task.service_id, getattr(task.line, "service_override", None), *task.fallback_service_ids]
        for service_id in candidate_ids:
            if not service_id:
                continue
            try:
                endpoint = service_registry.get(service_id)
            except KeyError:
                continue
            if endpoint.base_url.startswith("mock://"):
                mock_services.append(endpoint.service_id)
    if mock_services:
        unique = ", ".join(sorted(set(mock_services)))
        raise HTTPException(status_code=409, detail=f"real validation cannot use mock endpoint: {unique}")


def _path_report(path: Path) -> dict[str, Any]:
    return {"path": str(path), "exists": path.exists(), "is_dir": path.is_dir()}


def _find_existing_character_for_candidate(existing: list[Character], candidate: dict[str, Any], character: Character) -> Character | None:
    candidate_keys = _character_identity_keys(character)
    for value in [
        candidate.get("logs_name"),
        candidate.get("logs_id"),
        *(candidate.get("aliases") or []),
        *(candidate.get("nicknames") or []),
        *(candidate.get("match_names") or []),
    ]:
        key = _identity_key(value)
        if key:
            candidate_keys.add(key)
    for item in existing:
        if candidate_keys.intersection(_character_identity_keys(item)):
            return item
    return None


def _character_identity_keys(character: Character) -> set[str]:
    values: list[Any] = [character.id, character.name, *character.aliases, *character.nicknames, *character.match_names]
    for profile in character.profiles:
        values.extend([profile.id, profile.name])
        for binding in profile.bindings:
            values.extend([binding.binding_id, binding.config.get("logs_name"), binding.config.get("logs_id"), binding.config.get("character_filter")])
    return {key for value in values if (key := _identity_key(value))}


def _identity_key(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value).casefold())


app = create_app()
