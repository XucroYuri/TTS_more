from __future__ import annotations

import json
import os
import base64
import hashlib
import io
import re
import time
import uuid
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName, ProviderType, TTSIntent, TTSServiceEndpoint, VoiceBinding
from app.net_guard import scrub_error


class TTSServiceClient(Protocol):
    endpoint: TTSServiceEndpoint

    def health(self) -> dict[str, Any]:
        ...

    def capabilities(self) -> dict[str, Any]:
        ...

    def load(self, profile: str, parameters: dict[str, Any] | None = None) -> None:
        ...

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        ...

    def unload(self) -> None:
        ...


@dataclass(frozen=True)
class ServiceRoute:
    endpoint: TTSServiceEndpoint
    client: TTSServiceClient
    binding: VoiceBinding | None = None


class ServiceRegistry:
    def __init__(self, services: list[TTSServiceEndpoint]) -> None:
        self.services = services
        self._by_id = {service.service_id: service for service in services}

    @classmethod
    def load(cls, path: Path) -> "ServiceRegistry":
        if not path.exists():
            return cls.default_local(repo_root=Path("repo"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([TTSServiceEndpoint.model_validate(item) for item in raw])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([service.model_dump(mode="json") for service in self.services], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def default_local(cls, repo_root: Path) -> "ServiceRegistry":
        return cls(
            [
                TTSServiceEndpoint(
                    service_id="local-gpt-sovits",
                    display_name="GPT-SoVITS Gradio",
                    service_kind="tts",
                    engine=EngineName.GPT_SOVITS,
                    provider_type=ProviderType.GPT_SOVITS,
                    api_contract="gradio-gpt-sovits-webui",
                    base_url="http://127.0.0.1:9872",
                    network_scope="localhost",
                    mode="external",
                    managed=False,
                    source_profile="local_endpoint",
                    resource_group="gradio-gpu-0",
                    priority=10,
                    capabilities=["tts", "trained_weights_voice", "reference_audio_voice", "gpt-weights", "sovits-weights", "wav_output", "gradio_webui"],
                ),
                TTSServiceEndpoint(
                    service_id="local-indextts",
                    display_name="IndexTTS Gradio",
                    service_kind="tts",
                    engine=EngineName.INDEX_TTS,
                    provider_type=ProviderType.INDEX_TTS,
                    api_contract="gradio-indextts2-webui",
                    base_url="http://127.0.0.1:7860",
                    network_scope="localhost",
                    mode="external",
                    managed=False,
                    source_profile="local_endpoint",
                    resource_group="gradio-gpu-0",
                    priority=20,
                    capabilities=["tts", "reference_audio_voice", "emotion_text", "emotion_audio", "wav_output", "gradio_webui"],
                ),
                TTSServiceEndpoint(
                    service_id="local-cosyvoice",
                    display_name="CosyVoice Gradio",
                    service_kind="tts",
                    engine=EngineName.COSYVOICE,
                    provider_type=ProviderType.COSYVOICE,
                    api_contract="gradio-cosyvoice-webui",
                    base_url="http://127.0.0.1:50000",
                    network_scope="localhost",
                    mode="external",
                    managed=False,
                    enabled=False,
                    source_profile="local_endpoint",
                    resource_group="gradio-gpu-0",
                    priority=30,
                    capabilities=["tts", "reference_audio_voice", "zero_shot_voice", "cross_lingual_voice", "style_instruction", "wav_output", "gradio_webui"],
                    default_params={"mode": "zero_shot", "response_format": "wav"},
                ),
            ]
        )

    def get(self, service_id: str) -> TTSServiceEndpoint:
        return self._by_id[service_id]

    def by_engine(self, engine: EngineName) -> list[TTSServiceEndpoint]:
        return sorted(
            [service for service in self.services if _is_generation_candidate(service) and service.engine == engine],
            key=lambda service: service.priority,
        )

    def by_provider(self, provider_type: ProviderType) -> list[TTSServiceEndpoint]:
        return sorted(
            [service for service in self.services if _is_generation_candidate(service) and service.provider_type == provider_type],
            key=lambda service: service.priority,
        )


def build_load_signature(endpoint: TTSServiceEndpoint, parameters: dict[str, Any]) -> str:
    if endpoint.provider_type == ProviderType.GPT_SOVITS or endpoint.engine == EngineName.GPT_SOVITS:
        parts = [
            f"service_id={endpoint.service_id}",
            f"logs_name={parameters.get('logs_name', parameters.get('logs_id', ''))}",
            f"gpt_weights_path={parameters.get('gpt_weights_path', parameters.get('gpt_weights', ''))}",
            f"sovits_weights_path={parameters.get('sovits_weights_path', parameters.get('sovits_weights', ''))}",
            f"ref_audio_path={parameters.get('ref_audio_path', parameters.get('reference_audio', ''))}",
            f"prompt_text={parameters.get('prompt_text', '')}",
            f"prompt_lang={parameters.get('prompt_lang', '')}",
            f"text_lang={parameters.get('text_lang', '')}",
        ]
        return "|".join(parts)
    if endpoint.provider_type == ProviderType.INDEX_TTS or endpoint.engine == EngineName.INDEX_TTS:
        parts = [
            f"service_id={endpoint.service_id}",
            f"voice={parameters.get('voice', parameters.get('ref_audio_path', parameters.get('reference_audio', '')))}",
            f"emotion_mode={parameters.get('emotion_mode', 'same_as_voice')}",
            f"emotion_audio={parameters.get('emotion_audio', '')}",
            f"emotion_text={parameters.get('emotion_text', '')}",
        ]
        return "|".join(parts)
    if endpoint.provider_type == ProviderType.COSYVOICE or endpoint.engine == EngineName.COSYVOICE:
        parts = [
            f"service_id={endpoint.service_id}",
            f"mode={parameters.get('mode', 'zero_shot')}",
            f"speaker_id={parameters.get('speaker_id', parameters.get('voice', ''))}",
            f"prompt_audio_path={parameters.get('prompt_audio_path', parameters.get('prompt_audio', parameters.get('reference_audio', '')))}",
            f"prompt_text={parameters.get('prompt_text', '')}",
            f"instruct_text={parameters.get('instruct_text', parameters.get('instruction', ''))}",
            f"speed={parameters.get('speed', '')}",
            f"seed={parameters.get('seed', '')}",
        ]
        return "|".join(parts)
    return "|".join(
        [
            f"service_id={endpoint.service_id}",
            f"model={parameters.get('model', '')}",
            f"voice={parameters.get('voice', parameters.get('voice_id', parameters.get('voice_name', '')))}",
        ]
    )


class ServiceRouter:
    def __init__(
        self,
        registry: ServiceRegistry,
        clients: dict[str, TTSServiceClient] | None = None,
    ) -> None:
        self.registry = registry
        self.clients = clients or {service.service_id: build_service_client(service) for service in registry.services}

    def resolve(
        self,
        engine: EngineName,
        service_id: str | None = None,
        fallback_service_ids: list[str] | None = None,
    ) -> ServiceRoute:
        for endpoint in self._candidate_endpoints(engine, service_id, fallback_service_ids or []):
            client = self.clients.get(endpoint.service_id)
            if client is not None and self._client_ready(client):
                return ServiceRoute(endpoint=endpoint, client=client)
        raise RuntimeError(f"no ready TTS service for engine {engine.value}")

    def resolve_intent(self, intent: TTSIntent) -> ServiceRoute:
        for binding in intent.bindings:
            if not _has_capabilities(binding.capabilities, intent.required_capabilities):
                continue
            for endpoint in self._candidate_endpoints_for_binding(binding, intent):
                if not _endpoint_can_use_binding(endpoint, binding):
                    continue
                if not _has_capabilities(endpoint.capabilities, ["tts", *intent.required_capabilities]):
                    continue
                client = self.clients.get(endpoint.service_id)
                if client is not None and self._client_ready(client):
                    return ServiceRoute(endpoint=endpoint, client=client, binding=binding)
        if intent.bindings:
            raise RuntimeError("no ready TTS service matches requested voice binding")
        for endpoint in self._candidate_endpoints_for_intent(intent):
            if not _has_capabilities(endpoint.capabilities, ["tts", *intent.required_capabilities]):
                continue
            client = self.clients.get(endpoint.service_id)
            if client is not None and self._client_ready(client):
                return ServiceRoute(endpoint=endpoint, client=client)
        raise RuntimeError("no ready TTS service matches requested capabilities")

    def resolve_task(self, task: Any) -> ServiceRoute:
        if getattr(task, "provider_type", None) is not None or getattr(task, "required_capabilities", None):
            binding = None
            if getattr(task, "provider_type", None) is not None:
                binding = VoiceBinding(
                    binding_id=task.binding_id or task.profile,
                    provider_type=task.provider_type,
                    service_id=task.service_id or getattr(task.line, "service_override", None),
                    fallback_services=task.fallback_service_ids,
                    capabilities=task.required_capabilities,
                    config=task.parameters,
                )
            return self.resolve_intent(
                TTSIntent(
                    text=task.line.text,
                    character_id=task.line.character_id,
                    language=task.line.language,
                    note=task.line.note,
                    required_capabilities=task.required_capabilities,
                    bindings=[binding] if binding else [],
                    service_id=task.service_id or getattr(task.line, "service_override", None),
                    fallback_service_ids=task.fallback_service_ids,
                )
            )
        service_id = task.service_id or getattr(task.line, "service_override", None)
        return self.resolve(task.engine, service_id=service_id, fallback_service_ids=task.fallback_service_ids)

    def health(self) -> list[dict[str, Any]]:
        if not self.registry.services:
            return []
        max_workers = min(len(self.registry.services), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {endpoint.service_id: executor.submit(self._service_health, endpoint) for endpoint in self.registry.services}
            return [futures[endpoint.service_id].result() for endpoint in self.registry.services]

    def _service_health(self, endpoint: TTSServiceEndpoint) -> dict[str, Any]:
        if not endpoint.enabled:
            return {
                **endpoint.model_dump(mode="json"),
                "ready": False,
                "health": {"ready": False, "status": "disabled"},
            }
        client = self.clients.get(endpoint.service_id)
        try:
            health = client.health() if client is not None else {"ready": False, "error": "client not configured"}
        except Exception as exc:
            health = {"ready": False, "error": scrub_error(exc, getattr(endpoint, "base_url", None))}
        return {
            **endpoint.model_dump(mode="json"),
            "ready": bool(health.get("ready")),
            "health": health,
        }

    def _candidate_endpoints_for_binding(self, binding: VoiceBinding, intent: TTSIntent) -> list[TTSServiceEndpoint]:
        seen: set[str] = set()
        candidates: list[TTSServiceEndpoint] = []
        explicit_ids = [intent.service_id, binding.service_id, *intent.fallback_service_ids, *binding.fallback_services]

        def add(service: TTSServiceEndpoint) -> None:
            if _is_generation_candidate(service) and service.provider_type == binding.provider_type and service.service_id not in seen:
                candidates.append(service)
                seen.add(service.service_id)

        for service_id in explicit_ids:
            if service_id:
                add(self.registry.get(service_id))
        if not any(explicit_ids):
            for service in self.registry.by_provider(binding.provider_type):
                add(service)
        return candidates

    def _candidate_endpoints_for_intent(self, intent: TTSIntent) -> list[TTSServiceEndpoint]:
        return sorted([service for service in self.registry.services if _is_generation_candidate(service)], key=lambda service: service.priority)

    def _candidate_endpoints(
        self,
        engine: EngineName,
        service_id: str | None,
        fallback_service_ids: list[str],
    ) -> list[TTSServiceEndpoint]:
        seen: set[str] = set()
        candidates: list[TTSServiceEndpoint] = []

        def add(service: TTSServiceEndpoint) -> None:
            if _is_generation_candidate(service) and service.engine == engine and service.service_id not in seen:
                candidates.append(service)
                seen.add(service.service_id)

        if service_id:
            add(self.registry.get(service_id))
        for fallback_id in fallback_service_ids:
            add(self.registry.get(fallback_id))
        for service in self.registry.by_engine(engine):
            add(service)
        return candidates

    def _client_ready(self, client: TTSServiceClient) -> bool:
        try:
            return bool(client.health().get("ready"))
        except Exception:
            return False


def build_service_client(endpoint: TTSServiceEndpoint, transport: httpx.BaseTransport | None = None) -> TTSServiceClient:
    if endpoint.base_url.startswith("mock://"):
        return MockServiceClient(endpoint)
    if endpoint.api_contract == "comfyui-tts-v1":
        return ComfyUITTSClient(endpoint, transport=transport)
    if endpoint.api_contract == "tts-more-v1":
        return HttpTTSServiceClient(endpoint, transport=transport)
    if endpoint.api_contract.startswith("gradio-"):
        return GradioWebUIServiceClient(endpoint, transport=transport)
    if endpoint.api_contract == "gpt-sovits-api-v2" or "gpt-sovits-api-v2" in endpoint.capabilities:
        return GPTSoVITSApiV2ServiceClient(endpoint, transport=transport)
    if endpoint.provider_type == ProviderType.OPENAI:
        return OpenAISpeechClient(endpoint, transport=transport)
    if endpoint.provider_type == ProviderType.XAI:
        return XAISpeechClient(endpoint, transport=transport)
    if endpoint.provider_type == ProviderType.GEMINI:
        return GeminiSpeechClient(endpoint, transport=transport)
    if endpoint.provider_type == ProviderType.VOLCENGINE:
        return VolcengineSpeechClient(endpoint, transport=transport)
    return HttpTTSServiceClient(endpoint, transport=transport)


def require_remote_artifact_transfer(endpoint: TTSServiceEndpoint) -> None:
    if endpoint.mode != "external" or endpoint.network_scope == "localhost":
        return
    normalized = {capability.replace("_", "-").casefold() for capability in endpoint.capabilities}
    if "artifact-transfer" not in normalized:
        raise RuntimeError(f"external worker {endpoint.service_id} is missing artifact-transfer capability")


def _endpoint_can_use_binding(endpoint: TTSServiceEndpoint, binding: VoiceBinding) -> bool:
    params = binding.config or {}
    origin_service_id = str(params.get("path_service_id") or params.get("asset_service_id") or params.get("source_service_id") or "")
    if origin_service_id and origin_service_id == endpoint.service_id:
        return True
    if endpoint.network_scope == "localhost":
        return True
    if endpoint.provider_type == ProviderType.GPT_SOVITS or endpoint.engine == EngineName.GPT_SOVITS:
        for field in ("gpt_weights_path", "gpt_weights", "sovits_weights_path", "sovits_weights"):
            value = params.get(field)
            if value and not _endpoint_can_access_path(endpoint, str(value)):
                return False
    return True


def _is_generation_candidate(service: TTSServiceEndpoint) -> bool:
    if not service.enabled or service.service_kind != "tts":
        return False
    if service.setup_state in {"not_configured", "repo_missing", "env_missing", "endpoint_unreachable"}:
        return False
    return True


def _endpoint_can_access_path(endpoint: TTSServiceEndpoint, raw_path: str) -> bool:
    if _is_remote_reference(raw_path):
        return True
    if not _looks_like_absolute_filesystem_path(raw_path):
        return True
    roots = _endpoint_accessible_roots(endpoint)
    if not roots:
        return False
    normalized = _normalize_path_for_compare(raw_path)
    return any(normalized == root or normalized.startswith(root.rstrip("/") + "/") for root in roots)


def _endpoint_accessible_roots(endpoint: TTSServiceEndpoint) -> list[str]:
    params = endpoint.default_params or {}
    roots: list[str] = []

    def add(value: Any) -> None:
        if not value:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        roots.append(_normalize_path_for_compare(str(value)))

    for key in (
        "accessible_path_roots",
        "remote_path_roots",
        "gpt_weights_root",
        "sovits_weights_root",
        "reference_audio_root",
        "logs_root",
        "logs_roots",
    ):
        add(params.get(key))
    return roots


def _is_remote_reference(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("http://", "https://", "file=", "/file=", "data:"))


def _looks_like_absolute_filesystem_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\") or value.startswith("//") or value.startswith("/"))


def _normalize_path_for_compare(value: str) -> str:
    # Normalize to forward slashes — the portable neutral form. Both Windows
    # backslashes and POSIX forward slashes fold to "/", so comparisons are
    # correct on every host platform. (Previously this forced backslashes,
    # which worked only by coincidence on POSIX.)
    return value.replace("\\", "/").rstrip("/").casefold()


class MockServiceClient:
    def __init__(self, endpoint: TTSServiceEndpoint) -> None:
        self.endpoint = endpoint
        self.loaded_profile: str | None = None

    def health(self) -> dict[str, Any]:
        return {"engine": self.endpoint.engine.value, "ready": True, "mode": "mock-service"}

    def capabilities(self) -> dict[str, Any]:
        return {"capabilities": self.endpoint.capabilities}

    def load(self, profile: str, parameters: dict[str, Any] | None = None) -> None:
        self.loaded_profile = profile

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(_tiny_wav_bytes())
        return SynthesisResult(audio_path=request.output_path, metadata={"service_id": self.endpoint.service_id})

    def unload(self) -> None:
        self.loaded_profile = None


class HttpTTSServiceClient:
    def __init__(self, endpoint: TTSServiceEndpoint, transport: httpx.BaseTransport | None = None) -> None:
        self.endpoint = endpoint
        self.transport = transport
        self._uploaded_references: dict[tuple[str, int, int], tuple[str, float]] = {}

    def health(self) -> dict[str, Any]:
        missing = self._missing_env()
        if missing:
            return {"engine": self.endpoint.engine.value, "ready": False, "status": "needs key", "missing_env": missing}
        url = self.endpoint.health_url or self.endpoint.base_url.rstrip("/") + "/health"
        try:
            with httpx.Client(timeout=_health_timeout_seconds(), transport=self.transport) as client:
                response = client.get(url, headers=self._headers())
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return {"engine": self.endpoint.engine.value, "ready": False, "error": scrub_error(exc, self.endpoint.base_url)}
        return {"engine": self.endpoint.engine.value, "ready": True, **payload}

    def capabilities(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=5.0, transport=self.transport) as client:
                response = client.get(self.endpoint.base_url.rstrip("/") + "/capabilities", headers=self._headers())
                response.raise_for_status()
                return response.json()
        except Exception:
            return {"capabilities": self.endpoint.capabilities}

    def model_catalog(self, limit: int = 120) -> dict[str, Any]:
        with httpx.Client(timeout=self.endpoint.default_params.get("timeout_seconds", 30.0), transport=self.transport) as client:
            response = client.get(self.endpoint.base_url.rstrip("/") + "/models", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        return _gpt_sovits_models_payload_to_catalog(self.endpoint, payload, source="worker", limit=limit)

    def model_samples(self, logs_name: str, limit: int = 120) -> dict[str, Any]:
        encoded_name = quote(logs_name, safe="")
        with httpx.Client(timeout=self.endpoint.default_params.get("timeout_seconds", 30.0), transport=self.transport) as client:
            response = client.get(self.endpoint.base_url.rstrip("/") + f"/models/{encoded_name}/samples", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        return {
            "service_id": self.endpoint.service_id,
            "logs_name": logs_name,
            "samples": list(payload.get("samples") or [])[:limit],
            "raw": payload,
        }

    def load(self, profile: str, parameters: dict[str, Any] | None = None) -> None:
        if self._uses_artifact_delivery():
            self._require_artifact_transfer()
        prepared = self._prepare_remote_parameters(parameters or {}) if self._uses_artifact_delivery() else (parameters or {})
        payload = {"profile": profile, "parameters": prepared}
        with httpx.Client(timeout=120.0, transport=self.transport) as client:
            response = client.post(self.endpoint.base_url.rstrip("/") + "/load", json=payload, headers=self._headers())
            response.raise_for_status()

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        artifact_delivery = self._uses_artifact_delivery()
        if artifact_delivery:
            self._require_artifact_transfer()
        parameters = self._prepare_remote_parameters(request.parameters) if artifact_delivery else request.parameters
        payload = {
            "line": request.line.model_dump(mode="json"),
            "profile": request.profile,
            "output_path": str(request.output_path),
            "parameters": parameters,
            "delivery": "artifact" if artifact_delivery else "path",
        }
        with httpx.Client(timeout=request.parameters.get("timeout_seconds", 600.0), transport=self.transport) as client:
            response = client.post(self.endpoint.base_url.rstrip("/") + "/synthesize", json=payload, headers=self._headers())
            response.raise_for_status()
            data = response.json()
            if artifact_delivery:
                self._download_artifact(client, data, request.output_path)
                return SynthesisResult(audio_path=request.output_path, metadata=data.get("metadata", {}))
        return SynthesisResult(audio_path=Path(data["audio_path"]), metadata=data.get("metadata", {}))

    def unload(self) -> None:
        with httpx.Client(timeout=120.0, transport=self.transport) as client:
            response = client.post(self.endpoint.base_url.rstrip("/") + "/unload", headers=self._headers())
            response.raise_for_status()

    def _headers(self) -> dict[str, str]:
        if not self.endpoint.auth_header_env:
            api_key_env = self.endpoint.auth_profile.get("api_key_env")
            if api_key_env:
                value = os.environ.get(api_key_env)
                return {"Authorization": f"Bearer {value}"} if value else {}
            return {}
        value = os.environ.get(self.endpoint.auth_header_env)
        return {"Authorization": value} if value else {}

    def _missing_env(self) -> list[str]:
        keys = [value for key, value in self.endpoint.auth_profile.items() if key.endswith("_env")]
        if self.endpoint.auth_header_env:
            keys.append(self.endpoint.auth_header_env)
        return [key for key in keys if not os.environ.get(key)]

    def _uses_remote_artifacts(self) -> bool:
        return self.endpoint.mode == "external" and self.endpoint.network_scope != "localhost"

    def _uses_artifact_delivery(self) -> bool:
        return self._uses_remote_artifacts() or str(self.endpoint.default_params.get("delivery") or "path") == "artifact"

    def _require_artifact_transfer(self) -> None:
        normalized = {capability.replace("_", "-").casefold() for capability in self.endpoint.capabilities}
        if "artifact-transfer" not in normalized:
            raise RuntimeError(f"worker {self.endpoint.service_id} is missing artifact-transfer capability")

    def _prepare_remote_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(parameters)
        scalar_fields = {
            "ref_audio_path",
            "reference_audio",
            "voice",
            "prompt_audio_path",
            "prompt_audio",
            "prompt_wav_upload",
            "emotion_audio",
            "voice_reference_audio",
        }
        for field in scalar_fields:
            value = prepared.get(field)
            if isinstance(value, (str, Path)) and self._is_local_upload(value):
                prepared[field] = self._upload_reference(Path(value))
        values = prepared.get("aux_ref_audio_paths")
        if isinstance(values, (list, tuple)):
            prepared["aux_ref_audio_paths"] = [
                self._upload_reference(Path(value))
                if isinstance(value, (str, Path)) and self._is_local_upload(value)
                else value
                for value in values
            ]
        return prepared

    @staticmethod
    def _is_local_upload(value: str | Path) -> bool:
        try:
            return Path(value).is_file()
        except OSError:
            return False

    def _upload_reference(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
        cache_key = (str(resolved), stat.st_mtime_ns, stat.st_size)
        cached = self._uploaded_references.get(cache_key)
        try:
            configured_cache_seconds = float(os.environ.get("TTS_MORE_REFERENCE_UPLOAD_CACHE_SECONDS", "3600"))
        except ValueError:
            configured_cache_seconds = 3600.0
        cache_seconds = min(23 * 60 * 60, max(0.0, configured_cache_seconds))
        if cached and time.monotonic() - cached[1] < cache_seconds:
            return cached[0]
        with httpx.Client(timeout=120.0, transport=self.transport) as client:
            with resolved.open("rb") as handle:
                response = client.post(
                    self.endpoint.base_url.rstrip("/") + "/upload_ref",
                    files={"file": (resolved.name, handle, "application/octet-stream")},
                    headers=self._headers(),
                )
            response.raise_for_status()
            remote_path = str(response.json().get("path") or "")
        if not remote_path:
            raise RuntimeError(f"worker {self.endpoint.service_id} returned no uploaded reference path")
        self._uploaded_references[cache_key] = (remote_path, time.monotonic())
        return remote_path

    def _download_artifact(self, client: httpx.Client, data: dict[str, Any], output_path: Path) -> None:
        artifact_id = str(data.get("artifact_id") or "")
        download_url = str(data.get("download_url") or "")
        expected_hash = str(data.get("sha256") or "").casefold()
        try:
            expected_size = int(data["size_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("worker artifact response has invalid size_bytes") from exc
        expected_path = f"/artifacts/{artifact_id}"
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id) or download_url != expected_path:
            raise RuntimeError("worker artifact response has an invalid download URL")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise RuntimeError("worker artifact response has an invalid sha256")
        max_bytes = 100 * 1024 * 1024
        if expected_size < 1 or expected_size > max_bytes:
            raise RuntimeError("worker artifact exceeds download limit")

        response = client.get(self.endpoint.base_url.rstrip("/") + download_url, headers=self._headers())
        response.raise_for_status()
        content = response.content
        if len(content) != expected_size or len(content) > max_bytes:
            raise RuntimeError("worker artifact size mismatch")
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise RuntimeError("worker artifact sha256 mismatch")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, output_path)
        finally:
            temp_path.unlink(missing_ok=True)
        delete_response = client.delete(
            self.endpoint.base_url.rstrip("/") + expected_path,
            headers=self._headers(),
        )
        delete_response.raise_for_status()


def _gpt_sovits_models_payload_to_catalog(
    endpoint: TTSServiceEndpoint,
    payload: dict[str, Any],
    *,
    source: str,
    limit: int,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for item in (payload.get("models") or [])[:limit]:
        name = str(item.get("name") or item.get("model_name") or "").strip()
        if not name:
            continue
        candidate = _logs_candidate({}, endpoint.service_id, name, source=source)
        candidate["sample_count"] = int(item.get("sample_count") or 0)
        candidate["has_training_data"] = bool(item.get("has_training_data") or candidate["sample_count"] > 0)
        for path in item.get("gpt_weights") or []:
            option = {
                "name": Path(str(path)).name or str(path),
                "path": str(path),
                "value": str(path),
                "score": _weight_score_tuple(str(path)),
            }
            candidate.setdefault("gpt_weights", []).append(option)
            _prefer_recommended(candidate, "gpt", option)
        for path in item.get("sovits_weights") or []:
            option = {
                "name": Path(str(path)).name or str(path),
                "path": str(path),
                "value": str(path),
                "score": _weight_score_tuple(str(path)),
            }
            candidate.setdefault("sovits_weights", []).append(option)
            _prefer_recommended(candidate, "sovits", option)
        candidates.append(candidate)
    return {"service_id": endpoint.service_id, "candidates": candidates, "raw": payload}


class GradioWebUIServiceClient(HttpTTSServiceClient):
    """FALLBACK client for services reachable only via their Gradio WebUI.

    The primary integration path is the non-invasive tts-more-v1 worker
    (see backend/app/workers/*), which imports the upstream model directly and
    exposes a complete REST contract. This Gradio client is retained as a
    limited fallback for users who run the upstream Gradio WebUI directly.

    Limitations of the Gradio path:
    - Model/reference auto-discovery depends on fork-specific api_names
      (on_select_ref_audio, update_model_choices) that upstream official
      GPT-SoVITS does not define; discovery yields partial/empty results
      against upstream builds.
    - The extended synthesis params (if_freeze, aux_ref_audio_paths,
      sample_steps, super_sampling) match the v2ProPlus fork signature; upstream
      may reject or ignore them.
    - The 3-10s reference-audio hard limit in upstream blocks some inputs.

    Prefer the worker for full capability; use Gradio only when the worker
    cannot be deployed.
    """

    def __init__(self, endpoint: TTSServiceEndpoint, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(endpoint, transport=transport)
        self._config_cache: dict[str, Any] | None = None

    def load(self, profile: str, parameters: dict[str, Any] | None = None) -> None:
        # Gradio WebUIs keep model state behind their own event handlers. GPT-SoVITS
        # switches weights inside synthesize(), while IndexTTS has no explicit load API.
        return

    def unload(self) -> None:
        return

    def health(self) -> dict[str, Any]:
        missing = self._missing_env()
        if missing:
            return {
                "engine": self.endpoint.engine.value,
                "ready": False,
                "state": "blocked",
                "severity": "danger",
                "status": "needs key",
                "auth_ok": False,
                "missing_env": missing,
            }
        try:
            payload = self._config(timeout=_health_timeout_seconds())
        except httpx.TimeoutException as exc:
            return {
                "engine": self.endpoint.engine.value,
                "ready": False,
                "state": "partial",
                "severity": "attention",
                "reachable": True,
                "port_reachable": True,
                "config_ok": False,
                "required_api_ok": False,
                "auth_ok": True,
                "status": "config timeout",
                "error": scrub_error(exc, self.endpoint.base_url),
            }
        except Exception as exc:
            return {
                "engine": self.endpoint.engine.value,
                "ready": False,
                "state": "blocked",
                "severity": "danger",
                "reachable": False,
                "port_reachable": False,
                "config_ok": False,
                "required_api_ok": False,
                "auth_ok": True,
                "error": scrub_error(exc, self.endpoint.base_url),
            }

        api_names = sorted(
            {
                item.get("api_name")
                for item in payload.get("dependencies", [])
                if isinstance(item, dict) and item.get("api_name")
            }
        )
        expected = _expected_gradio_api_names(self.endpoint.api_contract)
        missing_api_names = [name for name in expected if name not in api_names]
        status = "unsupported gradio app" if missing_api_names else "ready"
        state = "blocked" if missing_api_names else "ready"
        severity = "danger" if missing_api_names else "ready"
        return {
            "engine": self.endpoint.engine.value,
            "ready": not missing_api_names,
            "state": state,
            "severity": severity,
            "reachable": True,
            "port_reachable": True,
            "config_ok": True,
            "required_api_ok": not missing_api_names,
            "auth_ok": True,
            "status": status,
            "mode": "gradio-webui",
            "api_contract": self.endpoint.api_contract,
            "api_prefix": payload.get("api_prefix") or "",
            "available_api_names": api_names,
            "expected_api_names": expected,
            "missing_api_names": missing_api_names,
            "title": payload.get("title") or "",
            "version": payload.get("version") or "",
        }

    def capabilities(self) -> dict[str, Any]:
        return {"capabilities": [*self.endpoint.capabilities, "gradio_webui"]}

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if self.endpoint.api_contract == "gradio-gpt-sovits-webui":
            return self._synthesize_gpt_sovits(request)
        if self.endpoint.api_contract == "gradio-indextts2-webui":
            return self._synthesize_indextts(request)
        if self.endpoint.api_contract == "gradio-cosyvoice-webui":
            return self._synthesize_cosyvoice(request)
        raise RuntimeError(f"unsupported Gradio API contract: {self.endpoint.api_contract}")

    def gradio_index(self) -> dict[str, Any]:
        payload = self._config()
        if self.endpoint.api_contract != "gradio-gpt-sovits-webui":
            return {"service_id": self.endpoint.service_id, "candidates": [], "raw": _gradio_api_summary(payload)}
        return {
            "service_id": self.endpoint.service_id,
            "candidates": _gpt_sovits_gradio_logs_candidates(self.endpoint.service_id, payload),
            "raw": _gradio_api_summary(payload),
        }

    def _synthesize_gpt_sovits(self, request: SynthesisRequest) -> SynthesisResult:
        params = {**self.endpoint.default_params, **request.parameters}
        gpt_weights = str(params.get("gpt_weights_path") or params.get("gpt_weights") or "")
        sovits_weights = str(params.get("sovits_weights_path") or params.get("sovits_weights") or "")
        prompt_lang = _gradio_language(params.get("prompt_lang") or "zh")
        text_lang = _gradio_language(params.get("text_lang") or request.line.language or "zh")

        # 1. Switch weights (before assembling payload).
        weight_changed = bool(sovits_weights)
        if gpt_weights:
            self._post_gradio_api("change_gpt_weights", [gpt_weights], timeout=params.get("timeout_seconds", 600.0))
        if sovits_weights:
            self._post_gradio_api("change_sovits_weights", [sovits_weights, prompt_lang, text_lang], timeout=params.get("timeout_seconds", 600.0))

        # 2. Assemble the get_tts_wav payload.
        ref_audio = params.get("ref_audio_path") or params.get("reference_audio")
        prompt_text = str(params.get("prompt_text") or "")
        selected_audio: Any = ref_audio
        if params.get("ref_audio_choice"):
            selected = self._post_gradio_api(
                "on_select_ref_audio",
                [params.get("ref_audio_choice"), params.get("character_filter", "全部")],
                timeout=params.get("timeout_seconds", 600.0),
            )
            data = selected.get("data") or []
            selected_audio = _component_value(data[0]) if data else selected_audio
            if len(data) > 1 and not prompt_text:
                prompt_text = str(_component_value(data[1]) or "")
        selected_audio = self._prepare_gradio_file(selected_audio, timeout=params.get("timeout_seconds", 900.0))
        aux_ref_audio_paths = [
            self._prepare_gradio_file(path, timeout=params.get("timeout_seconds", 900.0))
            for path in (params.get("aux_ref_audio_paths") or [])
        ]
        payload = params.get("gradio_data")
        if not isinstance(payload, list):
            payload = [
                selected_audio,
                prompt_text,
                prompt_lang,
                request.line.text,
                text_lang,
                params.get("text_split_method", "凑四句一切"),
                params.get("top_k", 15),
                params.get("top_p", 1.0),
                params.get("temperature", 1.0),
                bool(params.get("ref_free", not prompt_text)),
                params.get("speed_factor", params.get("speed", 1.0)),
                bool(params.get("if_freeze", False)),
                aux_ref_audio_paths,
                params.get("sample_steps", 8),
                bool(params.get("super_sampling", False)),
                params.get("fragment_interval", 0.3),
                bool(params.get("parallel_infer", True)),
            ]

        # 3. Synthesize. When weights were just changed, change_sovits_weights is a
        # generator whose Gradio queue stream closes on the first yield while model
        # loading continues asynchronously on the server. Wait for loading to finish
        # before calling get_tts_wav (the Gradio queue is serial — submitting
        # get_tts_wav while the generator is still loading would queue behind it
        # and return null, and rapid retries would flood the queue).
        if weight_changed:
            time.sleep(params.get("weight_load_wait_seconds", 5))
        response = self._post_gradio_api("get_tts_wav", payload, timeout=params.get("timeout_seconds", 900.0))
        if response.get("data") is None:
            raise RuntimeError("GPT-SoVITS get_tts_wav returned no audio (model may still be loading — try increasing weight_load_wait_seconds)")
        return self._write_gradio_audio(request, response, {"api_contract": self.endpoint.api_contract, "gradio_api_name": "get_tts_wav"})

    def _synthesize_indextts(self, request: SynthesisRequest) -> SynthesisResult:
        params = {**self.endpoint.default_params, **request.parameters}
        payload = params.get("gradio_data")
        if not isinstance(payload, list):
            emotion_mode = str(params.get("emotion_mode", "same_as_voice"))
            example_values: list[Any] = []
            if not (params.get("voice") or params.get("ref_audio_path") or params.get("reference_audio")) and params.get("gradio_example_index") is not None:
                example = self._post_gradio_api("load_example", [params.get("gradio_example_index")], timeout=params.get("timeout_seconds", 900.0))
                example_values = [_component_value(item) for item in (example.get("data") or [])]
                if len(example_values) > 1 and emotion_mode == "same_as_voice":
                    emotion_mode = str(example_values[1] or emotion_mode)
            example_vector = example_values[6:14] if len(example_values) >= 14 else []
            emotion_vector = list(params.get("emotion_vector") or example_vector or [0.0] * 8)[:8]
            emotion_vector.extend([0.0] * (8 - len(emotion_vector)))
            voice_reference = self._prepare_gradio_file(
                params.get("voice") or params.get("ref_audio_path") or params.get("reference_audio") or (example_values[0] if example_values else ""),
                timeout=params.get("timeout_seconds", 900.0),
            )
            raw_emotion_audio = params.get("emotion_audio") or (example_values[3] if len(example_values) > 3 else None)
            emotion_audio = (
                self._prepare_gradio_file(raw_emotion_audio, timeout=params.get("timeout_seconds", 900.0))
                if raw_emotion_audio
                else None
            )
            payload = [
                _indextts_emotion_mode_label(emotion_mode),
                voice_reference,
                request.line.text,
                emotion_audio,
                params.get("emotion_weight", example_values[4] if len(example_values) > 4 else 0.8),
                *emotion_vector,
                params.get("emotion_text") or request.line.note or (example_values[5] if len(example_values) > 5 else ""),
                bool(params.get("emotion_random", False)),
                params.get("max_text_tokens_per_segment", 120),
                bool(params.get("do_sample", True)),
                params.get("top_p", 0.8),
                params.get("top_k", 30),
                params.get("temperature", 0.8),
                params.get("length_penalty", 0.0),
                params.get("num_beams", 3),
                params.get("repetition_penalty", 10.0),
                params.get("max_mel_tokens", 1500),
            ]
        response = self._post_gradio_api("gen_single", payload, timeout=params.get("timeout_seconds", 900.0))
        return self._write_gradio_audio(request, response, {"api_contract": self.endpoint.api_contract, "gradio_api_name": "gen_single"})

    def _synthesize_cosyvoice(self, request: SynthesisRequest) -> SynthesisResult:
        params = {**self.endpoint.default_params, **request.parameters}
        payload = params.get("gradio_data")
        if not isinstance(payload, list):
            mode = str(params.get("mode") or params.get("mode_checkbox_group") or "预训练音色")
            supported_modes = ("预训练音色", "3s极速复刻", "跨语种复刻", "自然语言控制")
            if mode not in supported_modes:
                mode = "预训练音色"
            sft_voice = str(params.get("sft_voice") or params.get("sft_dropdown") or "")
            prompt_text = str(params.get("prompt_text") or "")
            ref_audio = params.get("ref_audio_path") or params.get("reference_audio") or params.get("prompt_wav_upload")
            prompt_wav = self._prepare_gradio_file(ref_audio, timeout=params.get("timeout_seconds", 900.0)) if ref_audio else None
            instruct_text = str(params.get("instruct_text") or "")
            seed = params.get("seed", 0)
            stream = bool(params.get("stream", False))
            speed = float(params.get("speed", 1.0))
            payload = [
                request.line.text,
                mode,
                sft_voice,
                prompt_text,
                prompt_wav,
                None,
                instruct_text,
                seed,
                stream,
                speed,
            ]
        response = self._post_gradio_api("generate_audio", payload, timeout=params.get("timeout_seconds", 900.0))
        return self._write_gradio_audio(request, response, {"api_contract": self.endpoint.api_contract, "gradio_api_name": "generate_audio"})

    def _config(self, timeout: float | int | None = None) -> dict[str, Any]:
        if self._config_cache is not None:
            return self._config_cache
        with httpx.Client(timeout=float(timeout or _health_timeout_seconds()), transport=self.transport) as client:
            response = client.get(self.endpoint.base_url.rstrip("/") + "/config", headers=self._headers())
            response.raise_for_status()
            self._config_cache = response.json()
            return self._config_cache

    def _post_gradio_api(self, api_name: str, data: list[Any], timeout: float | int | None = None) -> dict[str, Any]:
        config = self._config(timeout=timeout)
        api_prefix = str(config.get("api_prefix") or "").strip("/")
        if _gradio_api_is_queued(config, api_name):
            return self._post_gradio_queue_api(api_name, data, timeout=timeout, api_prefix=api_prefix)
        path_candidates = []
        if api_prefix:
            path_candidates.append(f"/{api_prefix}/api/{api_name}")
        path_candidates.append(f"/api/{api_name}")
        if api_prefix:
            path_candidates.append(f"/{api_prefix}/run/{api_name}")
        path_candidates.append(f"/run/{api_name}")
        payload = {"data": data}
        last_error: Exception | None = None
        with httpx.Client(timeout=float(timeout or 900.0), transport=self.transport) as client:
            for path in path_candidates:
                try:
                    response = client.post(self.endpoint.base_url.rstrip("/") + path, json=payload, headers=self._headers())
                    if response.status_code == 404:
                        continue
                    if response.status_code >= 400:
                        raise RuntimeError(f"{response.status_code} {scrub_error(response.text[:800], self.endpoint.base_url)}")
                    response.raise_for_status()
                    return response.json()
                except Exception as exc:
                    last_error = exc
        raise RuntimeError(f"Gradio API {api_name!r} failed: {scrub_error(last_error, self.endpoint.base_url)}")

    def _post_gradio_queue_api(self, api_name: str, data: list[Any], timeout: float | int | None = None, api_prefix: str = "") -> dict[str, Any]:
        config = self._config(timeout=timeout)
        fn_index = _gradio_fn_index(config, api_name)
        if fn_index is None:
            raise RuntimeError(f"Gradio API {api_name!r} not found in dependencies")
        session_hash = uuid.uuid4().hex[:12]
        join_paths = [f"/{api_prefix}/queue/join", "/queue/join"] if api_prefix else ["/queue/join"]
        data_paths = [f"/{api_prefix}/queue/data", "/queue/data"] if api_prefix else ["/queue/data"]
        join_payload = {
            "data": data,
            "event_data": None,
            "fn_index": fn_index,
            "trigger_id": None,
            "session_hash": session_hash,
        }
        # POST /queue/join with a short-lived client, then GET /queue/data with a
        # separate streaming client. Reusing the same httpx.Client for both causes
        # keep-alive connection issues with SSE streams on some Gradio servers.
        request_timeout = float(timeout or 900.0)
        joined = False
        for join_path in join_paths:
            try:
                with httpx.Client(timeout=30.0, transport=self.transport) as join_client:
                    response = join_client.post(self.endpoint.base_url.rstrip("/") + join_path, json=join_payload, headers=self._headers())
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                joined = True
                break
            except httpx.HTTPStatusError:
                continue
        if not joined:
            raise RuntimeError(f"Gradio queue join failed for {api_name!r}: no /queue/join endpoint accepted the request")
        for data_path in data_paths:
            try:
                with httpx.Client(timeout=request_timeout, transport=self.transport) as stream_client:
                    return self._read_gradio_queue_result(stream_client, f"{data_path}?session_hash={session_hash}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                raise
        raise RuntimeError(f"Gradio queued API {api_name!r} failed: no /queue/data endpoint available")

    def _read_gradio_queue_result(self, client: httpx.Client, stream_path: str) -> dict[str, Any]:
        # Gradio 4.x queue/data SSE uses JSON messages with a "msg" field:
        #   data: {"msg":"estimation","rank":0,"queue_size":1}
        #   data: {"msg":"process_completed","output":{"data":[...]}}
        # Gradio /call/ simple-predict uses event:/data: pairs:
        #   event: complete
        #   data: [...]
        # This reader handles both formats.
        current_event = ""
        last_data: Any = None
        with client.stream("GET", self.endpoint.base_url.rstrip("/") + stream_path, headers=self._headers()) as response:
            if response.status_code >= 400:
                raise RuntimeError(f"{response.status_code} {scrub_error(response.text[:800], self.endpoint.base_url)}")
            response.raise_for_status()
            for raw_line in response.iter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line.removeprefix("event:").strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data_text = line.removeprefix("data:").strip()
                if data_text == "null":
                    continue
                try:
                    last_data = json.loads(data_text)
                except json.JSONDecodeError:
                    last_data = data_text
                # Gradio 4.x queue protocol: check msg field
                if isinstance(last_data, dict):
                    msg = last_data.get("msg", "")
                    if msg == "process_completed":
                        output = last_data.get("output") or {}
                        return {"data": output.get("data")}
                    if msg == "close_stream":
                        break
                    if msg == "error" or last_data.get("success") is False:
                        raise RuntimeError(f"Gradio queue error: {last_data.get('message') or last_data}")
                # Gradio /call/ simple-predict protocol: check event type
                if current_event == "error":
                    raise RuntimeError(f"Gradio queued API error: {last_data}")
                if current_event == "complete":
                    return {"data": last_data}
        if last_data is not None:
            return {"data": last_data}
        raise RuntimeError("Gradio queue stream ended without result data")

    def _write_gradio_audio(self, request: SynthesisRequest, payload: dict[str, Any], metadata: dict[str, Any]) -> SynthesisResult:
        data = payload.get("data")
        audio_ref = _first_audio_reference(data)
        if audio_ref is None:
            raise RuntimeError("Gradio response did not include audio output")
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_bytes = self._download_gradio_audio(audio_ref)
        request.output_path.write_bytes(audio_bytes)
        return SynthesisResult(
            audio_path=request.output_path,
            metadata={
                "service_id": self.endpoint.service_id,
                **metadata,
                "remote_audio_path": _audio_reference_path(audio_ref),
            },
        )

    def _download_gradio_audio(self, audio_ref: Any) -> bytes:
        if isinstance(audio_ref, (bytes, bytearray)):
            return bytes(audio_ref)
        path = _audio_reference_path(audio_ref)
        if not path:
            raise RuntimeError("Gradio audio output path is empty")
        url = path if path.startswith(("http://", "https://")) else self.endpoint.base_url.rstrip("/") + _gradio_file_path(path)
        with httpx.Client(timeout=120.0, transport=self.transport) as client:
            response = client.get(url, headers=self._headers())
            response.raise_for_status()
            return response.content

    def _prepare_gradio_file(self, value: Any, timeout: float | int | None = None) -> Any:
        path = _audio_reference_path(value)
        if not path or path.startswith(("http://", "https://", "/file=", "file=")):
            return value
        local_path = Path(path)
        if not local_path.exists() or not local_path.is_file():
            return value
        return self._upload_gradio_file(local_path, timeout=timeout)

    def _upload_gradio_file(self, path: Path, timeout: float | int | None = None) -> dict[str, Any]:
        config = self._config(timeout=timeout)
        api_prefix = str(config.get("api_prefix") or "").strip("/")
        upload_paths = [f"/{api_prefix}/upload", "/upload"] if api_prefix else ["/upload"]
        last_error: Exception | None = None
        with httpx.Client(timeout=float(timeout or 120.0), transport=self.transport) as client:
            for upload_path in upload_paths:
                try:
                    with path.open("rb") as file_handle:
                        response = client.post(
                            self.endpoint.base_url.rstrip("/") + upload_path,
                            files={"files": (path.name, file_handle, "application/octet-stream")},
                            headers=self._headers(),
                        )
                    if response.status_code == 404:
                        continue
                    if response.status_code >= 400:
                        raise RuntimeError(f"{response.status_code} {scrub_error(response.text[:800], self.endpoint.base_url)}")
                    data = response.json()
                    if isinstance(data, list) and data:
                        if isinstance(data[0], dict):
                            uploaded = dict(data[0])
                        else:
                            uploaded = {"path": str(data[0])}
                        self._normalize_uploaded_gradio_file(uploaded, path)
                        return uploaded
                    if isinstance(data, dict) and data.get("path"):
                        uploaded = dict(data)
                        self._normalize_uploaded_gradio_file(uploaded, path)
                        return uploaded
                    raise RuntimeError("Gradio upload response did not include a path")
                except Exception as exc:
                    last_error = exc
        raise RuntimeError(f"Gradio upload failed for {path}: {last_error}")

    def _normalize_uploaded_gradio_file(self, uploaded: dict[str, Any], path: Path) -> None:
        uploaded.setdefault("orig_name", path.name)
        uploaded.setdefault("mime_type", "audio/wav")
        uploaded.setdefault("size", None)
        uploaded.setdefault("is_stream", False)
        uploaded.setdefault("meta", {"_type": "gradio.FileData"})


class GPTSoVITSApiV2ServiceClient(HttpTTSServiceClient):
    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=_health_timeout_seconds(), transport=self.transport) as client:
                response = client.get(self.endpoint.base_url.rstrip("/") + "/docs", headers=self._headers())
                response.raise_for_status()
        except Exception as exc:
            return {"engine": self.endpoint.engine.value, "ready": False, "error": scrub_error(exc, self.endpoint.base_url)}
        return {"engine": self.endpoint.engine.value, "ready": True, "mode": "gpt-sovits-api-v2"}

    def model_catalog(self, limit: int = 120) -> dict[str, Any]:
        with httpx.Client(timeout=self.endpoint.default_params.get("timeout_seconds", 30.0), transport=self.transport) as client:
            response = client.get(self.endpoint.base_url.rstrip("/") + "/models", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        return _gpt_sovits_models_payload_to_catalog(self.endpoint, payload, source="api_v2", limit=limit)

    def model_samples(self, logs_name: str, limit: int = 120) -> dict[str, Any]:
        encoded_name = quote(logs_name, safe="")
        with httpx.Client(timeout=self.endpoint.default_params.get("timeout_seconds", 30.0), transport=self.transport) as client:
            response = client.get(self.endpoint.base_url.rstrip("/") + f"/models/{encoded_name}/samples", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        samples: list[dict[str, Any]] = []
        for item in (payload.get("samples") or [])[:limit]:
            audio_name = str(item.get("audio_name") or Path(str(item.get("audio_path") or "")).name)
            text = str(item.get("text") or "")
            emotion = str(item.get("emotion") or "")
            label = audio_name
            if text:
                label = f"{label} · {text[:42]}"
            if emotion:
                label = f"{label} · {emotion}"
            samples.append(
                {
                    "sample_id": f"{logs_name}:{audio_name}",
                    "display_label": label,
                    "path": str(item.get("audio_path") or ""),
                    "text": text,
                    "text_source": "api_v2",
                    "character": logs_name,
                    "emotion": emotion,
                    "remark": "",
                    "prompt_lang": str(item.get("lang") or "zh"),
                    "source": "api_v2",
                    "logs_name": logs_name,
                }
            )
        return {"service_id": self.endpoint.service_id, "logs_name": logs_name, "samples": samples, "diagnostics": []}

    def load(self, profile: str, parameters: dict[str, Any] | None = None) -> None:
        parameters = parameters or {}
        with httpx.Client(timeout=300.0, transport=self.transport) as client:
            if parameters.get("gpt_weights_path"):
                response = client.get(
                    self.endpoint.base_url.rstrip("/") + "/set_gpt_weights",
                    params={"weights_path": parameters["gpt_weights_path"]},
                    headers=self._headers(),
                )
                response.raise_for_status()
            if parameters.get("sovits_weights_path"):
                response = client.get(
                    self.endpoint.base_url.rstrip("/") + "/set_sovits_weights",
                    params={"weights_path": parameters["sovits_weights_path"]},
                    headers=self._headers(),
                )
                response.raise_for_status()

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        payload = {
            "text": request.line.text,
            "text_lang": request.parameters.get("text_lang", request.line.language or "zh"),
            "ref_audio_path": request.parameters.get("ref_audio_path"),
            "prompt_lang": request.parameters.get("prompt_lang", "zh"),
            "prompt_text": request.parameters.get("prompt_text", ""),
            "media_type": "wav",
            "streaming_mode": False,
        }
        payload.update(request.parameters.get("gpt_sovits_payload", {}))
        with httpx.Client(timeout=request.parameters.get("timeout_seconds", 600.0), transport=self.transport) as client:
            response = client.post(self.endpoint.base_url.rstrip("/") + "/tts", json=payload, headers=self._headers())
            response.raise_for_status()
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(response.content)
        return SynthesisResult(audio_path=request.output_path, metadata={"service_id": self.endpoint.service_id})


class CommercialSpeechClient(HttpTTSServiceClient):
    def health(self) -> dict[str, Any]:
        missing = self._missing_env()
        if missing:
            return {"engine": self.endpoint.engine.value, "ready": False, "status": "needs key", "missing_env": missing}
        return {"engine": self.endpoint.engine.value, "ready": True, "provider_type": self.endpoint.provider_type.value}

    def _merged_params(self, request: SynthesisRequest) -> dict[str, Any]:
        return {**self.endpoint.default_params, **request.parameters}

    def _write_response_bytes(self, request: SynthesisRequest, content: bytes, metadata: dict[str, Any]) -> SynthesisResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(content)
        return SynthesisResult(audio_path=request.output_path, metadata={"service_id": self.endpoint.service_id, **metadata})


class OpenAISpeechClient(CommercialSpeechClient):
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        params = self._merged_params(request)
        payload = {
            "model": params.get("model", "gpt-4o-mini-tts"),
            "voice": params.get("voice", "alloy"),
            "input": request.line.text,
            "response_format": params.get("response_format", "wav"),
        }
        if params.get("instructions"):
            payload["instructions"] = params["instructions"]
        with httpx.Client(timeout=params.get("timeout_seconds", 600.0), transport=self.transport) as client:
            response = client.post(self.endpoint.base_url.rstrip("/") + "/audio/speech", json=payload, headers=self._headers())
            response.raise_for_status()
        return self._write_response_bytes(request, response.content, {"provider_type": "openai"})


class XAISpeechClient(OpenAISpeechClient):
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        params = self._merged_params(request)
        payload = {
            "model": params.get("model", "grok-tts"),
            "voice": params.get("voice") or params.get("voice_id", "voice-1"),
            "input": request.line.text,
            "response_format": params.get("response_format", "wav"),
        }
        with httpx.Client(timeout=params.get("timeout_seconds", 600.0), transport=self.transport) as client:
            response = client.post(self.endpoint.base_url.rstrip("/") + "/audio/speech", json=payload, headers=self._headers())
            response.raise_for_status()
        return self._write_response_bytes(request, response.content, {"provider_type": "xai"})


class GeminiSpeechClient(CommercialSpeechClient):
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        params = self._merged_params(request)
        model = params.get("model", "gemini-2.5-flash-preview-tts")
        payload = {
            "contents": [{"parts": [{"text": request.line.text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": params.get("voice_name", "Kore"),
                        }
                    }
                },
            },
        }
        key = os.environ.get(self.endpoint.auth_profile.get("api_key_env", ""))
        with httpx.Client(timeout=params.get("timeout_seconds", 600.0), transport=self.transport) as client:
            response = client.post(
                self.endpoint.base_url.rstrip("/") + f"/models/{model}:generateContent",
                params={"key": key},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        audio_data = _extract_gemini_audio(data)
        return self._write_response_bytes(request, audio_data, {"provider_type": "gemini"})


class VolcengineSpeechClient(CommercialSpeechClient):
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        params = self._merged_params(request)
        app_id = os.environ.get(self.endpoint.auth_profile.get("app_id_env", ""))
        access_token = os.environ.get(self.endpoint.auth_profile.get("access_token_env", ""))
        cluster = os.environ.get(self.endpoint.auth_profile.get("cluster_id_env", ""))
        payload = {
            "app": {"appid": app_id, "token": access_token, "cluster": cluster},
            "user": {"uid": params.get("uid", "tts-more")},
            "audio": {
                "voice_type": params.get("voice_type"),
                "encoding": params.get("encoding", "wav"),
                "speed_ratio": params.get("speed_ratio", params.get("speed", 1.0)),
                "pitch_ratio": params.get("pitch_ratio", params.get("pitch", 1.0)),
            },
            "request": {
                "reqid": params.get("reqid", request.line.id),
                "text": request.line.text,
                "operation": params.get("operation", "query"),
            },
        }
        if params.get("emotion"):
            payload["audio"]["emotion"] = params["emotion"]
        headers = {"Authorization": f"Bearer;{access_token}"}
        with httpx.Client(timeout=params.get("timeout_seconds", 600.0), transport=self.transport) as client:
            response = client.post(self.endpoint.base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        audio_data = base64.b64decode(data.get("data", ""))
        return self._write_response_bytes(request, audio_data, {"provider_type": "volcengine"})


def _has_capabilities(actual: list[str], required: list[str]) -> bool:
    return set(required).issubset(set(actual))


def _health_timeout_seconds() -> float:
    raw = os.environ.get("TTS_MORE_HEALTH_TIMEOUT_SECONDS", "0.75")
    try:
        return min(max(float(raw), 0.1), 10.0)
    except ValueError:
        return 0.75


def _expected_gradio_api_names(api_contract: str) -> list[str]:
    if api_contract == "gradio-gpt-sovits-webui":
        return ["get_tts_wav"]
    if api_contract == "gradio-indextts2-webui":
        return ["gen_single"]
    if api_contract == "gradio-cosyvoice-webui":
        return ["generate_audio"]
    return []


def _gradio_api_is_queued(config: dict[str, Any], api_name: str) -> bool:
    for dependency in config.get("dependencies") or []:
        if dependency.get("api_name") == api_name:
            return dependency.get("queue") is True
    return False


def _gradio_fn_index(config: dict[str, Any], api_name: str) -> int | None:
    """Find the fn_index (position in dependencies array) for a named Gradio API.

    Gradio 4.x /queue/join requires fn_index instead of api_name.
    """
    for index, dependency in enumerate(config.get("dependencies") or []):
        if isinstance(dependency, dict) and dependency.get("api_name") == api_name:
            return index
    return None


def _gradio_api_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": payload.get("title") or "",
        "version": payload.get("version") or "",
        "api_prefix": payload.get("api_prefix") or "",
        "api_names": sorted(
            {
                item.get("api_name")
                for item in payload.get("dependencies", [])
                if isinstance(item, dict) and item.get("api_name")
            }
        ),
    }


def _gpt_sovits_gradio_logs_candidates(service_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """FALLBACK discovery via Gradio /config scraping (fork-specific).

    The primary discovery path is the worker's GET /models endpoint (filesystem
    scan, works against any upstream build). This Gradio-scraping path depends
    on fork-added api_names (update_model_choices, refresh_ref_audio_choices)
    and labeled dropdowns; it returns partial/empty results against upstream
    official GPT-SoVITS. Kept for compatibility with existing fork deployments.
    """
    components = {component.get("id"): component for component in payload.get("components", []) if isinstance(component, dict)}
    dependencies = [item for item in payload.get("dependencies", []) if isinstance(item, dict)]
    by_api = {item.get("api_name"): item for item in dependencies if item.get("api_name")}
    model_dep = by_api.get("update_model_choices") or {}
    ref_dep = by_api.get("refresh_ref_audio_choices") or {}
    gpt_component = components.get((model_dep.get("outputs") or [None, None])[1])
    sovits_component = components.get((model_dep.get("outputs") or [None, None, None])[2])
    ref_component = components.get((ref_dep.get("outputs") or [None])[0])
    role_component = components.get((model_dep.get("outputs") or [None])[0])

    # GPT-SoVITS WebUI does not define api_name on its refresh button, so the
    # dependency-based lookup above returns nothing. Fall back to finding the
    # dropdown components by label — GPT-SoVITS uses "GPT模型列表" /
    # "SoVITS模型列表" (and the English equivalents in localized builds).
    if gpt_component is None:
        gpt_component = _find_dropdown_by_label(components, ("GPT",), ("SoVITS",))
    if sovits_component is None:
        sovits_component = _find_dropdown_by_label(components, ("SoVITS",), ("GPT",))

    grouped: dict[str, dict[str, Any]] = {}
    for role in _gradio_choices(role_component):
        if role in {"全部", "All", "all"}:
            continue
        item = _logs_candidate(grouped, service_id, role, source="gradio")
        item.setdefault("aliases", []).append(role)
    for choice in _gradio_choices(gpt_component):
        name = _extract_logs_name(choice)
        item = _logs_candidate(grouped, service_id, name, source="gradio")
        option = {"name": Path(choice).name or choice, "path": choice, "value": choice, "score": _weight_score_tuple(choice)}
        item.setdefault("gpt_weights", []).append(option)
        _prefer_recommended(item, "gpt", option)
    for choice in _gradio_choices(sovits_component):
        name = _extract_logs_name(choice)
        item = _logs_candidate(grouped, service_id, name, source="gradio")
        option = {"name": Path(choice).name or choice, "path": choice, "value": choice, "score": _weight_score_tuple(choice)}
        item.setdefault("sovits_weights", []).append(option)
        _prefer_recommended(item, "sovits", option)
    for choice in _gradio_choices(ref_component):
        name = _extract_logs_name(choice)
        item = _logs_candidate(grouped, service_id, name, source="gradio")
        group = {
            "id": _slugish(choice),
            "name": Path(choice).stem or choice,
            "paths": [choice],
            "samples": [{"path": choice, "text": "", "text_source": "none"}],
        }
        item.setdefault("reference_audio_groups", []).append(group)
        item.setdefault("recommended_ref_audio_path", choice)
    candidates = list(grouped.values())
    candidates.sort(key=lambda item: (0 if item.get("recommended_gpt_weights_path") and item.get("recommended_sovits_weights_path") else 1, item["logs_name"]))
    return candidates


def _logs_candidate(grouped: dict[str, dict[str, Any]], service_id: str, logs_name: str, source: str) -> dict[str, Any]:
    name = logs_name.strip() or "unknown"
    key = _slugish(name)
    if key not in grouped:
        grouped[key] = {
            "id": key,
            "logs_id": key,
            "logs_name": name,
            "name": name,
            "aliases": [name],
            "service_id": service_id,
            "source": source,
            "gpt_weights": [],
            "sovits_weights": [],
            "reference_audio_groups": [],
        }
    elif grouped[key].get("source") != source:
        grouped[key]["source"] = "merged"
    return grouped[key]


def _prefer_recommended(item: dict[str, Any], kind: str, option: dict[str, Any]) -> None:
    score = tuple(option.get("score") or (0, 0))
    key = f"recommended_{kind}_weights_path"
    score_key = f"{key}_score"
    if key not in item or score > tuple(item.get(score_key) or (-1, -1)):
        item[key] = option.get("path") or option.get("value")
        item[score_key] = score


def _find_dropdown_by_label(
    components: dict[Any, dict[str, Any]],
    include_keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Find a Gradio dropdown component by matching its label keywords.

    GPT-SoVITS labels its model dropdowns "GPT模型列表" / "SoVITS模型列表".
    Localized builds may use "GPT model list" / "SoVITS model list".
    """
    for component in components.values():
        props = component.get("props") or {}
        if component.get("type") != "dropdown":
            continue
        label = str(props.get("label") or "")
        if not any(keyword.lower() in label.lower() for keyword in include_keywords):
            continue
        if any(keyword.lower() in label.lower() for keyword in exclude_keywords):
            continue
        return component
    return None


def _gradio_choices(component: dict[str, Any] | None) -> list[str]:
    if not component:
        return []
    props = component.get("props") or {}
    output: list[str] = []
    for choice in props.get("choices") or []:
        value = choice
        if isinstance(choice, (list, tuple)) and choice:
            value = choice[1] if len(choice) > 1 else choice[0]
        elif isinstance(choice, dict):
            value = choice.get("value") or choice.get("label") or choice.get("name")
        if value not in (None, ""):
            output.append(str(value))
    value = props.get("value")
    if isinstance(value, str) and value and value not in output:
        output.append(value)
    return output


def _extract_logs_name(raw: str) -> str:
    text = Path(raw).stem
    text = text.replace("\\", "/").split("/")[-1]
    text = text.lstrip("0123456789").strip()
    cleanup_patterns = [
        r"(?:[-_])e\d+(?:[-_])s\d+$",
        r"(?:[-_])e\d+$",
        r"(?:[-_])s\d+$",
        r"(?:[-_])epoch=\d+(?:[-_])step=\d+$",
    ]
    changed = True
    while changed:
        changed = False
        for pattern in cleanup_patterns:
            next_text = re.sub(pattern, "", text, flags=re.IGNORECASE)
            if next_text != text:
                text = next_text
                changed = True
    text = "".join(ch for ch in text if ch not in "（）()").strip("-_ ")
    return text or raw


def _weight_score_tuple(value: str) -> tuple[int, int]:
    import re

    epoch = max([int(match) for match in re.findall(r"(?:^|[-_])e(\d+)", value, flags=re.IGNORECASE)] or [0])
    step = max([int(match) for match in re.findall(r"(?:^|[-_])s(\d+)", value, flags=re.IGNORECASE)] or [0])
    return (epoch, step)


def _slugish(value: str) -> str:
    import re
    import unicodedata

    tokens: list[str] = []
    current: list[str] = []
    current_is_ascii: bool | None = None
    for char in value.strip():
        normalized = unicodedata.normalize("NFKC", char)
        is_ascii_alnum = normalized.isascii() and normalized.isalnum()
        is_non_ascii_alnum = (not normalized.isascii()) and normalized.isalnum()
        if not (is_ascii_alnum or is_non_ascii_alnum):
            # Separator character — flush current token.
            if current:
                tokens.append("".join(current))
                current = []
                current_is_ascii = None
            continue
        if current_is_ascii is None:
            current_is_ascii = is_ascii_alnum
            current.append(normalized.lower() if is_ascii_alnum else normalized)
        elif is_ascii_alnum == current_is_ascii:
            current.append(normalized.lower() if is_ascii_alnum else normalized)
        else:
            tokens.append("".join(current))
            current = [normalized.lower() if is_ascii_alnum else normalized]
            current_is_ascii = is_ascii_alnum
    if current:
        tokens.append("".join(current))
    slug = "-".join(token for token in tokens if token)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "logs"


def _gradio_language(value: Any) -> str:
    raw = str(value or "zh").lower()
    if raw in {"zh", "zh-cn", "chinese"}:
        return "中文"
    if raw in {"en", "en-us", "english"}:
        return "英文"
    if raw in {"ja", "jp", "japanese"}:
        return "日文"
    return str(value or "中文")


def _indextts_emotion_mode_label(mode: str) -> str:
    return {
        "same_as_voice": "与音色参考音频相同",
        "emotion_audio": "使用情感参考音频",
        "emotion_vector": "使用情感向量控制",
        "emotion_text": "使用情感描述文本控制",
    }.get(mode, mode)


def _first_audio_reference(data: Any) -> Any:
    if isinstance(data, list):
        for item in data:
            if _audio_reference_path(item):
                return item
    if _audio_reference_path(data):
        return data
    return None


def _component_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _audio_reference_path(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "value" in value:
            return _audio_reference_path(value["value"])
        for key in ("url", "path", "name"):
            if value.get(key):
                return str(value[key])
    return ""


def _gradio_file_path(path: str) -> str:
    if path.startswith("/"):
        return path
    if path.startswith("file="):
        return "/" + path
    return "/file=" + quote(path, safe=":/\\")


def _extract_gemini_audio(data: dict[str, Any]) -> bytes:
    candidates = data.get("candidates") or []
    for candidate in candidates:
        parts = candidate.get("content", {}).get("parts", [])
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise RuntimeError("Gemini response did not include inline audio data")


class ComfyUITTSClient:
    def __init__(self, endpoint: TTSServiceEndpoint, transport: httpx.BaseTransport | None = None) -> None:
        from app.comfyui.client import ComfyUIAPIClient
        from app.comfyui.workflow_builder import build_workflow as _build_workflow

        self.endpoint = endpoint
        self.transport = transport
        self.api = ComfyUIAPIClient(endpoint.base_url, transport=transport)
        self._build_workflow = _build_workflow

    def health(self) -> dict[str, Any]:
        stats = self.api.system_stats()
        stats["engine"] = self.endpoint.engine.value if self.endpoint.engine else "comfyui"
        return stats

    def capabilities(self) -> dict[str, Any]:
        return self.api.bridge_capabilities()

    def load(self, profile: str, parameters: dict[str, Any] | None = None) -> None:
        return

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        try:
            return self._synthesize_impl(request)
        except Exception as exc:
            raise RuntimeError(scrub_error(exc, self.endpoint.base_url)) from exc

    def _synthesize_impl(self, request: SynthesisRequest) -> SynthesisResult:
        engine_value = request.parameters.get("engine") or (
            self.endpoint.engine.value if self.endpoint.engine else "cosyvoice"
        )
        params = {**self.endpoint.default_params, **request.parameters}
        params["text"] = request.line.text
        reference_path = next(
            (
                str(params[key])
                for key in ("reference_audio", "ref_audio_path", "prompt_audio_path")
                if params.get(key)
            ),
            "",
        )
        asset_id: str | None = None
        synthesis_error: Exception | None = None
        result: SynthesisResult | None = None
        try:
            if reference_path:
                upload = self.api.upload_audio(reference_path)
                asset_id = str(upload["asset_id"])
                params["asset_id"] = asset_id

            workflow = self._build_workflow(engine_value, params)
            prompt_id = self.api.submit_workflow(workflow)
            timeout = float(params.get("timeout_seconds", 600.0))
            poll_interval = float(params.get("poll_interval", 2.0))
            history_entry = self.api.poll_until_done(
                prompt_id, poll_interval=poll_interval, max_wait=timeout
            )
            output_files = self.api._extract_output_filenames(history_entry)
            if not output_files:
                raise RuntimeError(
                    f"ComfyUI prompt {prompt_id} completed but produced no output files"
                )
            first_output = output_files[0]
            audio_bytes = self.api.download_output(
                filename=first_output["filename"],
                subfolder=first_output["subfolder"],
                folder_type=first_output["type"],
            )
            _write_wav(request.output_path, audio_bytes)
            result = SynthesisResult(
                audio_path=request.output_path,
                metadata={
                    "service_id": self.endpoint.service_id,
                    "prompt_id": prompt_id,
                    "engine": engine_value,
                    "resource_id": params["resource_id"],
                    "comfyui_output_files": output_files,
                },
            )
        except Exception as exc:
            synthesis_error = exc
        cleanup_error: Exception | None = None
        if asset_id:
            try:
                self.api.delete_audio(asset_id)
            except Exception as exc:
                cleanup_error = exc
        if synthesis_error is not None:
            raise synthesis_error
        if cleanup_error is not None:
            raise cleanup_error
        assert result is not None
        return result

    def unload(self) -> None:
        resource_id = str(self.endpoint.default_params.get("resource_id", "")).strip()
        self.api.release_runtime(resource_id=resource_id or None)
        self.api.free_memory()


def _write_wav(output_path: Path, audio_bytes: bytes) -> None:
    """Decode ComfyUI's FLAC output and persist the WAV contract used by TTS More."""
    import soundfile

    samples, sample_rate = soundfile.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=True)
    if sample_rate <= 0 or samples.size == 0:
        raise ValueError("ComfyUI returned empty audio")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(output_path), samples, sample_rate, format="WAV", subtype="PCM_16")


def _tiny_wav_bytes() -> bytes:
    return (
        b"RIFF$\x00\x00\x00WAVEfmt "
        b"\x10\x00\x00\x00\x01\x00\x01\x00"
        b"@\x1f\x00\x00@\x1f\x00\x00"
        b"\x01\x00\x08\x00data\x00\x00\x00\x00"
    )
