from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app.models import EngineName, ProviderType, TTSServiceEndpoint
from app.services import ServiceRegistry

OpenSourceProvider = Literal["gpt-sovits", "indextts", "cosyvoice"]
SourceProfile = Literal["local_repo", "local_endpoint", "lan_endpoint", "cloud_endpoint", "api_placeholder"]


class OpenSourceTTSDetectRequest(BaseModel):
    provider_type: OpenSourceProvider
    repo_path: str | None = None
    base_url: str | None = None
    api_contract: str | None = None


class OpenSourceTTSConfigureRequest(BaseModel):
    provider_type: OpenSourceProvider
    service_id: str | None = None
    display_name: str | None = None
    source_profile: SourceProfile = "local_endpoint"
    repo_path: str | None = None
    base_url: str
    api_contract: str | None = None
    network_scope: Literal["localhost", "lan", "public", "commercial"] | None = None
    managed: bool = False
    enabled: bool = True
    resource_group: str = "local-gpu-0"
    capacity: int = Field(default=1, ge=1)
    start_command: list[str] = Field(default_factory=list)
    start_cwd: str | None = None


CATALOG: list[dict[str, Any]] = [
    {
        "provider_type": "gpt-sovits",
        "display_name": "GPT-SoVITS",
        "clone_url": "https://github.com/XucroYuri/GPT-SoVITS.git",
        "default_repo_path": "repo/GPT-SoVITS",
        "default_base_url": "http://127.0.0.1:9880",
        "default_ports": [9880, 9872],
        "api_contracts": ["gpt-sovits-api-v2", "gradio-gpt-sovits-webui"],
        "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice", "gpt-weights", "sovits-weights", "wav_output"],
        "priority": 10,
        "resource_group": "local-gpu-0",
        "recommended_clone_command": "git clone https://github.com/XucroYuri/GPT-SoVITS.git repo/GPT-SoVITS",
        "start_hint": "绑定 repo path 后配置 api_v2.py 或 Gradio WebUI 启动命令；推理仍通过 HTTP endpoint 调用。",
    },
    {
        "provider_type": "indextts",
        "display_name": "IndexTTS",
        "clone_url": "https://github.com/XucroYuri/index-tts.git",
        "default_repo_path": "repo/index-tts",
        "default_base_url": "http://127.0.0.1:9881",
        "default_ports": [9881, 7860],
        "api_contracts": ["tts-more-v1", "gradio-indextts-webui"],
        "capabilities": ["tts", "reference_audio_voice", "emotion_text", "emotion_audio", "wav_output"],
        "priority": 20,
        "resource_group": "local-gpu-0",
        "recommended_clone_command": "git clone https://github.com/XucroYuri/index-tts.git repo/index-tts",
        "start_hint": "可绑定本机项目路径用于启动/日志，也可直接绑定已运行的 HTTP/Gradio endpoint。",
    },
    {
        "provider_type": "cosyvoice",
        "display_name": "CosyVoice",
        "clone_url": "https://github.com/XucroYuri/CosyVoice.git",
        "default_repo_path": "repo/CosyVoice",
        "default_base_url": "http://127.0.0.1:50000",
        "default_ports": [50000],
        "api_contracts": ["cosyvoice-http-v1", "gradio-cosyvoice-webui"],
        "capabilities": ["tts", "reference_audio_voice", "zero_shot_voice", "cross_lingual_voice", "style_instruction", "wav_output"],
        "priority": 30,
        "resource_group": "local-gpu-0",
        "recommended_clone_command": "git clone https://github.com/XucroYuri/CosyVoice.git repo/CosyVoice",
        "start_hint": "第一版按 endpoint 接入；本机 repo 仅用于诊断、启动命令和资源定位。",
    },
]


def open_source_catalog(project_root: Path) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for item in CATALOG:
        default_repo = project_root / item["default_repo_path"]
        providers.append({**item, "resolved_default_repo_path": str(default_repo)})
    return providers


def detect_open_source_tts(request: OpenSourceTTSDetectRequest, project_root: Path) -> dict[str, Any]:
    catalog_item = _catalog_item(request.provider_type)
    repo_path = _resolve_path(request.repo_path, project_root) if request.repo_path else None
    repo_found = bool(repo_path and repo_path.exists() and repo_path.is_dir())
    endpoint_report = _probe_endpoint(request.base_url, request.api_contract or catalog_item["api_contracts"][0])
    setup_state = _setup_state(repo_path, repo_found, request.base_url, endpoint_report)
    return {
        "provider_type": request.provider_type,
        "repo_path": str(repo_path) if repo_path else None,
        "repo_found": repo_found,
        "base_url": request.base_url,
        "endpoint_reachable": endpoint_report["endpoint_reachable"],
        "api_contract_ok": endpoint_report["api_contract_ok"],
        "health": endpoint_report["health"],
        "setup_state": setup_state,
        "env_hint": _env_hint(catalog_item, setup_state),
    }


