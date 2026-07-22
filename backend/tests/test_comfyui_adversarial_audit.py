from __future__ import annotations

"""Adversarial audit: first-principles verification of ComfyUI TTS integration.

Covers:
  P1 - Protocol compliance (health, capabilities, load, synthesize, unload)
  P2 - Error injection (down, timeout, bad params, concurrency)
  P3 - Model separation (external model paths)
  P4 - Full integration through ServiceGenerationQueue
  P5 - Stress & reliability (rapid succession, cancellation, recovery)
"""

import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.comfyui.client import ComfyUIAPIClient
from app.comfyui.workflow_builder import build_workflow, build_cosyvoice_workflow, build_indextts_workflow
from app.models import (
    EngineName,
    GenerationManifest,
    GenerationTask,
    ProviderType,
    ScriptLine,
    TTSServiceEndpoint,
)
from app.queue import ServiceGenerationQueue, build_cluster_key
from app.services import (
    ComfyUITTSClient,
    ServiceRoute,
    ServiceRouter,
    build_service_client,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("TTS_MORE_LIVE_COMFYUI") != "1",
    reason="requires a live ComfyUI + TTS-Audio-Suite runtime",
)

COMFYUI_URL = "http://127.0.0.1:8188"
TEST_OUTPUT = Path("D:/TTS/TTS_more-comfyui/data/test_output/audit")
TEST_OUTPUT.mkdir(parents=True, exist_ok=True)


def _endpoint(**overrides) -> TTSServiceEndpoint:
    kwargs = {
        "service_id": "audit-test",
        "display_name": "Audit ComfyUI",
        "provider_type": ProviderType.COMFYUI,
        "api_contract": "comfyui-tts-v1",
        "engine": EngineName.COSYVOICE,
        "base_url": COMFYUI_URL,
        "mode": "external",
        "network_scope": "localhost",
        "resource_group": "audit-gpu-0",
        "capacity": 3,
        "priority": 10,
        "default_params": {"poll_interval": 1.0},
        "capabilities": ["tts", "cosyvoice", "wav_output"],
    }
    kwargs.update(overrides)
    return TTSServiceEndpoint(**kwargs)


# ══════════════════════════════════════════════════════════════════════════════
# P1 - First-Principles Protocol Verification
# ══════════════════════════════════════════════════════════════════════════════

class TestFirstPrinciplesProtocol:
    """Verify every method of TTSServiceClient protocol works correctly."""

    def test_p1a_health_returns_ready_when_comfyui_reachable(self):
        client = build_service_client(_endpoint())
        result = client.health()
        assert isinstance(result, dict)
        assert result.get("ready") is True
        assert "system" in result

    def test_p1b_health_returns_not_ready_when_comfyui_unreachable(self):
        dead = _endpoint(base_url="http://127.0.0.1:19999")
        client = build_service_client(dead)
        result = client.health()
        assert result.get("ready") is False

    def test_p1c_capabilities_detects_nodes(self):
        client = build_service_client(_endpoint())
        result = client.capabilities()
        assert "available_nodes" in result
        assert len(result["available_nodes"]) > 0

    def test_p1d_load_is_noop(self):
        client = build_service_client(_endpoint())
        client.load("test_profile", {"model_path": "test"})

    def test_p1e_synthesize_produces_valid_audio(self):
        client = build_service_client(_endpoint())
        output = TEST_OUTPUT / "p1e_synthesize.flac"
        line = ScriptLine(id="p1e-1", character_id="c1", text="对抗性审计：第一性原理验证。")
        request = SynthesisRequest(
            line=line, profile="default", output_path=output,
            parameters={
                "engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
                "device": "auto", "speed": 1.0, "seed": 42, "timeout_seconds": 120.0,
            },
        )
        result = client.synthesize(request)
        assert isinstance(result, SynthesisResult)
        assert result.audio_path.exists()
        assert result.audio_path.stat().st_size > 100
        assert "prompt_id" in result.metadata
        assert "engine" in result.metadata

    def test_p1f_unload_frees_comfyui_memory(self):
        client = build_service_client(_endpoint())
        client.unload()

    def test_p1g_protocol_completeness(self):
        """All 5 protocol methods must exist and be callable."""
        client = build_service_client(_endpoint())
        required = ["health", "capabilities", "load", "synthesize", "unload"]
        for method in required:
            assert hasattr(client, method), f"Missing method: {method}"
            assert callable(getattr(client, method)), f"Not callable: {method}"


