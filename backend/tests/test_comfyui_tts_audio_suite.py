import json
from pathlib import Path

import httpx
import pytest

from app.adapters.base import SynthesisRequest
from app.models import EngineName, GenerationManifest, GenerationTask, ProviderType, ScriptLine, TTSServiceEndpoint
from app.queue import ServiceGenerationQueue
from app.services import ServiceRoute, build_service_client


def _comfy_endpoint() -> TTSServiceEndpoint:
    return TTSServiceEndpoint(
        service_id="comfyui-local",
        provider_type=ProviderType.GPT_SOVITS,
        api_contract="comfyui-tts-audio-suite-v1",
        base_url="http://127.0.0.1:8188",
        mode="external",
        network_scope="localhost",
        managed=False,
        capabilities=["tts", "comfyui", "tts-audio-suite"],
    )


def _capabilities_payload() -> dict[str, object]:
    return {
        "protocol_version": 1,
        "plugin_version": "5.5.2",
        "nodes": {
            "gpt_sovits": "TTSExternalGPTSovitsEngine",
            "index_tts": "TTSExternalIndexTTSEngine",
            "cosyvoice": "TTSExternalCosyVoiceEngine",
            "audio_asset": "TTSExternalAudioAsset",
            "text": "UnifiedTTSTextNode",
            "save_audio": "SaveAudio",
        },
        "resources": [{"resource_id": "hero-main", "engine": "gpt_sovits", "ready": True}],
    }


def test_comfyui_audio_suite_health_uses_bridge_capabilities() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tts-audio-suite/v1/capabilities":
            return httpx.Response(200, json=_capabilities_payload())
        return httpx.Response(404)

    client = build_service_client(_comfy_endpoint(), transport=httpx.MockTransport(handler))

    health = client.health()

    assert calls == ["/api/tts-audio-suite/v1/capabilities"]
    assert health["ready"] is True
    assert health["api_contract"] == "comfyui-tts-audio-suite-v1"
    assert health["resources"] == [{"resource_id": "hero-main", "engine": "gpt_sovits", "ready": True}]


def test_comfyui_audio_suite_synthesizes_via_prompt_history_and_view(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFFreference")
    output = tmp_path / "line.wav"
    generated = b"RIFFgenerated"
    calls: list[tuple[str, str]] = []
    progress_updates: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/tts-audio-suite/v1/capabilities":
            return httpx.Response(200, json=_capabilities_payload())
        if request.method == "POST" and request.url.path == "/api/tts-audio-suite/v1/assets/audio":
            assert b"RIFFreference" in request.content
            return httpx.Response(
                201,
                json={"asset_id": "a" * 32, "sha256": "b" * 64, "size_bytes": 13, "filename": "asset.wav"},
            )
        if request.method == "POST" and request.url.path == "/prompt":
            payload = json.loads(request.content.decode("utf-8"))
            workflow = payload["prompt"]
            assert workflow["1"]["class_type"] == "TTSExternalGPTSovitsEngine"
            assert workflow["1"]["inputs"]["resource_id"] == "hero-main"
            assert workflow["2"]["class_type"] == "TTSExternalAudioAsset"
            assert workflow["2"]["inputs"] == {"asset_id": "a" * 32, "reference_text": "参考文本"}
            assert workflow["3"]["class_type"] == "UnifiedTTSTextNode"
            assert workflow["3"]["inputs"]["TTS_engine"] == ["1", 0]
            assert workflow["3"]["inputs"]["opt_narrator"] == ["2", 0]
            assert workflow["3"]["inputs"]["text"] == "马上出发"
            assert workflow["3"]["inputs"]["seed"] == 77
            assert workflow["4"]["class_type"] == "SaveAudio"
            assert workflow["4"]["inputs"]["audio"] == ["3", 0]
            return httpx.Response(200, json={"prompt_id": "prompt-1", "number": 7, "node_errors": {}})
        if request.method == "GET" and request.url.path == "/history/prompt-1":
            if calls.count(("GET", "/history/prompt-1")) == 1:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "prompt-1": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "4": {
                                "audio": [
                                    {"filename": "tts_more_line_00001.wav", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [["prompt-1"]], "queue_pending": []})
        if request.method == "GET" and request.url.path == "/view":
            assert request.url.params["filename"] == "tts_more_line_00001.wav"
            assert request.url.params["type"] == "output"
            return httpx.Response(200, content=generated)
        if request.method == "DELETE" and request.url.path == "/api/tts-audio-suite/v1/assets/audio/" + "a" * 32:
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"detail": str(request.url)})

    client = build_service_client(_comfy_endpoint(), transport=httpx.MockTransport(handler))
    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="hero", text="马上出发"),
            profile="hero",
            output_path=output,
            parameters={
                "resource_id": "hero-main",
                "ref_audio_path": str(reference),
                "prompt_text": "参考文本",
                "seed": 77,
                "poll_interval_seconds": 0,
            },
            progress_callback=progress_updates.append,
        )
    )

    assert result.audio_path == output
    assert output.read_bytes() == generated
    assert result.metadata["api_contract"] == "comfyui-tts-audio-suite-v1"
    assert result.metadata["prompt_id"] == "prompt-1"
    assert result.metadata["resource_id"] == "hero-main"
    assert [item["external_status"] for item in progress_updates] == ["queued", "running", "completed"]


