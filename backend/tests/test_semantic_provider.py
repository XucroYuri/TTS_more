from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.net_guard import EgressError
from app.parser import ParserProviderConfig
from app.semantic_provider import (
    AnalysisChunk,
    CharacterCandidatePayload,
    SemanticProviderContractError,
    SemanticProviderTimeout,
    SemanticProviderUnavailable,
    SemanticProviderUpstream,
    UtteranceCandidate,
    build_semantic_provider,
    decode_semantic_payload,
)


def _config(**changes: object) -> ParserProviderConfig:
    values: dict[str, object] = {
        "name": "semantic-test",
        "base_url": "https://semantic.example/v1",
        "api_key_env": "SEMANTIC_TEST_KEY",
        "model": "semantic-model",
        "enabled": True,
        "timeout_seconds": 12,
        "adapter": "openai-compatible",
    }
    values.update(changes)
    return ParserProviderConfig(**values)


class FakeResponse:
    def __init__(
        self,
        payload: object | None = None,
        *,
        status_code: int = 200,
        json_error: Exception | None = None,
        body: str = "",
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error
        self.text = body

    def json(self) -> object:
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://semantic.example/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("upstream rejected secret-response-body", request=request, response=response)


class FakeClient:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or FakeResponse()
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.closed = True


def _openai_response(payload: object) -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})


def _empty_payload(chunk: AnalysisChunk | None = None) -> dict[str, object]:
    chunk = chunk or AnalysisChunk.single("文本")
    return {
        "character_candidates": [],
        "utterance_candidates": [],
        "chunk_id": chunk.chunk_id,
        "start_utf16": chunk.start_utf16,
        "end_utf16": chunk.end_utf16,
        "overlap_before": chunk.overlap_before,
        "overlap_after": chunk.overlap_after,
    }


def test_strict_payload_contract_accepts_empty_and_rejects_invalid_candidates() -> None:
    decoded = decode_semantic_payload(_empty_payload())
    assert decoded.character_candidates == []
    assert decoded.utterance_candidates == []

    with pytest.raises(SemanticProviderContractError):
        decode_semantic_payload({"character_candidates": [], "utterance_candidates": []})

    with pytest.raises(SemanticProviderContractError) as malformed:
        decode_semantic_payload({"character_candidates": [], "utterance_candidates": "not-an-array"})
    assert (malformed.value.code, malformed.value.http_status, malformed.value.retryable) == (
        "semantic_contract_invalid",
        422,
        False,
    )

    with pytest.raises(ValidationError):
        CharacterCandidatePayload(canonical_name="胶布", confidence=0.9, surprise="extra")
    with pytest.raises(ValidationError):
        UtteranceCandidate(dialogue_excerpt="台词", confidence=0.9, anchor_before="前" * 33)
    with pytest.raises(ValidationError):
        UtteranceCandidate(dialogue_excerpt="台词", confidence=0.9, occurrence_index=-1)
    with pytest.raises(ValidationError):
        UtteranceCandidate(
            dialogue_excerpt="台词",
            confidence=0.9,
            normalized_emotion="other",
            emotion_intensity=0.8,
            emotion_origin="inferred",
        )


