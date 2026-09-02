from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.parser import ParserProviderConfig
from app.voice_metadata_inference import (
    OpenAIVoiceMetadataInferrer,
    VoiceMetadataContractError,
    VoiceMetadataInferenceItem,
    decode_voice_metadata_payload,
)


def _provider() -> ParserProviderConfig:
    return ParserProviderConfig(
        name="metadata-test",
        base_url="http://127.0.0.1:9821/v1",
        api_key_env="VOICE_METADATA_TEST_KEY",
        model="metadata-model",
        enabled=True,
        timeout_seconds=5,
        priority=1,
        adapter="openai-compatible",
    )


def test_inference_item_rejects_absolute_or_nested_filename() -> None:
    with pytest.raises(ValidationError):
        VoiceMetadataInferenceItem(asset_id="asset-1", filename=r"E:\训练\voice.wav")
    with pytest.raises(ValidationError):
        VoiceMetadataInferenceItem(asset_id="asset-1", filename="refs/voice.wav")


def test_openai_inference_sends_only_bounded_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        content = {
            "items": [
                {
                    "asset_id": "asset-1",
                    "character": "诸葛九九",
                    "character_confidence": 0.96,
                    "emotion": "happy",
                    "emotion_confidence": 0.88,
                    "language": "zh",
                }
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]},
        )

    monkeypatch.setenv("VOICE_METADATA_TEST_KEY", "not-recorded")
    transport = httpx.MockTransport(handler)
    inferrer = OpenAIVoiceMetadataInferrer(
        _provider(),
        client=httpx.Client(transport=transport),
    )

    result = inferrer.infer(
        [
            VoiceMetadataInferenceItem(
                asset_id="asset-1",
                filename="九九-开心.wav",
                prompt_text="真的太好了",
            )
        ]
    )

    serialized = json.dumps(captured, ensure_ascii=False)
    assert r"E:\训练" not in serialized
    assert "audio_bytes" not in serialized
    assert "not-recorded" not in serialized
    assert result[0].character == "诸葛九九"
    assert result[0].emotion == "happy"


def test_contract_rejects_extra_fields_and_reports_only_field_paths() -> None:
    payload = {
        "items": [
            {
                "asset_id": "asset-1",
                "character": "九九",
                "character_confidence": 0.95,
                "emotion": "happy",
                "emotion_confidence": 0.9,
                "language": "zh",
                "raw_response": "must never survive",
            }
        ]
    }

    with pytest.raises(VoiceMetadataContractError) as exc_info:
        decode_voice_metadata_payload(payload)

    assert exc_info.value.code == "voice_metadata_contract_invalid"
    assert exc_info.value.field_paths == ("items.0.raw_response",)
    assert "must never survive" not in str(exc_info.value)
