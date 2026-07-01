from pathlib import Path

import pytest

from app.models import Character, EngineName, ProjectCharacter, ProjectCharacterMode, ReferenceAudioGroup, ReferenceAudioSample, ScriptLine, ScriptProject, VoiceBinding
from app.storage import ProjectStore


def test_script_project_rejects_duplicate_line_ids() -> None:
    with pytest.raises(ValueError, match="duplicate line id"):
        ScriptProject(
            title="demo",
            default_language="zh",
            lines=[
                ScriptLine(id="l001", character_id="alice", text="hello"),
                ScriptLine(id="l001", character_id="bob", text="world"),
            ],
        )


def test_character_default_profile_must_exist() -> None:
    with pytest.raises(ValueError, match="default profile"):
        Character(
            id="alice",
            name="Alice",
            default_engine=EngineName.GPT_SOVITS,
            default_profile="missing",
            profiles=[],
        )


def test_project_store_round_trips_json(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = ScriptProject(
        title="demo",
        default_language="zh",
        lines=[ScriptLine(id="l001", character_id="alice", text="你好。")],
    )

    store.save_project("demo", project)

    assert store.load_project("demo") == project


def test_character_library_metadata_and_reference_sample_text_are_optional() -> None:
    character = Character(
        id="xiao-pin",
        name="小品",
        aliases=["小品"],
        nicknames=["包子脸"],
        match_names=["小品同学"],
        tags=["常用", "女声"],
        library_status="partial",
        source_assets={"gpt_sovits": {"logs_name": "1小品-斯月学杨师版"}},
        reference_audio_groups=[
            ReferenceAudioGroup(
                id="xiao-pin-refs",
                name="小品参考音频",
                paths=["refs/xiao-pin"],
                samples=[
                    ReferenceAudioSample(
                        path="refs/xiao-pin/ref.wav",
                        text="快走！",
                        text_source="sidecar",
                    )
                ],
            )
        ],
    )

    assert character.tags == ["常用", "女声"]
    assert character.nicknames == ["包子脸"]
    assert character.match_names == ["小品同学"]
    assert character.library_status == "partial"
    assert character.reference_audio_groups[0].samples[0].text == "快走！"
    assert character.updated_at is not None


def test_script_line_can_hold_temporary_voice_binding() -> None:
    binding = VoiceBinding(
        binding_id="line-temp-index",
        provider_type="indextts",
        service_id="lan-indextts",
        capabilities=["reference_audio_voice", "emotion_text"],
        config={"voice": "tmp/ref.wav", "emotion_mode": "emotion_text", "emotion_text": "紧张"},
    )

    line = ScriptLine(id="l001", character_id="guest", text="救命啊！", temporary_binding=binding)

    assert line.temporary_binding == binding
    assert line.temporary_binding.config["voice"] == "tmp/ref.wav"


def test_script_project_can_reference_and_snapshot_library_characters() -> None:
    snapshot = Character(id="xiao-pin", name="小品")
    project = ScriptProject(
        title="demo",
        default_language="zh",
        project_characters=[
            ProjectCharacter(
                project_character_id="role-1",
                name="小品",
                library_character_id="xiao-pin",
                mode=ProjectCharacterMode.REFERENCE,
            ),
            ProjectCharacter(
                project_character_id="role-2",
                name="小品快照",
                library_character_id="xiao-pin",
                mode=ProjectCharacterMode.SNAPSHOT,
                character_snapshot=snapshot,
            ),
        ],
        lines=[ScriptLine(id="l001", character_id="role-1", text="你好")],
    )

    assert project.project_characters[0].mode == ProjectCharacterMode.REFERENCE
    assert project.project_characters[1].character_snapshot == snapshot
