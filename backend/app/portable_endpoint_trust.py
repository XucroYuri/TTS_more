from __future__ import annotations

import re
import unicodedata
from typing import Protocol, Sequence

from app.models import TTSServiceEndpoint


class ResolvedPortableDescriptor(Protocol):
    component: str
    package_id: str
    package_root: str
    default_url: str
    manageable: bool
    initialized: bool


def require_unique_service_identities(services: list[TTSServiceEndpoint]) -> None:
    service_ids: set[str] = set()
    package_identities: set[tuple[str, str]] = set()
    for service in services:
        service_id = _require_canonical_identity_text(service.service_id, "service_id")
        canonical_service_id = service_id.casefold()
        if canonical_service_id in service_ids:
            raise ValueError(f"duplicate service_id: {service.service_id}")
        service_ids.add(canonical_service_id)
        locator = service.portable_locator
        if service.control_kind != "portable-package" or locator is None:
            continue
        component = _require_canonical_identity_text(
            locator.component,
            "portable package component",
        )
        package_id = _require_canonical_identity_text(
            locator.package_id,
            "portable package identity",
        )
        if (
            component not in {"gpt-sovits", "indextts", "cosyvoice"}
            or component != component.casefold()
        ):
            raise ValueError(f"noncanonical portable package component: {locator.component}")
        if (
            package_id != package_id.casefold()
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", package_id) is None
        ):
            raise ValueError(
                f"noncanonical portable package identity: {locator.component}/{locator.package_id}"
            )
        identity = (component, package_id)
        if identity in package_identities:
            raise ValueError(
                f"duplicate portable package identity: {locator.component}/{locator.package_id}"
            )
        package_identities.add(identity)


def preflight_service_endpoints(
    services: Sequence[TTSServiceEndpoint],
) -> list[TTSServiceEndpoint]:
    """Round-trip and validate the exact merged sequence before publication."""

    validated = [
        TTSServiceEndpoint.model_validate(service.model_dump(mode="json"))
        for service in services
    ]
    require_unique_service_identities(validated)
    return validated


def _require_canonical_identity_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if unicodedata.normalize("NFKC", value) != value:
        raise ValueError(f"{label} must use canonical NFKC characters")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} must not contain Unicode category C characters")
    return value


def sanitize_portable_endpoint(endpoint: TTSServiceEndpoint) -> TTSServiceEndpoint:
    """Strip machine-local authority from every persisted portable endpoint."""

    if endpoint.control_kind != "portable-package":
        return endpoint
    return endpoint.model_copy(
        update={
            "managed": False,
            "repo_path": None,
            "start_command": [],
            "start_cwd": None,
        }
    )


def trust_resolved_portable_endpoint(
    endpoint: TTSServiceEndpoint,
    descriptor: ResolvedPortableDescriptor,
    *,
    include_runner: bool = False,
) -> TTSServiceEndpoint:
    """Restore local control only after a fresh, identity-bound locator resolution."""

    sanitized = sanitize_portable_endpoint(endpoint)
    locator = sanitized.portable_locator
    trusted = (
        sanitized.control_kind == "portable-package"
        and locator is not None
        and locator.component == descriptor.component
        and locator.package_id == descriptor.package_id
        and sanitized.mode == "local"
        and sanitized.network_scope == "localhost"
        and sanitized.api_contract == "tts-more-v1"
        and descriptor.manageable
    )
    default_params = dict(sanitized.default_params)
    if trusted:
        # The portable-package controller launches the package's root Start.cmd,
        # whose safe default refuses host paths. Artifact transfer is therefore
        # the only delivery mode that is valid for this managed control kind.
        default_params["delivery"] = "artifact"
    return sanitized.model_copy(
        update={
            "base_url": descriptor.default_url if trusted else sanitized.base_url,
            "managed": trusted,
            "repo_path": descriptor.package_root if trusted else None,
            "start_command": [
                "python.exe",
                "scripts/portable_package_runner.py",
                "--package-root",
                descriptor.package_root,
            ]
            if trusted and include_runner
            else [],
            "start_cwd": "." if trusted and include_runner else None,
            "default_params": default_params,
            "setup_state": (
                "ready" if descriptor.initialized else "env_missing"
            )
            if trusted
            else sanitized.setup_state,
        }
    )
