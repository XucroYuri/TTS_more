from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.parser import ParserProviderConfig


_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ParserProviderRecord(ParserProviderConfig):
    priority: int = 100


class ParserProviderUpdate(ParserProviderRecord):
    api_key: str | None = None


class ParserProviderPublic(ParserProviderRecord):
    key_configured: bool = False


class ParserProvidersUpdate(BaseModel):
    providers: list[ParserProviderUpdate] = Field(default_factory=list)


def default_parser_providers() -> list[ParserProviderRecord]:
    return [
        ParserProviderRecord(
            name="开物基模",
            base_url="",
            api_key_env="KWJM_API_KEY",
            model="gpt-5.5",
            enabled=False,
            timeout_seconds=45.0,
            priority=10,
        )
    ]


def load_parser_providers(path: Path) -> list[ParserProviderRecord]:
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _sorted_records([ParserProviderRecord.model_validate(item) for item in raw])
    raw_env = os.environ.get("TTS_MORE_PARSER_PROVIDERS")
    if raw_env:
        return _sorted_records([ParserProviderRecord.model_validate(item) for item in json.loads(raw_env)])
    return default_parser_providers()


def public_parser_providers(path: Path, env_path: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        "providers": [
            ParserProviderPublic(
                **record.model_dump(mode="python"),
                key_configured=_api_key_configured(record.api_key_env, env_path),
            ).model_dump(mode="json")
            for record in load_parser_providers(path)
        ]
    }


def save_parser_providers(path: Path, env_path: Path, payload: ParserProvidersUpdate) -> list[ParserProviderRecord]:
    records = _sorted_records(
        [
            ParserProviderRecord(
                name=provider.name,
                base_url=provider.base_url,
                api_key_env=provider.api_key_env,
                model=provider.model,
                enabled=provider.enabled,
                timeout_seconds=provider.timeout_seconds,
                priority=provider.priority,
            )
            for provider in payload.providers
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([record.model_dump(mode="json") for record in records], ensure_ascii=False, indent=2), encoding="utf-8")
    for provider in payload.providers:
        if provider.api_key:
            set_env_value(env_path, provider.api_key_env, provider.api_key)
    return records


def set_env_value(path: Path, key: str, value: str) -> None:
    if not _ENV_NAME_RE.match(key):
        raise ValueError(f"invalid env var name: {key}")
    sanitized = value.replace("\r", "").replace("\n", "")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    updated = False
    output: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            output.append(f"{key}={sanitized}")
            updated = True
        else:
            output.append(line)
    if not updated:
        output.append(f"{key}={sanitized}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.environ[key] = sanitized


def _api_key_configured(key: str, env_path: Path) -> bool:
    if os.environ.get(key):
        return True
    prefix = f"{key}="
    if not env_path.exists():
        return False
    return any(line.startswith(prefix) and line[len(prefix) :].strip() for line in env_path.read_text(encoding="utf-8").splitlines())


def _sorted_records(records: list[ParserProviderRecord]) -> list[ParserProviderRecord]:
    return sorted(records, key=lambda provider: (provider.priority, provider.name))