def configure_open_source_tts(
    request: OpenSourceTTSConfigureRequest,
    registry: ServiceRegistry,
    writable_path: Path,
    project_root: Path,
) -> tuple[ServiceRegistry, TTSServiceEndpoint, dict[str, Any]]:
    catalog_item = _catalog_item(request.provider_type)
    detect_payload = detect_open_source_tts(
        OpenSourceTTSDetectRequest(
            provider_type=request.provider_type,
            repo_path=request.repo_path,
            base_url=request.base_url,
            api_contract=request.api_contract or catalog_item["api_contracts"][0],
        ),
        project_root,
    )
    source_profile = request.source_profile
    network_scope = request.network_scope or _network_scope_for_source(source_profile)
    service_id = request.service_id or _default_service_id(request.provider_type, source_profile)
    endpoint = TTSServiceEndpoint(
        service_id=service_id,
        display_name=request.display_name or f"{catalog_item['display_name']} {source_profile.replace('_', ' ').title()}",
        service_kind="tts",
        engine=EngineName(request.provider_type if request.provider_type != "indextts" else "indextts"),
        provider_type=ProviderType(request.provider_type),
        api_contract=request.api_contract or catalog_item["api_contracts"][0],
        base_url=request.base_url,
        network_scope=network_scope,
        mode="local" if network_scope == "localhost" else "external",
        managed=request.managed if source_profile == "local_repo" else False,
        enabled=request.enabled,
        repo_path=str(_resolve_path(request.repo_path, project_root)) if request.repo_path else None,
        start_command=request.start_command,
        start_cwd=str(_resolve_path(request.start_cwd, project_root)) if request.start_cwd else None,
        resource_group=request.resource_group,
        capacity=request.capacity,
        priority=int(catalog_item["priority"]),
        capabilities=list(catalog_item["capabilities"]),
        source_profile=source_profile,
        catalog_provider=request.provider_type,
        setup_state=detect_payload["setup_state"],
        default_params={"response_format": "wav"} if request.provider_type == "cosyvoice" else {},
    )
    services = [service for service in registry.services if service.service_id != endpoint.service_id]
    services.append(endpoint)
    services.sort(key=lambda service: (service.priority, service.service_id))
    updated = ServiceRegistry(services)
    updated.save(writable_path)
    return updated, endpoint, detect_payload


def _catalog_item(provider_type: OpenSourceProvider) -> dict[str, Any]:
    for item in CATALOG:
        if item["provider_type"] == provider_type:
            return item
    raise ValueError(f"unsupported open-source TTS provider: {provider_type}")


def _resolve_path(raw_path: str | None, project_root: Path) -> Path:
    path = Path(raw_path or "")
    return path if path.is_absolute() else project_root / path


def _probe_endpoint(base_url: str | None, api_contract: str) -> dict[str, Any]:
    if not base_url:
        return {"endpoint_reachable": False, "api_contract_ok": False, "health": {"status": "missing base_url"}}
    if base_url.startswith("mock://"):
        return {"endpoint_reachable": True, "api_contract_ok": True, "health": {"ready": True, "status": "mock"}}
    timeout = httpx.Timeout(1.5, connect=0.8)
    health: dict[str, Any] = {}
    reachable = False
    contract_ok = False
    with httpx.Client(timeout=timeout) as client:
        for suffix in ("/health", "/config"):
            try:
                response = client.get(f"{base_url.rstrip('/')}{suffix}")
            except Exception as exc:
                health[suffix] = {"ok": False, "error": str(exc)}
                continue
            reachable = True
            health[suffix] = {"ok": response.is_success, "status_code": response.status_code}
            if response.is_success and (suffix == "/config" or not api_contract.startswith("gradio-")):
                contract_ok = True
        if reachable and not contract_ok and not api_contract.startswith("gradio-"):
            contract_ok = True
    return {"endpoint_reachable": reachable, "api_contract_ok": contract_ok, "health": health}


def _setup_state(repo_path: Path | None, repo_found: bool, base_url: str | None, endpoint_report: dict[str, Any]) -> str:
    if repo_path and not repo_found:
        return "repo_missing"
    if endpoint_report["endpoint_reachable"] and endpoint_report["api_contract_ok"]:
        return "ready"
    if endpoint_report["endpoint_reachable"]:
        return "partial"
    if base_url:
        return "endpoint_unreachable"
    if repo_found:
        return "repo_found"
    return "not_configured"


def _env_hint(catalog_item: dict[str, Any], setup_state: str) -> str:
    if setup_state == "repo_missing":
        return f"先获取项目：{catalog_item['recommended_clone_command']}"
    if setup_state == "endpoint_unreachable":
        return "检查服务是否已启动、端口是否正确、防火墙或网络是否允许访问。"
    if setup_state == "partial":
        return "端口可达，但 API contract 未完全匹配；请检查选择的 WebUI/API 类型。"
    if setup_state == "repo_found":
        return catalog_item["start_hint"]
    if setup_state == "ready":
        return "服务可用，可以保存为生成 endpoint。"
    return f"选择本机项目路径或填写 endpoint；推荐：{catalog_item['recommended_clone_command']}"


def _network_scope_for_source(source_profile: SourceProfile) -> Literal["localhost", "lan", "public", "commercial"]:
    if source_profile in {"local_repo", "local_endpoint"}:
        return "localhost"
    if source_profile == "lan_endpoint":
        return "lan"
    if source_profile == "api_placeholder":
        return "commercial"
    return "public"


def _default_service_id(provider_type: str, source_profile: str) -> str:
    prefix = {
        "local_repo": "local",
        "local_endpoint": "local",
        "lan_endpoint": "lan",
        "cloud_endpoint": "cloud",
        "api_placeholder": "api",
    }[source_profile]
    return f"{prefix}-{provider_type}"