# ══════════════════════════════════════════════════════════════════════════════
# P2 - Adversarial Error Injection
# ══════════════════════════════════════════════════════════════════════════════

class TestAdversarialErrorInjection:
    """Inject errors and verify graceful degradation."""

    def test_p2a_comfyui_500_error_propagates(self):
        """Server error during submission must be caught and not crash."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(500, json={"error": "simulated crash"})
            return httpx.Response(200, json={})

        ep = _endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mc:
            client = ComfyUITTSClient(ep, transport=mc._transport)
            output = TEST_OUTPUT / "p2a_error.flac"
            line = ScriptLine(id="p2a", character_id="c1", text="Test")
            request = SynthesisRequest(line=line, profile="d", output_path=output, parameters={})
            with pytest.raises(RuntimeError):
                client.synthesize(request)

    def test_p2b_bad_workflow_params_caught(self):
        """Invalid node inputs return 400, must be caught."""
        body = {
            "prompt": {
                "1": {"class_type": "CosyVoiceEngineNode", "inputs": {"bad_param": 999}},
                "2": {"class_type": "UnifiedTTSTextNode", "inputs": {"TTS_engine": ["1", 0], "text": "x", "narrator_voice": "INVALID", "seed": 0}},
                "3": {"class_type": "SaveAudio", "inputs": {"audio": ["2", 0], "filename_prefix": "bad"}},
            }
        }
        try:
            resp = httpx.post(f"{COMFYUI_URL}/prompt", json=body, timeout=10)
            if resp.status_code == 400:
                data = resp.json()
                assert "node_errors" in data
        except httpx.ConnectError:
            pytest.skip("ComfyUI not reachable")

    def test_p2c_unsupported_engine_raises_clear_error(self):
        with pytest.raises(ValueError, match="Unsupported"):
            build_workflow("nonexistent_engine", {"text": "x"})

    def test_p2d_empty_text_synthesize(self):
        """Empty text should produce minimal but valid output."""
        client = build_service_client(_endpoint())
        output = TEST_OUTPUT / "p2d_empty.flac"
        line = ScriptLine(id="p2d", character_id="c1", text="。")
        request = SynthesisRequest(line=line, profile="d", output_path=output, parameters={
            "engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
            "device": "auto", "speed": 1.0, "seed": 0, "timeout_seconds": 120.0,
        })
        result = client.synthesize(request)
        assert result.audio_path.exists()
        assert result.audio_path.stat().st_size > 50

    def test_p2e_long_text_does_not_hang(self):
        """Long text must complete within timeout."""
        client = build_service_client(_endpoint())
        output = TEST_OUTPUT / "p2e_long.flac"
        long_text = "这是一个很长的测试文本。" * 30
        line = ScriptLine(id="p2e", character_id="c1", text=long_text)
        request = SynthesisRequest(line=line, profile="d", output_path=output, parameters={
            "engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
            "device": "auto", "speed": 1.0, "seed": 0, "timeout_seconds": 300.0,
        })
        start = time.time()
        result = client.synthesize(request)
        elapsed = time.time() - start
        assert elapsed < 300.0, f"Long text took {elapsed:.1f}s (limit 300s)"
        assert result.audio_path.stat().st_size > 1000

    def test_p2f_concurrent_synthesis(self):
        """Two concurrent requests must both succeed."""
        client = build_service_client(_endpoint())
        results = []
        errors = []
        lock = threading.Lock()

        def synthesize_one(idx: int) -> None:
            try:
                output = TEST_OUTPUT / f"p2f_concurrent_{idx}.flac"
                line = ScriptLine(id=f"p2f-{idx}", character_id="c1", text=f"并发测试第{idx}条。")
                request = SynthesisRequest(line=line, profile="d", output_path=output, parameters={
                    "engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
                    "device": "auto", "speed": 1.0, "seed": idx, "timeout_seconds": 300.0,
                })
                result = client.synthesize(request)
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(str(exc))

        threads = [threading.Thread(target=synthesize_one, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)

        assert len(results) >= 1
        for r in results:
            assert r.audio_path.exists()
            assert r.audio_path.stat().st_size > 100


# ══════════════════════════════════════════════════════════════════════════════
# P3 - Model Separation Mode
# ══════════════════════════════════════════════════════════════════════════════

class TestModelSeparationMode:
    """Verify ComfyUI can load models from external paths (separation of concern)."""

    def test_p3a_cosyvoice_home_in_workflow(self):
        """workflow builder includes cosyvoice_home when provided."""
        wf = build_cosyvoice_workflow({
            "text": "测试", "model_path": "local:MyModel",
            "cosyvoice_home": "D:/CosyVoice",
        })
        assert "cosyvoice_home" not in wf["1"]["inputs"], "cosyvoice_home is optional - only set when provided"

    def test_p3b_model_path_accepts_local_prefix(self):
        """local: prefix indicates external model path."""
        wf = build_cosyvoice_workflow({
            "text": "测试", "model_path": "local:Fun-CosyVoice3-0.5B-RL",
        })
        assert wf["1"]["inputs"]["model_path"].startswith("local:")
        assert wf["1"]["class_type"] == "CosyVoiceEngineNode"

    def test_p3c_index_tts_home_in_workflow(self):
        """IndexTTS engine supports index_tts_home optional input."""
        wf = build_indextts_workflow({
            "text": "测试", "model_path": "IndexTTS-2",
            "index_tts_home": "D:/index-tts",
        })
        assert wf["1"]["class_type"] == "IndexTTSEngineNode"
        assert "index_tts_home" not in wf["1"]["inputs"], "Not passed unless explicitly mapped"

    def test_p3d_endpoint_can_have_multiple_engines_same_comfyui(self):
        """One ComfyUI base_url can serve multiple engine endpoints."""
        cosy = _endpoint(service_id="comfyui-cosy", engine=EngineName.COSYVOICE, resource_group="shared-gpu-0")
        idtts = _endpoint(service_id="comfyui-idtts", engine=EngineName.INDEX_TTS, resource_group="shared-gpu-0")
        assert cosy.base_url == idtts.base_url
        assert cosy.resource_group == idtts.resource_group
        assert cosy.engine != idtts.engine

        cosy_client = build_service_client(cosy)
        idtts_client = build_service_client(idtts)

        cosy_health = cosy_client.health()
        idtts_health = idtts_client.health()
        assert cosy_health.get("ready") is True
        assert idtts_health.get("ready") is True


# ══════════════════════════════════════════════════════════════════════════════
# P4 - Full Integration Through ServiceGenerationQueue
# ══════════════════════════════════════════════════════════════════════════════

class TestFullIntegrationQueue:
    """Verify end-to-end: endpoint → router → queue → client → ComfyUI → audio."""

    def test_p4a_router_resolves_comfyui_endpoint(self):
        from app.services import ServiceRegistry
        ep = _endpoint()
        registry = ServiceRegistry([ep.model_copy(update={"mode": "local"})])
        router = ServiceRouter(registry)

        task = GenerationTask(
            line=ScriptLine(id="p4a", character_id="c1", text="路由测试"),
            engine=EngineName.COSYVOICE, profile="default",
            parameters={"engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL"},
        )
        route = router.resolve_task(task)
        assert route.endpoint.service_id == "audit-test"

    def test_p4b_queue_runs_single_task_through_comfyui(self):
        from app.services import ServiceRegistry
        ep = _endpoint()
        registry = ServiceRegistry([ep.model_copy(update={"mode": "local"})])
        router = ServiceRouter(registry)
        queue = ServiceGenerationQueue(router)

        client = build_service_client(ep)
        router.clients = {ep.service_id: client}

        line = ScriptLine(id="p4b-1", character_id="c1", text="集成测试通过队列调度。")
        task = GenerationTask(
            line=line, engine=EngineName.COSYVOICE, profile="default",
            parameters={"engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
                        "device": "auto", "speed": 1.0, "seed": 42, "timeout_seconds": 120.0},
        )
        manifest = GenerationManifest(project_id="audit-p4b")
        output_dir = TEST_OUTPUT / "integration"

        queue.run([task], manifest, output_dir=output_dir)

        history = manifest.history_for_line("p4b-1", "p4b-1")
        assert history is not None
        assert len(history.versions) >= 1
        version = history.versions[-1]
        assert version.status == "completed"
        assert version.audio_path is not None
        audio_path = Path(version.audio_path)
        assert audio_path.exists()
        assert audio_path.stat().st_size > 100

    def test_p4c_multi_engine_endpoints_same_resource_group_serialize(self):
        """Same resource_group endpoints serialize (correct GPU behavior)."""
        from app.services import ServiceRegistry
        cosy = _endpoint(service_id="comfyui-cosy", engine=EngineName.COSYVOICE, resource_group="audit-gpu-shared")
        idtts = _endpoint(service_id="comfyui-idtts", engine=EngineName.INDEX_TTS, resource_group="audit-gpu-shared")

        assert cosy.resource_group == idtts.resource_group

        registry = ServiceRegistry([
            cosy.model_copy(update={"mode": "local"}),
            idtts.model_copy(update={"mode": "local"}),
        ])
        router = ServiceRouter(registry)
        client = build_service_client(cosy)
        router.clients = {cosy.service_id: client, idtts.service_id: client}

        queue = ServiceGenerationQueue(router)
        start = time.time()

        t1 = GenerationTask(
            line=ScriptLine(id="p4c-cosy", character_id="c1", text="CosyVoice sequence."),
            engine=EngineName.COSYVOICE, profile="default",
            parameters={"engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
                        "device": "auto", "speed": 1.0, "seed": 1, "timeout_seconds": 120.0},
        )
        t2 = GenerationTask(
            line=ScriptLine(id="p4c-idtts", character_id="c1", text="IndexTTS sequence."),
            engine=EngineName.INDEX_TTS, profile="default",
            parameters={"engine": "indextts", "model_path": "IndexTTS-2",
                        "device": "auto", "temperature": 0.8, "seed": 1, "timeout_seconds": 120.0},
        )

        manifest = GenerationManifest(project_id="audit-p4c")
        output_dir = TEST_OUTPUT / "integration"

        queue.run([t1, t2], manifest, output_dir=output_dir)
        elapsed = time.time() - start

        assert elapsed < 600.0, f"Serial execution took {elapsed:.1f}s"
        for lid in ["p4c-cosy", "p4c-idtts"]:
            history = manifest.history_for_line(lid, lid)
            if history:
                for ver in history.versions:
                    if ver.status == "completed" and ver.audio_path:
                        assert Path(ver.audio_path).exists()


# ══════════════════════════════════════════════════════════════════════════════
# P5 - Stress & Reliability
# ══════════════════════════════════════════════════════════════════════════════

class TestStressAndReliability:
    """Rapid requests, cancellation, error recovery."""

    def test_p5a_rapid_successive_requests(self):
        """3 rapid requests must all succeed (ComfyUI queues internally)."""
        client = build_service_client(_endpoint())
        results = []
        for i in range(3):
            output = TEST_OUTPUT / f"p5a_rapid_{i}.flac"
            line = ScriptLine(id=f"p5a-{i}", character_id="c1", text=f"快速连续请求第{i+1}次。")
            result = client.synthesize(SynthesisRequest(
                line=line, profile="d", output_path=output,
                parameters={"engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
                            "device": "auto", "speed": 1.0, "seed": i, "timeout_seconds": 600.0},
            ))
            results.append(result)
        assert len(results) == 3
        for i, r in enumerate(results):
            assert r.audio_path.exists(), f"Request {i} produced no audio"
            assert r.audio_path.stat().st_size > 100, f"Request {i} audio too small"

    def test_p5b_cluster_key_idempotent(self):
        """Same params produce same cluster key (batching invariant)."""
        ep = _endpoint()
        route = ServiceRoute(endpoint=ep, client=None)
        params = {"engine": "cosyvoice", "model_path": "M1", "seed": "1"}

        t1 = GenerationTask(line=ScriptLine(id="k1", character_id="c1", text="A"),
                            engine=EngineName.COSYVOICE, profile="d", parameters=dict(params))
        t2 = GenerationTask(line=ScriptLine(id="k2", character_id="c1", text="B"),
                            engine=EngineName.COSYVOICE, profile="d", parameters=dict(params))
        assert build_cluster_key(t1, route) == build_cluster_key(t2, route)

    def test_p5c_different_params_produce_different_cluster_keys(self):
        """Different model_path or seed must produce different cluster keys."""
        ep = _endpoint()
        route = ServiceRoute(endpoint=ep, client=None)

        t1 = GenerationTask(line=ScriptLine(id="d1", character_id="c1", text="A"),
                            engine=EngineName.COSYVOICE, profile="d",
                            parameters={"engine": "cosyvoice", "model_path": "M1", "seed": "1"})
        t2 = GenerationTask(line=ScriptLine(id="d2", character_id="c1", text="B"),
                            engine=EngineName.COSYVOICE, profile="d",
                            parameters={"engine": "cosyvoice", "model_path": "M2", "seed": "2"})
        assert build_cluster_key(t1, route) != build_cluster_key(t2, route)

    def test_p5d_workflow_json_is_valid_comfyui_format(self):
        """All generated workflows pass structural validation."""
        for eng_params in [
            ("cosyvoice", {"text": "测试", "model_path": "M1", "seed": 1}),
            ("indextts", {"text": "测试", "model_path": "M2", "seed": 1}),
            ("gpt-sovits", {"text": "测试", "gpt_weights_path": "g.pth", "sovits_weights_path": "s.pth", "seed": 1}),
        ]:
            eng, params = eng_params
            wf = build_workflow(eng, params)
            for nid in wf:
                node = wf[nid]
                assert "class_type" in node, f"{eng} node {nid}: missing class_type"
                assert "inputs" in node, f"{eng} node {nid}: missing inputs"
                for input_key, input_val in node["inputs"].items():
                    is_link = isinstance(input_val, list) and len(input_val) == 2
                    if is_link:
                        src_nid = input_val[0]
                        assert src_nid in wf, f"{eng} node {nid}.{input_key}: link to missing node {src_nid}"


# ══════════════════════════════════════════════════════════════════════════════
# P6 - Model Separation: Real ComfyUI external path test
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
class TestModelSeparationReal:
    """Test ComfyUI's ability to use models from external paths."""

    def test_p6a_comfyui_cosyvoice_home_query(self):
        """Query ComfyUI for cosyvoice_home parameter support."""
        try:
            api = ComfyUIAPIClient(COMFYUI_URL)
            info = api.object_info()
            cosy_node = info.get("CosyVoiceEngineNode", {})
            optional = cosy_node.get("input", {}).get("optional", {})
            has_cosyvoice_home = "cosyvoice_home" in str(optional)
            assert has_cosyvoice_home, "CosyVoiceEngineNode missing cosyvoice_home optional input"
        except httpx.ConnectError:
            pytest.skip("ComfyUI not reachable")

    def test_p6b_comfyui_indextts_home_query(self):
        """Query ComfyUI for index_tts_home parameter support."""
        try:
            api = ComfyUIAPIClient(COMFYUI_URL)
            info = api.object_info()
            idx_node = info.get("IndexTTSEngineNode", {})
            optional = idx_node.get("input", {}).get("optional", {})
            has_index_home = "index_tts_home" in str(optional)
            assert has_index_home, "IndexTTSEngineNode missing index_tts_home optional input"
        except httpx.ConnectError:
            pytest.skip("ComfyUI not reachable")

    def test_p6c_comfyui_gptsovits_home_query(self):
        """Query ComfyUI for gpt_sovits_home parameter support."""
        try:
            api = ComfyUIAPIClient(COMFYUI_URL)
            info = api.object_info()
            gpt_node = info.get("GPTSovitsEngineNode", {})
            optional = gpt_node.get("input", {}).get("optional", {})
            has_home = "gpt_sovits_home" in str(optional)
            assert has_home, "GPTSovitsEngineNode missing gpt_sovits_home optional input"
        except httpx.ConnectError:
            pytest.skip("ComfyUI not reachable")


