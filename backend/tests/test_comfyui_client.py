from __future__ import annotations

import json
import io
import wave
from pathlib import Path

import httpx
import pytest

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.comfyui.client import ComfyUIAPIClient
from app.comfyui.workflow_builder import (
    build_workflow,
    build_cosyvoice_workflow,
    build_indextts_workflow,
    build_gpt_sovits_workflow,
)
from app.models import EngineName, ProviderType, ScriptLine, TTSServiceEndpoint
from app.services import ComfyUITTSClient, build_service_client


def _cosyvoice_endpoint(base_url: str = "http://127.0.0.1:8188") -> TTSServiceEndpoint:
    return TTSServiceEndpoint(
        service_id="comfyui-cosyvoice",
        display_name="ComfyUI CosyVoice",
        provider_type=ProviderType.COMFYUI,
        api_contract="comfyui-tts-v1",
        engine=EngineName.COSYVOICE,
        base_url=base_url,
        mode="external",
        network_scope="localhost",
        resource_group="comfyui-gpu-0",
        capacity=3,
        priority=10,
        capabilities=["tts", "cosyvoice", "wav_output"],
        default_params={"resource_id": "cosy-main"},
    )


def _audio_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


class TestComfyUIAPIClient:
    def test_system_stats_ready(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"system": {"cuda": True}})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.system_stats()
            assert result["ready"] is True

    def test_system_stats_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.system_stats()
            assert result["ready"] is False
            assert "error" in result

    def test_submit_workflow_returns_prompt_id(self):
        prompt_id = "abc123-def456"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": prompt_id})
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.submit_workflow({"1": {"class_type": "TestNode", "inputs": {}}})
            assert result == prompt_id

    def test_poll_until_done_completes(self):
        prompt_id = "abc123"
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/history/" in request.url.path:
                call_count[0] += 1
                if call_count[0] >= 2:
                    return httpx.Response(
                        200,
                        json={
                            prompt_id: {
                                "outputs": {
                                    "4": {
                                        "audio": [
                                            {
                                                "filename": "tts_more_cosyvoice_00001.flac",
                                                "subfolder": "",
                                                "type": "output",
                                            }
                                        ]
                                    }
                                }
                            }
                        },
                    )
                return httpx.Response(200, json={})
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.poll_until_done(prompt_id, poll_interval=0.01, max_wait=5.0)
            assert "outputs" in result
            assert result["outputs"]["4"]["audio"][0]["filename"] == "tts_more_cosyvoice_00001.flac"

    def test_download_output(self):
        wav_content = b"RIFF\x24\x00\x00\x00WAVEfake"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=wav_content)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.download_output("test.wav")
            assert result == wav_content

    def test_free_memory(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.free_memory()
            assert result["status"] == "ok"


class TestWorkflowBuilder:
    def test_cosyvoice_workflow_basic(self):
        w = build_cosyvoice_workflow({"text": "Hello", "speed": 1.0, "resource_id": "cosy-main"})
        assert len(w) == 3
        assert w["1"]["class_type"] == "TTSExternalCosyVoiceEngine"
        assert w["1"]["inputs"]["resource_id"] == "cosy-main"
        assert w["3"]["class_type"] == "UnifiedTTSTextNode"
        assert w["3"]["inputs"]["text"] == "Hello"
        assert w["3"]["inputs"]["narrator_voice"] == "none"
        assert w["4"]["class_type"] == "SaveAudio"

    def test_cosyvoice_workflow_with_reference_audio(self):
        w = build_cosyvoice_workflow({
            "text": "Hello",
            "resource_id": "cosy-main",
            "asset_id": "asset-1",
            "prompt_text": "Hello there",
        })
        assert len(w) == 4
        assert w["2"]["class_type"] == "TTSExternalAudioAsset"
        assert w["2"]["inputs"]["asset_id"] == "asset-1"
        assert w["3"]["inputs"]["opt_narrator"] == ["2", 0]

    def test_cosyvoice_workflow_with_instruct(self):
        w = build_cosyvoice_workflow({
            "text": "Hello",
            "resource_id": "cosy-main",
            "instruct_text": "Speak with excitement",
        })
        assert w["1"]["inputs"]["instruct_text"] == "Speak with excitement"

    def test_indextts_workflow_basic(self):
        w = build_indextts_workflow({
            "text": "Hello world",
            "resource_id": "index-main",
            "do_sample": True,
            "top_p": 0.8,
            "temperature": 0.8,
        })
        assert len(w) == 3
        assert w["1"]["class_type"] == "TTSExternalIndexTTSEngine"
        assert w["1"]["inputs"]["do_sample"] is True
        assert w["3"]["class_type"] == "UnifiedTTSTextNode"
        assert w["3"]["inputs"]["text"] == "Hello world"
        assert w["3"]["inputs"]["narrator_voice"] == "none"

    def test_indextts_workflow_with_emotion_audio(self):
        w = build_indextts_workflow({
            "text": "Hello",
            "resource_id": "index-main",
            "asset_id": "asset-emotion",
        })
        assert len(w) == 4
        assert w["2"]["inputs"]["asset_id"] == "asset-emotion"

    def test_indextts_workflow_with_emotion_vector(self):
        vector = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        w = build_indextts_workflow({
            "text": "Hello",
            "resource_id": "index-main",
            "emotion_vector": vector,
        })
        assert w["1"]["inputs"]["emotion_alpha"] == 1.0

    def test_gpt_sovits_workflow(self):
        w = build_gpt_sovits_workflow({
            "text": "Hello",
            "resource_id": "gpt-main",
        })
        assert len(w) == 3
        assert w["1"]["class_type"] == "TTSExternalGPTSovitsEngine"
        assert w["4"]["class_type"] == "SaveAudio"
        assert w["1"]["inputs"]["resource_id"] == "gpt-main"

    def test_build_workflow_dispatcher(self):
        w = build_workflow("cosyvoice", {"text": "Hi", "resource_id": "cosy-main"})
        assert w["1"]["class_type"] == "TTSExternalCosyVoiceEngine"

        w = build_workflow("indextts", {"text": "Hi", "resource_id": "index-main"})
        assert w["1"]["class_type"] == "TTSExternalIndexTTSEngine"

        w = build_workflow("gpt-sovits", {"text": "Hi", "resource_id": "gpt-main"})
        assert w["1"]["class_type"] == "TTSExternalGPTSovitsEngine"

    def test_build_workflow_unknown_engine(self):
        with pytest.raises(ValueError, match="Unsupported"):
            build_workflow("unknown_engine", {"text": "Hi"})


class TestComfyUITTSClient:
    def test_build_client_via_factory(self):
        endpoint = _cosyvoice_endpoint()
        client = build_service_client(endpoint)
        assert isinstance(client, ComfyUITTSClient)
        assert client.endpoint == endpoint

    def test_health_mocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/system_stats" in request.url.path:
                return httpx.Response(200, json={"system": {"cuda": True}})
            return httpx.Response(404)

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            result = client.health()
            assert result["ready"] is True

    def test_synthesize_mocked(self, tmp_path: Path):
        prompt_id = "test-pid-001"
        wav_content = _audio_bytes()
        call_log: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_log.append(f"{request.method} {request.url.path}")
            if request.url.path == "/api/tts-audio-suite/v1/assets/audio" and request.method == "POST":
                return httpx.Response(201, json={"asset_id": "asset-1"})
            if request.url.path == "/api/tts-audio-suite/v1/assets/audio/asset-1" and request.method == "DELETE":
                return httpx.Response(200, json={"asset_id": "asset-1", "deleted": True})
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": prompt_id})
            if "/history/" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        prompt_id: {
                            "outputs": {
                                "4": {
                                    "audio": [
                                        {
                                            "filename": "tts_more_cosyvoice_00001.flac",
                                            "subfolder": "",
                                            "type": "output",
                                        }
                                    ]
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/view":
                return httpx.Response(200, content=wav_content)
            return httpx.Response(404)

        endpoint = _cosyvoice_endpoint()
        output_path = tmp_path / "result.wav"
        reference_path = tmp_path / "reference.wav"
        reference_path.write_bytes(_audio_bytes())
        line = ScriptLine(id="line-1", character_id="char-1", text="Hello world")
        request = SynthesisRequest(
            line=line,
            profile="default",
            output_path=output_path,
            parameters={
                "engine": "cosyvoice",
                "text": "Hello world",
                "speed": 1.0,
                "reference_audio": str(reference_path),
            },
        )

        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            client.api.poll_interval_override = 0.01
            result = client.synthesize(request)

        assert output_path.exists()
        assert output_path.read_bytes().startswith(b"RIFF")
        assert isinstance(result, SynthesisResult)
        assert result.audio_path == output_path
        assert result.metadata["prompt_id"] == prompt_id
        assert result.metadata["engine"] == "cosyvoice"
        assert result.metadata["resource_id"] == "cosy-main"
        assert "POST /api/tts-audio-suite/v1/assets/audio" in call_log
        assert "DELETE /api/tts-audio-suite/v1/assets/audio/asset-1" in call_log

    def test_unload(self):
        calls: list[tuple[str, dict | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.path, json.loads(request.content) if request.content else None))
            return httpx.Response(200, json={"status": "ok"})

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            client.unload()
        assert calls == [
            ("/api/tts-audio-suite/v1/runtime/release", {"resource_id": "cosy-main"}),
            ("/free", {"unload_models": True, "free_memory": True}),
        ]

    def test_capabilities(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tts-audio-suite/v1/capabilities":
                return httpx.Response(
                    200,
                    json={
                        "protocol_version": 1,
                        "nodes": {"cosyvoice": "TTSExternalCosyVoiceEngine"},
                        "resources": [{"resource_id": "cosy-main", "engine": "cosyvoice", "ready": True}],
                    },
                )
            return httpx.Response(404)

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            result = client.capabilities()
            assert result["protocol_version"] == 1
            assert result["resources"][0]["resource_id"] == "cosy-main"
