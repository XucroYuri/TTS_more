from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.models import TTSServiceEndpoint
from app.parser_config import set_env_value
from app.services import ServiceRegistry


class ServiceSettingsRecord(TTSServiceEndpoint):
    secrets: dict[str, str] = Field(default_factory=dict)


class ServiceSettingsUpdate(BaseModel):
    services: list[ServiceSettingsRecord] = Field(default_factory=list)


def public_service_settings(registry: ServiceRegistry, env_path: Path) -> dict[str, Any]:
    return {
        "services": [
            {
                **service.model_dump(mode="json"),
                "key_configured": _service_key_configured(service, env_path),
            }
            for service in registry.services
        ]
    }


def save_service_settings(
    path: Path,
    env_path: Path,
    payload: ServiceSettingsUpdate,
    *,
    publish: Callable[[ServiceRegistry], None] | None = None,
) -> ServiceRegistry:
    services: list[TTSServiceEndpoint] = []
    for record in payload.services:
        for key, value in record.secrets.items():
            if value:
                set_env_value(env_path, key, value)
        data = record.model_dump(mode="python", exclude={"secrets"})
        services.append(TTSServiceEndpoint.model_validate(data))
    registry = ServiceRegistry.load(path).with_services(services)
    registry.save(path, publish=publish)
    return registry


def _service_key_configured(service: TTSServiceEndpoint, env_path: Path) -> bool:
    keys = [value for key, value in service.auth_profile.items() if key.endswith("_env")]
    if service.auth_header_env:
        keys.append(service.auth_header_env)
    if not keys:
        return True
    return all(_env_value_exists(key, env_path) for key in keys)


def _env_value_exists(key: str, env_path: Path) -> bool:
    if os.environ.get(key):
        return True
    if not env_path.exists():
        return False
    prefix = f"{key}="
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix) and line[len(prefix) :].strip():
            return True
    return False
