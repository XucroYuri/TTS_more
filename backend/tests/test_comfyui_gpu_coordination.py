from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from app.adapters.base import SynthesisCoordinationError, SynthesisPreempted, SynthesisRequest, SynthesisResult
from app.comfyui.client import ComfyUIAPIClient, PromptCancellationResult
from app.comfyui.gpu_coordination import GPUCoordinationConfig, admission_probe, coordination_config, native_priority_pending, require_suite_coordination
from app.models import EngineName, GenerationManifest, GenerationTask, ScriptLine, TTSServiceEndpoint
from app.native_gpu.protocol import CoordinationTimeout
from app.queue import ServiceGenerationQueue
from app.services import ComfyUITTSClient, ServiceRoute


def test_operator_config_disabled_and_lan_independent(tmp_path, monkeypatch):
    monkeypatch.delenv("TTS_MORE_GPU_COORDINATION_CONFIG", raising=False)
    assert coordination_config("g") is None
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"enabled": True, "groups": {"g": {"coordinator_url": "http://127.0.0.1:9001", "token": "private-token-value"}}}), encoding="utf8")
    monkeypatch.setenv("TTS_MORE_GPU_COORDINATION_CONFIG", str(config))
    assert coordination_config("lan") is None
    assert coordination_config("g").resource_group == "g"
    assert "private-token-value" not in repr(coordination_config("g"))
    config.write_text("invalid", encoding="utf8")
    with pytest.raises(SynthesisCoordinationError):
        coordination_config("g")


def test_probe_does_not_hold_lease_and_native_timeout_is_retryable():
    events = []
    config = GPUCoordinationConfig("g", "http://127.0.0.1:9001", "secret")
    client = SimpleNamespace(acquire_comfy=lambda *_a, **_k: "lease", check_comfy=lambda _t: True, release_comfy=lambda t, clean: events.append((t, clean)) or True, snapshot=lambda: {"groups": {"g": {"state": "native_busy", "natives": {"n": {"fresh": True, "status": {"ready": True}}}}}})
    admission_probe(client, config, "holder")
    assert events == [("lease", True)]
    client.acquire_comfy = lambda *_a, **_k: (_ for _ in ()).throw(CoordinationTimeout())
    with pytest.raises(SynthesisPreempted) as caught:
        admission_probe(client, config, "holder")
    assert caught.value.details["cleanup_confirmed"] is True
    client.snapshot = lambda: {"groups": {"g": {"comfy": {"cleanup_failed": True}}}}
    with pytest.raises(SynthesisCoordinationError, match="fence"):
        admission_probe(client, config, "holder")
    client.acquire_comfy = lambda *_a, **_k: "lease"
    client.release_comfy = lambda *_a, **_k: False
    with pytest.raises(SynthesisCoordinationError):
        admission_probe(client, config, "holder")


@pytest.mark.parametrize("group", [{"native_waiting": 1}, {"native_active": 1}, {"comfy": {"revoked": True}}, {"natives": {"n": {"status": {"waiting": 1}}}}])
def test_priority_snapshot_includes_pre_acquire_native_intent(group):
    config = GPUCoordinationConfig("g", "http://127.0.0.1:9001", "secret")
    group = {"state": "native_busy", "natives": {"n": {"fresh": True, "status": {"ready": True}}}, **group}
    for native in group["natives"].values():
        native.update({"fresh": True, "status": {"ready": True, **native.get("status", {})}})
    assert native_priority_pending(SimpleNamespace(snapshot=lambda: {"groups": {"g": group}}), config)


@pytest.mark.parametrize("group", [{"recovery_required": True}, {"cleanup_failed": True}, {"comfy": {"reason": "comfy_heartbeat_expired"}}, {"state": "unknown"}, {"state": "comfy_running", "natives": {"n": {"fresh": False, "status": {"ready": True}}}}])
def test_unknown_or_dirty_snapshot_is_fatal(group):
    config = GPUCoordinationConfig("g", "http://127.0.0.1:9001", "secret")
    with pytest.raises(SynthesisCoordinationError):
        native_priority_pending(SimpleNamespace(snapshot=lambda: {"groups": {"g": group}}), config)


def test_suite_capability_requires_enabled_same_group():
    config = GPUCoordinationConfig("g", "http://127.0.0.1:9001", "secret")
    require_suite_coordination({"gpu_coordination": {"enabled": True, "protocol_version": 1, "resource_group": "g"}}, config)
    for value in [{}, {"enabled": False}, {"enabled": True, "protocol_version": 1, "resource_group": "lan"}]:
        with pytest.raises(SynthesisCoordinationError):
            require_suite_coordination({"gpu_coordination": value}, config)


