from __future__ import annotations

import json
import os
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.models import Character, ScriptLine
from app.net_guard import EgressError, scrub_error, validate_egress_url
from app.role_library import slugify_role_name

_WRAPPING_DIALOGUE_QUOTES = "\"'“”‘’「」『』《》"


class LineSourceEvidence(BaseModel):
    source_text: str = ""
    source_excerpt: str = ""


class ParserDiagnosticIssue(BaseModel):
    type: str
    path: str


class ParserDiagnosticRecord(BaseModel):
    stage: str
    attempt_phase: str | None = None
    provider_index: int | None = Field(default=None, exclude=True, repr=False)
    issues: list[ParserDiagnosticIssue] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class ParsedScriptDraft(BaseModel):
    provider: str
    characters: list[Character] = Field(default_factory=list)
    lines: list[ScriptLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_evidence: dict[str, LineSourceEvidence] = Field(default_factory=dict, exclude=True)
    diagnostic_counts: dict[str, int] = Field(default_factory=dict, exclude=True, repr=False)
    normalization_issues: list[ParserDiagnosticIssue] = Field(default_factory=list, exclude=True, repr=False)


class ParserProbeResult(BaseModel):
    draft: ParsedScriptDraft
    content_preview: str


class ParserProvider(Protocol):
    name: str

    def parse(self, text: str) -> ParsedScriptDraft:
        ...

    def probe(self, api_key: str) -> ParserProbeResult:
        ...


ParserAdapterName = Literal["openai-compatible", "anthropic"]


class ParserProviderConfig(BaseModel):
    name: str
    base_url: str
    api_key_env: str
    model: str
    enabled: bool = True
    timeout_seconds: float = 45.0
    adapter: ParserAdapterName = "openai-compatible"


class ParserProviderUnavailable(RuntimeError):
    pass


class ParserQualityError(RuntimeError):
    """A string-compatible contract error with safe, machine-readable categories."""

    def __init__(
        self,
        message: str,
        *,
        reason_codes: list[str] | None = None,
        diagnostics: list[ParserDiagnosticRecord | dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_codes = reason_codes or _reason_codes_for_message(message)
        self.reasons = self.reason_codes
        self.diagnostics: list[ParserDiagnosticRecord] = []
        for diagnostic in diagnostics or []:
            try:
                normalized = (
                    diagnostic.model_copy(deep=True)
                    if isinstance(diagnostic, ParserDiagnosticRecord)
                    else ParserDiagnosticRecord.model_validate(diagnostic)
                )
            except (TypeError, ValueError):
                continue
            self.diagnostics.append(normalized)


def _diagnostic_record(
    stage: str,
    issue_type: str,
    path: str,
    counts: dict[str, int] | None = None,
) -> ParserDiagnosticRecord:
    return ParserDiagnosticRecord(
        stage=stage,
        issues=[ParserDiagnosticIssue(type=issue_type, path=path)],
        counts=dict(counts or {}),
    )


def _tag_diagnostic_phase(exc: ParserQualityError, phase: str) -> ParserQualityError:
    for diagnostic in exc.diagnostics:
        if diagnostic.attempt_phase is None:
            diagnostic.attempt_phase = phase
    return exc


def _normalization_diagnostic(
    issue_type: str,
    path: str,
    counts: dict[str, int],
    *,
    partial_scan: bool,
    related_issues: list[ParserDiagnosticIssue] | None = None,
) -> ParserDiagnosticRecord:
    diagnostic = _diagnostic_record("normalize", issue_type, path, counts)
    for issue in related_issues or []:
        if issue not in diagnostic.issues:
            diagnostic.issues.append(issue.model_copy(deep=True))
    if partial_scan:
        diagnostic.issues.append(
            ParserDiagnosticIssue(
                type="partial_normalization_scan",
                path="provider.payload.lines.*",
            )
        )
    return diagnostic


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    path = urlparse(normalized).path.rstrip("/").lower()
    if path.endswith("/chat/completions"):
        return normalized
    if path.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def anthropic_messages_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    path = urlparse(normalized).path.rstrip("/").lower()
    if path.endswith("/v1/messages"):
        return normalized
    if path.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


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
_ATTRIBUTED_QUOTE_PATTERNS = (
    re.compile(
        r'(?P<speaker>[\u4e00-\u9fffA-Za-z][^"\n“”]{0,40}?)'
        r'(?:说|表示|称|提到|补充|问道|回应|强调|解释|写道|答道|喊道|提醒|指出|告诉记者|告诉大家)'
        r'\s*[:：,，]?\s*[“"](?P<quote>[^“”"\n]{1,200})[”"]'
    ),
    re.compile(
        r'[“"](?P<quote>[^“”"\n]{1,200})[”"]\s*'
        r'(?P<speaker>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z .\'’·-]{0,40}?)'
        r'(?:说|表示|称|提到|补充|问道|回应|强调|解释|写道|答道|喊道|提醒|指出|告诉记者|告诉大家)'
    ),
    re.compile(
        r'(?P<speaker>[A-Z][A-Za-z][A-Za-z .\'-]{0,40})'
        r'\s+(?:said|says|told|asked|replied|added|wrote|tweeted|explained|warned|noted)'
        r'\s*[,:\-]?\s*"(?P<quote>[^"\n]{1,200})"'
    ),
    re.compile(
        r'"(?P<quote>[^"\n]{1,200})"\s*[, -]?\s*'
        r'(?P<speaker>[A-Z][A-Za-z][A-Za-z .\'-]{0,40})'
        r'\s+(?:said|says|told|asked|replied|added|wrote|tweeted|explained|warned|noted)\b'
    ),
)
_SYSTEM_PROMPT = """You extract line-level TTS dialogue from scripts, prose, interviews, news articles, Markdown, and mixed-format drafts.

Return one JSON object only:
{
  "characters": [{"id": "stable-slug", "name": "display name"}],
  "lines": [
    {
      "id": "l001",
      "character_id": "stable-slug",
      "text": "spoken dialogue copied exactly from source",
      "note": "optional emotion or parenthetical without brackets",
      "language": "zh|en",
      "source_text": "exact source substring used for text",
      "source_excerpt": "smallest source excerpt containing speaker, note, and source_text"
    }
  ],
  "warnings": ["optional short diagnostics"]
}

Rules:
- Before returning JSON, perform an internal audit: identify every candidate spoken line, classify speaker and emotion/note, reject non-TTS cues, verify character references, verify source_text, verify source_excerpt, verify original ordering, and check for missing dialogue.
- Do not reveal the audit, chain-of-thought, or reasoning notes. Return only the final JSON object.
- Output only lines that should be synthesized by TTS: character dialogue, narrator/voice-over, host, announcer, and quoted speaker lines.
- Exclude scene headings, action descriptions, SFX, MUSIC, ON SCREEN text, camera directions, transitions, timestamps, metadata, captions that are not spoken, and article body text that is not quoted or attributed speech.
- Preserve original dialogue order and exact wording. Do not rewrite, summarize, translate, normalize punctuation, invent, merge unrelated speakers, or add stage directions to text.
- The text field must be copied from source_text exactly except for removing wrapping quote marks and moving a leading parenthetical into note.
- Put parentheticals such as （压低声音）, (urgent whisper), or leading dialogue parentheticals in note without parentheses.
- Reuse the same character_id for repeated display names. Use lowercase kebab-case ids.
- Use each character's canonical full name as its display name. A short name may appear in the source evidence, but do not create a separate character for it.
- Treat a speaker as established only by an explicit speaker anchor: an independent Markdown speaker block, a clean NAME: dialogue label, or a clear NAME said/说/问道 attribution. Do not infer a speaker from narrative setup, action description, or nearby prose.
- Accept Chinese colon lines like 角色（括注）: 台词, Markdown screenplay blocks like **CHARACTER** / (parenthetical) / dialogue, prose quotes with speaker attribution, interview speaker labels, and news quotes attributed to named speakers.
"""
_REPAIR_PROMPT = """Repair the previous JSON so it satisfies the TTS dialogue extraction contract.

Return JSON only. Remove non-TTS rows, restore missing dialogue from the script, keep original order, use valid character references, keep parentheticals in note, include source_text and source_excerpt for every line, and keep text copied exactly from source_text. Use canonical full name display names and include a clear speaker anchor in source_excerpt when one exists. Do not treat narrative/action prose as a speaker label. Do not rewrite, translate, summarize, or normalize punctuation.
"""
_CONTRACT_PROBE_SCRIPT = "**NARRATOR**\n(calm)\nHello from the contract test."


def parser_contract_probe_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Script:\n```text\n{_CONTRACT_PROBE_SCRIPT}\n```"},
    ]


def validate_parser_contract_response(provider: str, content: str) -> ParsedScriptDraft:
    draft = _draft_from_provider_payload(provider, _decode_json_content(content))
    return ScriptParseVerifier().verify(_CONTRACT_PROBE_SCRIPT, draft)


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


def _clean_source_excerpt(value: Any) -> str:
    return str(value or "").strip()


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


def _strip_wrapping_dialogue_quotes(value: str) -> str:
    text = value.strip()
    quote_pairs = {
        "\"": "\"",
        "'": "'",
        "“": "”",
        "‘": "’",
        "「": "」",
        "『": "』",
        "《": "》",
    }
    for left, right in quote_pairs.items():
        if text.startswith(left) and text.endswith(right) and len(text) >= 2:
            return text[1:-1].strip()
    return text


def _source_fidelity_text(value: Any) -> str:
    text = _clean_markup(value)
    leading_note = _LEADING_NOTE_RE.match(text)
    if leading_note:
        text = leading_note.group("text")
    text = _strip_wrapping_dialogue_quotes(text)
    return re.sub(r"\s+", " ", text).strip()


def _source_fidelity_source(value: str) -> str:
    text = _clean_markup(value)
    return re.sub(r"\s+", " ", text).strip()


_AMBIGUOUS_SPEAKER_REFERENTS = {
    "他",
    "她",
    "它",
    "ta",
    "他们",
    "她们",
    "它们",
    "he",
    "him",
    "his",
    "she",
    "her",
    "hers",
    "they",
    "them",
    "their",
    "theirs",
}
_SPEAKER_ACTION_MARKERS = (
    "沉默",
    "皱",
    "抬",
    "看",
    "走",
    "转",
    "停",
    "朝",
    "望",
    "听",
    "笑",
    "哭",
    "点头",
    "说",
    "表示",
    "补充",
    "问道",
    "回应",
)


def _normalize_attributed_speaker(raw: str) -> str | None:
    speaker = _clean_markup(raw)
    if not speaker:
        return None
    if speaker.casefold() in _AMBIGUOUS_SPEAKER_REFERENTS:
        return None
    if re.search(r"[\u4e00-\u9fff]", speaker):
        segments = [segment.strip() for segment in re.split(r"[\s,，、。；;：:]+", speaker) if segment.strip()]
        if segments:
            speaker = segments[-1]
            if speaker.casefold() in _AMBIGUOUS_SPEAKER_REFERENTS:
                return None
    return speaker


def _source_excerpt_markdown_speaker(source_excerpt: str, source_text: str) -> str | None:
    needle = _source_fidelity_text(source_text)
    if not needle:
        return None

    raw_lines = source_excerpt.splitlines()
    for index, raw in enumerate(raw_lines):
        speaker = _markdown_speaker(raw)
        if speaker is None:
            continue
        dialogue: list[str] = []
        line_index = index + 1
        if line_index < len(raw_lines) and _NOTE_ONLY_RE.match(raw_lines[line_index].strip()):
            line_index += 1
        while line_index < len(raw_lines):
            candidate = raw_lines[line_index]
            if not candidate.strip():
                break
            if _is_non_tts_cue(candidate) or _LINE_RE.match(candidate) or _markdown_speaker(candidate):
                break
            dialogue.append(_clean_dialogue(candidate))
            line_index += 1
        if dialogue and _source_fidelity_text(" ".join(dialogue)).find(needle) >= 0:
            return speaker
    return None


def _quoted_dialogue_candidates(source_text: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    for pattern in _ATTRIBUTED_QUOTE_PATTERNS:
        for match in pattern.finditer(source_text):
            quote = _source_fidelity_text(match.group("quote"))
            if quote:
                matches.append((match.start("quote"), quote))
    ordered: list[str] = []
    seen: set[tuple[int, str]] = set()
    for item in sorted(matches):
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item[1])
    return ordered


def _is_explicit_speaker_anchor(speaker: str, character_names: set[str]) -> bool:
    cleaned = _clean_markup(speaker)
    if any(cleaned.casefold() == name.casefold() for name in character_names):
        return True
    if any(marker in cleaned for marker in _SPEAKER_ACTION_MARKERS):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cleaned)) or bool(
        re.fullmatch(r"[A-Z][A-Za-z]*(?:[ .'-][A-Z][A-Za-z]*){0,4}", cleaned)
    )


