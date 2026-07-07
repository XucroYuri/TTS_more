import json

import pytest

from app.parser import (
    MultiProviderParser,
    OpenAICompatibleProvider,
    ParserProviderConfig,
    ParserQualityError,
    RuleBasedParser,
    chat_completions_url,
)


def test_rule_based_parser_extracts_character_note_and_lines() -> None:
    text = "小美（压低声音）: 你终于来了。\n王强: 我一直都在。"

    draft = RuleBasedParser().parse(text)

    assert [c.name for c in draft.characters] == ["小美", "王强"]
    assert draft.lines[0].character_id == "xiao-mei"
    assert draft.lines[0].id == "l001"
    assert draft.lines[0].note == "压低声音"
    assert draft.lines[0].text == "你终于来了。"


def test_rule_based_parser_extracts_hollywood_screenplay_fixture_without_non_dialogue_roles() -> None:
    source = """### SC1: INT. TEST BAY - NIGHT

**NARRATOR**
(quiet)
The console blinked once.

**LEAD**
(urgent)
Keep the signal low.

> **SFX**: Static crackles.

`ON SCREEN: READY`

### SC2: EXT. ROOFTOP - DAWN

**TECH**
(dry)
That antenna is listening.

**CONTROL VOICE**
(calm)
Unauthorized access detected.
"""

    draft = RuleBasedParser().parse(source)

    assert [line.text for line in draft.lines] == [
        "The console blinked once.",
        "Keep the signal low.",
        "That antenna is listening.",
        "Unauthorized access detected.",
    ]
    assert [character.name for character in draft.characters] == [
        "NARRATOR",
        "LEAD",
        "TECH",
        "CONTROL VOICE",
    ]
    assert [line.note for line in draft.lines[:3]] == ["quiet", "urgent", "dry"]
    assert not {"SC1", "SFX", "MUSIC", "ON SCREEN"} & {character.name for character in draft.characters}


def test_rule_based_parser_extracts_chinese_colon_and_markdown_role_blocks() -> None:
    text = "\n".join(
        [
            "**旁白**",
            "（低声）",
            "天亮了。",
            "",
            "小美：快走！",
            "",
            "**王强**",
            "(喘息)",
            "我一直都在。",
        ]
    )

    draft = RuleBasedParser().parse(text)

    assert [(line.character_id, line.note, line.text) for line in draft.lines] == [
        ("pang-bai", "低声", "天亮了。"),
        ("xiao-mei", "", "快走！"),
        ("wang-qiang", "喘息", "我一直都在。"),
    ]


def test_multi_provider_parser_falls_back_when_provider_has_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    provider = OpenAICompatibleProvider(
        ParserProviderConfig(
            name="missing",
            base_url="https://example.invalid/v1",
            api_key_env="MISSING_KEY",
            model="fake",
        )
    )

    draft = MultiProviderParser([provider], fallback=RuleBasedParser()).parse("旁白: 天亮了。")

    assert draft.lines[0].character_id == "pang-bai"
    assert draft.provider == "rule-based"


def test_multi_provider_parser_does_not_fallback_after_quality_failure() -> None:
    class BadQualityProvider:
        name = "bad-quality"

        def parse(self, _text: str):
            raise ParserQualityError("non-dialogue role SFX is not allowed")

    with pytest.raises(ParserQualityError, match="SFX"):
        MultiProviderParser([BadQualityProvider()], fallback=RuleBasedParser()).parse("旁白: 天亮了。")


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