@pytest.mark.parametrize("report", [{"status": "ok"}, {"released": [], "busy": ["r"], "errors": []}, {"released": [], "busy": [], "errors": ["unload failed"]}])
def test_unload_rejects_unconfirmed_or_dirty_reports(report, monkeypatch):
    endpoint = TTSServiceEndpoint(service_id="comfy", base_url="http://127.0.0.1:8188", engine=EngineName.GPT_SOVITS, api_contract="comfyui-tts-audio-suite-v1", resource_group="g")
    client = ComfyUITTSClient(endpoint)
    monkeypatch.setattr(client.api, "release_runtime", lambda **_k: report)
    monkeypatch.setattr(client.api, "free_memory", lambda: pytest.fail("must not hide failed runtime cleanup with /free"))
    with pytest.raises(SynthesisCoordinationError):
        client.unload()


def test_poll_preemption_distinct_from_cancel_and_completed_output_wins(monkeypatch):
    api = ComfyUIAPIClient("http://127.0.0.1:8188")
    monkeypatch.setattr(api, "get_history", lambda _p: {})
    monkeypatch.setattr(api, "cancel_prompt", lambda p, max_wait: PromptCancellationResult(p, "running", "interrupted", (), 0, True))
    with pytest.raises(SynthesisPreempted) as caught:
        api.poll_until_done("p", preempt_check=lambda: True)
    assert caught.value.details["cancellation"]["converged"] is True
    monkeypatch.setattr(api, "get_history", lambda _p: {"p": {"outputs": {"1": {"audio": [{"filename": "x.wav"}]}}}})
    assert api.poll_until_done("p", preempt_check=lambda: True)["outputs"]


def test_suite_preemption_marker_is_preserved(monkeypatch):
    api = ComfyUIAPIClient("http://127.0.0.1:8188")
    monkeypatch.setattr(api, "get_history", lambda _p: {"p": {"status": {"status_str": "error", "messages": [["execution_error", {"exception_message": "TTSMoreGPUPreempted: priority"}]]}}})
    with pytest.raises(SynthesisPreempted):
        api.poll_until_done("p")


def test_suite_cleanup_exception_type_is_fatal_without_marker_in_message(monkeypatch):
    api = ComfyUIAPIClient("http://127.0.0.1:8188")
    monkeypatch.setattr(api, "get_history", lambda _p: {"p": {"status": {"status_str": "error", "messages": [["execution_error", {"exception_type": "api_bridge.gpu_coordination.TTSMoreGPUCleanupFailed", "exception_message": "allocation unavailable"}]]}}})
    with pytest.raises(SynthesisCoordinationError):
        api.poll_until_done("p")


@pytest.mark.parametrize("busy", [False, True])
def test_service_preemption_requires_terminal_prompt_and_runtime_cleanup(tmp_path, monkeypatch, busy):
    from app.comfyui import gpu_coordination as gpu

    config = GPUCoordinationConfig("g", "http://127.0.0.1:9001", "private")
    fake = SimpleNamespace(acquire_comfy=lambda *_a, **_k: "probe", check_comfy=lambda _t: True, release_comfy=lambda _t, clean: True)
    monkeypatch.setattr(gpu, "coordination_config", lambda _g: config)
    monkeypatch.setattr(GPUCoordinationConfig, "client", lambda _c: fake)
    endpoint = TTSServiceEndpoint(service_id="comfy", engine=EngineName.GPT_SOVITS, base_url="http://127.0.0.1:8188", api_contract="comfyui-tts-audio-suite-v1", resource_group="g", default_params={"resource_id": "r"})
    client = ComfyUITTSClient(endpoint)
    client.api.bridge_capabilities = lambda: {"gpu_coordination": {"enabled": True, "resource_group": "g", "protocol_version": 1}}
    events = []
    client._build_workflow = lambda *_a: {}
    client.api.submit_workflow = lambda _w: events.append("submit") or "p"
    client.api.poll_until_done = lambda *_a, **_k: (_ for _ in ()).throw(SynthesisPreempted("priority", details={"prompt_id": "p", "suite_preempted": True}))
    client.api.cancel_prompt = lambda *_a, **_k: events.append("terminal") or PromptCancellationResult("p", "error", "error", (), 0, True)
    client.api.get_queue = lambda **_k: {"queue_running": [], "queue_pending": []}
    client.api.release_runtime = lambda **_k: events.append("cleanup") or {"released": [], "busy": ["r"] if busy else [], "errors": []}
    expected = SynthesisCoordinationError if busy else SynthesisPreempted
    with pytest.raises(expected) as caught:
        client.synthesize(SynthesisRequest(ScriptLine(id="l", character_id="r", text="hello"), "p", tmp_path / "out.wav"))
    assert events == ["submit", "terminal", "cleanup"]
    if not busy:
        assert caught.value.details["cleanup_confirmed"] is True