def _is_clean_colon_speaker_label(speaker: str, character_names: set[str]) -> bool:
    cleaned = _clean_markup(speaker)
    if not cleaned or re.search(r"[,，。；;\n]", cleaned) or any(marker in cleaned for marker in _SPEAKER_ACTION_MARKERS):
        return False
    return all(
        cleaned == name or _is_controlled_short_name_alias(name, cleaned)
        for name in character_names
        if name in cleaned
    )


def _is_controlled_short_name_alias(short_name: str, full_name: str) -> bool:
    return bool(
        re.fullmatch(r"[\u4e00-\u9fff]{2,}", short_name)
        and re.fullmatch(r"[\u4e00-\u9fff]{3,}", full_name)
        and len(short_name) < len(full_name)
        and (full_name.startswith(short_name) or full_name.endswith(short_name))
    )


def _is_contextual_prose_speaker(raw_speaker: str, normalized_speaker: str, character_names: set[str]) -> bool:
    if re.search(r"[,，]\s*$", raw_speaker):
        return True
    return any(
        name in raw_speaker
        and normalized_speaker.casefold() != name.casefold()
        and not _is_controlled_short_name_alias(name, normalized_speaker)
        for name in character_names
    )


def _source_excerpt_speaker(source_excerpt: str, source_text: str, character_names: set[str]) -> str | None:
    excerpt = _source_fidelity_source(source_excerpt)
    needle = _source_fidelity_text(source_text)
    if not excerpt or not needle:
        return None

    markdown_speaker = _source_excerpt_markdown_speaker(source_excerpt, source_text)
    if markdown_speaker:
        return markdown_speaker

    for pattern in _ATTRIBUTED_QUOTE_PATTERNS:
        for match in pattern.finditer(excerpt):
            quote = _source_fidelity_text(match.group("quote"))
            if quote == needle:
                raw_speaker = match.group("speaker")
                speaker = _normalize_attributed_speaker(raw_speaker)
                if (
                    speaker
                    and not _is_contextual_prose_speaker(raw_speaker, speaker, character_names)
                    and _is_explicit_speaker_anchor(speaker, character_names)
                ):
                    return speaker

    line_match = _LINE_RE.match(excerpt)
    if line_match:
        speaker = _clean_markup(line_match.group("speaker"))
        line_text = _source_fidelity_text(line_match.group("text"))
        if (
            speaker
            and not _is_non_dialogue_role(speaker)
            and _is_clean_colon_speaker_label(speaker, character_names)
            and line_text.find(needle) >= 0
        ):
            return speaker
    return None


