from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.models import EngineName, ProviderType, TTSServiceEndpoint


SUPPORTED_COMPONENTS = {"gpt-sovits", "indextts", "cosyvoice"}
PROVIDER_ENGINES = {
    "gpt-sovits": (ProviderType.GPT_SOVITS, EngineName.GPT_SOVITS),
    "indextts": (ProviderType.INDEX_TTS, EngineName.INDEX_TTS),
    "cosyvoice": (ProviderType.COSYVOICE, EngineName.COSYVOICE),
}


class PortablePackageDiscoverRequest(BaseModel):
    roots: list[str] = Field(default_factory=list)
    include_siblings: bool = True


class PortablePackageRegisterRequest(BaseModel):
    package_root: str
    base_url: str | None = None
    display_name: str | None = None
    enabled: bool = True


class PortablePackageDescriptor(BaseModel):
    package_root: str
    manifest_path: str
    schema_version: int
    component: str
    package_id: str
    version: str
    build_id: str
    package_profile: str | None = None
    default_url: str
    port: int
    launcher: str
    health_path: str
    capabilities: list[str]
    protocol_version: str
    controller_range: str
    operations_path: str
    initialized: bool
    valid: bool
    errors: list[str] = Field(default_factory=list)


def discover_portable_packages(
    project_root: Path, explicit_roots: list[str | Path], *, include_siblings: bool = True
) -> list[PortablePackageDescriptor]:
    candidates: set[Path] = set()
    if include_siblings:
        parent = project_root.resolve(strict=False).parent
        if parent.is_dir():
            candidates.update(path for path in parent.iterdir() if path.is_dir())
    for raw in explicit_roots:
        root = Path(raw).expanduser().resolve(strict=False)
        if _manifest_path(root).is_file():
            candidates.add(root)
        elif root.is_dir():
            candidates.update(path for path in root.iterdir() if path.is_dir())

    discovered: list[PortablePackageDescriptor] = []
    for root in sorted(candidates, key=lambda path: str(path).casefold()):
        manifest = _manifest_path(root)
        if not manifest.is_file():
            continue
        try:
            discovered.append(read_portable_package(root))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            discovered.append(
                PortablePackageDescriptor(
                    package_root=str(root),
                    manifest_path=str(manifest),
                    schema_version=0,
                    component="unknown",
                    package_id="unknown",
                    version="",
                    build_id="",
                    default_url="",
                    port=0,
                    launcher="",
                    health_path="/health",
                    capabilities=[],
                    protocol_version="",
                    controller_range="",
                    operations_path="data/local/operations",
                    initialized=False,
                    valid=False,
                    errors=[str(exc)],
                )
            )
    return sorted(discovered, key=lambda item: (item.component, item.package_root.casefold()))


