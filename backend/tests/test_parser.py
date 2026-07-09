import json
from pathlib import Path

import pytest

from app.parser import (
    MultiProviderParser,
    OpenAICompatibleProvider,
    ParsedScriptDraft,
    ParserProviderConfig,
    ParserProviderUnavailable,
    ParserQualityError,
    ScriptParseVerifier,
    chat_completions_url,
    parser_contract_probe_messages,
)
from app.parser_config import ParserProviderRecord, ParserProvidersUpdate, load_parser_providers, save_parser_providers
from app.models import Character, ScriptLine


def make_draft(
    *,
    characters: list[Character] | None = None,
    lines: list[ScriptLine] | None = None,
    provider: str = "llm-test",
) -> ParsedScriptDraft:
    return ParsedScriptDraft(
        provider=provider,
        characters=characters or [Character(id="narrator", name="NARRATOR")],
        lines=lines or [ScriptLine(id="l001", character_id="narrator", text="Hello.", language="en")],
    )


def test_script_parse_verifier_accepts_llm_draft_with_traceable_dialogue() -> None:
    source = "**NARRATOR**\n(calm)\nHello."
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", note="calm", text="Hello.", language="en")])

    ScriptParseVerifier().verify(source, draft)


def test_script_parse_verifier_rejects_non_dialogue_roles() -> None:
    draft = make_draft(
        characters=[Character(id="sfx", name="SFX")],
        lines=[ScriptLine(id="l001", character_id="sfx", text="Rain hits metal.", language="en")],
    )

    with pytest.raises(ParserQualityError, match="non-dialogue role SFX"):
        ScriptParseVerifier().verify("> **SFX**: Rain hits metal.", draft)


def test_script_parse_verifier_rejects_empty_line_text() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", text="", language="en")])

    with pytest.raises(ParserQualityError, match="l001 has empty text"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.", draft)


def test_script_parse_verifier_rejects_duplicate_line_ids() -> None:
    draft = make_draft(
        lines=[
            ScriptLine(id="l001", character_id="narrator", text="Hello.", language="en"),
            ScriptLine(id="l001", character_id="narrator", text="Again.", language="en"),
        ]
    )

    with pytest.raises(ParserQualityError, match="duplicate normalized line id: l001"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.\nAgain.", draft)


def test_script_parse_verifier_rejects_unknown_character_reference() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="ghost", text="Hello.", language="en")])

    with pytest.raises(ParserQualityError, match="l001 references unknown character ghost"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.", draft)


def test_script_parse_verifier_rejects_untraceable_text() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", text="Invented line.", language="en")])

    with pytest.raises(ParserQualityError, match="l001 text is not traceable in source order"):
        ScriptParseVerifier().verify("**NARRATOR**\nHello.", draft)


def test_script_parse_verifier_rejects_out_of_order_dialogue() -> None:
    draft = make_draft(
        characters=[Character(id="alice", name="ALICE"), Character(id="bob", name="BOB")],
        lines=[
            ScriptLine(id="l001", character_id="bob", text="Second line.", language="en"),
            ScriptLine(id="l002", character_id="alice", text="First line.", language="en"),
        ],
    )

    with pytest.raises(ParserQualityError, match="l002 text is not traceable in source order"):
        ScriptParseVerifier().verify("ALICE: First line.\nBOB: Second line.", draft)


def test_script_parse_verifier_rejects_missing_recognizable_dialogue() -> None:
    draft = make_draft(lines=[ScriptLine(id="l001", character_id="narrator", text="First line.", language="en")])

    with pytest.raises(ParserQualityError, match="missing dialogue lines: expected at least 2, got 1"):
        ScriptParseVerifier().verify("NARRATOR: First line.\nNARRATOR: Second line.", draft)


def test_parser_contract_prompt_requires_internal_quality_audit() -> None:
    messages = parser_contract_probe_messages()

    system_prompt = messages[0]["content"].lower()

    assert "internal audit" in system_prompt
    assert "traceability" in system_prompt
    assert "do not reveal" in system_prompt


def test_multi_provider_parser_requires_enabled_llm_provider_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(
            name="missing",
            base_url="https://example.invalid/v1",
            api_key_env="MISSING_KEY",
            model="fake",
        )
    )

    with pytest.raises(ParserProviderUnavailable, match="missing env MISSING_KEY"):
        MultiProviderParser([provider]).parse("旁白: 天亮了。")


def test_multi_provider_parser_requires_at_least_one_enabled_llm_provider() -> None:
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(
            name="disabled",
            base_url="https://example.invalid/v1",
            api_key_env="DISABLED_KEY",
            model="fake",
            enabled=False,
        )
    )

    with pytest.raises(ParserProviderUnavailable, match="no enabled parser providers"):
        MultiProviderParser([provider]).parse("旁白: 天亮了。")


def test_multi_provider_parser_does_not_fallback_after_quality_failure() -> None:
    class BadQualityProvider:
        name = "bad-quality"

        def parse(self, _text: str):
            raise ParserQualityError("non-dialogue role SFX is not allowed")

    with pytest.raises(ParserQualityError, match="SFX"):
        MultiProviderParser([BadQualityProvider()]).parse("旁白: 天亮了。")