def test_openai_adapter_uses_grounded_json_contract_and_runtime_egress_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations: list[tuple[str, bool, bool]] = []

    def validate(url: str, *, allow_loopback: bool, resolve_dns: bool) -> str:
        validations.append((url, allow_loopback, resolve_dns))
        return url

    monkeypatch.setattr("app.semantic_provider.validate_egress_url", validate)
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "sk-not-visible")
    chunk = AnalysisChunk.single("只有叙述😀\r\n第二段")
    client = FakeClient(_openai_response(_empty_payload(chunk)))
    provider = build_semantic_provider(_config(), client=client)

    result = provider.analyze_chunk(chunk)

    assert result.utterance_candidates == []
    assert validations == [
        ("https://semantic.example/v1", True, False),
        ("https://semantic.example/v1/chat/completions", True, True),
    ]
    request = client.calls[0]
    assert request["url"] == "https://semantic.example/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer sk-not-visible"  # type: ignore[index]
    payload = request["json"]
    assert payload["response_format"] == {"type": "json_object"}  # type: ignore[index]
    prompt = payload["messages"][0]["content"]  # type: ignore[index]
    for requirement in (
        "source-grounded semantic annotations",
        "unquoted dialogue",
        "weak evidence",
        "verbatim",
        "emotion evidence",
        "no visible chain-of-thought",
        "fixed uncertainty codes",
        "leave speaker_name empty",
        "诸葛九九",
        "never infer 王/老王 from substring alone",
        "all five chunk identity fields",
    ):
        assert requirement in prompt
    contract_prompt = payload["messages"][1]["content"]  # type: ignore[index]
    assert "Return exactly one JSON object matching this schema" in contract_prompt
    schema_marker = "JSON_SCHEMA="
    schema_text = contract_prompt.split(schema_marker, 1)[1].split("\nCHUNK_METADATA=", 1)[0]
    transmitted_schema = json.loads(schema_text)
    assert transmitted_schema["additionalProperties"] is False
    assert set(transmitted_schema["required"]) == {
        "chunk_id",
        "start_utf16",
        "end_utf16",
        "overlap_before",
        "overlap_after",
    }
    assert transmitted_schema["properties"]["character_candidates"]["type"] == "array"
    assert transmitted_schema["properties"]["utterance_candidates"]["type"] == "array"
    assert transmitted_schema["$defs"]["EmotionOrigin"]["enum"] == ["source_grounded", "inferred", "none"]
    assert transmitted_schema["$defs"]["UncertaintyCode"]["enum"] == [
        "speaker_unknown",
        "speaker_ambiguous",
        "dialogue_ambiguous",
        "emotion_inferred",
        "emotion_ambiguous",
        "source_anchor_ambiguous",
    ]
    assert client.closed is False


def test_anthropic_adapter_uses_only_dedicated_tool_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "anthropic-secret")
    chunk = AnalysisChunk.single("叙述")
    response = FakeResponse(
        {
            "content": [
                {"type": "text", "text": json.dumps(_empty_payload())},
                {"type": "tool_use", "name": "wrong_tool", "input": {"utterance_candidates": "bad"}},
                {"type": "tool_use", "name": "emit_semantic_analysis", "input": _empty_payload(chunk)},
            ]
        }
    )
    client = FakeClient(response)
    provider = build_semantic_provider(_config(adapter="anthropic"), client=client)

    result = provider.analyze_chunk(chunk)
    assert result.utterance_candidates == []
    request = client.calls[0]
    assert request["url"] == "https://semantic.example/v1/messages"
    payload = request["json"]
    assert payload["tool_choice"] == {"type": "tool", "name": "emit_semantic_analysis"}  # type: ignore[index]
    assert payload["tools"][0]["name"] == "emit_semantic_analysis"  # type: ignore[index]
    assert set(payload["tools"][0]["input_schema"]["required"]) >= {  # type: ignore[index]
        "chunk_id",
        "start_utf16",
        "end_utf16",
        "overlap_before",
        "overlap_after",
    }
    assert request["headers"]["x-api-key"] == "anthropic-secret"  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("chunk_id", "stale-chunk"),
        ("start_utf16", 1),
        ("end_utf16", 999),
        ("overlap_before", 1),
        ("overlap_after", 1),
    ],
)
def test_provider_rejects_each_mismatched_response_chunk_identity_as_safe_422(
    monkeypatch: pytest.MonkeyPatch, field: str, wrong_value: object
) -> None:
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "secret-key")
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)
    chunk = AnalysisChunk.single("文本")
    payload = _empty_payload(chunk)
    payload[field] = wrong_value
    provider = build_semantic_provider(_config(), client=FakeClient(_openai_response(payload)))

    with pytest.raises(SemanticProviderContractError) as error:
        provider.analyze_chunk(chunk)

    assert error.value.http_status == 422
    assert error.value.retryable is False
    assert "secret-key" not in str(error.value)


@pytest.mark.parametrize(
    ("config_changes", "expected_code"),
    [({"enabled": False}, "semantic_provider_unavailable"), ({}, "semantic_provider_unavailable")],
)
def test_disabled_or_missing_runtime_key_is_typed_and_never_posts(
    monkeypatch: pytest.MonkeyPatch,
    config_changes: dict[str, object],
    expected_code: str,
) -> None:
    monkeypatch.delenv("SEMANTIC_TEST_KEY", raising=False)
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)
    client = FakeClient(_openai_response(_empty_payload()))
    provider = build_semantic_provider(_config(**config_changes), client=client)

    with pytest.raises(SemanticProviderUnavailable) as error:
        provider.analyze_chunk(AnalysisChunk.single("文本"))

    assert error.value.code == expected_code
    assert error.value.http_status == 502
    assert error.value.retryable is False
    assert client.calls == []


