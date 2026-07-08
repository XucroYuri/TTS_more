from app.models import Character, EngineName, ScriptLine, ScriptProject, VoiceBinding, VoiceProfile
from app.role_library import common_logs_presets, match_project_characters, resolve_project_characters, slugify_role_name


def test_project_character_matching_uses_aliases_nicknames_and_match_names() -> None:
    library = [
        Character(
            id="hero",
            name="主角",
            aliases=["英雄队长"],
            nicknames=["队长"],
            match_names=["主角"],
        ),
        Character(
            id="mentor",
            name="导师",
            aliases=["顾问"],
            nicknames=["顾问"],
            match_names=["demo-mentor-logs"],
        ),
    ]
    project = ScriptProject(
        title="demo",
        lines=[
            ScriptLine(id="l001", character_id="队长", text="我们必须出发。"),
            ScriptLine(id="l002", character_id="顾问", text="保持阵型。"),
        ],
    )

    mappings = match_project_characters(project, library)

    assert [item.library_character_id for item in mappings] == ["hero", "mentor"]
    assert [item.name for item in mappings] == ["主角", "导师"]


def test_unmatched_project_character_resolves_without_default_tts_profile() -> None:
    project = ScriptProject(title="demo", lines=[ScriptLine(id="l001", character_id="临时路人", text="啊？")])

    resolved = resolve_project_characters(project, [])

    assert resolved[0].id == "临时路人"
    assert resolved[0].profiles == []
    assert resolved[0].default_engine is None
    assert resolved[0].default_profile is None


def test_common_logs_presets_include_user_supplied_roles() -> None:
    presets = common_logs_presets()

    by_name = {item["name"]: item for item in presets}
    assert by_name["主角"]["logs_name"] == "demo-hero-logs"
    assert "队长" in by_name["主角"]["nicknames"]
    assert by_name["导师"]["logs_name"] == "demo-mentor-logs"
    assert "顾问" in by_name["导师"]["nicknames"]


def test_slugify_role_name_has_stable_ids_for_common_chinese_roles() -> None:
    assert slugify_role_name("主角") == "zhu-jue"
    assert slugify_role_name("导师") == "dao-shi"
    assert slugify_role_name("反派") == "fan-pai"


def test_slugify_role_name_preserves_unmapped_chinese_characters() -> None:
    # PINYIN_FALLBACK 字典未覆盖的字不能被丢弃，
    # 否则不同中文名（例如“小明”和“小红”）会被压缩成同一个 slug。
    assert slugify_role_name("小明") != slugify_role_name("小红")
    assert slugify_role_name("小明") != slugify_role_name("小美")
    assert slugify_role_name("小明").startswith("xiao-")
    assert slugify_role_name("小红").startswith("xiao-")
    # 字典里 “美” = “mei”，不应被 Unicode fallback 替换。
    assert slugify_role_name("小美") == "xiao-mei"
    # 完全未覆盖的角色名必须自洽：相同输入稳定，不同输入不冲突。
    assert slugify_role_name("阿古") == slugify_role_name("阿古")
    assert slugify_role_name("阿古") != slugify_role_name("阿明")
    # 多字符全角名（包含字典外字符）每个未覆盖字符都要贡献 token。
    assert slugify_role_name("柊筱") == "u67ca-u7b71"


def test_resolved_matched_character_keeps_logs_first_gpt_binding() -> None:
    library = [
        Character(
            id="hero",
            name="主角",
            nicknames=["队长"],
            profiles=[
                VoiceProfile(
                    id="hero-gpt",
                    name="主角 GPT",
                    engine=EngineName.GPT_SOVITS,
                    bindings=[
                        VoiceBinding(
                            binding_id="hero-gpt-binding",
                            provider_type="gpt-sovits",
                            capabilities=["trained_weights_voice", "reference_audio_voice"],
                            config={"logs_name": "demo-hero-logs", "gpt_weights_path": "gpt.ckpt"},
                        )
                    ],
                )
            ],
            default_profile="hero-gpt",
        )
    ]
    project = ScriptProject(title="demo", lines=[ScriptLine(id="l001", character_id="队长", text="我们必须出发。")])

    resolved = resolve_project_characters(project, library)

    assert resolved[0].id == "队长"
    assert resolved[0].name == "主角"
    assert resolved[0].profiles[0].bindings[0].config["logs_name"] == "demo-hero-logs"
