from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.models import EngineName, ProviderType, TTSServiceEndpoint

API_CONTRACT = "comfyui-tts-audio-suite-v1"
ROUTE_PREFIX = "/api/tts-audio-suite/v1"

ENGINE_NODE_KEYS: dict[ProviderType, str] = {
    ProviderType.GPT_SOVITS: "gpt_sovits",
    ProviderType.INDEX_TTS: "index_tts",
    ProviderType.COSYVOICE: "cosyvoice",
}
ENGINE_DEFAULT_CLASSES = {
    "gpt_sovits": "TTSExternalGPTSovitsEngine",
    "index_tts": "TTSExternalIndexTTSEngine",
    "cosyvoice": "TTSExternalCosyVoiceEngine",
}
BRIDGE_REQUIRED_NODE_KEYS = ("audio_asset", "text", "save_audio")
_ASSET_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PROMPT_ID_RE = re.compile(r"^[0-9A-Za-z_.:-]{1,128}$")


def assert_bridge_ready_for_resource(endpoint: TTSServiceEndpoint, payload: dict[str, Any], resource_id: str) -> None:
    nodes = dict_payload(payload.get("nodes"))
    engine_key = endpoint_engine_key(endpoint)
    required_nodes = [engine_key, *BRIDGE_REQUIRED_NODE_KEYS]
    missing_nodes = [key for key in required_nodes if not nodes.get(key)]
    if missing_nodes:
        raise RuntimeError(f"TTS-Audio-Suite bridge is missing nodes: {', '.join(missing_nodes)}")
    for resource in resource_list(payload):
        if resource.get("resource_id") == resource_id:
            if str(resource.get("engine") or "") != engine_key:
                raise RuntimeError(f"resource_id {resource_id} is not a {engine_key} resource")
            if not bool(resource.get("ready")):
                raise RuntimeError(f"resource_id {resource_id} is not ready")
            return
    raise RuntimeError(f"resource_id {resource_id} is not registered in TTS-Audio-Suite")


def endpoint_engine_key(endpoint: TTSServiceEndpoint) -> str:
    provider = endpoint.provider_type
    if provider is not None and provider in ENGINE_NODE_KEYS:
        return ENGINE_NODE_KEYS[provider]
    if endpoint.engine == EngineName.GPT_SOVITS:
        return "gpt_sovits"
    if endpoint.engine == EngineName.INDEX_TTS:
        return "index_tts"
    if endpoint.engine == EngineName.COSYVOICE:
        return "cosyvoice"
    raise RuntimeError(f"unsupported TTS-Audio-Suite engine for service {endpoint.service_id}")


def resource_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resources = payload.get("resources")
    if not isinstance(resources, list):
        return []
    return [dict(item) for item in resources if isinstance(item, dict)]


def dict_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def required_string(payload: dict[str, Any], key: str) -> str:
    value = string_value(payload.get(key))
    if not value:
        raise RuntimeError(f"{key} is required")
    return value


def required_asset_id(payload: dict[str, Any]) -> str:
    asset_id = required_string(payload, "asset_id")
    if not _ASSET_ID_RE.fullmatch(asset_id):
        raise RuntimeError("TTS-Audio-Suite asset upload returned an invalid asset_id")
    return asset_id


def required_prompt_id(payload: dict[str, Any]) -> str:
    prompt_id = required_string(payload, "prompt_id")
    if not _PROMPT_ID_RE.fullmatch(prompt_id):
        raise RuntimeError("ComfyUI returned an invalid prompt_id")
    return prompt_id


def string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def history_entry(payload: Any, prompt_id: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        entry = payload.get(prompt_id)
        if isinstance(entry, dict):
            return entry
        if "status" in payload or "outputs" in payload:
            return payload
    return {}


def first_audio_output(entry: dict[str, Any]) -> dict[str, Any]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        raise RuntimeError("ComfyUI prompt completed without outputs")
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        candidates = node_output.get("audio") or node_output.get("audios")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("filename"):
                return dict(candidate)
    raise RuntimeError("ComfyUI prompt completed without SaveAudio output")


def contains_prompt_id(value: Any, prompt_id: str) -> bool:
    if isinstance(value, str):
        return value == prompt_id
    if isinstance(value, dict):
        return any(contains_prompt_id(item, prompt_id) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_prompt_id(item, prompt_id) for item in value)
    return False


def audio_content_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".flac":
        return "audio/flac"
    if suffix == ".ogg":
        return "audio/ogg"
    return "application/octet-stream"


def health_timeout_seconds() -> float:
    raw = os.environ.get("TTS_MORE_HEALTH_TIMEOUT_SECONDS", "0.75")
    try:
        return min(max(float(raw), 0.1), 10.0)
    except ValueError:
        return 0.75


def request_headers(endpoint: TTSServiceEndpoint) -> dict[str, str]:
    if not endpoint.auth_header_env:
        api_key_env = endpoint.auth_profile.get("api_key_env")
        if api_key_env:
            value = os.environ.get(api_key_env)
            return {"Authorization": f"Bearer {value}"} if value else {}
        return {}
    value = os.environ.get(endpoint.auth_header_env)
    return {"Authorization": value} if value else {}


def missing_env(endpoint: TTSServiceEndpoint) -> list[str]:
    keys = [value for key, value in endpoint.auth_profile.items() if key.endswith("_env")]
    if endpoint.auth_header_env:
        keys.append(endpoint.auth_header_env)
    return [key for key in keys if not os.environ.get(key)]


def merged_params(endpoint: TTSServiceEndpoint, parameters: dict[str, Any]) -> dict[str, Any]:
    return {**endpoint.default_params, **parameters}


def trust_env(parameters: dict[str, Any]) -> bool:
    return bool(parameters.get("trust_env", False))


def endpoint_url(endpoint: TTSServiceEndpoint, path: str) -> str:
    return endpoint.base_url.rstrip("/") + path
