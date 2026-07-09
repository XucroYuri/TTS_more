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
    """Default OpenAI-compatible LLM parser providers.

    Ordered by ``priority`` (lower = tried first). Mainstream providers come
    first; the project-specific 开物基模 (KWJM) is kept last as a fallback.
    All are disabled by default — the user enables the ones they have a key
    for. Every entry must support the OpenAI /v1/chat/completions contract
    with ``response_format: {"type": "json_object"}``.
    """
    return [
        ParserProviderRecord(name="OpenAI", base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY", model="gpt-4o-mini", enabled=False, timeout_seconds=45.0, priority=10),
        ParserProviderRecord(name="智谱 GLM", base_url="https://open.bigmodel.cn/api/paas/v4", api_key_env="ZHIPU_API_KEY", model="glm-4-flash", enabled=False, timeout_seconds=45.0, priority=20),
        ParserProviderRecord(name="DeepSeek", base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY", model="deepseek-chat", enabled=False, timeout_seconds=45.0, priority=30),
        ParserProviderRecord(name="阿里通义", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key_env="DASHSCOPE_API_KEY", model="qwen-turbo", enabled=False, timeout_seconds=45.0, priority=40),
        ParserProviderRecord(name="月之暗面", base_url="https://api.moonshot.cn/v1", api_key_env="MOONSHOT_API_KEY", model="moonshot-v1-8k", enabled=False, timeout_seconds=45.0, priority=50),
        ParserProviderRecord(name="百度千帆", base_url="https://qianfan.baidubce.com/v2", api_key_env="QIANFAN_API_KEY", model="ernie-speed-8k", enabled=False, timeout_seconds=45.0, priority=60),
        ParserProviderRecord(name="字节豆包", base_url="https://ark.cn-beijing.volces.com/api/v3", api_key_env="ARK_API_KEY", model="doubao-lite-4k", enabled=False, timeout_seconds=45.0, priority=70),
        ParserProviderRecord(name="零一万物", base_url="https://api.lingyiwanwu.com/v1", api_key_env="YI_API_KEY", model="yi-large", enabled=False, timeout_seconds=45.0, priority=80),
        ParserProviderRecord(name="xAI Grok", base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY", model="grok-2-latest", enabled=False, timeout_seconds=45.0, priority=90),
        ParserProviderRecord(name="Mistral", base_url="https://api.mistral.ai/v1", api_key_env="MISTRAL_API_KEY", model="mistral-small-latest", enabled=False, timeout_seconds=45.0, priority=100),
        ParserProviderRecord(name="Groq", base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY", model="llama-3.1-8b-instant", enabled=False, timeout_seconds=45.0, priority=110),
        # Project-specific provider, kept last as a fallback.
        ParserProviderRecord(name="开物基模", base_url="https://kwjm.com", api_key_env="KWJM_API_KEY", model="gpt-5.5", enabled=False, timeout_seconds=45.0, priority=200),
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
