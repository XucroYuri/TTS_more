from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.net_guard import EgressError, scrub_error, validate_egress_url
from app.parser import ParserProviderConfig, anthropic_messages_url, chat_completions_url
from app.semantic_models import EmotionOrigin, NormalizedEmotion, UncertaintyCode
from app.semantic_source import py_index_to_utf16


PROMPT_VERSION = "semantic-grounding-v1"
CONTRACT_VERSION = "semantic-provider-v1"
ANTHROPIC_TOOL_NAME = "emit_semantic_analysis"

SEMANTIC_SYSTEM_PROMPT = """You produce source-grounded semantic annotations for dubbing scripts.

Infer whether text is speakable dialogue by meaning, including unquoted dialogue. Quotes,
parentheses, colons, and Markdown are weak evidence only. Return dialogue_excerpt with the
source bytes, spacing, and punctuation verbatim; exclude only wrapper quotation marks.
Never rewrite, normalize, complete, or fuzzy-correct source text.

Keep emotion evidence separate from emotion inference. Use only the fixed normalized emotion
enum, fixed emotion origins, and fixed uncertainty codes in the schema. Return no visible chain-of-thought.
When the speaker is not grounded, leave speaker_name empty and use the
appropriate uncertainty code rather than inventing a character.

Propose aliases only with contextual evidence under one canonical character. For example,
诸葛九九 and 九九 may be related when the source context supports that relationship;
never infer 王/老王 from substring alone. Anchors must be exact adjacent source text of at most 32
Unicode characters, and occurrence_index is the zero-based occurrence in this chunk.
Always echo all five chunk identity fields exactly as supplied: chunk_id, start_utf16,
end_utf16, overlap_before, and overlap_after. They are required even when both candidate arrays are empty.
"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisChunk(_StrictModel):
    chunk_id: str
    text: str
    start_utf16: int = Field(ge=0)
    end_utf16: int = Field(ge=0)
    overlap_before: int = Field(default=0, ge=0)
    overlap_after: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> "AnalysisChunk":
        if self.end_utf16 < self.start_utf16:
            raise ValueError("chunk end_utf16 must not precede start_utf16")
        if self.end_utf16 - self.start_utf16 != py_index_to_utf16(self.text, len(self.text)):
            raise ValueError("chunk UTF-16 range must match exact text")
        return self

    @classmethod
    def single(cls, text: str) -> "AnalysisChunk":
        return cls(
            chunk_id="chunk-0000",
            text=text,
            start_utf16=0,
            end_utf16=py_index_to_utf16(text, len(text)),
        )


class CharacterCandidatePayload(_StrictModel):
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    evidence_excerpts: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @field_validator("aliases", "evidence_excerpts")
    @classmethod
    def reject_empty_items(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("candidate string arrays cannot contain empty values")
        return list(dict.fromkeys(values))


class UtteranceCandidate(_StrictModel):
    dialogue_excerpt: str = Field(min_length=1)
    speaker_name: str | None = None
    speaker_evidence_excerpt: str | None = None
    emotion_evidence_excerpts: list[str] = Field(default_factory=list)
    source_excerpt: str | None = None
    normalized_emotion: NormalizedEmotion | None = None
    custom_emotion: str | None = Field(default=None, max_length=32)
    emotion_intensity: float | None = Field(default=None, ge=0, le=1)
    emotion_origin: EmotionOrigin = EmotionOrigin.NONE
    language: str = "zh"
    confidence: float = Field(ge=0, le=1)
    uncertainty_codes: list[UncertaintyCode] = Field(default_factory=list)
    anchor_before: str = Field(default="", max_length=32)
    anchor_after: str = Field(default="", max_length=32)
    occurrence_index: int = Field(default=0, ge=0)
    model_start_utf16: int | None = Field(default=None, ge=0)
    model_end_utf16: int | None = Field(default=None, ge=0)

    @field_validator("emotion_evidence_excerpts")
    @classmethod
    def validate_evidence(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("emotion evidence cannot contain an empty excerpt")
        return list(dict.fromkeys(values))

    @field_validator("uncertainty_codes")
    @classmethod
    def deduplicate_uncertainty(cls, values: list[UncertaintyCode]) -> list[UncertaintyCode]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_emotion_contract(self) -> "UtteranceCandidate":
        if self.normalized_emotion is None:
            if self.custom_emotion is not None or self.emotion_intensity is not None:
                raise ValueError("emotion fields require normalized_emotion")
            if self.emotion_origin is not EmotionOrigin.NONE:
                raise ValueError("emotion_origin must be none without normalized_emotion")
        else:
            if self.emotion_intensity is None:
                raise ValueError("emotion_intensity is required with normalized_emotion")
            if self.emotion_origin is EmotionOrigin.NONE:
                raise ValueError("emotion_origin must describe normalized_emotion")
            if self.normalized_emotion is NormalizedEmotion.OTHER:
                if not self.custom_emotion:
                    raise ValueError("other emotion requires custom_emotion")
            elif self.custom_emotion is not None:
                raise ValueError("custom_emotion is only allowed for other emotion")
        return self


class SemanticProviderResponse(_StrictModel):
    character_candidates: list[CharacterCandidatePayload] = Field(default_factory=list)
    utterance_candidates: list[UtteranceCandidate] = Field(default_factory=list)
    chunk_id: str
    start_utf16: int = Field(ge=0)
    end_utf16: int = Field(ge=0)
    overlap_before: int = Field(ge=0)
    overlap_after: int = Field(ge=0)


class SemanticProviderError(RuntimeError):
    default_code = "semantic_provider_error"
    default_http_status = 502
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        safe_message = scrub_error(message)
        super().__init__(safe_message)
        self.code = code or self.default_code
        self.http_status = http_status if http_status is not None else self.default_http_status
        self.retryable = retryable if retryable is not None else self.default_retryable


class SemanticProviderUnavailable(SemanticProviderError):
    default_code = "semantic_provider_unavailable"
    default_http_status = 502


class SemanticProviderUpstream(SemanticProviderError):
    default_code = "semantic_provider_upstream"
    default_http_status = 502
    default_retryable = True


class SemanticProviderTimeout(SemanticProviderError):
    default_code = "semantic_provider_timeout"
    default_http_status = 504
    default_retryable = True


class SemanticProviderContractError(SemanticProviderError):
    default_code = "semantic_contract_invalid"
    default_http_status = 422


class SemanticProvider(Protocol):
    name: str
    model: str

    def analyze_chunk(self, chunk: AnalysisChunk) -> SemanticProviderResponse:
        ...


def decode_semantic_payload(payload: object) -> SemanticProviderResponse:
    try:
        return SemanticProviderResponse.model_validate(payload)
    except ValidationError:
        raise SemanticProviderContractError("semantic provider response failed schema validation") from None


def _safe_private_free_message(message: str) -> str:
    # Provider exception text is intentionally not reflected. net_guard's scrubber is
    # still the final mandatory pass for every client-visible semantic message.
    return scrub_error(message)


def _semantic_schema() -> dict[str, Any]:
    return SemanticProviderResponse.model_json_schema()


class _BaseSemanticProvider:
    def __init__(self, config: ParserProviderConfig, client: Any | None = None) -> None:
        self.config = config
        self.name = config.name
        self.model = config.model
        self._client = client
        try:
            validate_egress_url(config.base_url, allow_loopback=True, resolve_dns=False)
        except EgressError:
            raise SemanticProviderUnavailable(
                _safe_private_free_message("semantic provider configured URL is unavailable")
            ) from None

    def analyze_chunk(self, chunk: AnalysisChunk) -> SemanticProviderResponse:
        if not self.config.enabled:
            raise SemanticProviderUnavailable(
                _safe_private_free_message(f"semantic provider {self.name} is disabled")
            )
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise SemanticProviderUnavailable(
                _safe_private_free_message(f"semantic provider {self.name} API key is unavailable")
            )
        endpoint = self._endpoint()
        try:
            validate_egress_url(endpoint, allow_loopback=True, resolve_dns=True)
        except EgressError:
            raise SemanticProviderUnavailable(
                _safe_private_free_message("semantic provider endpoint failed egress validation")
            ) from None

        if self._client is not None:
            return self._request(self._client, endpoint, api_key, chunk)
        try:
            with httpx.Client(
                timeout=self.config.timeout_seconds,
                trust_env=self.config.use_environment_proxy,
            ) as client:
                return self._request(client, endpoint, api_key, chunk)
        except SemanticProviderError:
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise SemanticProviderTimeout(
                _safe_private_free_message("semantic provider client initialization timed out")
            ) from None
        except (httpx.TransportError, OSError):
            raise SemanticProviderUpstream(
                _safe_private_free_message("semantic provider client initialization failed")
            ) from None

    def _request(self, client: Any, endpoint: str, api_key: str, chunk: AnalysisChunk) -> SemanticProviderResponse:
        try:
            response = client.post(
                endpoint,
                headers=self._headers(api_key),
                json=self._payload(chunk),
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
        except (httpx.TimeoutException, TimeoutError):
            raise SemanticProviderTimeout(_safe_private_free_message("semantic provider request timed out")) from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise SemanticProviderUpstream(
                _safe_private_free_message(f"semantic provider returned HTTP {status}")
            ) from None
        except (httpx.TransportError, OSError):
            raise SemanticProviderUpstream(
                _safe_private_free_message("semantic provider transport failed")
            ) from None

        try:
            envelope = response.json()
            raw_payload = self._extract_payload(envelope)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise SemanticProviderContractError(
                _safe_private_free_message("semantic provider returned malformed JSON")
            ) from None
        decoded = decode_semantic_payload(raw_payload)
        expected_identity = (
            chunk.chunk_id,
            chunk.start_utf16,
            chunk.end_utf16,
            chunk.overlap_before,
            chunk.overlap_after,
        )
        actual_identity = (
            decoded.chunk_id,
            decoded.start_utf16,
            decoded.end_utf16,
            decoded.overlap_before,
            decoded.overlap_after,
        )
        if actual_identity != expected_identity:
            raise SemanticProviderContractError(
                _safe_private_free_message("semantic provider response chunk identity mismatch")
            )
        return decoded

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _headers(self, api_key: str) -> dict[str, str]:
        raise NotImplementedError

    def _payload(self, chunk: AnalysisChunk) -> dict[str, Any]:
        raise NotImplementedError

    def _extract_payload(self, envelope: object) -> object:
        raise NotImplementedError


class OpenAISemanticProvider(_BaseSemanticProvider):
    def _endpoint(self) -> str:
        return chat_completions_url(self.config.base_url)

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _payload(self, chunk: AnalysisChunk) -> dict[str, Any]:
        schema_text = json.dumps(
            _semantic_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Analyze this exact chunk. Return exactly one JSON object matching this schema. "
                        "Do not add keys outside the schema. Use the exact enum strings and chunk identity values.\n"
                        f"JSON_SCHEMA={schema_text}\n"
                        f"CHUNK_METADATA=chunk_id={chunk.chunk_id}; start_utf16={chunk.start_utf16}; "
                        f"end_utf16={chunk.end_utf16}; overlap_before={chunk.overlap_before}; "
                        f"overlap_after={chunk.overlap_after}\n```text\n{chunk.text}\n```"
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }

    def _extract_payload(self, envelope: object) -> object:
        if not isinstance(envelope, dict):
            raise TypeError("invalid envelope")
        content = envelope["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("invalid content")
        return json.loads(content)


class AnthropicSemanticProvider(_BaseSemanticProvider):
    def _endpoint(self) -> str:
        return anthropic_messages_url(self.config.base_url)

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _payload(self, chunk: AnalysisChunk) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "max_tokens": 4096,
            "temperature": 0,
            "system": SEMANTIC_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Analyze this exact chunk and call the semantic tool.\n"
                        f"chunk_id={chunk.chunk_id}; start_utf16={chunk.start_utf16}; "
                        f"end_utf16={chunk.end_utf16}; overlap_before={chunk.overlap_before}; "
                        f"overlap_after={chunk.overlap_after}\n```text\n{chunk.text}\n```"
                    ),
                }
            ],
            "tools": [
                {
                    "name": ANTHROPIC_TOOL_NAME,
                    "description": "Emit source-grounded semantic analysis candidates.",
                    "input_schema": _semantic_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": ANTHROPIC_TOOL_NAME},
        }

    def _extract_payload(self, envelope: object) -> object:
        if not isinstance(envelope, dict) or not isinstance(envelope.get("content"), list):
            raise TypeError("invalid envelope")
        matches = [
            item.get("input")
            for item in envelope["content"]
            if isinstance(item, dict)
            and item.get("type") == "tool_use"
            and item.get("name") == ANTHROPIC_TOOL_NAME
        ]
        if len(matches) != 1:
            raise ValueError("expected one semantic tool result")
        return matches[0]


def build_semantic_provider(config: ParserProviderConfig, client: Any | None = None) -> SemanticProvider:
    if config.adapter == "anthropic":
        return AnthropicSemanticProvider(config, client)
    return OpenAISemanticProvider(config, client)