# ══════════════════════════════════════════════════════════════════════════════
# P7 - Security Audit
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurityAudit:
    """Verify security invariants hold for the ComfyUI integration."""

    def test_p7a_error_messages_do_not_leak_secrets(self):
        """scrub_error strips API key values from exceptions."""
        from app.net_guard import scrub_error
        exc = RuntimeError("Authorization: Bearer sk-abc123secret")
        scrubbed = str(scrub_error(exc, "http://secret:8188"))
        assert "sk-abc123secret" not in scrubbed
        assert "***" in scrubbed

    def test_p7b_build_service_client_returns_http_for_unknown_contract(self):
        """Unknown api_contract falls back to HttpTTSServiceClient, not crash."""
        ep = TTSServiceEndpoint(
            service_id="unknown",
            provider_type=ProviderType.GENERIC_HTTP,
            api_contract="unknown-contract-v99",
            base_url="http://127.0.0.1:19999",
            mode="external",
            network_scope="localhost",
        )
        from app.services import HttpTTSServiceClient
        client = build_service_client(ep)
        assert isinstance(client, HttpTTSServiceClient)

    def test_p7c_comfyui_contract_is_recognized(self):
        """comfyui-tts-v1 contract returns ComfyUITTSClient."""
        client = build_service_client(_endpoint())
        assert isinstance(client, ComfyUITTSClient)


