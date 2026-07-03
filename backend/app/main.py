from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pydantic import BaseModel

from app.adapter_factory import build_adapters
from app.hardware import collect_local_hardware_status
from app.models import Character, EngineName, GenerationManifest, GenerationTask, PROVIDER_ENGINE_DEFAULTS, ProjectCharacter, ProjectCharacterMode, ScriptProject
from app.parser import MultiProviderParser, OpenAICompatibleProvider, ParserProviderConfig, RuleBasedParser
from app.parser_config import ParserProvidersUpdate, load_parser_providers, public_parser_providers, save_parser_providers
from app.queue import GenerationJobManager, ServiceGenerationQueue
from app.resources import collect_voice_candidates
from app.role_library import candidate_to_character, common_logs_presets, freeze_project_character, match_project_characters, referenced_projects, resolve_project_characters, scan_logs_index_candidates, scan_role_library_candidates
from app.service_config import ServiceSettingsUpdate, public_service_settings, save_service_settings
from app.services import ServiceRegistry, ServiceRouter
from app.storage import ProjectStore
from app.supervisor import ServiceSupervisor

DEFAULT_REFERENCE_AUDIO_ROOT = Path(r"\\192.168.2.12\ai\项目\音色克隆\音源归档")
DEFAULT_DATA_ROOT = Path("data")
DEFAULT_RUNTIME_ROOT = Path("data") / ".runtime"
REPO_LOCK_PATH = Path(__file__).resolve().parents[2] / "repo.lock.json"

load_dotenv(".env.local")
load_dotenv(".env")


class ParseScriptRequest(BaseModel):
    text: str


class GenerateRequest(BaseModel):
    project_id: str
    tasks: list[GenerationTask]


class RoleLibraryScanRequest(BaseModel):
    limit: int = 80


class RoleLibraryImportRequest(BaseModel):
    candidate: dict[str, Any]


class ProjectCharactersUpdate(BaseModel):
    project_characters: list[ProjectCharacter]