def _speaker_matches_character(
    expected_speaker: str,
    character_id: str,
    character_name: str,
    character_names: set[str],
) -> bool:
    expected = _clean_markup(expected_speaker)
    actual_name = _clean_markup(character_name or character_id)
    if not expected:
        return True
    if expected.casefold() == actual_name.casefold() or expected.casefold() == character_id.casefold():
        return True
    expected_slug = slugify_name(expected)
    if expected_slug == character_id or expected_slug == slugify_name(actual_name):
        return True
    if not _is_controlled_short_name_alias(expected, actual_name):
        return False
    alias_matches = [
        name
        for name in character_names
        if _is_controlled_short_name_alias(expected, name)
    ]
    return alias_matches == [actual_name]


def _ordered_coverage_count(expected: list[str], actual: list[str]) -> int:
    actual_index = 0
    matched = 0
    for candidate in expected:
        while actual_index < len(actual) and actual[actual_index] != candidate:
            actual_index += 1
        if actual_index >= len(actual):
            continue
        matched += 1
        actual_index += 1
    return matched


def _finalize_lines(
    provider: str,
    records: list[dict[str, str]],
    warnings: list[str] | None = None,
) -> ParsedScriptDraft:
    characters: dict[str, Character] = {}
    lines: list[ScriptLine] = []
    source_evidence: dict[str, LineSourceEvidence] = {}
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
        line_id = f"l{len(lines) + 1:03d}"
        characters.setdefault(character_id, Character(id=character_id, name=name))
        lines.append(
            ScriptLine(
                id=line_id,
                character_id=character_id,
                note=note,
                text=text,
                language=record.get("language") or _infer_language(text),
            )
        )
        evidence = LineSourceEvidence(
            source_text=_clean_markup(record.get("source_text", "")),
            source_excerpt=_clean_source_excerpt(record.get("source_excerpt", "")),
        )
        if evidence.source_text or evidence.source_excerpt:
            source_evidence[line_id] = evidence
    return ParsedScriptDraft(
        provider=provider,
        characters=list(characters.values()),
        lines=lines,
        warnings=warnings or [],
        source_evidence=source_evidence,
    )