def test_comfyui_audio_suite_requires_resource_id_before_prompt(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tts-audio-suite/v1/capabilities":
            return httpx.Response(200, json=_capabilities_payload())
        raise AssertionError("resource validation must stop before prompt submission")

    client = build_service_client(_comfy_endpoint(), transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="resource_id"):
        client.synthesize(
            SynthesisRequest(
                line=ScriptLine(id="l1", character_id="hero", text="马上出发"),
                profile="hero",
                output_path=tmp_path / "line.wav",
                parameters={"poll_interval_seconds": 0},
            )
        )

    assert calls == ["/api/tts-audio-suite/v1/capabilities"]


def test_comfyui_audio_suite_unload_releases_runtime_and_frees_comfy_memory() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append((request.method, request.url.path, payload))
        if request.method == "POST" and request.url.path == "/api/tts-audio-suite/v1/runtime/release":
            return httpx.Response(200, json={"released": ["runtime"], "busy": [], "errors": []})
        if request.method == "POST" and request.url.path == "/free":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": str(request.url)})

    client = build_service_client(_comfy_endpoint(), transport=httpx.MockTransport(handler))

    client.unload()

    assert calls == [
        ("POST", "/api/tts-audio-suite/v1/runtime/release", {"all": True}),
        ("POST", "/free", {"unload_models": True, "free_memory": True}),
    ]


def test_comfyui_audio_suite_queue_writes_prompt_metadata_to_manifest(tmp_path: Path) -> None:
    endpoint = _comfy_endpoint()
    generated = b"RIFFmanifest"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tts-audio-suite/v1/capabilities":
            return httpx.Response(200, json=_capabilities_payload())
        if request.method == "POST" and request.url.path == "/prompt":
            payload = json.loads(request.content.decode("utf-8"))
            workflow = payload["prompt"]
            assert workflow["1"]["class_type"] == "TTSExternalGPTSovitsEngine"
            assert workflow["1"]["inputs"]["resource_id"] == "hero-main"
            assert workflow["2"]["class_type"] == "UnifiedTTSTextNode"
            assert workflow["2"]["inputs"]["TTS_engine"] == ["1", 0]
            assert workflow["3"]["class_type"] == "SaveAudio"
            assert workflow["3"]["inputs"]["audio"] == ["2", 0]
            return httpx.Response(200, json={"prompt_id": "prompt-manifest", "number": 8, "node_errors": {}})
        if request.method == "GET" and request.url.path == "/history/prompt-manifest":
            return httpx.Response(
                200,
                json={
                    "prompt-manifest": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {"3": {"audio": [{"filename": "manifest.wav", "subfolder": "", "type": "output"}]}},
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/view":
            assert request.url.params["filename"] == "manifest.wav"
            return httpx.Response(200, content=generated)
        return httpx.Response(404, json={"detail": str(request.url)})

    service_client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    class StaticRouter:
        def resolve_task(self, _task: GenerationTask) -> ServiceRoute:
            return ServiceRoute(endpoint=endpoint, client=service_client)

    manifest = GenerationManifest(project_id="demo")
    ServiceGenerationQueue(StaticRouter()).run(
        [
            GenerationTask(
                line=ScriptLine(id="l1", character_id="hero", text="马上出发"),
                engine=EngineName.GPT_SOVITS,
                profile="hero",
                service_id=endpoint.service_id,
                provider_type=ProviderType.GPT_SOVITS,
                parameters={"resource_id": "hero-main", "seed": 77, "poll_interval_seconds": 0},
            )
        ],
        manifest,
        output_dir=tmp_path,
    )

    version = manifest.lines["l1"].versions[0]
    assert version.status == "completed"
    assert version.metadata["api_contract"] == "comfyui-tts-audio-suite-v1"
    assert version.metadata["prompt_id"] == "prompt-manifest"
    assert version.metadata["resource_id"] == "hero-main"
    assert version.audio_path is not None
    assert Path(version.audio_path).read_bytes() == generated
