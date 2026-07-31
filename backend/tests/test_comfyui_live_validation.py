from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import soundfile

from app.adapters.base import SynthesisResult
from app.comfyui.live_validation import (
    LiveValidationConfig,
    _parse_args,
    build_live_endpoint,
    validate_audio_file,
    validate_live_engine,
)
from app.models import EngineName


def make_config(tmp_path: Path) -> LiveValidationConfig:
    reference_audio = tmp_path / "voice.wav"
    soundfile.write(reference_audio, [0.1] * 1600, 16000)
    return LiveValidationConfig(
        engine="indextts",
        resource_id="indextts-local",
        base_url="http://127.0.0.1:8188",
        reference_audio=reference_audio,
        reference_text="参考音频文本。",
        text="这是 IndexTTS 的真实验证。",
        output_path=tmp_path / "out.wav",
        evidence_path=tmp_path / "evidence.json",
    )


class FakeLiveClient:
    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.unload_calls = 0
        self.requests = []

    def health(self) -> dict:
        return {"ready": True}

    def capabilities(self) -> dict:
        return {"resources": [{"resource_id": "indextts-local", "ready": True}]}

    def synthesize(self, request):
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        soundfile.write(request.output_path, [0.2] * 800, 16000)
        assert request.progress_callback is not None
        request.progress_callback({"external_status": "queued", "progress": 0.1})
        request.progress_callback({"external_status": "completed", "progress": 1.0})
        return SynthesisResult(audio_path=request.output_path, metadata={"prompt_id": "test-prompt"})

    def unload(self) -> None:
        self.unload_calls += 1


def test_build_live_endpoint_uses_audio_suite_and_capacity_one(tmp_path):
    config = LiveValidationConfig(
        engine="indextts",
        resource_id="indextts-local",
        base_url="http://127.0.0.1:8188",
        reference_audio=tmp_path / "voice.wav",
        reference_text="",
        text="这是 IndexTTS 的真实验证。",
        output_path=tmp_path / "out.wav",
        evidence_path=tmp_path / "evidence.json",
    )

    endpoint = build_live_endpoint(config)

    assert endpoint.api_contract == "comfyui-tts-audio-suite-v1"
    assert endpoint.engine == EngineName.INDEX_TTS
    assert endpoint.capacity == 1
    assert endpoint.resource_group == "comfyui-local-0"
    assert endpoint.default_params["resource_id"] == "indextts-local"
    assert endpoint.default_params["poll_interval"] == 2.0
    assert endpoint.default_params["timeout_seconds"] == config.timeout_seconds


def test_parse_args_accepts_powershell_elided_empty_reference_text(tmp_path):
    config = _parse_args(
        [
            "--engine",
            "indextts",
            "--resource-id",
            "indextts-local",
            "--base-url",
            "http://127.0.0.1:8188",
            "--reference-audio",
            str(tmp_path / "voice.wav"),
            "--reference-text",
            "--text",
            "这是 IndexTTS 的真实验证。",
            "--output",
            str(tmp_path / "out.wav"),
            "--evidence",
            str(tmp_path / "evidence.json"),
        ]
    )

    assert config.reference_text == ""
    assert config.text == "这是 IndexTTS 的真实验证。"


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8189", "http://192.168.1.10:8188"])
def test_live_validation_config_requires_mandated_comfyui_url(tmp_path, base_url):
    config = make_config(tmp_path)

    assert config.base_url == "http://127.0.0.1:8188"
    with pytest.raises(ValueError, match="must use http://127.0.0.1:8188"):
        replace(config, base_url=base_url)


def test_validate_audio_file_rejects_silence(tmp_path):
    path = tmp_path / "silent.wav"
    soundfile.write(path, [0.0] * 16000, 16000)

    with pytest.raises(ValueError, match="silent"):
        validate_audio_file(path)


def test_validate_live_engine_writes_failure_evidence_and_unloads(tmp_path):
    fake = FakeLiveClient(fail_with=RuntimeError("model load failed"))
    config = make_config(tmp_path)

    result = validate_live_engine(config, client_factory=lambda endpoint: fake)

    assert result.status == "failed"
    assert result.error == "model load failed"
    assert fake.unload_calls == 1
    payload = json.loads(config.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["resource_id"] == "indextts-local"


def test_validate_live_engine_records_valid_audio_and_prompt_progress(tmp_path):
    fake = FakeLiveClient()
    config = make_config(tmp_path)

    result = validate_live_engine(config, client_factory=lambda endpoint: fake)

    assert result.status == "passed"
    assert result.sample_rate == 16000
    assert result.frames == 800
    assert result.peak > 0.1
    assert [update["external_status"] for update in result.progress] == ["queued", "completed"]
    assert fake.requests[0].parameters["reference_audio"] == str(config.reference_audio)
    assert fake.requests[0].parameters["prompt_text"] == config.reference_text
    assert fake.unload_calls == 1