class ScriptParseVerifier:
    def verify(self, source_text: str, draft: ParsedScriptDraft) -> ParsedScriptDraft:
        normalization_diagnostics = _draft_normalization_diagnostics(draft)
        alias_reasons = _canonicalize_short_name_characters(draft, source_text)
        if alias_reasons:
            raise ParserQualityError(
                "; ".join(alias_reasons),
                reason_codes=_reason_codes_for_reasons(alias_reasons),
                diagnostics=[
                    *normalization_diagnostics,
                    _diagnostic_record(
                        "verify",
                        "ambiguous_short_name_alias",
                        "verifier.characters",
                        draft.diagnostic_counts,
                    )
                ],
            )
        reasons = _quality_reasons(draft, source_text)
        if reasons:
            raise ParserQualityError(
                "; ".join(reasons),
                reason_codes=_reason_codes_for_reasons(reasons),
                diagnostics=[*normalization_diagnostics, _quality_diagnostic(draft, source_text, reasons)],
            )
        return draft


def _reference_dialogue_candidates(text: str) -> list[tuple[str, str]]:
    dialogue_lines: list[tuple[str, str]] = []
    raw_lines = text.splitlines()
    index = 0
    while index < len(raw_lines):
        raw = raw_lines[index]
        if not raw.strip():
            index += 1
            continue
        if _is_non_tts_cue(raw):
            index += 1
            continue
        match = _LINE_RE.match(raw)
        if match is not None:
            speaker = match.group("speaker").strip()
            if _is_non_dialogue_role(speaker):
                index += 1
                continue
            line_text = match.group("text").strip()
            leading_note = _LEADING_NOTE_RE.match(line_text)
            if leading_note:
                line_text = leading_note.group("text").strip()
            cleaned = _clean_dialogue(line_text)
            if cleaned:
                dialogue_lines.append(("colon", cleaned))
            index += 1
            continue
        speaker = _markdown_speaker(raw)
        if speaker is not None:
            dialogue: list[str] = []
            index += 1
            if index < len(raw_lines) and _NOTE_ONLY_RE.match(raw_lines[index].strip()):
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
                dialogue_lines.append(("markdown", " ".join(dialogue)))
            continue
        index += 1
    return dialogue_lines


def _reference_dialogue_texts(text: str) -> list[str]:
    return [candidate for _detector, candidate in _reference_dialogue_candidates(text)]


def _canonicalize_short_name_characters(draft: ParsedScriptDraft, source_text: str) -> list[str]:
    """Merge only unique Chinese prefix/suffix aliases into canonical character names."""
    names_by_id = {character.id: character.name for character in draft.characters}
    targets_by_alias_id: dict[str, str] = {}
    reasons: list[str] = []

    for character in draft.characters:
        candidates = [
            other.name
            for other in draft.characters
            if other.id != character.id and _is_controlled_short_name_alias(character.name, other.name)
        ]
        if len(candidates) == 1:
            targets_by_alias_id[character.id] = candidates[0]
        elif len(candidates) > 1:
            reasons.append(f"ambiguous short-name character alias {character.name}")

    if source_text:
        source_candidates_by_alias_id: dict[str, set[str]] = {}
        for line in draft.lines:
            evidence = draft.source_evidence.get(line.id)
            if not evidence:
                continue
            anchor = _source_excerpt_speaker(
                evidence.source_excerpt,
                evidence.source_text or line.text,
                set(names_by_id.values()),
            )
            actual_name = names_by_id.get(line.character_id)
            if anchor and actual_name and _is_controlled_short_name_alias(actual_name, anchor):
                source_candidates_by_alias_id.setdefault(line.character_id, set()).add(anchor)
        for alias_id, candidates in source_candidates_by_alias_id.items():
            if len(candidates) == 1:
                targets_by_alias_id[alias_id] = next(iter(candidates))
            else:
                reasons.append(f"ambiguous short-name character alias {names_by_id[alias_id]}")

    if reasons:
        return list(dict.fromkeys(reasons))
    if not targets_by_alias_id:
        return []

    existing_ids_by_name = {character.name: character.id for character in draft.characters}
    target_ids = {
        alias_id: existing_ids_by_name.get(full_name, slugify_name(full_name))
        for alias_id, full_name in targets_by_alias_id.items()
    }
    for line in draft.lines:
        if line.character_id in target_ids:
            line.character_id = target_ids[line.character_id]

    normalized_characters: list[Character] = []
    seen_ids: set[str] = set()
    for character in draft.characters:
        if character.id in targets_by_alias_id:
            full_name = targets_by_alias_id[character.id]
            target_id = target_ids[character.id]
            if target_id not in seen_ids:
                normalized_characters.append(Character(id=target_id, name=full_name))
                seen_ids.add(target_id)
            continue
        if character.id not in seen_ids:
            normalized_characters.append(character)
            seen_ids.add(character.id)
    draft.characters = normalized_characters
    return []