def test_openai_provider_accepts_valid_llm_output_without_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "characters": [{"id": "narrator", "name": "NARRATOR"}],
                                    "lines": [{"id": "l001", "character_id": "narrator", "note": "calm", "text": "Hello."}],
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(name="openai-test", base_url="https://example.invalid", api_key_env="OPENAI_TEST_KEY", model="fake")
    )

    draft = provider.parse("**NARRATOR**\n(calm)\nHello.")

    assert len(calls) == 1
    assert draft.provider == "openai-test"
    assert draft.lines[0].text == "Hello."


def test_openai_provider_repairs_invalid_quality_output_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")
    calls: list[dict[str, object]] = []
    responses = [
        {
            "characters": [{"id": "sfx", "name": "SFX"}],
            "lines": [{"id": "scene-1", "character_id": "sfx", "text": "Rain hits metal."}],
        },
        {
            "characters": [{"id": "narrator", "name": "NARRATOR"}],
            "lines": [{"id": "bad-id", "character_id": "narrator", "note": "(calm)", "text": "**Hello.**"}],
        },
    ]

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": json.dumps(self.payload)}}]}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(responses[len(calls) - 1])

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(name="openai-test", base_url="https://example.invalid", api_key_env="OPENAI_TEST_KEY", model="fake")
    )

    draft = provider.parse("**NARRATOR**\n(calm)\nHello.")

    assert len(calls) == 2
    assert "repair" in str(calls[1]["json"]).lower()
    assert draft.provider == "openai-test"
    assert [character.model_dump(include={"id", "name"}) for character in draft.characters] == [
        {"id": "narrator", "name": "NARRATOR"}
    ]
    assert draft.lines[0].model_dump(include={"id", "character_id", "note", "text", "language"}) == {
        "id": "l001",
        "character_id": "narrator",
        "note": "calm",
        "text": "Hello.",
        "language": "en",
    }
    assert any("repaired" in warning for warning in draft.warnings)


def test_openai_provider_raises_quality_error_when_repair_is_still_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "sk-test")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "characters": [{"id": "sfx", "name": "SFX"}],
                                    "lines": [{"id": "l001", "character_id": "sfx", "text": "Rain hits metal."}],
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(name="openai-test", base_url="https://example.invalid", api_key_env="OPENAI_TEST_KEY", model="fake")
    )

    with pytest.raises(ParserQualityError, match="SFX"):
        provider.parse("> **SFX**: Rain hits metal.")


def test_chat_completions_url_normalizes_kwjm_root_and_legacy_v1_base() -> None:
    assert chat_completions_url("https://kwjm.com") == "https://kwjm.com/v1/chat/completions"
    assert chat_completions_url("https://example.invalid/v1") == "https://example.invalid/v1/chat/completions"
    assert chat_completions_url("https://example.invalid/v1/chat/completions") == "https://example.invalid/v1/chat/completions"


def test_parser_provider_config_defaults_to_openai_compatible_adapter() -> None:
    config = ParserProviderConfig(
        name="legacy-compatible",
        base_url="https://example.invalid/v1",
        api_key_env="LEGACY_API_KEY",
        model="legacy-model",
    )

    assert config.adapter == "openai-compatible"


def test_parser_provider_record_persists_adapter(tmp_path: Path) -> None:
    config_path = tmp_path / "parser_providers.json"
    env_path = tmp_path / ".env.local"

    save_parser_providers(
        config_path,
        env_path,
        ParserProvidersUpdate(
            providers=[
                {
                    "name": "Anthropic",
                    "base_url": "https://api.anthropic.com",
                    "api_key_env": "ANTHROPIC_API_KEY",
                    "model": "claude-fable-5",
                    "enabled": True,
                    "timeout_seconds": 60,
                    "priority": 20,
                    "adapter": "anthropic",
                }
            ]
        ),
    )

    loaded = load_parser_providers(config_path)

    assert loaded[0].adapter == "anthropic"
    assert '"adapter": "anthropic"' in config_path.read_text(encoding="utf-8")


def test_default_parser_providers_include_agentic_presets_and_exclude_removed_providers() -> None:
    from app.parser_config import default_parser_providers

    providers = default_parser_providers()
    names = [provider.name for provider in providers]
    by_name = {provider.name: provider for provider in providers}

    assert "百度千帆" not in names
    assert "Mistral" not in names
    assert by_name["OpenAI"].adapter == "openai-compatible"
    assert by_name["OpenAI"].model == "gpt-5.5"
    assert by_name["Anthropic"].adapter == "anthropic"
    assert by_name["Anthropic"].base_url == "https://api.anthropic.com"
    assert by_name["Gemini"].base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert by_name["OpenRouter"].api_key_env == "OPENROUTER_API_KEY"
    assert by_name["Aihubmix"].base_url == "https://aihubmix.com/v1"
    assert names[-1] == "开物基模"
    assert all(provider.enabled is False for provider in providers)
