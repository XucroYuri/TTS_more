import json
from threading import Barrier
from pathlib import Path

import httpx

from app.adapters.base import SynthesisRequest
from app.models import EngineName, ScriptLine, TTSServiceEndpoint
from app.services import ServiceRegistry, ServiceRouter, build_load_signature, _health_timeout_seconds, build_service_client, _gpt_sovits_gradio_logs_candidates, _slugish


class ReadyClient:
    def __init__(self, endpoint: TTSServiceEndpoint, ready: bool = True) -> None:
        self.endpoint = endpoint
        self.ready = ready

    def health(self) -> dict:
        return {"engine": self.endpoint.engine.value, "ready": self.ready}


class BarrierClient(ReadyClient):
    def __init__(self, endpoint: TTSServiceEndpoint, barrier: Barrier) -> None:
        super().__init__(endpoint)
        self.barrier = barrier

    def health(self) -> dict:
        self.barrier.wait(timeout=1)
        return {"engine": self.endpoint.engine.value, "ready": True}


def test_registry_loads_services_json(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "remote-gpt",
    "engine": "gpt-sovits",
    "base_url": "http://192.0.2.20:9880",
    "mode": "external",
    "resource_group": "remote-a-gpu-0",
    "priority": 5,
    "capabilities": ["tts", "gpt-weights"]
  }
]
""",
        encoding="utf-8",
    )

    registry = ServiceRegistry.load(services_path)

    assert registry.get("remote-gpt").base_url == "http://192.0.2.20:9880"
    assert registry.get("remote-gpt").mode == "external"
    assert registry.get("remote-gpt").resource_group == "remote-a-gpu-0"


def test_registry_default_local_services_share_one_gpu() -> None:
    registry = ServiceRegistry.default_local(repo_root=Path("repo"))

    assert {service.service_id for service in registry.services} == {
        "local-gpt-sovits",
        "local-indextts",
        "local-cosyvoice",
    }
    assert {service.resource_group for service in registry.services} == {"local-gpu-0"}
    assert all(service.capacity == 1 for service in registry.services)
    assert all(service.base_url.startswith("http://") for service in registry.services)
    assert all(service.service_kind == "tts" for service in registry.services)
    assert all(service.network_scope == "localhost" for service in registry.services)
    cosyvoice = registry.get("local-cosyvoice")
    assert cosyvoice.enabled is False
    assert cosyvoice.provider_type.value == "cosyvoice"
    assert cosyvoice.api_contract == "cosyvoice-http-v1"


def test_registry_keeps_external_vibevoice_only_as_generic_http() -> None:
    service = TTSServiceEndpoint(
        service_id="studio-vibevoice",
        provider_type="generic-http",
        api_contract="tts-more-v1",
        base_url="http://192.0.2.50:9882",
        mode="external",
        network_scope="lan",
        capabilities=["tts", "legacy_vibevoice"],
    )

    registry = ServiceRegistry([service])

    assert registry.get("studio-vibevoice").provider_type.value == "generic-http"
    assert registry.get("studio-vibevoice").mode == "external"
    assert registry.get("studio-vibevoice").network_scope == "lan"


def test_health_timeout_is_short_and_configurable(monkeypatch) -> None:
    monkeypatch.delenv("TTS_MORE_HEALTH_TIMEOUT_SECONDS", raising=False)
    assert _health_timeout_seconds() == 0.75

    monkeypatch.setenv("TTS_MORE_HEALTH_TIMEOUT_SECONDS", "3.5")
    assert _health_timeout_seconds() == 3.5

    monkeypatch.setenv("TTS_MORE_HEALTH_TIMEOUT_SECONDS", "30")
    assert _health_timeout_seconds() == 10.0

    monkeypatch.setenv("TTS_MORE_HEALTH_TIMEOUT_SECONDS", "invalid")
    assert _health_timeout_seconds() == 0.75


def test_gradio_webui_endpoint_is_reachable_and_routable_with_bridge(tmp_path: Path) -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-indextts2-gradio",
        engine=EngineName.INDEX_TTS,
        provider_type="indextts",
        api_contract="gradio-indextts2-webui",
        base_url="http://192.0.2.166:7860",
        mode="external",
        network_scope="lan",
        managed=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://192.0.2.166:7860/config":
            return httpx.Response(
                200,
                json={
                    "title": "IndexTTS Demo",
                    "version": "5.38.0",
                    "api_prefix": "/gradio_api",
                    "dependencies": [{"api_name": "gen_single"}],
                },
            )
        if str(request.url) == "http://192.0.2.166:7860/gradio_api/api/gen_single":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["data"][0] == "与音色参考音频相同"
            assert payload["data"][1] == "ref.wav"
            assert payload["data"][2] == "你好"
            return httpx.Response(200, json={"data": [{"visible": True, "value": {"url": "/file=/tmp/generated.wav", "orig_name": "generated.wav"}, "__type__": "update"}]})
        if str(request.url) == "http://192.0.2.166:7860/file=/tmp/generated.wav":
            return httpx.Response(200, content=b"RIFFfake-wav")
        return httpx.Response(404, json={"detail": str(request.url)})

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    health = client.health()

    assert health["reachable"] is True
    assert health["ready"] is True
    assert health["status"] == "ready"
    assert health["expected_api_names"] == ["gen_single"]

    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="xiao-pin", text="你好"),
            profile="xiao-pin-index",
            output_path=tmp_path / "line.wav",
            parameters={"voice": "ref.wav", "emotion_mode": "same_as_voice"},
        )
    )

    assert result.audio_path.read_bytes() == b"RIFFfake-wav"
    assert result.metadata["api_contract"] == "gradio-indextts2-webui"


def test_gradio_endpoint_missing_required_api_is_blocked() -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-indextts2-gradio",
        engine=EngineName.INDEX_TTS,
        provider_type="indextts",
        api_contract="gradio-indextts2-webui",
        base_url="http://192.0.2.166:7860",
        mode="external",
        network_scope="lan",
        managed=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "title": "IndexTTS Demo",
                "version": "5.38.0",
                "api_prefix": "/gradio_api",
                "dependencies": [{"api_name": "load_example"}],
            },
        )

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    health = client.health()

    assert health["reachable"] is True
    assert health["ready"] is False
    assert health["status"] == "unsupported gradio app"
    assert health["expected_api_names"] == ["gen_single"]


def test_gradio_config_timeout_is_partial_not_ready() -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-gpt-gradio",
        engine=EngineName.GPT_SOVITS,
        provider_type="gpt-sovits",
        api_contract="gradio-gpt-sovits-webui",
        base_url="http://192.0.2.166:9872",
        mode="external",
        network_scope="lan",
        managed=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("config timed out", request=request)

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    health = client.health()

    assert health["ready"] is False
    assert health["state"] == "partial"
    assert health["severity"] == "attention"
    assert health["port_reachable"] is True
    assert health["config_ok"] is False
    assert "timed out" in health["error"]


def test_indextts_gradio_uses_example_voice_when_binding_only_has_example_index(tmp_path: Path) -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-indextts2-gradio",
        engine=EngineName.INDEX_TTS,
        provider_type="indextts",
        api_contract="gradio-indextts2-webui",
        base_url="http://192.0.2.166:7860",
        mode="external",
        network_scope="lan",
        managed=False,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://192.0.2.166:7860/config":
            return httpx.Response(200, json={"api_prefix": "/gradio_api", "dependencies": [{"api_name": "gen_single"}, {"api_name": "load_example"}]})
        if str(request.url) == "http://192.0.2.166:7860/gradio_api/api/load_example":
            calls.append("load_example")
            assert json.loads(request.content.decode("utf-8"))["data"] == [2]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"value": {"path": "C:/tmp/voice_03.wav", "url": "http://192.0.2.166:7860/gradio_api/file=C:/tmp/voice_03.wav"}},
                        {"value": "与音色参考音频相同"},
                        {"value": "示例文本"},
                        {"value": None},
                        {"value": 1.0},
                        {"value": ""},
                        *[{"value": 0.0} for _ in range(8)],
                    ]
                },
            )
        if str(request.url) == "http://192.0.2.166:7860/gradio_api/api/gen_single":
            calls.append("gen_single")
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["data"][1]["path"] == "C:/tmp/voice_03.wav"
            assert payload["data"][2] == "当前台词"
            return httpx.Response(200, json={"data": [{"url": "/file=/tmp/generated.wav"}]})
        if str(request.url) == "http://192.0.2.166:7860/file=/tmp/generated.wav":
            return httpx.Response(200, content=b"RIFFexample")
        return httpx.Response(404)

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="xiao-pin", text="当前台词"),
            profile="xiao-pin-index",
            output_path=tmp_path / "example.wav",
            parameters={"gradio_example_index": 2},
        )
    )

    assert calls == ["load_example", "gen_single"]
    assert result.audio_path.read_bytes() == b"RIFFexample"


def test_indextts_gradio_uses_queue_call_when_dependency_is_queued(tmp_path: Path) -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-indextts2-gradio",
        engine=EngineName.INDEX_TTS,
        provider_type="indextts",
        api_contract="gradio-indextts2-webui",
        base_url="http://192.0.2.166:7860",
        mode="external",
        network_scope="lan",
        managed=False,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://192.0.2.166:7860/config":
            return httpx.Response(200, json={"api_prefix": "/gradio_api", "dependencies": [{"api_name": "gen_single", "queue": True}]})
        if url == "http://192.0.2.166:7860/gradio_api/call/gen_single":
            calls.append("call")
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["data"][0] == "与音色参考音频相同"
            assert payload["data"][1] == "ref.wav"
            assert payload["data"][3] is None
            return httpx.Response(200, json={"event_id": "evt-1"})
        if url == "http://192.0.2.166:7860/gradio_api/call/gen_single/evt-1":
            calls.append("stream")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'event: complete\ndata: [{"url": "/file=/tmp/generated.wav"}]\n\n',
            )
        if url == "http://192.0.2.166:7860/file=/tmp/generated.wav":
            return httpx.Response(200, content=b"RIFFqueued")
        return httpx.Response(404, json={"detail": url})

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="xiao-pin", text="当前台词"),
            profile="xiao-pin-index",
            output_path=tmp_path / "queued.wav",
            parameters={"voice": "ref.wav", "emotion_mode": "same_as_voice"},
        )
    )

    assert calls == ["call", "stream"]
    assert result.audio_path.read_bytes() == b"RIFFqueued"


def test_gpt_sovits_gradio_uses_selected_reference_audio_update(tmp_path: Path) -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-gpt-gradio",
        engine=EngineName.GPT_SOVITS,
        provider_type="gpt-sovits",
        api_contract="gradio-gpt-sovits-webui",
        base_url="http://192.0.2.166:9872",
        mode="external",
        network_scope="lan",
        managed=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://192.0.2.166:9872/config":
            return httpx.Response(200, json={"dependencies": [{"api_name": "get_tts_wav"}, {"api_name": "on_select_ref_audio"}]})
        if str(request.url) == "http://192.0.2.166:9872/api/on_select_ref_audio":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"visible": True, "value": {"path": "C:/tmp/ref.wav", "url": "http://192.0.2.166:9872/file=C:/tmp/ref.wav"}, "__type__": "update"},
                        {"value": "参考文本"},
                    ]
                },
            )
        if str(request.url) == "http://192.0.2.166:9872/api/get_tts_wav":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["data"][0]["path"] == "C:/tmp/ref.wav"
            assert payload["data"][1] == "参考文本"
            assert payload["data"][3] == "马上过去"
            return httpx.Response(200, json={"data": [{"value": {"url": "/file=/tmp/gpt.wav"}}]})
        if str(request.url) == "http://192.0.2.166:9872/file=/tmp/gpt.wav":
            return httpx.Response(200, content=b"RIFFgpt")
        return httpx.Response(404)

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="xiao-pin", text="马上过去"),
            profile="xiao-pin-gpt",
            output_path=tmp_path / "gpt.wav",
            parameters={"ref_audio_choice": "ref-choice", "character_filter": "小品"},
        )
    )

    assert result.audio_path.read_bytes() == b"RIFFgpt"


def test_gpt_sovits_gradio_index_preserves_full_logs_names_from_choices() -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-gpt-gradio",
        engine=EngineName.GPT_SOVITS,
        provider_type="gpt-sovits",
        api_contract="gradio-gpt-sovits-webui",
        base_url="http://192.0.2.166:9872",
        mode="external",
        network_scope="lan",
        managed=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://192.0.2.166:9872/config":
            return httpx.Response(
                200,
                json={
                    "components": [
                        {"id": 1, "props": {"choices": ["全部", "demo-hero-logs"]}},
                        {"id": 2, "props": {"choices": ["demo-hero-logs-e50.ckpt"]}},
                        {"id": 3, "props": {"choices": ["demo-hero-logs_e24_s264.pth"]}},
                        {"id": 4, "props": {"choices": ["参考音频/demo-hero-logs/ref.wav"]}},
                    ],
                    "dependencies": [
                        {"api_name": "get_tts_wav"},
                        {"api_name": "update_model_choices", "outputs": [1, 2, 3]},
                        {"api_name": "refresh_ref_audio_choices", "outputs": [4]},
                    ],
                },
            )
        return httpx.Response(404)

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    index = client.gradio_index()

    by_logs = {candidate["logs_name"]: candidate for candidate in index["candidates"]}
    assert "demo-hero-logs" in by_logs
    assert by_logs["demo-hero-logs"]["recommended_gpt_weights_path"] == "demo-hero-logs-e50.ckpt"
    assert by_logs["demo-hero-logs"]["recommended_sovits_weights_path"] == "demo-hero-logs_e24_s264.pth"


def test_gpt_sovits_gradio_uploads_local_reference_audio(tmp_path: Path) -> None:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFref")
    endpoint = TTSServiceEndpoint(
        service_id="lan-gpt-gradio",
        engine=EngineName.GPT_SOVITS,
        provider_type="gpt-sovits",
        api_contract="gradio-gpt-sovits-webui",
        base_url="http://192.0.2.166:9872",
        mode="external",
        network_scope="lan",
        managed=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://192.0.2.166:9872/config":
            return httpx.Response(200, json={"dependencies": [{"api_name": "get_tts_wav"}]})
        if str(request.url) == "http://192.0.2.166:9872/upload":
            assert request.method == "POST"
            return httpx.Response(200, json=["D:/gradio/tmp/ref.wav"])
        if str(request.url) == "http://192.0.2.166:9872/api/get_tts_wav":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["data"][0]["path"] == "D:/gradio/tmp/ref.wav"
            assert payload["data"][0]["orig_name"] == "ref.wav"
            return httpx.Response(200, json={"data": [{"value": {"url": "/file=/tmp/gpt.wav"}}]})
        if str(request.url) == "http://192.0.2.166:9872/file=/tmp/gpt.wav":
            return httpx.Response(200, content=b"RIFFgpt")
        return httpx.Response(404)

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="xiao-pin", text="马上过去"),
            profile="xiao-pin-gpt",
            output_path=tmp_path / "gpt.wav",
            parameters={"ref_audio_path": str(ref), "prompt_text": "参考文本"},
        )
    )

    assert result.audio_path.read_bytes() == b"RIFFgpt"


def test_gradio_upload_prefers_api_prefix_when_present(tmp_path: Path) -> None:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFref")
    endpoint = TTSServiceEndpoint(
        service_id="lan-indextts2-gradio",
        engine=EngineName.INDEX_TTS,
        provider_type="indextts",
        api_contract="gradio-indextts2-webui",
        base_url="http://192.0.2.166:7860",
        mode="external",
        network_scope="lan",
        managed=False,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://192.0.2.166:7860/config":
            return httpx.Response(200, json={"api_prefix": "/gradio_api", "dependencies": [{"api_name": "gen_single"}]})
        if url == "http://192.0.2.166:7860/upload":
            calls.append("bare-upload")
            return httpx.Response(404)
        if url == "http://192.0.2.166:7860/gradio_api/upload":
            calls.append("prefixed-upload")
            return httpx.Response(200, json=["D:/gradio/tmp/ref.wav"])
        if url == "http://192.0.2.166:7860/gradio_api/api/gen_single":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["data"][1]["path"] == "D:/gradio/tmp/ref.wav"
            assert "url" not in payload["data"][1]
            return httpx.Response(200, json={"data": [{"url": "/file=/tmp/generated.wav"}]})
        if url == "http://192.0.2.166:7860/file=/tmp/generated.wav":
            return httpx.Response(200, content=b"RIFFprefixed")
        return httpx.Response(404)

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="xiao-pin", text="当前台词"),
            profile="xiao-pin-index",
            output_path=tmp_path / "prefixed.wav",
            parameters={"voice": str(ref), "emotion_mode": "same_as_voice"},
        )
    )

    assert calls == ["prefixed-upload"]
    assert result.audio_path.read_bytes() == b"RIFFprefixed"


def test_gradio_synthesis_uses_operation_timeout_for_config_fetch(tmp_path: Path) -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-indextts2-gradio",
        engine=EngineName.INDEX_TTS,
        provider_type="indextts",
        api_contract="gradio-indextts2-webui",
        base_url="http://192.0.2.166:7860",
        mode="external",
        network_scope="lan",
        managed=False,
    )
    config_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://192.0.2.166:7860/config":
            timeout = request.extensions.get("timeout") or {}
            config_timeouts.append(timeout.get("read"))
            return httpx.Response(200, json={"api_prefix": "/gradio_api", "dependencies": [{"api_name": "gen_single"}]})
        if url == "http://192.0.2.166:7860/gradio_api/api/gen_single":
            return httpx.Response(200, json={"data": [{"url": "/file=/tmp/generated.wav"}]})
        if url == "http://192.0.2.166:7860/file=/tmp/generated.wav":
            return httpx.Response(200, content=b"RIFFtimeout")
        return httpx.Response(404)

    client = build_service_client(endpoint, transport=httpx.MockTransport(handler))

    result = client.synthesize(
        SynthesisRequest(
            line=ScriptLine(id="l1", character_id="xiao-pin", text="当前台词"),
            profile="xiao-pin-index",
            output_path=tmp_path / "timeout.wav",
            parameters={"voice": "ref.wav", "emotion_mode": "same_as_voice", "timeout_seconds": 123},
        )
    )

    assert result.audio_path.read_bytes() == b"RIFFtimeout"
    assert config_timeouts[0] == 123


def test_gpt_sovits_load_signature_covers_weights_reference_and_prompt() -> None:
    endpoint = TTSServiceEndpoint(
        service_id="lan-gpt-gradio",
        engine=EngineName.GPT_SOVITS,
        provider_type="gpt-sovits",
        api_contract="gradio-gpt-sovits-webui",
        base_url="http://192.0.2.166:9872",
    )

    signature = build_load_signature(
        endpoint,
        {
            "logs_name": "demo-hero-logs",
            "gpt_weights_path": "GPT_weights_v2ProPlus/demo-hero-e50.ckpt",
            "sovits_weights_path": "SoVITS_weights_v2ProPlus/demo-hero_e24_s264.pth",
            "ref_audio_path": "logs/demo-hero/5-wav32k/ref.wav",
            "prompt_text": "不好！",
            "prompt_lang": "zh",
            "text_lang": "zh",
        },
    )

    assert signature == (
        "service_id=lan-gpt-gradio|logs_name=demo-hero-logs|"
        "gpt_weights_path=GPT_weights_v2ProPlus/demo-hero-e50.ckpt|"
        "sovits_weights_path=SoVITS_weights_v2ProPlus/demo-hero_e24_s264.pth|"
        "ref_audio_path=logs/demo-hero/5-wav32k/ref.wav|"
        "prompt_text=不好！|prompt_lang=zh|text_lang=zh"
    )


def test_router_checks_service_health_concurrently() -> None:
    first = TTSServiceEndpoint(
        service_id="first-gpt",
        engine=EngineName.GPT_SOVITS,
        base_url="mock://first",
        resource_group="gpu-a",
    )
    second = TTSServiceEndpoint(
        service_id="second-vibe",
        engine=EngineName.VIBEVOICE,
        base_url="mock://second",
        resource_group="gpu-b",
    )
    barrier = Barrier(2)
    router = ServiceRouter(
        ServiceRegistry([first, second]),
        clients={"first-gpt": BarrierClient(first, barrier), "second-vibe": BarrierClient(second, barrier)},
    )

    health = router.health()

    assert [service["service_id"] for service in health] == ["first-gpt", "second-vibe"]
    assert all(service["ready"] for service in health)


def test_router_prefers_explicit_ready_service() -> None:
    slow = TTSServiceEndpoint(
        service_id="slow-gpt",
        engine=EngineName.GPT_SOVITS,
        base_url="mock://slow",
        priority=50,
        resource_group="gpu-a",
    )
    fast = TTSServiceEndpoint(
        service_id="fast-gpt",
        engine=EngineName.GPT_SOVITS,
        base_url="mock://fast",
        priority=1,
        resource_group="gpu-b",
    )
    router = ServiceRouter(
        ServiceRegistry([slow, fast]),
        clients={"slow-gpt": ReadyClient(slow), "fast-gpt": ReadyClient(fast)},
    )

    route = router.resolve(EngineName.GPT_SOVITS, service_id="slow-gpt")

    assert route.endpoint.service_id == "slow-gpt"


def test_router_falls_back_to_first_ready_service() -> None:
    offline = TTSServiceEndpoint(
        service_id="offline-gpt",
        engine=EngineName.GPT_SOVITS,
        base_url="mock://offline",
        priority=1,
        resource_group="gpu-a",
    )
    online = TTSServiceEndpoint(
        service_id="online-gpt",
        engine=EngineName.GPT_SOVITS,
        base_url="mock://online",
        priority=10,
        resource_group="gpu-b",
    )
    router = ServiceRouter(
        ServiceRegistry([offline, online]),
        clients={"offline-gpt": ReadyClient(offline, ready=False), "online-gpt": ReadyClient(online)},
    )

    route = router.resolve(EngineName.GPT_SOVITS, fallback_service_ids=["offline-gpt", "online-gpt"])

    assert route.endpoint.service_id == "online-gpt"


def test_slugish_keeps_distinct_chinese_names_separate() -> None:
    """Regression: _slugish previously mapped all Chinese names not in a small
    fallback dict to the same key 'logs', collapsing distinct characters into
    one candidate. Chinese characters must be preserved so each role groups
    independently."""
    assert _slugish("主角") != _slugish("导师")
    assert _slugish("主角") == "主角"
    assert _slugish("导师") == "导师"
    assert _slugish("English-Name") == "english-name"


def test_gradio_logs_candidates_extracts_weights_by_label_without_api_name() -> None:
    """GPT-SoVITS WebUI does not define api_name on its refresh button, so the
    dependency-based component lookup returns nothing. Candidates must still be
    extracted by matching dropdown labels ('GPT模型列表' / 'SoVITS模型列表')."""
    config = {
        "components": [
            {
                "id": 0,
                "type": "dropdown",
                "props": {
                    "label": "GPT模型列表",
                    "choices": [
                        "GPT_weights/主角-e20.ckpt",
                        "GPT_weights/导师-e15.ckpt",
                    ],
                    "value": "GPT_weights/主角-e20.ckpt",
                },
            },
            {
                "id": 1,
                "type": "dropdown",
                "props": {
                    "label": "SoVITS模型列表",
                    "choices": [
                        "SoVITS_weights/主角-e20.pth",
                        "SoVITS_weights/导师-e15.pth",
                    ],
                    "value": "SoVITS_weights/主角-e20.pth",
                },
            },
        ],
        "dependencies": [],
    }

    candidates = _gpt_sovits_gradio_logs_candidates("lan-gpt-sovits", config)

    assert len(candidates) == 2
    by_name = {c["logs_name"]: c for c in candidates}

    assert "主角" in by_name
    assert "导师" in by_name

    hero = by_name["主角"]
    assert hero["recommended_gpt_weights_path"] == "GPT_weights/主角-e20.ckpt"
    assert hero["recommended_sovits_weights_path"] == "SoVITS_weights/主角-e20.pth"
    assert len(hero["gpt_weights"]) == 1
    assert len(hero["sovits_weights"]) == 1