class OpenAICompatibleProvider:
    def __init__(self, config: ParserProviderConfig, verifier: ScriptParseVerifier | None = None) -> None:
        self.config = config
        self.name = config.name
        self.verifier = verifier or ScriptParseVerifier()
        if config.base_url.strip():
            try:
                validate_egress_url(config.base_url, allow_loopback=True, resolve_dns=False)
            except EgressError as exc:
                raise EgressError(f"provider {self.name}: {exc}") from exc

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
            try:
                decoded = self._post_json(client, url, headers, messages)
            except ParserQualityError as initial_error:
                _tag_diagnostic_phase(initial_error, "initial")
                raise
            try:
                draft = _draft_from_provider_payload(self.name, decoded)
                return self.verifier.verify(text, draft)
            except ParserQualityError as first_error:
                _tag_diagnostic_phase(first_error, "initial")
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
                try:
                    repaired = self._post_json(client, url, headers, repair_messages)
                    draft = _draft_from_provider_payload(self.name, repaired)
                    self.verifier.verify(text, draft)
                except ParserQualityError as repair_error:
                    _tag_diagnostic_phase(repair_error, "repair")
                    repair_error.diagnostics = [*first_error.diagnostics, *repair_error.diagnostics]
                    raise
                draft.warnings = [f"LLM output repaired after quality failure: {first_error}", *draft.warnings]
                return draft

    def probe(self, api_key: str) -> ParserProbeResult:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = chat_completions_url(self.config.base_url)
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            decoded = self._post_json(client, url, headers, parser_contract_probe_messages())
        draft = self.verifier.verify(_CONTRACT_PROBE_SCRIPT, _draft_from_provider_payload(self.name, decoded))
        return ParserProbeResult(draft=draft, content_preview=json.dumps(decoded, ensure_ascii=False)[:120])

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
        try:
            response_payload = response.json()
            content = response_payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ParserQualityError(
                "parser response envelope was invalid",
                diagnostics=[_diagnostic_record("decode", "invalid_type", "provider.payload")],
            ) from exc
        if type(content) is not str:
            raise ParserQualityError(
                "parser response envelope was invalid",
                diagnostics=[_diagnostic_record("decode", "invalid_type", "provider.payload")],
            )
        return _decode_json_content(content)


_ANTHROPIC_TOOL = {
    "name": "emit_tts_parse",
    "description": "Emit the final TTS dialogue extraction JSON.",
    "input_schema": {
        "type": "object",
        "properties": {
            "characters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["id", "name"],
                },
            },
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "character_id": {"type": "string"},
                        "text": {"type": "string"},
                        "note": {"type": "string"},
                        "language": {"type": "string"},
                        "source_text": {"type": "string"},
                        "source_excerpt": {"type": "string"},
                    },
                    "required": ["id", "character_id", "text", "source_text", "source_excerpt"],
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["characters", "lines"],
    },
}