@pytest.mark.parametrize(
    ("failure", "error_type", "code", "status"),
    [
        (httpx.ReadTimeout("Authorization: Bearer super-secret"), SemanticProviderTimeout, "semantic_provider_timeout", 504),
        (httpx.ConnectError("169.254.169.254 x-api-key: super-secret"), SemanticProviderUpstream, "semantic_provider_upstream", 502),
    ],
)
def test_timeout_and_transport_failures_have_safe_typed_metadata(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    error_type: type[Exception],
    code: str,
    status: int,
) -> None:
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "super-secret")
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)
    provider = build_semantic_provider(_config(), client=FakeClient(error=failure))

    with pytest.raises(error_type) as error:
        provider.analyze_chunk(AnalysisChunk.single("文本"))

    assert error.value.code == code  # type: ignore[attr-defined]
    assert error.value.http_status == status  # type: ignore[attr-defined]
    assert error.value.retryable is True  # type: ignore[attr-defined]
    assert "super-secret" not in str(error.value)
    assert "169.254.169.254" not in str(error.value)


def test_runtime_egress_rejection_is_scrubbed_and_prevents_post(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "super-secret")
    validations = 0

    def validate(url: str, **kwargs: object) -> str:
        nonlocal validations
        validations += 1
        if kwargs.get("resolve_dns"):
            raise EgressError("host resolves to private 169.254.169.254 Authorization: Bearer super-secret")
        return url

    monkeypatch.setattr("app.semantic_provider.validate_egress_url", validate)
    client = FakeClient(_openai_response(_empty_payload()))
    provider = build_semantic_provider(_config(), client=client)

    with pytest.raises(SemanticProviderUnavailable) as error:
        provider.analyze_chunk(AnalysisChunk.single("文本"))

    assert validations == 2
    assert client.calls == []
    assert "169.254.169.254" not in str(error.value)
    assert "super-secret" not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(json_error=ValueError("secret-response-body"), body="secret-response-body"),
        FakeResponse({"choices": []}, body="secret-response-body"),
        _openai_response({"character_candidates": [], "utterance_candidates": [{"dialogue_excerpt": 42}]}),
    ],
)
def test_malformed_json_or_schema_never_exposes_response_body(
    monkeypatch: pytest.MonkeyPatch, response: FakeResponse
) -> None:
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "secret-key")
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)
    provider = build_semantic_provider(_config(), client=FakeClient(response))

    with pytest.raises(SemanticProviderContractError) as error:
        provider.analyze_chunk(AnalysisChunk.single("文本"))

    assert error.value.http_status == 422
    assert "secret-response-body" not in str(error.value)
    assert "secret-key" not in str(error.value)


def test_http_status_never_exposes_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "secret-key")
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)
    provider = build_semantic_provider(
        _config(), client=FakeClient(FakeResponse(status_code=503, body="secret-response-body secret-key"))
    )

    with pytest.raises(SemanticProviderUpstream) as error:
        provider.analyze_chunk(AnalysisChunk.single("文本"))

    assert error.value.http_status == 502
    assert error.value.retryable is True
    assert "503" in str(error.value)
    assert "secret-response-body" not in str(error.value)
    assert "secret-key" not in str(error.value)


def test_production_client_context_exits_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "secret-key")
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)

    class ContextClient(FakeClient):
        def __enter__(self) -> "ContextClient":
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    client = ContextClient(_openai_response(_empty_payload()))
    monkeypatch.setattr("app.semantic_provider.httpx.Client", lambda **_kwargs: client)

    provider = build_semantic_provider(_config())
    assert provider.analyze_chunk(AnalysisChunk.single("文本")).utterance_candidates == []
    assert client.closed is True


def test_production_client_context_transport_failure_is_safe_502(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "secret-key")
    monkeypatch.setattr("app.semantic_provider.validate_egress_url", lambda url, **_kwargs: url)

    class FailingContext:
        def __enter__(self) -> None:
            raise httpx.ConnectError("169.254.169.254 Authorization: Bearer secret-key")

        def __exit__(self, *_args: object) -> None:
            raise AssertionError("an unentered context must not be exited")

    monkeypatch.setattr("app.semantic_provider.httpx.Client", lambda **_kwargs: FailingContext())
    provider = build_semantic_provider(_config())

    with pytest.raises(SemanticProviderUpstream) as error:
        provider.analyze_chunk(AnalysisChunk.single("文本"))

    assert error.value.http_status == 502
    assert error.value.retryable is True
    assert "169.254.169.254" not in str(error.value)
    assert "secret-key" not in str(error.value)
