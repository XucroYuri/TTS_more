from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.models import Character, ScriptLine
from app.role_library import slugify_role_name


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


class ParserQualityError(RuntimeError):
    pass


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    path = urlparse(normalized).path.rstrip("/").lower()
    if path.endswith("/chat/completions"):
        return normalized
    if path.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


_LINE_RE = re.compile(r"^\s*(?P<speaker>[^:：\n（(]+?)\s*(?:[（(](?P<note>[^）)]*)[）)])?\s*[:：]\s*(?P<text>.+?)\s*$")
_LEADING_NOTE_RE = re.compile(r"^\s*[（(](?P<note>[^）)]*)[）)]\s*(?P<text>.+?)\s*$")
_MARKDOWN_SPEAKER_RE = re.compile(r"^\s*(?:>\s*)?(?:#{1,6}\s*)?(?:\*\*)?(?P<speaker>[^:*：`#\n][^*：:`\n]{0,80}?)(?:\*\*)?\s*$")
_NOTE_ONLY_RE = re.compile(r"^\s*[（(](?P<note>[^）)]{1,120})[）)]\s*$")
_KNOWN_CHINESE_SLUGS = {
    "小美": "xiao-mei",
    "王强": "wang-qiang",
    "旁白": "pang-bai",
}
_NON_DIALOGUE_ROLE_RE = re.compile(
    r"^(?:"
    r"SC(?:ENE)?\s*\d*|"
    r"SFX|MUSIC|ON\s+SCREEN|"
    r"FADE\s+(?:IN|OUT)|CUT\s+TO|"
    r"INT\.?|EXT\.?|INT/EXT\.?|"
    r"ACTION|TRANSITION|TITLE|CARD"
    r")(?:\b|[:：.-]|$)",
    re.IGNORECASE,
)
_SYSTEM_PROMPT = """You extract line-level TTS dialogue from screenplays.

Return one JSON object only:
{
  "characters": [{"id": "stable-slug", "name": "display name"}],
  "lines": [{"id": "l001", "character_id": "stable-slug", "text": "spoken dialogue", "note": "optional parenthetical", "language": "zh|en"}],
  "warnings": ["optional short diagnostics"]
}

Rules:
- Output only lines that should be synthesized by TTS: character dialogue and narrator/voice-over lines.
- Exclude scene headings, action descriptions, SFX, MUSIC, ON SCREEN text, camera directions, transitions, timestamps, and metadata.
- Preserve original dialogue order and exact wording. Do not summarize, translate, invent, merge unrelated speakers, or add stage directions to text.
- Put parentheticals such as （压低声音）, (urgent whisper), or leading dialogue parentheticals in note without parentheses.
- Reuse the same character_id for repeated display names. Use lowercase kebab-case ids.
- Accept Chinese colon lines like 角色（括注）: 台词 and Markdown screenplay blocks like **CHARACTER** / (parenthetical) / dialogue.
"""
_REPAIR_PROMPT = """Repair the previous JSON so it satisfies the TTS screenplay extraction contract.

Return JSON only. Remove non-TTS rows, restore missing dialogue from the script, keep original order, use valid character references, and keep parentheticals in note.
"""
_CONTRACT_PROBE_SCRIPT = "**NARRATOR**\n(calm)\nHello from the contract test."


def parser_contract_probe_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Script:\n```text\n{_CONTRACT_PROBE_SCRIPT}\n```"},
    ]


def validate_parser_contract_response(provider: str, content: str) -> ParsedScriptDraft:
    return _draft_from_provider_payload(provider, _decode_json_content(content), source_text=_CONTRACT_PROBE_SCRIPT)


def slugify_name(name: str) -> str:
    if name in _KNOWN_CHINESE_SLUGS:
        return _KNOWN_CHINESE_SLUGS[name]
    ascii_slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    if ascii_slug:
        return ascii_slug
    return slugify_role_name(name)