class AnthropicProvider:
    def __init__(self, config: ParserProviderConfig, verifier: ScriptParseVerifier | None = None) -> None:
        self.config = config
        self.name = config.name
        self.verifier = verifier or ScriptParseVerifier()
        if config.base_url.strip():
            try:
                validate_egress_url(config.base_url, allow_loopback=True, resolve_dns=False)
            except EgressError as exc:
                raise EgressError(f"provider {self.name}: {exc}") from exc

    def parse(self, text: str) -> ParsedScriptDraft:
        if not self.config.enabled:
            raise ParserProviderUnavailable(f"provider {self.name} is disabled")
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ParserProviderUnavailable(f"provider {self.name} missing env {self.config.api_key_env}")
        return self._parse_with_key(text, api_key)

    def probe(self, api_key: str) -> ParserProbeResult:
        decoded = self._post_json(
            api_key,
            [{"role": "user", "content": f"Script:\n```text\n{_CONTRACT_PROBE_SCRIPT}\n```"}],
        )
        draft = self.verifier.verify(_CONTRACT_PROBE_SCRIPT, _draft_from_provider_payload(self.name, decoded))
        return ParserProbeResult(draft=draft, content_preview=json.dumps(decoded, ensure_ascii=False)[:120])

    def _parse_with_key(self, text: str, api_key: str) -> ParsedScriptDraft:
        try:
            decoded = self._post_json(
                api_key,
                [{"role": "user", "content": f"Script:\n```text\n{text}\n```"}],
            )
        except ParserQualityError as initial_error:
            _tag_diagnostic_phase(initial_error, "initial")
            raise
        try:
            draft = _draft_from_provider_payload(self.name, decoded)
            return self.verifier.verify(text, draft)
        except ParserQualityError as first_error:
            _tag_diagnostic_phase(first_error, "initial")
            repair_messages = [
                {
                    "role": "user",
                    "content": (
                        f"{_REPAIR_PROMPT}\n\n"
                        f"Quality errors:\n{first_error}\n\n"
                        f"Previous JSON:\n```json\n{json.dumps(decoded, ensure_ascii=False)}\n```\n\n"
                        f"Original script:\n```text\n{text}\n```"
                    ),
                }
            ]
            try:
                repaired = self._post_json(
                    api_key,
                    repair_messages,
                )
                draft = _draft_from_provider_payload(self.name, repaired)
                self.verifier.verify(text, draft)
            except ParserQualityError as repair_error:
                _tag_diagnostic_phase(repair_error, "repair")
                repair_error.diagnostics = [*first_error.diagnostics, *repair_error.diagnostics]
                raise
            draft.warnings = [f"LLM output repaired after quality failure: {first_error}", *draft.warnings]
            return draft

    def _post_json(self, api_key: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "max_tokens": 4096,
            "temperature": 0,
            "system": _SYSTEM_PROMPT,
            "messages": messages,
            "tools": [_ANTHROPIC_TOOL],
            "tool_choice": {"type": "tool", "name": "emit_tts_parse"},
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(anthropic_messages_url(self.config.base_url), headers=headers, json=payload)
            response.raise_for_status()
            try:
                response_payload = response.json()
            except (ValueError, TypeError) as exc:
                raise ParserQualityError(
                    "anthropic response envelope was invalid",
                    diagnostics=[_diagnostic_record("decode", "invalid_type", "provider.payload")],
                ) from exc
            if type(response_payload) is not dict:
                raise ParserQualityError(
                    "anthropic response envelope was invalid",
                    diagnostics=[_diagnostic_record("decode", "invalid_type", "provider.payload")],
                )
            return _decode_anthropic_tool_input(response_payload)


def build_parser_provider(config: ParserProviderConfig, verifier: ScriptParseVerifier | None = None) -> ParserProvider:
    if config.adapter == "anthropic":
        return AnthropicProvider(config, verifier)
    return OpenAICompatibleProvider(config, verifier)


def _decode_json_content(content: str) -> dict[str, Any]:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as initial_error:
        stripped = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise ParserQualityError(
                "parser response was not valid JSON",
                diagnostics=[_diagnostic_record("decode", "invalid_type", "provider.payload")],
            ) from initial_error
        try:
            decoded = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as extracted_error:
            raise ParserQualityError(
                "parser response was not valid JSON",
                diagnostics=[_diagnostic_record("decode", "invalid_type", "provider.payload")],
            ) from extracted_error
    if not isinstance(decoded, dict):
        raise ParserQualityError(
            "parser response must be a JSON object",
            diagnostics=[_diagnostic_record("decode", "invalid_type", "provider.payload")],
        )
    return decoded


def _decode_anthropic_tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    content = payload.get("content", [])
    if type(content) is not list:
        raise ParserQualityError(
            "anthropic response content must be a list",
            diagnostics=[
                _diagnostic_record(
                    "decode",
                    "invalid_type",
                    "provider.payload.content",
                )
            ],
        )
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "emit_tts_parse":
            tool_input = item.get("input")
            if isinstance(tool_input, dict):
                return tool_input
    raise ParserQualityError(
        "anthropic response did not include emit_tts_parse tool input",
        diagnostics=[_diagnostic_record("decode", "missing_field", "provider.payload")],
    )


def _draft_from_provider_payload(provider: str, payload: dict[str, Any]) -> ParsedScriptDraft:
    warnings = [str(item) for item in payload.get("warnings", [])]
    records: list[dict[str, str]] = []
    normalization_issues: list[ParserDiagnosticIssue] = []
    raw_lines = payload.get("lines", [])
    diagnostic_counts = {
        "raw_line_item_count": len(raw_lines) if isinstance(raw_lines, list) else 0,
        "processed_line_item_count": 0,
        "normalized_line_count": 0,
        "dropped_non_object_count": 0,
        "dropped_missing_speaker_count": 0,
        "dropped_empty_text_count": 0,
        "dropped_non_dialogue_role_count": 0,
    }
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
    declared_names = set(names_by_key.values())
    for short_name in declared_names:
        candidates = [name for name in declared_names if _is_controlled_short_name_alias(short_name, name)]
        if len(candidates) > 1:
            raise ParserQualityError(
                f"ambiguous short-name character alias {short_name}",
                reason_codes=["ambiguous_short_name_alias"],
                diagnostics=[
                    _normalization_diagnostic(
                        "ambiguous_short_name_alias",
                        "provider.payload.characters.*",
                        diagnostic_counts,
                        partial_scan=True,
                    )
                ],
            )
        if len(candidates) == 1:
            canonical_name = candidates[0]
            for key, name in list(names_by_key.items()):
                if name == short_name:
                    names_by_key[key] = canonical_name
    if not isinstance(raw_lines, list):
        raise ParserQualityError(
            "lines must be a list",
            diagnostics=[
                _normalization_diagnostic(
                    "invalid_type",
                    "provider.payload.lines",
                    diagnostic_counts,
                    partial_scan=True,
                )
            ],
        )
    for index, item in enumerate(raw_lines, start=1):
        diagnostic_counts["processed_line_item_count"] = index
        if not isinstance(item, dict):
            diagnostic_counts["dropped_non_object_count"] += 1
            _append_diagnostic_issue(
                normalization_issues,
                "normalized_line_dropped",
                "provider.payload.lines.*",
            )
            continue
        text = _clean_dialogue(item.get("text") or item.get("dialogue"))
        if not text:
            diagnostic_counts["dropped_empty_text_count"] += 1
            _append_diagnostic_issue(
                normalization_issues,
                "normalized_line_dropped",
                "provider.payload.lines.*.text",
            )
        source_text = _clean_markup(item.get("source_text", ""))
        source_excerpt = _clean_source_excerpt(item.get("source_excerpt", ""))
        if not source_text:
            raise ParserQualityError(
                f"line {index} missing source_text",
                diagnostics=[
                    _normalization_diagnostic(
                        "missing_field",
                        "provider.payload.lines.*.source_text",
                        diagnostic_counts,
                        partial_scan=True,
                        related_issues=normalization_issues,
                    )
                ],
            )
        if not source_excerpt:
            raise ParserQualityError(
                f"line {index} missing source_excerpt",
                diagnostics=[
                    _normalization_diagnostic(
                        "missing_field",
                        "provider.payload.lines.*.source_excerpt",
                        diagnostic_counts,
                        partial_scan=True,
                        related_issues=normalization_issues,
                    )
                ],
            )
        if source_text and _source_fidelity_text(source_text) != _source_fidelity_text(text):
            raise ParserQualityError(
                "source_text does not match text",
                diagnostics=[
                    _normalization_diagnostic(
                        "source_text_mismatch",
                        "provider.payload.lines.*.source_text",
                        diagnostic_counts,
                        partial_scan=True,
                        related_issues=normalization_issues,
                    )
                ],
            )
        raw_character = _clean_markup(
            item.get("character_id")
            or item.get("speaker_id")
            or item.get("character")
            or item.get("speaker")
            or item.get("name")
        )
        name = names_by_key.get(raw_character) or names_by_key.get(slugify_name(raw_character)) or raw_character
        if not name:
            diagnostic_counts["dropped_missing_speaker_count"] += 1
            _append_diagnostic_issue(
                normalization_issues,
                "normalized_line_dropped",
                "provider.payload.lines.*.speaker",
            )
        is_non_dialogue_role = _is_non_dialogue_role(name)
        if is_non_dialogue_role:
            non_dialogue_names.append(name)
            diagnostic_counts["dropped_non_dialogue_role_count"] += 1
            _append_diagnostic_issue(
                normalization_issues,
                "normalized_line_dropped",
                "provider.payload.lines.*.speaker",
            )
        records.append(
            {
                "speaker": name,
                "note": _clean_note(item.get("note") or item.get("parenthetical")),
                "text": text,
                "language": str(item.get("language") or "") or _infer_language(text),
                "source_text": source_text,
                "source_excerpt": source_excerpt,
            }
        )
        if name and text and not is_non_dialogue_role:
            diagnostic_counts["normalized_line_count"] += 1
    draft = _finalize_lines(provider, records, warnings)
    diagnostic_counts["normalized_line_count"] = len(draft.lines)
    draft.diagnostic_counts = diagnostic_counts
    draft.normalization_issues = normalization_issues
    alias_reasons = _canonicalize_short_name_characters(draft, "")
    if alias_reasons:
        raise ParserQualityError(
            "; ".join(alias_reasons),
            reason_codes=_reason_codes_for_reasons(alias_reasons),
            diagnostics=[
                _diagnostic_record(
                    "normalize",
                    "ambiguous_short_name_alias",
                    "provider.payload.characters.*",
                    diagnostic_counts,
                )
            ],
        )
    reasons = _payload_reasons(non_dialogue_names)
    if reasons:
        raise ParserQualityError(
            "; ".join(reasons),
            diagnostics=[
                _diagnostic_record(
                    "normalize",
                    "non_dialogue_role",
                    "provider.payload.characters.*",
                    diagnostic_counts,
                )
            ],
        )
    return draft


def _payload_reasons(raw_non_dialogue_names: list[str]) -> list[str]:
    reasons: list[str] = []
    for name in raw_non_dialogue_names:
        reasons.append(f"non-dialogue role {name} is not allowed")
    return reasons


def _reason_codes_for_reasons(reasons: list[str]) -> list[str]:
    codes: list[str] = []
    for reason in reasons:
        if "missing dialogue lines" in reason:
            codes.append("missing_dialogue_coverage")
        elif "missing quoted dialogue coverage" in reason:
            codes.append("missing_quoted_dialogue_coverage")
        elif "text is not an exact source match" in reason:
            codes.append("text_not_in_source_order")
        elif "source_text does not match text" in reason:
            codes.append("source_text_mismatch")
        elif "source_excerpt is not traceable" in reason:
            codes.append("source_excerpt_not_traceable")
        elif "source_excerpt does not contain source_text" in reason:
            codes.append("source_excerpt_missing_source_text")
        elif "source_excerpt speaker" in reason:
            codes.append("speaker_anchor_mismatch")
        elif "ambiguous short-name character alias" in reason:
            codes.append("ambiguous_short_name_alias")
        elif "unknown character" in reason:
            codes.append("unknown_character")
        elif "non-dialogue role" in reason:
            codes.append("non_dialogue_role")
        else:
            codes.append("quality_contract_violation")
    return list(dict.fromkeys(codes))


def _reason_codes_for_message(message: str) -> list[str]:
    return _reason_codes_for_reasons([reason for reason in message.split("; ") if reason])


def _quality_reasons(draft: ParsedScriptDraft, source_text: str) -> list[str]:
    reasons: list[str] = []
    if not draft.lines:
        reasons.append("no TTS dialogue lines were extracted")
    character_ids = {character.id for character in draft.characters}
    character_names_by_id = {character.id: character.name for character in draft.characters}
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
    expected = _reference_dialogue_texts(source_text)
    if expected and len(draft.lines) < len(expected):
        reasons.append(f"missing dialogue lines: expected at least {len(expected)}, got {len(draft.lines)}")
    quoted_candidates = _quoted_dialogue_candidates(source_text)
    quoted_line_texts = [
        _source_fidelity_text((draft.source_evidence.get(line.id) or LineSourceEvidence()).source_text or line.text)
        for line in draft.lines
    ]
    quoted_matches = _ordered_coverage_count(quoted_candidates, quoted_line_texts)
    if quoted_candidates and quoted_matches < len(quoted_candidates):
        reasons.append(f"missing quoted dialogue coverage: expected at least {len(quoted_candidates)}, got {quoted_matches}")
    source_cursor = 0
    source_for_cursor = _source_fidelity_source(source_text)
    for line in draft.lines:
        needle = _source_fidelity_text(line.text)
        if not needle:
            continue
        position = source_for_cursor.find(needle, source_cursor)
        if position < 0:
            reasons.append(f"{line.id} text is not an exact source match in source order")
        else:
            source_cursor = position + len(needle)
        evidence = draft.source_evidence.get(line.id)
        if evidence and evidence.source_text:
            if _source_fidelity_text(evidence.source_text) != needle:
                reasons.append(f"{line.id} source_text does not match text")
        if evidence and evidence.source_excerpt:
            excerpt = _source_fidelity_source(evidence.source_excerpt)
            if excerpt and source_for_cursor.find(excerpt) < 0:
                reasons.append(f"{line.id} source_excerpt is not traceable in source")
            if excerpt and needle and excerpt.find(needle) < 0:
                reasons.append(f"{line.id} source_excerpt does not contain source_text")
            expected_speaker = _source_excerpt_speaker(
                evidence.source_excerpt,
                evidence.source_text or line.text,
                set(character_names_by_id.values()),
            )
            actual_character = character_names_by_id.get(line.character_id, line.character_id)
            if expected_speaker and not _speaker_matches_character(
                expected_speaker,
                line.character_id,
                actual_character,
                set(character_names_by_id.values()),
            ):
                reasons.append(
                    f"{line.id} source_excerpt speaker {expected_speaker} does not match character {actual_character}"
                )
    return list(dict.fromkeys(reasons))


_QUALITY_DIAGNOSTIC_PATHS = {
    "missing_dialogue_coverage": "verifier.dialogue_coverage",
    "missing_quoted_dialogue_coverage": "verifier.quoted_dialogue_coverage",
    "text_not_in_source_order": "verifier.lines.*.text",
    "source_text_mismatch": "verifier.lines.*.source_text",
    "source_excerpt_not_traceable": "verifier.lines.*.source_excerpt",
    "source_excerpt_missing_source_text": "verifier.lines.*.source_excerpt",
    "speaker_anchor_mismatch": "verifier.lines.*.speaker",
    "ambiguous_short_name_alias": "verifier.characters",
    "unknown_character": "verifier.lines.*.character_id",
    "non_dialogue_role": "verifier.characters.*",
    "quality_contract_violation": "verifier.contract",
}


def _quality_diagnostic(
    draft: ParsedScriptDraft,
    source_text: str,
    reasons: list[str],
) -> ParserDiagnosticRecord:
    candidates = _reference_dialogue_candidates(source_text)
    counts = dict(draft.diagnostic_counts)
    counts.update(
        {
            "normalized_line_count": len(draft.lines),
            "reference_candidate_count": len(candidates),
            "reference_colon_candidate_count": sum(1 for detector, _text in candidates if detector == "colon"),
            "reference_markdown_candidate_count": sum(
                1 for detector, _text in candidates if detector == "markdown"
            ),
        }
    )
    issues = [
        ParserDiagnosticIssue(
            type=code,
            path=_QUALITY_DIAGNOSTIC_PATHS.get(code, "verifier.contract"),
        )
        for code in _reason_codes_for_reasons(reasons)
    ]
    return ParserDiagnosticRecord(stage="verify", issues=issues, counts=counts)


def _append_diagnostic_issue(
    issues: list[ParserDiagnosticIssue],
    issue_type: str,
    path: str,
) -> None:
    issue = ParserDiagnosticIssue(type=issue_type, path=path)
    if issue not in issues:
        issues.append(issue)


def _draft_normalization_diagnostics(draft: ParsedScriptDraft) -> list[ParserDiagnosticRecord]:
    if not draft.normalization_issues:
        return []
    return [
        ParserDiagnosticRecord(
            stage="normalize",
            issues=[issue.model_copy(deep=True) for issue in draft.normalization_issues],
            counts=dict(draft.diagnostic_counts),
        )
    ]


class MultiProviderParser:
    def __init__(self, providers: list[ParserProvider]) -> None:
        self.providers = providers

    def parse(self, text: str) -> ParsedScriptDraft:
        quality_errors: list[tuple[int, str, ParserQualityError]] = []
        availability_errors: list[str] = []
        attempted_provider = False
        for provider_index, provider in enumerate(self.providers):
            if not _provider_enabled(provider):
                continue
            attempted_provider = True
            try:
                draft = provider.parse(text)
                return draft
            except ParserQualityError as exc:
                for diagnostic in exc.diagnostics:
                    if diagnostic.provider_index is None:
                        diagnostic.provider_index = provider_index
                quality_errors.append((provider_index, provider.name, exc))
            except ParserProviderUnavailable as exc:
                availability_errors.append(f"{provider.name}: {exc}")
            except Exception as exc:
                availability_errors.append(f"{provider.name}: {scrub_error(exc, getattr(provider.config, 'base_url', None))}")
        if quality_errors:
            reason_codes = list(
                dict.fromkeys(
                    code
                    for _provider_index, _provider_name, error in quality_errors
                    for code in error.reason_codes
                )
            )
            diagnostics = [
                diagnostic
                for _provider_index, _provider_name, error in quality_errors
                for diagnostic in error.diagnostics
            ]
            raise ParserQualityError(
                "; ".join(
                    f"{provider_name}: {error}"
                    for _provider_index, provider_name, error in quality_errors
                ),
                reason_codes=reason_codes,
                diagnostics=diagnostics,
            )
        if attempted_provider and availability_errors:
            raise ParserProviderUnavailable("; ".join(availability_errors))
        raise ParserProviderUnavailable("no enabled parser providers")


def _provider_enabled(provider: ParserProvider) -> bool:
    config = getattr(provider, "config", None)
    return bool(getattr(config, "enabled", True))
