from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.models import EngineName, PortableServiceLocator, ProviderType, TTSServiceEndpoint
from app.portable_endpoint_trust import trust_resolved_portable_endpoint
from app.portable_file_io import safe_read_bytes
from app.portable_manifest import validate_portable_manifest_v2_raw


SUPPORTED_COMPONENTS = {"gpt-sovits", "indextts", "cosyvoice"}
CONTROLLER_VERSION = "0.2.0"
SUPPORTED_PROTOCOL_VERSION = "1.0"
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
    state_path: str = "data/local/install-state.json"
    launchers: dict[str, str] = Field(default_factory=dict)
    initialized: bool
    valid: bool
    errors: list[str] = Field(default_factory=list)
    complete_v2: bool = False
    protocol_compatible: bool = False
    controller_compatible: bool = False
    manageable: bool = False
    management_errors: list[str] = Field(default_factory=list)


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
    root = Path(os.path.abspath(package_root.expanduser()))
    manifest_path = _manifest_path(root)
    manifest_bytes = safe_read_bytes(
        root,
        manifest_path,
        max_bytes=512 * 1024,
        label="portable package manifest",
        retries=2,
    )
    assert manifest_bytes is not None
    payload = json.loads(manifest_bytes.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("portable manifest must be a JSON object")
    raw_schema = payload.get("schema_version")
    if type(raw_schema) is not int:
        raise ValueError("schema_version must be an exact integer 1 or 2")
    schema = raw_schema
    errors: list[str] = []
    raw_component = payload.get("component")
    component = raw_component if isinstance(raw_component, str) else ""
    raw_package_id = payload.get("package_id")
    package_id = raw_package_id if isinstance(raw_package_id, str) else component
    raw_release_version = payload.get("release_version") or payload.get("version")
    release_version = raw_release_version if isinstance(raw_release_version, str) else ""
    protocol = _mapping(payload.get("protocol"))
    raw_protocol_version = protocol.get("version")
    protocol_version = raw_protocol_version if isinstance(raw_protocol_version, str) and raw_protocol_version else "1.0"
    raw_controller_range = protocol.get("controller_range")
    controller_range = (
        raw_controller_range
        if isinstance(raw_controller_range, str) and raw_controller_range
        else ">=0.2.0,<0.3.0"
    )
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
        launchers = {"start": launcher}
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
    if not _loopback_default_url(default_url, port):
        errors.append("portable package default endpoint must be loopback")
    if not _safe_relative_path(launcher) or not (root / launcher).is_file():
        errors.append("portable package Start launcher is missing or unsafe")
    if not _safe_relative_path(state_path):
        errors.append("portable package install state path is unsafe")
    if not _safe_relative_path(operations_path):
        errors.append("portable package operations path is unsafe")

    management_errors = _management_errors(payload, root) if schema == 2 else ["schema_version 2 is required"]
    complete_v2 = schema == 2 and not management_errors
    protocol_compatible = (
        schema == 2
        and protocol.get("name") == "tts-more-v1"
        and protocol_version == SUPPORTED_PROTOCOL_VERSION
    )
    controller_compatible = complete_v2 and is_controller_compatible(controller_range, CONTROLLER_VERSION)
    manageable = not errors and complete_v2 and protocol_compatible and controller_compatible

    return PortablePackageDescriptor(
        package_root=str(root),
        manifest_path=str(manifest_path),
        schema_version=schema,
        component=component,
        package_id=package_id,
        version=release_version,
        build_id=payload.get("build_id") if isinstance(payload.get("build_id"), str) else "",
        package_profile=package_profile,
        default_url=default_url,
        port=port,
        launcher=launcher,
        health_path=health_path,
        capabilities=[str(item) for item in payload.get("capabilities") or []],
        protocol_version=protocol_version,
        controller_range=controller_range,
        operations_path=operations_path,
        state_path=state_path,
        launchers={str(key): str(value) for key, value in launchers.items()},
        initialized=(root / state_path).is_file(),
        valid=not errors,
        errors=errors,
        complete_v2=complete_v2,
        protocol_compatible=protocol_compatible,
        controller_compatible=controller_compatible,
        manageable=manageable,
        management_errors=management_errors,
    )


def inspect_locator_candidate(package_root: Path) -> PortablePackageDescriptor | None:
    """Read one locator candidate only when its root identity is stable and schema v2 is complete."""

    root = Path(os.path.abspath(package_root.expanduser()))
    try:
        if not root.is_dir() or _is_reparse_point(root):
            return None
        if _canonical_path(root.resolve(strict=True)) != _canonical_path(root):
            return None
        descriptor = read_portable_package(root)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if not descriptor.valid or not descriptor.complete_v2:
        return None
    return descriptor


def endpoint_from_portable_package(
    descriptor: PortablePackageDescriptor, request: PortablePackageRegisterRequest
) -> TTSServiceEndpoint:
    if not descriptor.valid:
        raise ValueError(f"portable package is invalid: {', '.join(descriptor.errors)}")
    requested_root = Path(request.package_root).expanduser().resolve(strict=False)
    if requested_root != Path(descriptor.package_root):
        raise ValueError("registered package root does not match the validated descriptor")
    provider, engine = PROVIDER_ENGINES[descriptor.component]
    suffix = re.sub(r"[^a-z0-9._-]+", "-", descriptor.package_id.casefold()).strip("-") or "portable"
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

    endpoint = TTSServiceEndpoint(
        service_id=service_id,
        display_name=request.display_name or f"{descriptor.component} portable",
        engine=engine,
        provider_type=provider,
        api_contract="tts-more-v1",
        base_url=descriptor.default_url,
        mode="local",
        network_scope="localhost",
        managed=False,
        enabled=request.enabled,
        repo_path=None,
        start_command=[],
        start_cwd=None,
        resource_group="local-gpu-0",
        capabilities=capabilities,
        source_profile="local_repo",
        catalog_provider=descriptor.component,
        setup_state="ready" if descriptor.initialized else "env_missing",
        default_params={"delivery": "path"},
        control_kind="portable-package",
        portable_locator=PortableServiceLocator(
            component=descriptor.component,
            package_id=descriptor.package_id,
            absolute_path_last_seen=descriptor.package_root,
            build_id_last_seen=descriptor.build_id or None,
        ),
    )
    return trust_resolved_portable_endpoint(endpoint, descriptor, include_runner=True)


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


def is_controller_compatible(controller_range: str, controller_version: str = CONTROLLER_VERSION) -> bool:
    try:
        actual = _version_tuple(controller_version)
        clauses = [clause.strip() for clause in controller_range.split(",") if clause.strip()]
        if not clauses:
            return False
        for clause in clauses:
            match = re.fullmatch(r"(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,3})", clause)
            if match is None:
                return False
            expected = _version_tuple(match.group(2))
            operator = match.group(1)
            if operator == ">=" and actual < expected:
                return False
            if operator == "<=" and actual > expected:
                return False
            if operator == ">" and actual <= expected:
                return False
            if operator == "<" and actual >= expected:
                return False
            if operator == "==" and actual != expected:
                return False
        return True
    except (TypeError, ValueError):
        return False


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    parts = value.split(".")
    if not 1 <= len(parts) <= 4 or any(not part.isdigit() for part in parts):
        raise ValueError("version must contain numeric dot-separated parts")
    return tuple([*(int(part) for part in parts), *(0 for _ in range(4 - len(parts)))])  # type: ignore[return-value]


def _management_errors(payload: dict[str, Any], root: Path) -> list[str]:
    _manifest, errors = validate_portable_manifest_v2_raw(payload)
    if errors:
        return errors
    for field in ("package_id", "release_version", "build_id"):
        if not _strict_identity(payload.get(field)):
            errors.append(f"{field} must be an unambiguous non-empty string")
    if payload.get("platform") != "windows-x64":
        errors.append("platform must be windows-x64")
    if payload.get("package_profile") not in {"bootstrap", "full"}:
        errors.append("package_profile must be bootstrap or full")
    if payload.get("api_contract") != "tts-more-v1":
        errors.append("api_contract must be tts-more-v1")

    source = _mapping(payload.get("source"))
    if not _strict_text(source.get("repository")):
        errors.append("source.repository is required")
    if not _immutable_revision(source.get("revision")):
        errors.append("source.revision must be immutable")
    integration = _mapping(payload.get("integration"))
    if not _strict_text(integration.get("version")):
        errors.append("integration.version is required")
    if not _immutable_revision(integration.get("source_revision")):
        errors.append("integration.source_revision must be immutable")
    if not _sha256(integration.get("bundle_sha256")):
        errors.append("integration.bundle_sha256 must be a SHA-256 digest")

    runtime = _mapping(payload.get("runtime"))
    if not _strict_text(runtime.get("python_version")):
        errors.append("runtime.python_version is required")
    profiles = runtime.get("device_profiles")
    if not isinstance(profiles, list) or not profiles or any(not isinstance(item, str) for item in profiles):
        errors.append("runtime.device_profiles must be a non-empty string list")
    _require_package_file(root, runtime.get("lock"), "runtime.lock", errors)
    _require_relative(runtime.get("state_path"), "runtime.state_path", errors, root=root)

    models = _mapping(payload.get("models"))
    _require_package_file(root, models.get("lock"), "models.lock", errors)
    if not isinstance(models.get("required"), bool):
        errors.append("models.required must be a boolean")
    _require_relative(payload.get("data_root"), "data_root", errors, root=root)

    protocol = _mapping(payload.get("protocol"))
    if protocol.get("name") != "tts-more-v1":
        errors.append("protocol.name must be tts-more-v1")
    for name in ("version", "controller_range"):
        if not _strict_text(protocol.get(name)):
            errors.append(f"protocol.{name} is required")
    data = _mapping(payload.get("data"))
    for name in ("user", "local", "cache", "operations"):
        _require_relative(data.get(name), f"data.{name}", errors, root=root)

    launchers = _mapping(payload.get("launchers"))
    exact_launchers = {
        "initialize": "Initialize.cmd",
        "start": "Start.cmd",
        "stop": "Stop.cmd",
        "repair": "Repair.cmd",
        "build": "Build-Package.ps1",
    }
    for name in ("initialize", "start", "stop", "repair", "build"):
        _require_package_file(root, launchers.get(name), f"launchers.{name}", errors)
        if launchers.get(name) != exact_launchers[name]:
            errors.append(f"launchers.{name} must be {exact_launchers[name]}")
    endpoint = _mapping(payload.get("endpoint"))
    port = endpoint.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        errors.append("endpoint.port must be between 1 and 65535")
    default_url = endpoint.get("default_url")
    if not isinstance(default_url, str) or not _loopback_default_url(default_url, port):
        errors.append("endpoint.default_url must be loopback")
    for name in ("health_path", "capabilities_path"):
        value = endpoint.get(name)
        if not isinstance(value, str) or not value.startswith("/"):
            errors.append(f"endpoint.{name} must start with /")
    if endpoint.get("bind_policy") not in {"loopback", "trusted-lan"}:
        errors.append("endpoint.bind_policy is invalid")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        errors.append("capabilities must be a string list")
    _require_package_file(root, payload.get("sha256_manifest"), "sha256_manifest", errors)
    _require_package_file(root, payload.get("licenses"), "licenses", errors)

    try:
        if _is_reparse_point(root):
            errors.append("package root must not be a reparse point")
        if _path_contains_reparse(root, _manifest_path(root)):
            errors.append("package manifest path must not traverse a reparse point")
    except OSError:
        errors.append("package root is unavailable")
    return errors


def _strict_text(value: Any) -> bool:
    import unicodedata

    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(unicodedata.category(character).startswith("C") for character in value)
    )


def _strict_identity(value: Any) -> bool:
    return _strict_text(value) and not any(character.isspace() for character in value)


def _immutable_revision(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", value) is not None


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _require_relative(
    value: Any,
    label: str,
    errors: list[str],
    *,
    root: Path | None = None,
) -> None:
    if not isinstance(value, str) or not _safe_relative_path(value):
        errors.append(f"{label} must be a safe relative path")
        return
    if root is not None:
        path = root / Path(value.replace("\\", "/"))
        try:
            if _path_contains_reparse(root, path):
                errors.append(f"{label} traverses a reparse point")
        except OSError:
            errors.append(f"{label} is unsafe")


def _require_package_file(root: Path, value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _safe_relative_path(value):
        errors.append(f"{label} must be a safe relative path")
        return
    path = root / Path(value.replace("\\", "/"))
    try:
        if not path.is_file() or _path_contains_reparse(root, path):
            errors.append(f"{label} is missing or unsafe")
    except OSError:
        errors.append(f"{label} is missing or unsafe")


def _path_contains_reparse(root: Path, path: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            return True
    return False


def _is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & flag)


def _canonical_path(path: Path) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", os.path.normcase(str(path))).casefold()


def _loopback_default_url(value: str, expected_port: int) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == expected_port
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False