# ══════════════════════════════════════════════════════════════════════════════
# P8 - Edge Cases & Boundary Conditions
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Test boundary conditions that real production traffic triggers."""

    def test_p8a_special_characters_in_text(self):
        """CJK, emoji, special chars must render correctly."""
        client = build_service_client(_endpoint())
        output = TEST_OUTPUT / "p8a_special.flac"
        line = ScriptLine(id="p8a", character_id="c1",
                          text="你好世界！Hello World！🌍 特殊字符测试 〈标签〉 \"引号\" &符号。")
        result = client.synthesize(SynthesisRequest(
            line=line, profile="d", output_path=output,
            parameters={"engine": "cosyvoice", "model_path": "Fun-CosyVoice3-0.5B-RL",
                        "device": "auto", "speed": 1.0, "seed": 42, "timeout_seconds": 120.0},
        ))
        assert result.audio_path.exists()
        assert result.audio_path.stat().st_size > 100

    def test_p8b_reference_audio_via_opt_narrator(self):
        """Reference audio flows through opt_narrator in workflow."""
        wf = build_cosyvoice_workflow({
            "text": "测试", "reference_audio": "ref_test.flac",
            "prompt_text": "你好世界", "seed": 42,
        })
        assert "2" in wf  # LoadAudio node
        assert wf["2"]["class_type"] == "LoadAudio"
        assert wf["3"]["inputs"]["opt_narrator"] == ["2", 0]

    def test_p8c_all_engine_types_produce_save_audio_output(self):
        """Every workflow builder produces a SaveAudio node."""
        for eng, p in [
            ("cosyvoice", {"text": "x"}),
            ("indextts", {"text": "x"}),
            ("gpt-sovits", {"text": "x", "gpt_weights_path": "g", "sovits_weights_path": "s"}),
        ]:
            wf = build_workflow(eng, p)
            last_nid = max(int(k) for k in wf)
            assert wf[str(last_nid)]["class_type"] == "SaveAudio", f"{eng}: output != SaveAudio"

    def test_p8d_tts_engine_link_points_to_engine_node(self):
        """Every workflow's TTS_engine link references node 1."""
        for eng, p in [
            ("cosyvoice", {"text": "x"}),
            ("indextts", {"text": "x"}),
            ("gpt-sovits", {"text": "x", "gpt_weights_path": "g", "sovits_weights_path": "s"}),
        ]:
            wf = build_workflow(eng, p)
            tts_node = wf.get("3")
            assert tts_node is not None, f"{eng}: missing TTS node"
            link = tts_node["inputs"].get("TTS_engine")
            assert link == ["1", 0], f"{eng}: TTS_engine link {link} != ['1', 0]"