def _clean_markup(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*>\s*", "", text)
    text = text.strip("`").strip()
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    text = re.sub(r"^\*\*(.*?)\*\*$", r"\1", text).strip()
    text = re.sub(r"^\*(.*?)\*$", r"\1", text).strip()
    text = re.sub(r"^_(.*?)_$", r"\1", text).strip()
    return re.sub(r"\s+", " ", text).strip()


def _clean_note(value: Any) -> str:
    text = _clean_markup(value)
    match = _NOTE_ONLY_RE.match(text)
    return (match.group("note") if match else text).strip()


def _clean_dialogue(value: Any) -> str:
    text = _clean_markup(value)
    leading_note = _LEADING_NOTE_RE.match(text)
    if leading_note:
        text = leading_note.group("text")
    return _clean_markup(text)


def _is_non_dialogue_role(value: str) -> bool:
    role = _clean_markup(value).strip()
    role = re.sub(r"\s*[:：].*$", "", role).strip()
    return bool(_NON_DIALOGUE_ROLE_RE.match(role))


def _is_non_tts_cue(raw: str) -> bool:
    text = _clean_markup(raw)
    if not text:
        return False
    if raw.lstrip().startswith("```") or raw.lstrip().startswith("`"):
        return True
    if _is_non_dialogue_role(text):
        return True
    colon_role = re.split(r"[:：]", text, maxsplit=1)[0].strip()
    return _is_non_dialogue_role(colon_role)


def _markdown_speaker(raw: str) -> str | None:
    stripped = raw.strip()
    if not stripped:
        return None
    has_markup = "**" in stripped or stripped.startswith("#")
    match = _MARKDOWN_SPEAKER_RE.match(stripped)
    if not match:
        return None
    speaker = _clean_markup(match.group("speaker"))
    if not speaker or _is_non_dialogue_role(speaker):
        return None
    if has_markup:
        return speaker
    if speaker.isascii() and speaker.upper() == speaker and len(speaker.split()) <= 5:
        return speaker
    return None


def _infer_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def _source_match_text(value: str) -> str:
    text = _clean_markup(value).casefold()
    text = re.sub(r"[\s*_`\"'“”‘’（）()\[\]{}<>:：,，.。!！?？;；-]+", "", text)
    return text


def _finalize_lines(
    provider: str,
    records: list[dict[str, str]],
    warnings: list[str] | None = None,
) -> ParsedScriptDraft:
    characters: dict[str, Character] = {}
    lines: list[ScriptLine] = []
    for record in records:
        name = _clean_markup(record.get("speaker", ""))
        text = _clean_dialogue(record.get("text", ""))
        if not name or not text or _is_non_dialogue_role(name):
            continue
        note = _clean_note(record.get("note", ""))
        leading_note = _LEADING_NOTE_RE.match(str(record.get("text", "")).strip())
        if leading_note and not note:
            note = _clean_note(leading_note.group("note"))
        character_id = slugify_name(name)
        characters.setdefault(character_id, Character(id=character_id, name=name))
        lines.append(
            ScriptLine(
                id=f"l{len(lines) + 1:03d}",
                character_id=character_id,
                note=note,
                text=text,
                language=record.get("language") or _infer_language(text),
            )
        )
    return ParsedScriptDraft(provider=provider, characters=list(characters.values()), lines=lines, warnings=warnings or [])


class RuleBasedParser:
    name = "rule-based"

    def parse(self, text: str) -> ParsedScriptDraft:
        records: list[dict[str, str]] = []
        warnings: list[str] = []
        raw_lines = text.splitlines()
        index = 0
        while index < len(raw_lines):
            raw = raw_lines[index]
            if not raw.strip():
                index += 1
                continue
            if _is_non_tts_cue(raw):
                warnings.append(f"Skipped non-TTS cue: {raw[:80]}")
                index += 1
                continue
            match = _LINE_RE.match(raw)
            if match is not None:
                speaker = match.group("speaker").strip()
                if _is_non_dialogue_role(speaker):
                    warnings.append(f"Skipped non-TTS cue: {raw[:80]}")
                    index += 1
                    continue
                note = (match.group("note") or "").strip()
                line_text = match.group("text").strip()
                leading_note = _LEADING_NOTE_RE.match(line_text)
                if leading_note:
                    note = note or leading_note.group("note").strip()
                    line_text = leading_note.group("text").strip()
                records.append({"speaker": speaker, "note": note, "text": line_text})
                index += 1
                continue
            speaker = _markdown_speaker(raw)
            if speaker is not None:
                note = ""
                dialogue: list[str] = []
                index += 1
                if index < len(raw_lines):
                    note_match = _NOTE_ONLY_RE.match(raw_lines[index].strip())
                    if note_match:
                        note = note_match.group("note").strip()
                        index += 1
                while index < len(raw_lines):
                    candidate = raw_lines[index]
                    if not candidate.strip():
                        index += 1
                        break
                    if _is_non_tts_cue(candidate) or _LINE_RE.match(candidate) or _markdown_speaker(candidate):
                        break
                    dialogue.append(_clean_dialogue(candidate))
                    index += 1
                if dialogue:
                    records.append({"speaker": speaker, "note": note, "text": " ".join(dialogue)})
                else:
                    warnings.append(f"Skipped speaker without dialogue: {raw[:80]}")
                continue
            warnings.append(f"Skipped unrecognized line: {raw[:80]}")
            index += 1
        return _finalize_lines(self.name, records, warnings)


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
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Script:\n```text\n{text}\n```"},
        ]
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = chat_completions_url(self.config.base_url)
        decoded: dict[str, Any]
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            decoded = self._post_json(client, url, headers, messages)
            try:
                return _draft_from_provider_payload(self.name, decoded, source_text=text)
            except ParserQualityError as first_error:
                repair_messages = [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"{_REPAIR_PROMPT}\n\n"
                            f"Quality errors:\n{first_error}\n\n"
                            f"Previous JSON:\n```json\n{json.dumps(decoded, ensure_ascii=False)}\n```\n\n"
                            f"Original script:\n```text\n{text}\n```"
                        ),
                    },
                ]
                repaired = self._post_json(client, url, headers, repair_messages)
                draft = _draft_from_provider_payload(self.name, repaired, source_text=text)
                draft.warnings = [f"LLM output repaired after quality failure: {first_error}", *draft.warnings]
                return draft

    def _post_json(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
        messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _decode_json_content(content)


def _decode_json_content(content: str) -> dict[str, Any]:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        stripped = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise
        decoded = json.loads(stripped[start : end + 1])
    if not isinstance(decoded, dict):
        raise ParserQualityError("parser response must be a JSON object")
    return decoded


def _draft_from_provider_payload(provider: str, payload: dict[str, Any], source_text: str | None = None) -> ParsedScriptDraft:
    warnings = [str(item) for item in payload.get("warnings", [])]
    duplicate_ids = _duplicate_line_ids(payload.get("lines", []))
    records: list[dict[str, str]] = []
    raw_characters = payload.get("characters", [])
    names_by_key: dict[str, str] = {}
    non_dialogue_names: list[str] = []
    if isinstance(raw_characters, list):
        for item in raw_characters:
            if not isinstance(item, dict):
                continue
            name = _clean_markup(item.get("name") or item.get("display_name") or item.get("id"))
            if not name:
                continue
            if _is_non_dialogue_role(name):
                non_dialogue_names.append(name)
            raw_id = _clean_markup(item.get("id") or slugify_name(name))
            names_by_key[raw_id] = name
            names_by_key[slugify_name(name)] = name
    raw_lines = payload.get("lines", [])
    if not isinstance(raw_lines, list):
        raise ParserQualityError("lines must be a list")
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        text = _clean_dialogue(item.get("text") or item.get("dialogue"))
        raw_character = _clean_markup(
            item.get("character_id")
            or item.get("speaker_id")
            or item.get("character")
            or item.get("speaker")
            or item.get("name")
        )
        name = names_by_key.get(raw_character) or names_by_key.get(slugify_name(raw_character)) or raw_character
        if _is_non_dialogue_role(name):
            non_dialogue_names.append(name)
        records.append(
            {
                "speaker": name,
                "note": _clean_note(item.get("note") or item.get("parenthetical")),
                "text": text,
                "language": str(item.get("language") or "") or _infer_language(text),
            }
        )
    draft = _finalize_lines(provider, records, warnings)
    reasons = _quality_reasons(draft, source_text, duplicate_ids, non_dialogue_names)
    if reasons:
        raise ParserQualityError("; ".join(reasons))
    return draft


def _duplicate_line_ids(raw_lines: Any) -> list[str]:
    if not isinstance(raw_lines, list):
        return []
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or "").strip()
        if not raw_id:
            continue
        if raw_id in seen and raw_id not in duplicates:
            duplicates.append(raw_id)
        seen.add(raw_id)
    return duplicates