def create_app(
    data_root: Path | str = DEFAULT_DATA_ROOT,
    reference_audio_root: Path | str = DEFAULT_REFERENCE_AUDIO_ROOT,
    services_path: Path | str | None = None,
    runtime_root: Path | str | None = None,
    parser_config_path: Path | str | None = None,
    env_path: Path | str | None = None,
) -> FastAPI:
    app = FastAPI(title="TTS More Orchestrator", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    supervisor = ServiceSupervisor(project_root=project_root, runtime_root=Path(runtime_root) if runtime_root else Path(data_root) / ".runtime")

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
        router = ServiceRouter(registry)
        queue = ServiceGenerationQueue(router)
        app.state.service_registry = registry
        app.state.service_router = router
        app.state.queue = queue
        app.state.job_manager = GenerationJobManager(queue, store)
        app.state.services_path = app.state.writable_services_path
        return public_service_settings(registry, env_file)

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
            "services": _service_health_with_supervisor(app.state.service_router, supervisor),
            "hardware": collect_local_hardware_status(),
        }

    @app.get("/api/startup/checks")
    def startup_checks() -> dict[str, Any]:
        resources = collect_voice_candidates(
            reference_audio_root=ref_root,
            gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root"),
            sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root"),
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
            gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root"),
            sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root"),
            indextts_model_dir=_indextts_model_dir(),
            runtime_checks=_runtime_checks(app.state.service_registry),
            limit=limit,
        )

    @app.post("/api/services/{service_id}/start")
    def start_service(service_id: str) -> dict[str, Any]:
        try:
            endpoint = service_registry.get(service_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="service not found") from exc
        return supervisor.start(endpoint)

    @app.post("/api/services/{service_id}/stop")
    def stop_service(service_id: str) -> dict[str, Any]:
        return supervisor.stop(service_id)

    @app.get("/api/services/{service_id}/logs")
    def service_logs(service_id: str, lines: int = 120) -> dict[str, Any]:
        return supervisor.logs(service_id, lines=lines)

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
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/repos")
    def repos() -> dict[str, Any]:
        return _read_repo_lock()

    @app.post("/api/parse-script")
    def parse_script(request: ParseScriptRequest) -> dict[str, Any]:
        if not request.text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        return app.state.parser.parse(request.text).model_dump(mode="json")

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

    @app.get("/api/characters")
    def get_characters() -> list[dict[str, Any]]:
        return [character.model_dump(mode="json") for character in store.load_characters()]

    @app.put("/api/characters")
    def put_characters(characters: list[Character]) -> dict[str, str]:
        store.save_characters(characters)
        return {"status": "saved"}

    @app.get("/api/character-library")
    def character_library() -> dict[str, Any]:
        return {"characters": [character.model_dump(mode="json") for character in store.load_characters()]}

    @app.post("/api/character-library/scan")
    def scan_character_library(request: RoleLibraryScanRequest) -> dict[str, Any]:
        return {
            "candidates": scan_role_library_candidates(
                reference_audio_root=ref_root,
                gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root"),
                sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root"),
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
                gpt_weights_roots=_configured_weight_roots(store.load_characters(), "gpt_weights_root"),
                sovits_weights_roots=_configured_weight_roots(store.load_characters(), "sovits_weights_root"),
                service_id=service_id,
                gradio_candidates=gradio_candidates,
                limit=limit,
            ),
            "diagnostics": diagnostics,
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
    def import_common_logs_preset_characters(service_id: str | None = None) -> dict[str, Any]:
        candidate_payload = character_library_logs_candidates(service_id=service_id, include_gradio=False, limit=200)
        preset_names = {item["name"] for item in common_logs_presets()}
        preset_candidates = [item for item in candidate_payload["candidates"] if item.get("name") in preset_names]
        existing = store.load_characters()
        existing_ids = {character.id for character in existing}
        imported: list[Character] = []
        skipped: list[str] = []
        for candidate in preset_candidates:
            character = candidate_to_character(candidate)
            if character.id in existing_ids:
                skipped.append(character.id)
                continue
            imported.append(character)
            existing_ids.add(character.id)
        if imported:
            store.save_characters([*existing, *imported])
        return {"imported": [character.model_dump(mode="json") for character in imported], "skipped": skipped}

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

    @app.post("/api/projects/{project_id}/reference-audio/upload")
    async def upload_project_reference_audio(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
            raise HTTPException(status_code=400, detail="unsupported audio file")
        output_dir = store.project_dir(project_id) / "reference_audio" / "temporary"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / _safe_upload_name(file.filename or f"reference{suffix}")
        counter = 1
        while output_path.exists():
            output_path = output_dir / f"{output_path.stem}-{counter}{suffix}"
            counter += 1
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="audio file is empty")
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
        store.save_project(project_id, project)
        return {"project_characters": [item.model_dump(mode="json") for item in project.project_characters]}

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
            resolved = _resolve_project_audio_file(store.project_dir(project_id) / "audio", Path(app.state.store.root), target.audio_path)
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
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        manifest = store.load_manifest(request.project_id)
        output_dir = store.project_dir(request.project_id) / "audio"
        queue.run(tasks, manifest, output_dir=output_dir)
        store.save_manifest(manifest)
        return manifest.model_dump(mode="json")

    @app.post("/api/jobs/generation")
    def create_generation_job(request: GenerateRequest) -> dict[str, Any]:
        try:
            tasks = _enrich_tasks_for_project(store, request.project_id, request.tasks)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = app.state.job_manager.submit(request.project_id, tasks)
        return job.model_dump(mode="json")

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
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if _service_mode() == "real":
            _reject_mock_validation_services(tasks, service_registry)
        manifest = store.load_manifest(request.project_id)
        output_dir = store.project_dir(request.project_id) / "audio"
        queue.run(tasks, manifest, output_dir=output_dir)
        store.save_manifest(manifest)
        return {"summary": _manifest_summary(manifest), "manifest": manifest.model_dump(mode="json")}

    @app.get("/api/reference-audio/scan")
    def scan_reference_audio(limit: int = 80) -> dict[str, Any]:
        return {"root": str(ref_root), "groups": _scan_reference_audio(ref_root, limit=limit)}

    @app.get("/api/audio")
    def get_audio(path: str) -> FileResponse:
        audio_path = _resolve_data_audio_path(Path(app.state.store.root), path)
        if not audio_path.is_file():
            raise HTTPException(status_code=404, detail="audio not found")
        return FileResponse(audio_path, media_type="audio/wav")

    return app


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


def _build_parser(config_path: Path | None = None) -> MultiProviderParser:
    providers: list[OpenAICompatibleProvider] = []
    if config_path is not None:
        for item in load_parser_providers(config_path):
            providers.append(OpenAICompatibleProvider(ParserProviderConfig.model_validate(item.model_dump(mode="python"))))
    else:
        raw = os.environ.get("TTS_MORE_PARSER_PROVIDERS")
        if raw:
            for item in json.loads(raw):
                providers.append(OpenAICompatibleProvider(ParserProviderConfig.model_validate(item)))
    return MultiProviderParser(providers, fallback=RuleBasedParser())


def _load_service_registry(path: Path) -> ServiceRegistry:
    registry = ServiceRegistry.load(path)
    if _service_mode() == "mock":
        return _mocked_registry(registry)
    return registry


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


def _service_health_with_supervisor(router: ServiceRouter, supervisor: ServiceSupervisor) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for service in router.health():
        endpoint = router.registry.get(service["service_id"])
        output.append({**service, "supervisor": supervisor.status(endpoint)})
    return output


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


def _configured_weight_roots(characters: list[Character], key: str) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for character in characters:
        for profile in character.profiles:
            values = [profile.config.get(key)]
            values.extend(binding.config.get(key) for binding in profile.bindings)
            for value in values:
                if value and str(value) not in seen:
                    roots.append(Path(str(value)))
                    seen.add(str(value))
    return roots


def _safe_upload_name(filename: str) -> str:
    path = Path(filename)
    stem = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", path.stem).strip("._-") or "reference"
    suffix = path.suffix.lower()
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
        if line.temporary_binding is None and stored_line and stored_line.temporary_binding:
            line = line.model_copy(update={"temporary_binding": stored_line.temporary_binding})
            task = task.model_copy(update={"line": line})
        if line.temporary_binding is not None:
            binding = line.temporary_binding
            parameters = {**binding.config, **task.parameters, "binding_source": "temporary"}
            output.append(
                task.model_copy(
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
            )
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
        parameters = {**profile.config, **(binding.config if binding else {}), **task.parameters}
        output.append(
            task.model_copy(
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
        )
    return output


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


def _resolve_data_audio_path(data_root: Path, raw_path: str) -> Path:
    root = data_root.resolve()
    requested = Path(raw_path)
    candidates = [requested] if requested.is_absolute() else [Path.cwd() / requested, root / requested]
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    raise HTTPException(status_code=400, detail="audio path is outside data root")


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


def _scan_reference_audio(root: Path, limit: int) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    groups: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        audio_paths = [
            str(path)
            for path in child.rglob("*")
            if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
        ]
        if audio_paths:
            groups.append({"id": child.name, "name": child.name, "path": str(child), "audio_count": len(audio_paths), "samples": audio_paths[:5]})
        if len(groups) >= limit:
            break
    return groups


app = create_app()
