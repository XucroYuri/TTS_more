from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.models import EngineName, ProviderType, TTSServiceEndpoint
from app.net_guard import EgressError, scrub_error, validate_egress_url
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
    resource_group: str = "gradio-gpu-0"
    capacity: int = Field(default=1, ge=1)
    start_command: list[str] = Field(default_factory=list)
    start_cwd: str | None = None


CATALOG: list[dict[str, Any]] = [
    {
        "provider_type": "gpt-sovits",
        "display_name": "GPT-SoVITS",
        "clone_url": "https://github.com/XucroYuri/GPT-SoVITS.git",
        "default_repo_path": "repo/GPT-SoVITS-main",
        "default_base_url": "http://127.0.0.1:9880",
        "default_ports": [9880, 9872],
        "api_contracts": ["tts-more-v1", "gradio-gpt-sovits-webui"],
        "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice", "gpt-weights", "sovits-weights", "wav_output", "gradio_webui"],
        "priority": 10,
        "resource_group": "gradio-gpu-0",
        "recommended_clone_command": "python scripts/tts_more_deploy.py sync-repos --clean",
        "start_hint": "启动 GPT-SoVITS 推理 WebUI 后，粘贴局域网可访问的 Gradio 地址，例如 http://tts-webui.local:9872。",
    },
    {
        "provider_type": "indextts",
        "display_name": "IndexTTS",
        "clone_url": "https://github.com/XucroYuri/index-tts.git",
        "default_repo_path": "repo/index-tts",
        "default_base_url": "http://127.0.0.1:9881",
        "default_ports": [9881, 7860],
        "api_contracts": ["tts-more-v1", "gradio-indextts2-webui"],
        "capabilities": ["tts", "reference_audio_voice", "emotion_text", "emotion_audio", "wav_output", "gradio_webui"],
        "priority": 20,
        "resource_group": "gradio-gpu-0",
        "recommended_clone_command": "git clone https://github.com/XucroYuri/index-tts.git repo/index-tts",
        "start_hint": "启动 IndexTTS 推理 WebUI 后，粘贴局域网可访问的 Gradio 地址，例如 http://tts-webui.local:7860。",
    },
    {
        "provider_type": "cosyvoice",
        "display_name": "CosyVoice",
        "clone_url": "https://github.com/XucroYuri/CosyVoice.git",
        "default_repo_path": "repo/CosyVoice",
        "default_base_url": "http://127.0.0.1:9882",
        "default_ports": [9882, 50000],
        "api_contracts": ["tts-more-v1", "gradio-cosyvoice-webui"],
        "capabilities": ["tts", "reference_audio_voice", "zero_shot_voice", "cross_lingual_voice", "style_instruction", "wav_output", "gradio_webui"],
        "priority": 30,
        "resource_group": "gradio-gpu-0",
        "recommended_clone_command": "git clone https://github.com/XucroYuri/CosyVoice.git repo/CosyVoice",
        "start_hint": "启动 CosyVoice 推理 WebUI 后，粘贴局域网可访问的 Gradio 地址，例如 http://tts-webui.local:50000。",
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
    api_contract = _gradio_contract(catalog_item, request.api_contract)
    repo_path = None
    repo_found = False
    endpoint_report = _probe_endpoint(request.base_url, api_contract)
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
    api_contract = _gradio_contract(catalog_item, request.api_contract)
    source_profile = _source_profile_for_endpoint(request.base_url)
    detect_payload = detect_open_source_tts(
        OpenSourceTTSDetectRequest(
            provider_type=request.provider_type,
            repo_path=None,
            base_url=request.base_url,
            api_contract=api_contract,
        ),
        project_root,
    )
    network_scope = _network_scope_for_source(source_profile)
    service_id = request.service_id or _default_service_id(request.provider_type, source_profile)
    endpoint = TTSServiceEndpoint(
        service_id=service_id,
        display_name=request.display_name or f"{catalog_item['display_name']} {source_profile.replace('_', ' ').title()}",
        service_kind="tts",
        engine=EngineName(request.provider_type if request.provider_type != "indextts" else "indextts"),
        provider_type=ProviderType(request.provider_type),
        api_contract=api_contract,
        base_url=request.base_url,
        network_scope=network_scope,
        mode="external",
        managed=False,
        enabled=request.enabled,
        repo_path=None,
        start_command=[],
        start_cwd=None,
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


def _gradio_contract(catalog_item: dict[str, Any], requested_contract: str | None = None) -> str:
    """Resolve the API contract for a provider.

    Preference order: the requested contract if it matches a declared one,
    then the non-invasive tts-more-v1 worker contract (primary path), then any
    gradio- contract (fallback for users running the upstream Gradio WebUI).
    """
    declared = [str(c) for c in catalog_item["api_contracts"]]
    if requested_contract and requested_contract in declared:
        return requested_contract
    if "tts-more-v1" in declared:
        return "tts-more-v1"
    for contract in declared:
        if contract.startswith("gradio-"):
            return contract
    if requested_contract and requested_contract.startswith("gradio-"):
        return requested_contract
    raise ValueError(f"{catalog_item['provider_type']} does not declare a contract")


def _resolve_path(raw_path: str | None, project_root: Path) -> Path:
    path = Path(raw_path or "")
    return path if path.is_absolute() else project_root / path


def _probe_endpoint(base_url: str | None, api_contract: str) -> dict[str, Any]:
    if not base_url:
        return {"endpoint_reachable": False, "api_contract_ok": False, "health": {"status": "missing base_url"}}
    if base_url.startswith("mock://"):
        return {"endpoint_reachable": True, "api_contract_ok": True, "health": {"ready": True, "status": "mock"}}
    # SSRF guard: local (loopback) and LAN (private) endpoints are legitimate
    # here — a user points this at their own Gradio WebUI — but link-local
    # (169.254.0.0/16, covers cloud metadata) is always blocked.
    try:
        validate_egress_url(base_url, allow_loopback=True, allow_private=True)
    except EgressError as exc:
        return {"endpoint_reachable": False, "api_contract_ok": False, "health": {"status": "blocked", "reason": str(exc)}}
    timeout = httpx.Timeout(1.5, connect=0.8)
    health: dict[str, Any] = {}
    reachable = False
    contract_ok = False
    # trust_env=False so the probe ignores host-level HTTP/HTTPS/SOCKS proxies.
    # Otherwise a host with a SOCKS proxy configured system-wide raises
    # ``ImportError: Using SOCKS proxy, but the 'socksio' package is not installed``
    # before the request even leaves the process, which masks the real
    # endpoint-unreachable diagnostic we want to surface.
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        for suffix in ("/health", "/config"):
            try:
                response = client.get(f"{base_url.rstrip('/')}{suffix}")
            except Exception as exc:
                health[suffix] = {"ok": False, "error": scrub_error(exc, base_url)}
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
    if setup_state == "endpoint_unreachable":
        return f"检查 Gradio WebUI 是否已启动、地址是否可从本机访问。{catalog_item['start_hint']}"
    if setup_state == "partial":
        return "端口可达，但 Gradio config 或 api_name 未完全匹配；请确认粘贴的是推理 WebUI 地址。"
    if setup_state == "ready":
        return "Gradio WebUI 可用，可以保存为生成 endpoint。"
    return f"启动推理 WebUI 后粘贴 Gradio 地址。{catalog_item['start_hint']}"


def _source_profile_for_endpoint(base_url: str) -> Literal["local_endpoint", "lan_endpoint", "cloud_endpoint"]:
    hostname = urlparse(base_url).hostname or ""
    normalized = hostname.strip("[]").lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return "local_endpoint"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if normalized.endswith(".local") or "." not in normalized:
            return "lan_endpoint"
        return "cloud_endpoint"
    if address.is_loopback:
        return "local_endpoint"
    if address.is_private:
        return "lan_endpoint"
    return "cloud_endpoint"


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
