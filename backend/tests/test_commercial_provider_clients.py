from __future__ import annotations

from pathlib import Path
import json

import httpx

from app.adapters.base import SynthesisRequest
from app.models import EngineName, ProviderType, ScriptLine, TTSServiceEndpoint
from app.services import build_service_client


def make_request(tmp_path: Path, parameters: dict) -> SynthesisRequest:
    return SynthesisRequest(
        line=ScriptLine(id="l1", character_id="alice", text="你好", note="温柔", language="zh"),
        profile="alice-commercial",
        output_path=tmp_path / "out.wav",
        parameters=parameters,
    )


def test_openai_client_maps_speech_request(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"RIFFopenai")

    endpoint = TTSServiceEndpoint(
        service_id="openai-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.OPENAI,
        api_contract="openai-speech-v1",
        base_url="https://api.openai.com/v1",
        auth_profile={"api_key_env": "OPENAI_API_KEY"},
        default_params={"model": "gpt-4o-mini-tts", "voice": "alloy", "response_format": "wav"},
        capabilities=["tts", "commercial_voice"],
    )
    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(make_request(tmp_path, {"instructions": "温柔"}))

    assert result.audio_path.read_bytes() == b"RIFFopenai"
    assert calls[0].url.path == "/v1/audio/speech"
    assert calls[0].headers["Authorization"] == "Bearer sk-test-secret"
    assert json.loads(calls[0].content)["input"] == "你好"


def test_xai_client_maps_tts_request(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"RIFFxai")

    endpoint = TTSServiceEndpoint(
        service_id="xai-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.XAI,
        api_contract="xai-tts-v1",
        base_url="https://api.x.ai/v1",
        auth_profile={"api_key_env": "XAI_API_KEY"},
        default_params={"model": "grok-tts", "voice_id": "voice-1", "response_format": "wav"},
        capabilities=["tts", "commercial_voice"],
    )
    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(make_request(tmp_path, {}))

    assert result.audio_path.read_bytes() == b"RIFFxai"
    assert calls[0].url.path == "/v1/audio/speech"
    assert calls[0].headers["Authorization"] == "Bearer xai-secret"
    assert b'"voice":"voice-1"' in calls[0].content


def test_gemini_client_maps_tts_request(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": "UklGRmdlbWluaQ=="}}]}}]})

    endpoint = TTSServiceEndpoint(
        service_id="gemini-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.GEMINI,
        api_contract="gemini-tts-v1",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        auth_profile={"api_key_env": "GEMINI_API_KEY"},
        default_params={"model": "gemini-2.5-flash-preview-tts", "voice_name": "Kore"},
        capabilities=["tts", "commercial_voice", "multi_speaker_tts"],
    )
    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(make_request(tmp_path, {}))

    assert result.audio_path.read_bytes() == b"RIFFgemini"
    assert calls[0].url.path.endswith(":generateContent")
    assert calls[0].url.params["key"] == "gemini-secret"
    assert b'"voiceName":"Kore"' in calls[0].content


def test_volcengine_client_maps_tts_request(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOLCENGINE_APP_ID", "app-id")
    monkeypatch.setenv("VOLCENGINE_ACCESS_TOKEN", "volc-token")
    monkeypatch.setenv("VOLCENGINE_CLUSTER_ID", "volc-cluster")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": "UklGRnZvbGM="})

    endpoint = TTSServiceEndpoint(
        service_id="volcengine-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.VOLCENGINE,
        api_contract="volcengine-tts-v1",
        base_url="https://openspeech.bytedance.com/api/v1/tts",
        auth_profile={
            "app_id_env": "VOLCENGINE_APP_ID",
            "access_token_env": "VOLCENGINE_ACCESS_TOKEN",
            "cluster_id_env": "VOLCENGINE_CLUSTER_ID",
        },
        default_params={"voice_type": "zh_female_xiaoxiao", "encoding": "wav"},
        capabilities=["tts", "commercial_voice"],
    )
    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(make_request(tmp_path, {"emotion": "happy"}))

    assert result.audio_path.read_bytes() == b"RIFFvolc"
    assert calls[0].url.path == "/api/v1/tts"
    assert calls[0].headers["Authorization"] == "Bearer;volc-token"
    assert b'"appid":"app-id"' in calls[0].content
    assert b'"cluster":"volc-cluster"' in calls[0].content
