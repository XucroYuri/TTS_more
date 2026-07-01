import pytest

from app.parser import (
    MultiProviderParser,
    OpenAICompatibleProvider,
    ParserProviderConfig,
    RuleBasedParser,
)


def test_rule_based_parser_extracts_character_note_and_lines() -> None:
    text = "小美（压低声音）: 你终于来了。\n王强: 我一直都在。"

    draft = RuleBasedParser().parse(text)

    assert [c.name for c in draft.characters] == ["小美", "王强"]
    assert draft.lines[0].character_id == "xiao-mei"
    assert draft.lines[0].note == "压低声音"
    assert draft.lines[0].text == "你终于来了。"


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