def read_portable_package(package_root: Path) -> PortablePackageDescriptor:
    root = package_root.expanduser().resolve(strict=False)
    manifest_path = _manifest_path(root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    schema = int(payload.get("schema_version") or 0)
    errors: list[str] = []
    component = str(payload.get("component") or "")
    package_id = str(payload.get("package_id") or component)
    release_version = str(payload.get("release_version") or payload.get("version") or "")
    protocol = _mapping(payload.get("protocol"))
    protocol_version = str(protocol.get("version") or "1.0")
    controller_range = str(protocol.get("controller_range") or ">=0.2.0,<0.3.0")
    data = _mapping(payload.get("data"))
    operations_path = str(data.get("operations") or "data/local/operations")
    if component not in SUPPORTED_COMPONENTS:
        errors.append(f"unsupported portable component: {component or 'missing'}")
    if payload.get("api_contract") != "tts-more-v1":
        errors.append("api_contract must be tts-more-v1")

    if schema == 1:
        default_url = str(payload.get("default_endpoint") or "")
        port = int(payload.get("port") or 0)
        launcher = str(payload.get("launcher") or "")
        health_path = str(payload.get("health_path") or "/health")
        package_profile = None
        state_path = "data/local/install-state.json"
    elif schema == 2:
        endpoint = _mapping(payload.get("endpoint"))
        launchers = _mapping(payload.get("launchers"))
        runtime = _mapping(payload.get("runtime"))
        default_url = str(endpoint.get("default_url") or "")
        port = int(endpoint.get("port") or 0)
        launcher = str(launchers.get("start") or "")
        health_path = str(endpoint.get("health_path") or "/health")
        package_profile = str(payload.get("package_profile") or "")
        state_path = str(runtime.get("state_path") or "data/local/install-state.json")
        if package_profile not in {"bootstrap", "full"}:
            errors.append("package_profile must be bootstrap or full")
    else:
        raise ValueError("schema_version must be 1 or 2")

    if not 1 <= port <= 65535:
        errors.append("portable endpoint port is invalid")
    if not default_url.startswith("http://127.0.0.1:") and not default_url.startswith("http://localhost:"):
        errors.append("portable package default endpoint must be loopback")
    if not _safe_relative_path(launcher) or not (root / launcher).is_file():
        errors.append("portable package Start launcher is missing or unsafe")
    if not _safe_relative_path(state_path):
        errors.append("portable package install state path is unsafe")

    return PortablePackageDescriptor(
        package_root=str(root),
        manifest_path=str(manifest_path),
        schema_version=schema,
        component=component,
        package_id=package_id,
        version=release_version,
        build_id=str(payload.get("build_id") or ""),
        package_profile=package_profile,
        default_url=default_url,
        port=port,
        launcher=launcher,
        health_path=health_path,
        capabilities=[str(item) for item in payload.get("capabilities") or []],
        protocol_version=protocol_version,
        controller_range=controller_range,
        operations_path=operations_path,
        initialized=(root / state_path).is_file(),
        valid=not errors,
        errors=errors,
    )


def endpoint_from_portable_package(
    descriptor: PortablePackageDescriptor, request: PortablePackageRegisterRequest
) -> TTSServiceEndpoint:
    if not descriptor.valid:
        raise ValueError(f"portable package is invalid: {', '.join(descriptor.errors)}")
    requested_root = Path(request.package_root).expanduser().resolve(strict=False)
    if requested_root != Path(descriptor.package_root):
        raise ValueError("registered package root does not match the validated descriptor")
    provider, engine = PROVIDER_ENGINES[descriptor.component]
    suffix = descriptor.build_id[:12] or descriptor.version.replace(".", "-") or "portable"
    service_id = f"portable-{descriptor.component}-{suffix}".lower()
    capabilities = list(dict.fromkeys([*descriptor.capabilities, "tts-more-worker", "artifact-transfer"]))

    if request.base_url:
        base_url = _trusted_lan_url(request.base_url, descriptor.port)
        return TTSServiceEndpoint(
            service_id=service_id,
            display_name=request.display_name or f"{descriptor.component} portable LAN",
            engine=engine,
            provider_type=provider,
            api_contract="tts-more-v1",
            base_url=base_url,
            mode="external",
            network_scope="lan",
            managed=False,
            enabled=request.enabled,
            repo_path=None,
            start_command=[],
            start_cwd=None,
            resource_group=f"lan-{descriptor.component}",
            capabilities=capabilities,
            source_profile="lan_endpoint",
            catalog_provider=descriptor.component,
            setup_state="endpoint_unreachable",
            default_params={"delivery": "artifact"},
        )

    return TTSServiceEndpoint(
        service_id=service_id,
        display_name=request.display_name or f"{descriptor.component} portable",
        engine=engine,
        provider_type=provider,
        api_contract="tts-more-v1",
        base_url=descriptor.default_url,
        mode="local",
        network_scope="localhost",
        managed=True,
        enabled=request.enabled,
        repo_path=descriptor.package_root,
        start_command=[
            "python.exe",
            "scripts/portable_package_runner.py",
            "--package-root",
            descriptor.package_root,
        ],
        start_cwd=".",
        resource_group="local-gpu-0",
        capabilities=capabilities,
        source_profile="local_repo",
        catalog_provider=descriptor.component,
        setup_state="ready" if descriptor.initialized else "env_missing",
        default_params={"delivery": "path"},
    )


def _trusted_lan_url(raw: str, default_port: int) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("portable worker URL must be an HTTP(S) trusted LAN endpoint")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." in host and not host.lower().endswith(".local"):
            raise ValueError("portable worker registration is limited to a trusted LAN")
    else:
        if not address.is_private or address.is_loopback or address.is_link_local:
            raise ValueError("portable worker registration is limited to a trusted LAN")
    port = parsed.port or default_port
    return f"{parsed.scheme}://{host}:{port}"


def _manifest_path(root: Path) -> Path:
    return root / "package" / "tts-more-package.json"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_relative_path(raw: str) -> bool:
    normalized = raw.replace("\\", "/")
    return bool(normalized) and not Path(normalized).is_absolute() and ":" not in normalized and ".." not in normalized.split("/")