@pytest.mark.parametrize("clean", [True, False])
def test_queue_retries_only_clean_preemption_exactly_one_version(tmp_path, monkeypatch, clean):
    endpoint = TTSServiceEndpoint(service_id="comfy", engine=EngineName.GPT_SOVITS, base_url="mock://comfy", resource_group="g")
    calls = []

    def synthesize(request):
        calls.append(request.line.id)
        if request.line.id == "two" and calls.count("two") == 1:
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b"partial")
            raise SynthesisPreempted("priority", details={"cleanup_confirmed": clean})
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"complete")
        return SynthesisResult(request.output_path, {"runtime_released": True})

    client = SimpleNamespace(load=lambda *_a: None, synthesize=synthesize)
    route = ServiceRoute(endpoint, client)
    queue = ServiceGenerationQueue(SimpleNamespace(resolve_task=lambda _t: route))
    manifest = GenerationManifest(project_id="demo")
    tasks = [GenerationTask(line=ScriptLine(id=value, character_id="r", text=value), engine=EngineName.GPT_SOVITS, profile="p", service_id="comfy") for value in ["one", "two", "three"]]
    updates = []
    if clean:
        queue.run(tasks, manifest, tmp_path / "out", status_callback=lambda t, s, *_a: updates.append((t.line.id, s)))
        assert calls == ["one", "two", "two", "three"]
        assert ("two", "queued") in updates
        assert all(len(manifest.history_for_line(t.line.id).versions) == 1 for t in tasks)
        assert all(manifest.history_for_line(t.line.id).versions[0].status == "completed" for t in tasks)
        assert queue.load_state("comfy")["loaded"] is False
    else:
        with pytest.raises(SynthesisCoordinationError):
            queue.run(tasks, manifest, tmp_path / "out")
        assert calls == ["one", "two"]
        assert manifest.history_for_line("two").versions[0].status == "failed"
        assert manifest.history_for_line("three") is None


def test_user_cancel_while_waiting_stops_retry_without_rewriting_completed_line(tmp_path):
    endpoint = TTSServiceEndpoint(service_id="comfy", engine=EngineName.GPT_SOVITS, base_url="mock://comfy", resource_group="g")
    cancelled = threading.Event()
    calls = []
    def synthesize(request):
        calls.append(request.line.id)
        if request.line.id == "two":
            raise SynthesisPreempted("native", details={"cleanup_confirmed": True})
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"completed")
        return SynthesisResult(request.output_path)
    queue = ServiceGenerationQueue(SimpleNamespace(resolve_task=lambda _t: ServiceRoute(endpoint, SimpleNamespace(load=lambda *_a: None, synthesize=synthesize))))
    tasks = [GenerationTask(line=ScriptLine(id=value, character_id="r", text=value), engine=EngineName.GPT_SOVITS, profile="p", service_id="comfy") for value in ["one", "two"]]
    manifest = GenerationManifest(project_id="demo")
    def status(_task, state, *_args):
        if state == "queued":
            cancelled.set()
    queue.run(tasks, manifest, tmp_path / "out", status_callback=status, cancel_check=cancelled.is_set)
    assert calls == ["one", "two"]
    assert manifest.history_for_line("one").versions[0].status == "completed"
    assert not manifest.history_for_line("two") or not manifest.history_for_line("two").versions


def test_lan_resource_completes_while_local_gpu_waits(tmp_path):
    preempted, lan_done = threading.Event(), threading.Event()
    count = [0]
    def synthesize(request):
        if request.line.id == "local":
            count[0] += 1
            if count[0] == 1:
                preempted.set()
                raise SynthesisPreempted("native", details={"cleanup_confirmed": True})
            assert lan_done.is_set()
        else:
            assert preempted.wait(2)
            lan_done.set()
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"completed")
        return SynthesisResult(request.output_path, {"runtime_released": True})
    routes = {}
    tasks = []
    for name in ["local", "lan"]:
        endpoint = TTSServiceEndpoint(service_id=name, engine=EngineName.GPT_SOVITS, base_url=f"mock://{name}", resource_group=name)
        routes[name] = ServiceRoute(endpoint, SimpleNamespace(load=lambda *_a: None, synthesize=synthesize))
        tasks.append(GenerationTask(line=ScriptLine(id=name, character_id="r", text=name), engine=EngineName.GPT_SOVITS, profile="p", service_id=name))
    queue = ServiceGenerationQueue(SimpleNamespace(resolve_task=lambda t: routes[t.service_id]))
    manifest = GenerationManifest(project_id="demo")
    queue.run(tasks, manifest, tmp_path / "out")
    assert count == [2]
    assert all(len(manifest.history_for_line(name).versions) == 1 for name in ["local", "lan"])
