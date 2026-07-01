from app.models import Character, EngineName, ScriptLine, ScriptProject, VoiceBinding, VoiceProfile
from app.role_library import common_logs_presets, match_project_characters, resolve_project_characters


def test_project_character_matching_uses_aliases_nicknames_and_match_names() -> None:
    library = [
        Character(
            id="guangtou",
            name="光头",
            aliases=["光头胖子"],
            nicknames=["小光"],
            match_names=["光头"],
        ),
        Character(
            id="yanjing",
            name="眼镜",
            aliases=["眼镜哥"],
            nicknames=["严镜"],
            match_names=["TTS-大鹏眼镜"],
        ),
    ]
    project = ScriptProject(
        title="demo",
        lines=[
            ScriptLine(id="l001", character_id="小光", text="怎么爆炸了……"),
            ScriptLine(id="l002", character_id="严镜", text="没有用！"),
        ],
    )

    mappings = match_project_characters(project, library)

    assert [item.library_character_id for item in mappings] == ["guangtou", "yanjing"]
    assert [item.name for item in mappings] == ["光头", "眼镜"]


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
    assert by_name["光头"]["logs_name"] == "光头TTS新-20260611"
    assert "小光" in by_name["光头"]["nicknames"]
    assert by_name["眼镜"]["logs_name"] == "TTS-大鹏眼镜"
    assert "严镜" in by_name["眼镜"]["nicknames"]


def test_resolved_matched_character_keeps_logs_first_gpt_binding() -> None:
    library = [
        Character(
            id="guangtou",
            name="光头",
            nicknames=["小光"],
            profiles=[
                VoiceProfile(
                    id="guangtou-gpt",
                    name="光头 GPT",
                    engine=EngineName.GPT_SOVITS,
                    bindings=[
                        VoiceBinding(
                            binding_id="guangtou-gpt-binding",
                            provider_type="gpt-sovits",
                            capabilities=["trained_weights_voice", "reference_audio_voice"],
                            config={"logs_name": "光头TTS新-20260611", "gpt_weights_path": "gpt.ckpt"},
                        )
                    ],
                )
            ],
            default_profile="guangtou-gpt",
        )
    ]
    project = ScriptProject(title="demo", lines=[ScriptLine(id="l001", character_id="小光", text="怎么爆炸了……")])

    resolved = resolve_project_characters(project, library)

    assert resolved[0].id == "小光"
    assert resolved[0].name == "光头"
    assert resolved[0].profiles[0].bindings[0].config["logs_name"] == "光头TTS新-20260611"
