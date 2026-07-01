from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from app.models import Character, ScriptLine


class ParsedScriptDraft(BaseModel):
    provider: str
    characters: list[Character] = Field(default_factory=list)
    lines: list[ScriptLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ParserProvider(Protocol):
    name: str

    def parse(self, text: str) -> ParsedScriptDraft:
        ...


class ParserProviderConfig(BaseModel):
    name: str
    base_url: str
    api_key_env: str
    model: str
    enabled: bool = True
    timeout_seconds: float = 45.0


class ParserProviderUnavailable(RuntimeError):
    pass


_LINE_RE = re.compile(r"^\s*(?P<speaker>[^:：\n（(]+?)\s*(?:[（(](?P<note>[^）)]*)[）)])?\s*[:：]\s*(?P<text>.+?)\s*$")
_LEADING_NOTE_RE = re.compile(r"^\s*[（(](?P<note>[^）)]*)[）)]\s*(?P<text>.+?)\s*$")
_KNOWN_CHINESE_SLUGS = {
    "小美": "xiao-mei",
    "王强": "wang-qiang",
    "旁白": "pang-bai",
}


def slugify_name(name: str) -> str:
    if name in _KNOWN_CHINESE_SLUGS:
        return _KNOWN_CHINESE_SLUGS[name]
    ascii_slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    if ascii_slug:
        return ascii_slug
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"role-{digest}"


class RuleBasedParser:
    name = "rule-based"

    def parse(self, text: str) -> ParsedScriptDraft:
        characters: dict[str, Character] = {}
        lines: list[ScriptLine] = []
        warnings: list[str] = []
        for raw in text.splitlines():
            if not raw.strip():
                continue
            match = _LINE_RE.match(raw)
            if match is None:
                warnings.append(f"Skipped unrecognized line: {raw[:80]}")
                continue
            speaker = match.group("speaker").strip()
            note = (match.group("note") or "").strip()
            line_text = match.group("text").strip()
            leading_note = _LEADING_NOTE_RE.match(line_text)
            if leading_note:
                note = note or leading_note.group("note").strip()
                line_text = leading_note.group("text").strip()
            character_id = slugify_name(speaker)
            characters.setdefault(character_id, Character(id=character_id, name=speaker))
            lines.append(
                ScriptLine(
                    id=f"l{len(lines) + 1:04d}",
                    character_id=character_id,
                    note=note,
                    text=line_text,
                )
            )
        return ParsedScriptDraft(provider=self.name, characters=list(characters.values()), lines=lines, warnings=warnings)


class OpenAICompatibleProvider:
    def __init__(self, config: ParserProviderConfig) -> None:
        self.config = config
        self.name = config.name

    def parse(self, text: str) -> ParsedScriptDraft:
        if not self.config.enabled:
            raise ParserProviderUnavailable(f"provider {self.name} is disabled")
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ParserProviderUnavailable(f"provider {self.name} missing env {self.config.api_key_env}")
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Extract a dubbing script into JSON with keys characters and lines. "
                        "characters items require id and name. lines items require id, character_id, text, and optional note."
                    ),
                },
                {"role": "user", "content": text},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        decoded = json.loads(content)
        return _draft_from_provider_payload(self.name, decoded)


def _draft_from_provider_payload(provider: str, payload: dict[str, Any]) -> ParsedScriptDraft:
    characters = [Character.model_validate(item) for item in payload.get("characters", [])]
    lines = [ScriptLine.model_validate(item) for item in payload.get("lines", [])]
    warnings = [str(item) for item in payload.get("warnings", [])]
    return ParsedScriptDraft(provider=provider, characters=characters, lines=lines, warnings=warnings)


class MultiProviderParser:
    def __init__(self, providers: list[ParserProvider], fallback: ParserProvider | None = None) -> None:
        self.providers = providers
        self.fallback = fallback or RuleBasedParser()

    def parse(self, text: str) -> ParsedScriptDraft:
        warnings: list[str] = []
        for provider in self.providers:
            try:
                draft = provider.parse(text)
                draft.warnings = warnings + draft.warnings
                return draft
            except Exception as exc:
                warnings.append(f"{provider.name}: {exc}")
        draft = self.fallback.parse(text)
        draft.warnings = warnings + draft.warnings
        return draft

