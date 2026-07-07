from pathlib import Path

import pytest

from app.models import Character, EngineName, GenerationManifest, GenerationVersion, LineGenerationHistory, ProjectCharacter, ProjectCharacterMode, ProviderType, ReferenceAudioGroup, ReferenceAudioSample, ScriptLine, ScriptProject, TTSServiceEndpoint, VoiceBinding
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


def test_cosyvoice_endpoint_defaults_to_core_engine_and_gradio_contract() -> None:
    endpoint = TTSServiceEndpoint(
        service_id="cosyvoice-http",
        provider_type=ProviderType.COSYVOICE,
        base_url="http://127.0.0.1:50000",
        managed=False,
        enabled=False,
        capabilities=["tts", "reference_audio_voice", "zero_shot_voice", "wav_output"],
    )

    assert endpoint.engine == EngineName.COSYVOICE
    assert endpoint.provider_type == ProviderType.COSYVOICE
    assert endpoint.api_contract == "gradio-cosyvoice-webui"


def test_character_voice_binding_can_target_cosyvoice() -> None:
    binding = VoiceBinding(
        binding_id="line-temp-cosyvoice",
        provider_type=ProviderType.COSYVOICE,
        service_id="cosyvoice-http",
        capabilities=["zero_shot_voice", "reference_audio_voice"],
        config={"mode": "zero_shot", "prompt_audio_path": "refs/voice.wav", "prompt_text": "hello"},
    )

    assert binding.provider_type == ProviderType.COSYVOICE
    assert binding.config["mode"] == "zero_shot"


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


def test_legacy_project_materializes_initial_script_and_parse_revisions() -> None:
    project = ScriptProject(
        title="demo",
        default_language="zh",
        project_characters=[
            ProjectCharacter(project_character_id="xiao-pin", name="小品"),
            ProjectCharacter(project_character_id="xiao-guang", name="小光"),
        ],
        lines=[
            ScriptLine(id="l001", character_id="xiao-pin", text="严镜、小光，我来救你们了！", note="目光坚定"),
            ScriptLine(id="l002", character_id="xiao-guang", text="呃……", note="痛苦呻吟"),
        ],
    )

    assert project.active_script_revision_id == "script-r001"
    assert project.active_parse_revision_id == "parse-r001"
    assert project.script_revisions[0].source_markdown.startswith("小品（目光坚定）")
    assert project.parse_revisions[0].script_revision_id == "script-r001"
    assert [line.line_uid for line in project.lines] == ["parse-r001:l001", "parse-r001:l002"]
    assert [line.line_uid for line in project.parse_revisions[0].lines] == ["parse-r001:l001", "parse-r001:l002"]


def test_manifest_append_version_keeps_version_ids_monotonic_per_line_uid() -> None:
    manifest = GenerationManifest(project_id="demo")
    manifest.append_version(
        "l001",
        GenerationVersion(
            version_id="v003",
            line_uid="parse-r001:l001",
            engine=EngineName.GPT_SOVITS,
            profile="a",
            status="failed",
        ),
    )
    manifest.append_version(
        "l001",
        GenerationVersion(
            version_id="v002",
            line_uid="parse-r001:l001",
            engine=EngineName.GPT_SOVITS,
            profile="a",
            status="completed",
        ),
    )

    assert [version.version_id for version in manifest.lines["parse-r001:l001"].versions] == ["v003", "v004"]


def test_manifest_history_for_stable_line_uid_does_not_fallback_to_legacy_line_id() -> None:
    manifest = GenerationManifest(project_id="demo")
    manifest.lines["l001"] = LineGenerationHistory(
        line_id="l001",
        versions=[
            GenerationVersion(
                version_id="v001",
                engine=EngineName.GPT_SOVITS,
                profile="legacy",
                status="completed",
            )
        ],
    )

    assert manifest.history_for_line("l001", "parse-r001:l001") is None

    manifest.append_version(
        "l001",
        GenerationVersion(
            version_id="v001",
            line_uid="parse-r001:l001",
            engine=EngineName.GPT_SOVITS,
            profile="current",
            status="completed",
        ),
    )

    assert [version.version_id for version in manifest.lines["parse-r001:l001"].versions] == ["v001"]