def _quality_reasons(
    draft: ParsedScriptDraft,
    source_text: str | None,
    duplicate_ids: list[str],
    raw_non_dialogue_names: list[str],
) -> list[str]:
    reasons: list[str] = []
    if duplicate_ids:
        reasons.append(f"duplicate line ids: {', '.join(duplicate_ids)}")
    for name in raw_non_dialogue_names:
        reasons.append(f"non-dialogue role {name} is not allowed")
    if not draft.lines:
        reasons.append("no TTS dialogue lines were extracted")
    character_ids = {character.id for character in draft.characters}
    for character in draft.characters:
        if _is_non_dialogue_role(character.name) or _is_non_dialogue_role(character.id):
            reasons.append(f"non-dialogue role {character.name} is not allowed")
    seen_line_ids: set[str] = set()
    for line in draft.lines:
        if line.id in seen_line_ids:
            reasons.append(f"duplicate normalized line id: {line.id}")
        seen_line_ids.add(line.id)
        if not line.text.strip():
            reasons.append(f"{line.id} has empty text")
        if line.character_id not in character_ids:
            reasons.append(f"{line.id} references unknown character {line.character_id}")
    if source_text:
        expected = RuleBasedParser().parse(source_text)
        if expected.lines and len(draft.lines) < len(expected.lines):
            reasons.append(f"missing dialogue lines: expected at least {len(expected.lines)}, got {len(draft.lines)}")
        source = _source_match_text(source_text)
        cursor = 0
        for line in draft.lines:
            needle = _source_match_text(line.text)
            if not needle:
                continue
            position = source.find(needle, cursor)
            if position < 0:
                reasons.append(f"{line.id} text is not traceable in source order")
            else:
                cursor = position + len(needle)
    return list(dict.fromkeys(reasons))


class MultiProviderParser:
    def __init__(self, providers: list[ParserProvider], fallback: ParserProvider | None = None) -> None:
        self.providers = providers
        self.fallback = fallback or RuleBasedParser()

    def parse(self, text: str) -> ParsedScriptDraft:
        warnings: list[str] = []
        quality_errors: list[str] = []
        for provider in self.providers:
            try:
                draft = provider.parse(text)
                draft.warnings = warnings + draft.warnings
                return draft
            except ParserQualityError as exc:
                quality_errors.append(f"{provider.name}: {exc}")
            except Exception as exc:
                warnings.append(f"{provider.name}: {exc}")
        if quality_errors:
            raise ParserQualityError("; ".join(quality_errors))
        draft = self.fallback.parse(text)
        draft.warnings = warnings + draft.warnings
        return draft
