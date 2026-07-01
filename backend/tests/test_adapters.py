import wave
import subprocess
from pathlib import Path

from app.adapters.base import SynthesisRequest
from app.adapters.indextts import IndexTTSSubprocessAdapter
from app.adapters.mock import MockAdapter
from app.models import EngineName, ScriptLine


def test_mock_adapter_writes_valid_wav(tmp_path: Path) -> None:
    adapter = MockAdapter(EngineName.GPT_SOVITS)
    output = tmp_path / "line.wav"

    result = adapter.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l001", character_id="alice", text="你好"),
            profile="demo",
            output_path=output,
            parameters={},
        )
    )

    assert result.audio_path == output
    with wave.open(str(output), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 24000
        assert wav.getnframes() > 0


def test_subprocess_adapters_resolve_repo_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    index_adapter = IndexTTSSubprocessAdapter(Path("repo/index-tts"))

    assert index_adapter.repo_dir == (tmp_path / "repo/index-tts").resolve(strict=False)


def test_indextts_subprocess_uses_absolute_output_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo/index-tts"
    (repo / "indextts").mkdir(parents=True)
    (repo / "indextts/cli_v2.py").write_text("", encoding="utf-8")
    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"voice")
    captured: dict[str, Path] = {}

    def fake_run(command, **kwargs):
        output_path = Path(command[command.index("--output") + 1])
        captured["output"] = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFFtest")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("app.adapters.indextts.subprocess.run", fake_run)
    adapter = IndexTTSSubprocessAdapter(Path("repo/index-tts"))

    result = adapter.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l001", character_id="alice", text="你好"),
            profile="demo",
            output_path=Path("data/out.wav"),
            parameters={"voice": str(voice)},
        )
    )

    assert captured["output"].is_absolute()
    assert result.audio_path == (tmp_path / "data/out.wav").resolve(strict=False)


def test_indextts_subprocess_maps_emotion_and_advanced_parameters(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo/index-tts"
    (repo / "indextts").mkdir(parents=True)
    (repo / "indextts/cli_v2.py").write_text("", encoding="utf-8")
    voice = tmp_path / "voice.wav"
    emotion = tmp_path / "emotion.wav"
    voice.write_bytes(b"voice")
    emotion.write_bytes(b"emotion")
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        output_path = Path(command[command.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFFtest")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("app.adapters.indextts.subprocess.run", fake_run)
    adapter = IndexTTSSubprocessAdapter(repo)

    adapter.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l001", character_id="alice", text="你好", note="愤怒"),
            profile="demo",
            output_path=tmp_path / "out.wav",
            parameters={
                "voice": str(voice),
                "emotion_mode": "emotion_audio",
                "emotion_audio": str(emotion),
                "emotion_weight": 0.7,
                "emotion_random": True,
                "top_p": 0.9,
                "top_k": 40,
                "temperature": 0.6,
                "max_text_tokens_per_segment": 80,
            },
        )
    )

    command = captured["command"]
    assert command[command.index("--emotion-audio") + 1] == str(emotion)
    assert command[command.index("--emotion-weight") + 1] == "0.7"
    assert "--emotion-random" in command
    assert command[command.index("--top-p") + 1] == "0.9"
    assert command[command.index("--top-k") + 1] == "40"
    assert command[command.index("--temperature") + 1] == "0.6"
    assert command[command.index("--max-text-tokens-per-segment") + 1] == "80"
