from __future__ import annotations

import json
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
    )


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
        w = build_cosyvoice_workflow({"text": "Hello", "speed": 1.0})
        assert len(w) == 3
        assert w["1"]["class_type"] == "CosyVoiceEngineNode"
        assert w["3"]["class_type"] == "UnifiedTTSTextNode"
        assert w["3"]["inputs"]["text"] == "Hello"
        assert w["3"]["inputs"]["narrator_voice"] == "voices_examples/higgs_audio/zh_man_sichuan.wav"
        assert w["4"]["class_type"] == "SaveAudio"

    def test_cosyvoice_workflow_with_reference_audio(self):
        w = build_cosyvoice_workflow({
            "text": "Hello",
            "reference_audio": "/data/ref.wav",
            "prompt_text": "Hello there",
        })
        assert len(w) == 4
        assert w["2"]["class_type"] == "LoadAudio"
        assert w["2"]["inputs"]["audio"] == "/data/ref.wav"
        assert w["3"]["inputs"]["opt_narrator"] == ["2", 0]

    def test_cosyvoice_workflow_with_instruct(self):
        w = build_cosyvoice_workflow({
            "text": "Hello",
            "instruct_text": "Speak with excitement",
        })
        assert w["1"]["inputs"]["instruct_text"] == "Speak with excitement"

    def test_indextts_workflow_basic(self):
        w = build_indextts_workflow({
            "text": "Hello world",
            "do_sample": True,
            "top_p": 0.8,
            "temperature": 0.8,
        })
        assert len(w) == 3
        assert w["1"]["class_type"] == "IndexTTSEngineNode"
        assert w["1"]["inputs"]["do_sample"] is True
        assert w["3"]["class_type"] == "UnifiedTTSTextNode"
        assert w["3"]["inputs"]["text"] == "Hello world"
        assert w["3"]["inputs"]["narrator_voice"] == "none"

    def test_indextts_workflow_with_emotion_audio(self):
        w = build_indextts_workflow({
            "text": "Hello",
            "emotion_audio": "/data/emotion.wav",
        })
        assert len(w) == 3
        assert w["1"]["inputs"]["emotion_audio"] == "/data/emotion.wav"

    def test_indextts_workflow_with_emotion_vector(self):
        vector = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        w = build_indextts_workflow({
            "text": "Hello",
            "emotion_vector": vector,
        })
        assert w["1"]["inputs"]["emotion_alpha"] == 1.0

    def test_gpt_sovits_workflow(self):
        w = build_gpt_sovits_workflow({
            "text": "Hello",
            "gpt_weights_path": "/weights/gpt.pth",
            "sovits_weights_path": "/weights/sovits.pth",
        })
        assert len(w) == 3
        assert w["1"]["class_type"] == "GPTSovitsEngineNode"
        assert w["4"]["class_type"] == "SaveAudio"
        assert "/weights/gpt.pth /weights/sovits.pth" in str(w["1"]["inputs"]["weight_pair"])

    def test_build_workflow_dispatcher(self):
        w = build_workflow("cosyvoice", {"text": "Hi"})
        assert w["1"]["class_type"] == "CosyVoiceEngineNode"

        w = build_workflow("indextts", {"text": "Hi"})
        assert w["1"]["class_type"] == "IndexTTSEngineNode"

        w = build_workflow("gpt-sovits", {"text": "Hi"})
        assert w["1"]["class_type"] == "GPTSovitsEngineNode"

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
        wav_content = b"RIFF\x24\x00\x00\x00WAVEsynthesized"
        call_log: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_log.append(f"{request.method} {request.url.path}")
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
        line = ScriptLine(id="line-1", character_id="char-1", text="Hello world")
        request = SynthesisRequest(
            line=line,
            profile="default",
            output_path=output_path,
            parameters={"engine": "cosyvoice", "text": "Hello world", "speed": 1.0},
        )

        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            client.api.poll_interval_override = 0.01
            result = client.synthesize(request)

        assert output_path.exists()
        assert output_path.read_bytes() == wav_content
        assert isinstance(result, SynthesisResult)
        assert result.audio_path == output_path
        assert result.metadata["prompt_id"] == prompt_id
        assert result.metadata["engine"] == "cosyvoice"

    def test_unload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            client.unload()

    def test_capabilities(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/object_info" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "CosyVoiceEngineNode": {"input": {"required": {}}},
                        "UnifiedTTSTextNode": {"input": {"required": {}}},
                        "SaveAudio": {"input": {"required": {}}},
                    },
                )
            return httpx.Response(404)

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            result = client.capabilities()
            assert "capabilities" in result
            assert "available_nodes" in result
            assert "CosyVoiceEngineNode" in result["available_nodes"]
