from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.adapters.base import SynthesisCancelled, SynthesisRequest, SynthesisResult, SynthesisTimeout
from app.models import EngineName, GenerationManifest, GenerationTask, ProviderType, ScriptLine, TTSServiceEndpoint
from app.queue import GenerationJobManager, ServiceGenerationQueue
from app.services import ServiceRoute


class RecordingServiceClient:
    def __init__(self, endpoint: TTSServiceEndpoint) -> None:
        self.endpoint = endpoint
        self.calls: list[str] = []

    def load(self, profile: str, parameters: dict | None = None) -> None:
        self.calls.append(f"load:{profile}")

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.calls.append(f"synthesize:{request.line.id}")
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"RIFFfake")
        return SynthesisResult(audio_path=request.output_path, metadata={"service": self.endpoint.service_id})

    def unload(self) -> None:
        self.calls.append("unload")


class PromptProgressServiceClient(RecordingServiceClient):
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if request.progress_callback is not None:
            request.progress_callback({"external_job_id": "prompt-abc", "external_status": "queued", "progress": 0.1})
            request.progress_callback({"external_job_id": "prompt-abc", "external_status": "running", "progress": 0.45})
            request.progress_callback({"external_job_id": "prompt-abc", "external_status": "completed", "progress": 1.0})
        return super().synthesize(request)


class BlockingServiceClient(RecordingServiceClient):
    def __init__(self, endpoint: TTSServiceEndpoint, release: threading.Event) -> None:
        super().__init__(endpoint)
        self.started = threading.Event()
        self.release = release

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.calls.append(f"synthesize:{request.line.id}")
        self.started.set()
        assert self.release.wait(2), "test timed out waiting for release"
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"RIFFfake")
        return SynthesisResult(audio_path=request.output_path, metadata={"service": self.endpoint.service_id})


class LoadFailingServiceClient(RecordingServiceClient):
    def load(self, profile: str, parameters: dict | None = None) -> None:
        self.calls.append(f"load:{profile}")
        raise RuntimeError("load failed for target signature")


class SynthesisFailingServiceClient(RecordingServiceClient):
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.calls.append(f"synthesize:{request.line.id}")
        raise RuntimeError("synthesis backend returned 500")


class UnloadFailingServiceClient(RecordingServiceClient):
    def unload(self) -> None:
        self.calls.append("unload")
        raise RuntimeError("old provider is still resident")


class StaticRouter:
    def __init__(self, clients: dict[str, RecordingServiceClient]) -> None:
        self.clients = clients

    def resolve_task(self, task: GenerationTask) -> ServiceRoute:
        assert task.service_id is not None
        client = self.clients[task.service_id]
        return ServiceRoute(endpoint=client.endpoint, client=client)


class RaisingQueue:
    def __init__(self, router: StaticRouter) -> None:
        self.router = router

    def run(self, *_args, **_kwargs) -> None:
        raise RuntimeError("resource worker crashed")


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = GenerationManifest(project_id="demo")
        self.save_calls = 0

    def load_manifest(self, _project_id: str) -> GenerationManifest:
        return self.manifest

    def project_dir(self, project_id: str) -> Path:
        path = self.root / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def project_audio_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "output" / "audio"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_manifest(self, manifest: GenerationManifest) -> None:
        self.save_calls += 1
        self.manifest = manifest


def endpoint(service_id: str, engine: EngineName, resource_group: str) -> TTSServiceEndpoint:
    return TTSServiceEndpoint(
        service_id=service_id,
        engine=engine,
        base_url=f"mock://{service_id}",
        resource_group=resource_group,
    )


def task(line_id: str, engine: EngineName, profile: str, service_id: str) -> GenerationTask:
    return GenerationTask(
        line=ScriptLine(id=line_id, character_id="role", text=f"text {line_id}"),
        engine=engine,
        profile=profile,
        service_id=service_id,
    )


def gpt_task(line_id: str, ref: str) -> GenerationTask:
    return GenerationTask(
        line=ScriptLine(id=line_id, character_id="role", text=f"text {line_id}"),
        engine=EngineName.GPT_SOVITS,
        profile="gpt-role",
        service_id="local-gpt",
        parameters={
            "gpt_weights_path": "gpt.ckpt",
            "sovits_weights_path": "sovits.pth",
            "ref_audio_path": ref,
        },
    )


def cosyvoice_task(line_id: str, prompt_audio: str, instruct_text: str = "") -> GenerationTask:
    return GenerationTask(
        line=ScriptLine(id=line_id, character_id="role", text=f"text {line_id}"),
        engine=EngineName.COSYVOICE,
        profile="cosy-role",
        service_id="local-cosyvoice",
        provider_type=ProviderType.COSYVOICE,
        binding_id="cosy-role-binding",
        required_capabilities=["tts", "zero_shot_voice"],
        parameters={
            "mode": "zero_shot",
            "speaker_id": "",
            "prompt_audio_path": prompt_audio,
            "prompt_text": "reference prompt",
            "instruct_text": instruct_text,
            "speed": 1.0,
            "seed": 42,
        },
    )


def test_service_queue_serializes_services_in_same_resource_group(tmp_path: Path) -> None:
    first = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    second = RecordingServiceClient(endpoint("local-index", EngineName.INDEX_TTS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": first, "local-index": second}))
    manifest = GenerationManifest(project_id="demo")

    queue.run(
        [
            task("l1", EngineName.GPT_SOVITS, "p1", "local-gpt"),
            task("l2", EngineName.INDEX_TTS, "p2", "local-index"),
        ],
        manifest,
        output_dir=tmp_path,
    )

    assert first.calls == ["load:p1", "synthesize:l1", "unload"]
    assert second.calls == ["load:p2", "synthesize:l2"]
    assert queue.load_state("local-gpt")["loaded"] is False
    assert queue.load_state("local-index")["loaded"] is True
    assert manifest.lines["l1"].versions[0].service_id == "local-gpt"
    assert manifest.lines["l2"].versions[0].resource_group == "local-gpu-0"


def test_service_queue_preserves_old_load_state_when_resource_unload_fails(tmp_path: Path) -> None:
    first = UnloadFailingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    second = RecordingServiceClient(endpoint("local-index", EngineName.INDEX_TTS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": first, "local-index": second}))
    manifest = GenerationManifest(project_id="demo")

    with pytest.raises(RuntimeError, match="still resident"):
        queue.run(
            [
                task("l1", EngineName.GPT_SOVITS, "p1", "local-gpt"),
                task("l2", EngineName.INDEX_TTS, "p2", "local-index"),
            ],
            manifest,
            output_dir=tmp_path,
        )

    assert queue.load_state("local-gpt")["loaded"] is True
    assert queue.load_state("local-index")["loaded"] is False
    assert second.calls == []
    assert manifest.lines["l2"].versions[0].status == "failed"
    assert manifest.lines["l2"].versions[0].metadata["failure_stage"] == "unloading"


def test_service_queue_clusters_same_weights_and_reference_before_switching(tmp_path: Path) -> None:
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    manifest = GenerationManifest(project_id="demo")

    queue.run(
        [
            gpt_task("a1", "a.wav"),
            gpt_task("b1", "b.wav"),
            gpt_task("a2", "a.wav"),
        ],
        manifest,
        output_dir=tmp_path,
    )

    synth_calls = [call for call in client.calls if call.startswith("synthesize")]
    assert synth_calls == ["synthesize:a1", "synthesize:a2", "synthesize:b1"]
    assert client.calls.count("load:gpt-role") == 2
    assert manifest.lines["a1"].versions[0].metadata["cluster_key"].endswith("ref_audio_path=a.wav")


def test_service_queue_clusters_cosyvoice_by_mode_reference_and_instruction(tmp_path: Path) -> None:
    client = RecordingServiceClient(endpoint("local-cosyvoice", EngineName.COSYVOICE, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-cosyvoice": client}))
    manifest = GenerationManifest(project_id="demo")

    queue.run(
        [
            cosyvoice_task("a1", "a.wav", "calm"),
            cosyvoice_task("b1", "b.wav", "calm"),
            cosyvoice_task("a2", "a.wav", "calm"),
            cosyvoice_task("a3", "a.wav", "urgent"),
        ],
        manifest,
        output_dir=tmp_path,
    )

    synth_calls = [call for call in client.calls if call.startswith("synthesize")]
    assert synth_calls == ["synthesize:a1", "synthesize:a2", "synthesize:b1", "synthesize:a3"]
    first_cluster = manifest.lines["a1"].versions[0].metadata["cluster_key"]
    assert "provider=cosyvoice" in first_cluster
    assert "prompt_audio_path=a.wav" in first_cluster
    assert "instruct_text=calm" in first_cluster
    assert manifest.lines["a3"].versions[0].metadata["cluster_key"] != first_cluster


def test_service_queue_keeps_generation_history_separate_by_line_uid(tmp_path: Path) -> None:
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    manifest = GenerationManifest(project_id="demo")
    first = gpt_task("l001", "a.wav").model_copy(
        update={"line": ScriptLine(id="l001", line_uid="parse-r001:l001", character_id="role", text="old text")}
    )
    second = gpt_task("l001", "a.wav").model_copy(
        update={"line": ScriptLine(id="l001", line_uid="parse-r002:l001", character_id="role", text="new text")}
    )

    queue.run([first, second], manifest, output_dir=tmp_path)

    assert sorted(manifest.lines) == ["parse-r001:l001", "parse-r002:l001"]
    assert manifest.lines["parse-r001:l001"].versions[0].line_uid == "parse-r001:l001"
    assert manifest.lines["parse-r002:l001"].versions[0].line_uid == "parse-r002:l001"
    assert manifest.lines["parse-r001:l001"].versions[0].audio_path != manifest.lines["parse-r002:l001"].versions[0].audio_path


def test_service_queue_load_state_tracks_successful_signature(tmp_path: Path) -> None:
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    manifest = GenerationManifest(project_id="demo")

    queue.run([gpt_task("a1", "a.wav")], manifest, output_dir=tmp_path)

    state = queue.load_state("local-gpt")
    assert state["loaded"] is True
    assert state["loaded_signature"] == manifest.lines["a1"].versions[0].requested_load_signature
    assert state["verification_level"] == "assumed_after_success"
    assert state["last_error"] is None
    assert state["updated_at"]


def test_service_queue_failed_load_does_not_pollute_load_state(tmp_path: Path) -> None:
    client = LoadFailingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    queue._loaded_signatures["local-gpt"] = "service_id=local-gpt|logs_name=old"
    manifest = GenerationManifest(project_id="demo")

    try:
        queue.run([gpt_task("a1", "new.wav")], manifest, output_dir=tmp_path)
    except RuntimeError:
        pass
    else:
        raise AssertionError("load failure should bubble out of the resource cluster")

    state = queue.load_state("local-gpt")
    assert state["loaded_signature"] == "service_id=local-gpt|logs_name=old"
    assert state["last_error"]
    assert "load failed" in state["last_error"]
    failed_version = manifest.lines["a1"].versions[0]
    assert failed_version.status == "failed"
    assert failed_version.error == "load failed for target signature"
    assert failed_version.metadata["failure_stage"] == "loading"
    assert failed_version.requested_load_signature is not None


def test_service_queue_records_synthesis_failure_stage(tmp_path: Path) -> None:
    client = SynthesisFailingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    manifest = GenerationManifest(project_id="demo")

    queue.run([gpt_task("a1", "a.wav")], manifest, output_dir=tmp_path)

    failed_version = manifest.lines["a1"].versions[0]
    assert failed_version.status == "failed"
    assert failed_version.error == "synthesis backend returned 500"
    assert failed_version.metadata["failure_stage"] == "synthesis"
    assert failed_version.metadata["requested_load_signature"] == failed_version.requested_load_signature
    assert failed_version.requested_load_signature is not None
    assert client.calls == ["load:gpt-role", "synthesize:a1"]


def test_generation_job_manager_copies_synthesis_errors_to_job_items(tmp_path: Path) -> None:
    client = SynthesisFailingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    store = MemoryStore(tmp_path)
    manager = GenerationJobManager(queue, store)

    job = manager.submit("demo", [gpt_task("a1", "a.wav")])

    for _ in range(50):
        current = manager.get(job.job_id)
        if current.status == "failed":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("job did not finish")

    current = manager.get(job.job_id)
    assert current.items[0].status == "failed"
    assert current.items[0].error == "synthesis backend returned 500"
    assert store.manifest.lines["a1"].versions[0].error == "synthesis backend returned 500"


def test_generation_job_manager_records_external_prompt_status(tmp_path: Path) -> None:
    client = PromptProgressServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    store = MemoryStore(tmp_path)
    manager = GenerationJobManager(queue, store)

    job = manager.submit("demo", [gpt_task("a1", "a.wav")])

    current = _wait_for_manager_job(manager, job.job_id)

    assert current.items[0].status == "completed"
    assert current.items[0].external_job_id == "prompt-abc"
    assert current.items[0].external_status == "completed"


def test_service_queue_records_provider_and_binding_metadata(tmp_path: Path) -> None:
    commercial_endpoint = TTSServiceEndpoint(
        service_id="openai-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.OPENAI,
        base_url="mock://openai",
        resource_group="paid-api",
    )
    client = RecordingServiceClient(commercial_endpoint)
    queue = ServiceGenerationQueue(StaticRouter({"openai-tts": client}))
    manifest = GenerationManifest(project_id="demo")

    queue.run(
        [
            GenerationTask(
                line=ScriptLine(id="l1", character_id="role", text="hello"),
                engine=EngineName.COMMERCIAL,
                profile="role-openai",
                service_id="openai-tts",
                provider_type=ProviderType.OPENAI,
                binding_id="role-openai-binding",
                required_capabilities=["commercial_voice"],
            )
        ],
        manifest,
        output_dir=tmp_path,
    )

    version = manifest.lines["l1"].versions[0]
    assert version.provider_type == ProviderType.OPENAI
    assert version.binding_id == "role-openai-binding"


def test_service_queue_runs_different_resource_groups_in_parallel(tmp_path: Path) -> None:
    release = threading.Event()
    local = BlockingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"), release)
    remote = BlockingServiceClient(endpoint("remote-index", EngineName.INDEX_TTS, "remote-gpu-0"), release)
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": local, "remote-index": remote}))
    manifest = GenerationManifest(project_id="demo")
    errors: list[BaseException] = []

    worker = threading.Thread(
        target=lambda: _run_queue(queue, manifest, tmp_path, errors),
        daemon=True,
    )
    worker.start()

    assert local.started.wait(1), "local group did not start"
    assert remote.started.wait(1), "remote group did not start in parallel"
    release.set()
    worker.join(2)

    assert errors == []
    assert manifest.lines["l1"].versions[0].status == "completed"
    assert manifest.lines["l2"].versions[0].status == "completed"


def test_service_queue_keeps_concurrent_run_callbacks_isolated(tmp_path: Path) -> None:
    release = threading.Event()
    first = BlockingServiceClient(endpoint("first-gpt", EngineName.GPT_SOVITS, "gpu-a"), release)
    second = BlockingServiceClient(endpoint("second-index", EngineName.INDEX_TTS, "gpu-b"), release)
    queue = ServiceGenerationQueue(StaticRouter({"first-gpt": first, "second-index": second}))
    first_events: list[str] = []
    second_events: list[str] = []
    errors: list[BaseException] = []

    first_worker = threading.Thread(
        target=lambda: _run_queue_with_callback(
            queue,
            [task("first-line", EngineName.GPT_SOVITS, "p1", "first-gpt")],
            GenerationManifest(project_id="first"),
            tmp_path / "first",
            first_events,
            errors,
        ),
        daemon=True,
    )
    second_worker = threading.Thread(
        target=lambda: _run_queue_with_callback(
            queue,
            [task("second-line", EngineName.INDEX_TTS, "p2", "second-index")],
            GenerationManifest(project_id="second"),
            tmp_path / "second",
            second_events,
            errors,
        ),
        daemon=True,
    )

    first_worker.start()
    assert first.started.wait(1), "first job did not start"
    second_worker.start()
    assert second.started.wait(1), "second job did not start"
    release.set()
    first_worker.join(2)
    second_worker.join(2)

    assert errors == []
    assert first_events
    assert second_events
    assert all(event.startswith("first-line:") for event in first_events)
    assert all(event.startswith("second-line:") for event in second_events)


def test_generation_job_manager_marks_items_failed_when_worker_crashes(tmp_path: Path) -> None:
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(RaisingQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))

    created = manager.submit("demo", [gpt_task("a1", "a.wav")])
    final = _wait_for_manager_job(manager, created.job_id)

    assert final.status == "failed"
    assert final.error == "resource worker crashed"
    assert final.progress == 1.0
    assert final.items[0].status == "failed"
    assert final.items[0].progress == 1.0
    assert final.items[0].error == "resource worker crashed"
    assert final.items[0].cluster_size == 1
    assert final.items[0].load_signature is not None


def test_generation_job_manager_skips_known_unroutable_items_and_runs_valid_items(tmp_path: Path) -> None:
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))
    good = gpt_task("good", "a.wav")
    bad = gpt_task("bad", "b.wav").model_copy(update={"service_id": "missing-gpt"})

    created = manager.submit("demo", [good, bad])
    final = _wait_for_manager_job(manager, created.job_id)

    assert final.status == "failed"
    assert final.items[0].line_id == "good"
    assert final.items[0].status == "completed"
    assert final.items[0].version_id == "v001"
    assert final.items[1].line_id == "bad"
    assert final.items[1].status == "failed"
    assert final.items[1].progress == 1.0
    assert final.items[1].error
    assert "missing-gpt" in final.items[1].error
    assert manager.store.manifest.lines["good"].versions[0].status == "completed"
    failed_version = manager.store.manifest.lines["bad"].versions[0]
    assert failed_version.status == "failed"
    assert failed_version.error
    assert "missing-gpt" in failed_version.error
    assert failed_version.service_id == "missing-gpt"
    assert failed_version.metadata["failure_stage"] == "routing"
    assert client.calls == ["load:gpt-role", "synthesize:good"]


def test_generation_job_manager_persists_load_failure_versions_when_worker_raises(tmp_path: Path) -> None:
    client = LoadFailingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    store = MemoryStore(tmp_path)
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), store)

    created = manager.submit("demo", [gpt_task("a1", "new.wav")])
    final = _wait_for_manager_job(manager, created.job_id)

    assert final.status == "failed"
    assert final.items[0].status == "failed"
    assert final.items[0].version_id == "v001"
    assert store.save_calls == 1
    failed_version = store.manifest.lines["a1"].versions[0]
    assert failed_version.status == "failed"
    assert failed_version.metadata["failure_stage"] == "loading"


def _wait_for_manager_job(manager: GenerationJobManager, job_id: str, timeout_seconds: float = 10.0):
    deadline = time.monotonic() + timeout_seconds
    payload = manager.get(job_id)
    while time.monotonic() < deadline:
        payload = manager.get(job_id)
        if payload.status in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish; last status={payload.status}")


def _run_queue(queue: ServiceGenerationQueue, manifest: GenerationManifest, output_dir: Path, errors: list[BaseException]) -> None:
    try:
        queue.run(
            [
                task("l1", EngineName.GPT_SOVITS, "p1", "local-gpt"),
                task("l2", EngineName.INDEX_TTS, "p2", "remote-index"),
            ],
            manifest,
            output_dir=output_dir,
        )
    except BaseException as exc:
        errors.append(exc)


def _run_queue_with_callback(
    queue: ServiceGenerationQueue,
    tasks: list[GenerationTask],
    manifest: GenerationManifest,
    output_dir: Path,
    events: list[str],
    errors: list[BaseException],
) -> None:
    try:
        queue.run(
            tasks,
            manifest,
            output_dir=output_dir,
            status_callback=lambda task, status, _progress, _cluster_key, _version_id, _external_update=None: events.append(f"{task.line.id}:{status}"),
        )
    except BaseException as exc:
        errors.append(exc)


def test_generation_job_manager_rejects_when_queue_full(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(GenerationJobManager, "MAX_JOBS", 2)
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))

    first = manager.submit("demo", [gpt_task("a1", "a.wav")])
    second = manager.submit("demo", [gpt_task("a2", "b.wav")])
    third = manager.submit("demo", [gpt_task("a3", "c.wav")])

    _wait_for_manager_job(manager, first.job_id)
    _wait_for_manager_job(manager, second.job_id)
    # The third submission should be rejected because the store is at capacity.
    assert third.status == "failed"
    assert "full" in (third.error or "")


def test_generation_job_manager_evicts_old_finished_jobs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(GenerationJobManager, "JOB_RETENTION_SECONDS", 0)
    monkeypatch.setattr(GenerationJobManager, "MAX_JOBS", 5)
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))

    created = manager.submit("demo", [gpt_task("a1", "a.wav")])
    _wait_for_manager_job(manager, created.job_id)
    # Force the updated_at into the past so eviction picks it up.
    from datetime import datetime, timedelta, timezone
    with manager._lock:
        manager._jobs[created.job_id].updated_at = datetime.now(timezone.utc) - timedelta(seconds=10)

    # status() triggers eviction; the finished job should be gone.
    manager.status()
    with pytest.raises(KeyError):
        manager.get(created.job_id)


def test_generation_job_cancel_stops_dispatching_remaining_lines(tmp_path: Path) -> None:
    """When a job is cancelled, lines not yet started should not run."""
    gate = threading.Event()
    started = threading.Event()
    client = BlockingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"), release=gate)
    client.started = started
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))

    # Two tasks in the same resource group serialize: the first blocks, the
    # second is queued. Cancelling after the first starts should prevent the
    # second from synthesizing.
    created = manager.submit("demo", [gpt_task("l1", "a.wav"), gpt_task("l2", "b.wav")])
    assert started.wait(2), "first task did not start"
    manager.cancel(created.job_id)
    gate.set()  # release the blocking first task
    final = _wait_for_manager_job(manager, created.job_id)

    assert final.status == "cancelled"
    # Only the first line should have been synthesized; the second was queued
    # and should be cancelled, never synthesized.
    assert any("synthesize:l1" in c for c in client.calls)
    assert not any("synthesize:l2" in c for c in client.calls)


def test_generation_cancel_transitions_inflight_item_through_cancelling_and_preserves_progress_identity(tmp_path: Path) -> None:
    started = threading.Event()
    send_progress = threading.Event()
    progress_sent = threading.Event()
    release = threading.Event()

    class CancelAwareClient(RecordingServiceClient):
        def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
            started.set()
            assert request.cancel_check is not None
            assert send_progress.wait(3)
            assert request.progress_callback is not None
            request.progress_callback(
                {"external_job_id": "prompt-1", "external_status": "cancelling", "progress": 0.7}
            )
            progress_sent.set()
            assert release.wait(3)
            if request.cancel_check():
                raise SynthesisCancelled(
                    "cancelled by operator",
                    details={"prompt_id": "prompt-1", "converged": True, "diagnostic": "prompt no longer active"},
                )
            return super().synthesize(request)

    service_endpoint = endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0")
    manager = GenerationJobManager(
        ServiceGenerationQueue(StaticRouter({"local-gpt": CancelAwareClient(service_endpoint)})),
        MemoryStore(tmp_path),
    )

    job = manager.submit("demo", [gpt_task("line-1", "reference.wav")])
    assert started.wait(3)
    cancelling = manager.cancel(job.job_id)
    assert cancelling.status == "cancelling"
    assert cancelling.items[0].status == "cancelling"

    send_progress.set()
    assert progress_sent.wait(3)
    after_progress = manager.get(job.job_id)
    assert after_progress.items[0].status == "cancelling"
    assert after_progress.items[0].external_job_id == "prompt-1"
    assert after_progress.items[0].external_status == "cancelling"

    release.set()
    final = _wait_for_manager_job(manager, job.job_id)
    assert final.status == "cancelled"
    assert final.progress == 1.0
    assert final.items[0].status == "cancelled"
    assert final.items[0].progress == 1.0
    version = manager.store.manifest.lines["line-1"].versions[0]
    assert version.status == "cancelled"
    assert version.audio_path is None
    assert version.metadata["control_code"] == "cancelled"
    assert version.metadata["control_details"]["prompt_id"] == "prompt-1"
    assert "failure_stage" not in version.metadata


def test_generation_cancel_before_dispatch_and_repeated_cancel_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(GenerationJobManager, "MAX_ACTIVE_JOBS", 1)
    release = threading.Event()
    client = BlockingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"), release)
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))
    active = manager.submit("demo", [gpt_task("active", "active.wav")])
    assert client.started.wait(3)

    queued = manager.submit("demo", [gpt_task("queued", "queued.wav")])
    first_cancel = manager.cancel(queued.job_id)
    first_payload = first_cancel.model_dump(mode="json")
    second_cancel = manager.cancel(queued.job_id)

    assert first_cancel.status == "cancelled"
    assert first_cancel.items[0].status == "cancelled"
    assert second_cancel.model_dump(mode="json") == first_payload
    release.set()
    assert _wait_for_manager_job(manager, active.job_id).status == "completed"
    assert _wait_for_manager_job(manager, queued.job_id).status == "cancelled"
    assert not any("synthesize:queued" in call for call in client.calls)


def test_generation_cancel_keeps_completed_job_terminal_truth(tmp_path: Path) -> None:
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))
    created = manager.submit("demo", [gpt_task("done", "done.wav")])
    completed = _wait_for_manager_job(manager, created.job_id)

    after_cancel = manager.cancel(created.job_id)

    assert completed.status == "completed"
    assert after_cancel.status == "completed"
    assert after_cancel.items[0].status == "completed"
    assert manager.store.manifest.lines["done"].versions[0].status == "completed"


def test_nonconverged_cancellation_fails_job_and_persists_sanitized_cleanup_evidence(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class NonConvergingClient(RecordingServiceClient):
        def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
            started.set()
            assert request.cancel_check is not None
            assert release.wait(3)
            raise SynthesisCancelled(
                "cancellation cleanup failed",
                details={
                    "prompt_id": "prompt-stuck",
                    "converged": False,
                    "diagnostic": "process exit could not be verified Authorization: Bearer cleanup-secret",
                    "cleanup": {"runner": "still-active"},
                },
            )

    client = NonConvergingClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))
    created = manager.submit("demo", [gpt_task("stuck", "stuck.wav"), gpt_task("never-run", "later.wav")])
    assert started.wait(3)
    manager.cancel(created.job_id)
    release.set()

    final = _wait_for_manager_job(manager, created.job_id)
    assert final.status == "failed"
    assert final.progress == 1.0
    assert final.items[0].status == "failed"
    assert final.items[1].status == "cancelled"
    version = manager.store.manifest.lines["stuck"].versions[0]
    assert version.status == "failed"
    assert version.audio_path is None
    assert version.metadata["failure_stage"] == "cancellation_cleanup"
    assert version.metadata["control_code"] == "cancelled"
    assert version.metadata["control_details"]["converged"] is False
    assert version.metadata["control_details"]["cleanup"] == {"runner": "still-active"}
    rendered = str(version.metadata)
    assert "cleanup-secret" not in rendered
    assert "Authorization: Bearer ***" in rendered


def test_synthesis_timeout_persists_timeout_stage_and_sanitized_control_details(tmp_path: Path) -> None:
    class TimingOutClient(RecordingServiceClient):
        def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
            raise SynthesisTimeout(
                "prompt timed out",
                details={
                    "prompt_id": "prompt-timeout",
                    "cancellation": {"converged": False, "diagnostic": "runner still active"},
                    "cleanup_error": "password=timeout-secret",
                },
            )

    client = TimingOutClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))
    final = _wait_for_manager_job(manager, manager.submit("demo", [gpt_task("timeout", "timeout.wav")]).job_id)

    assert final.status == "failed"
    version = manager.store.manifest.lines["timeout"].versions[0]
    assert version.status == "failed"
    assert version.audio_path is None
    assert version.metadata["failure_stage"] == "timeout"
    assert version.metadata["control_code"] == "timeout"
    assert version.metadata["control_details"]["prompt_id"] == "prompt-timeout"
    assert version.metadata["control_details"]["cancellation"]["converged"] is False
    assert "timeout-secret" not in str(version.metadata)
    assert "password=***" in str(version.metadata)


def test_generic_exception_after_cancel_is_failed_not_false_cancelled(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class CrashingClient(RecordingServiceClient):
        def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
            started.set()
            assert release.wait(3)
            raise RuntimeError("runner crashed during cancellation")

    client = CrashingClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))
    created = manager.submit("demo", [gpt_task("crash", "crash.wav")])
    assert started.wait(3)
    assert manager.cancel(created.job_id).status == "cancelling"
    release.set()

    final = _wait_for_manager_job(manager, created.job_id)
    assert final.status == "failed"
    assert final.items[0].status == "failed"
    version = manager.store.manifest.lines["crash"].versions[0]
    assert version.status == "failed"
    assert version.metadata["failure_stage"] == "synthesis"


def test_cancel_wins_completion_race_and_discards_uncommitted_output(tmp_path: Path) -> None:
    output_written = threading.Event()
    release = threading.Event()

    class CompletingClient(RecordingServiceClient):
        def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
            assert request.cancel_check is not None
            assert request.progress_callback is not None
            request.progress_callback(
                {"external_job_id": "prompt-race", "external_status": "running", "progress": 0.8}
            )
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b"RIFFuncommitted")
            output_written.set()
            assert release.wait(3)
            return SynthesisResult(audio_path=request.output_path, metadata={"prompt_id": "prompt-race"})

    client = CompletingClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    store = MemoryStore(tmp_path)
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), store)
    created = manager.submit("demo", [gpt_task("race", "race.wav")])
    assert output_written.wait(3)
    assert manager.cancel(created.job_id).status == "cancelling"
    release.set()

    final = _wait_for_manager_job(manager, created.job_id)
    assert final.status == "cancelled"
    assert final.items[0].status == "cancelled"
    version = store.manifest.lines["race"].versions[0]
    assert version.status == "cancelled"
    assert version.audio_path is None
    assert version.metadata["control_details"]["prompt_id"] == "prompt-race"
    assert not list(store.project_audio_dir("demo").rglob("*.wav"))


def test_cancel_at_finalizing_boundary_prevents_completed_manifest_commit(tmp_path: Path) -> None:
    class CancelAtFinalizingManager(GenerationJobManager):
        def _update_item(self, job_id, task, status, progress, cluster_key, version_id, external_update=None):
            super()._update_item(job_id, task, status, progress, cluster_key, version_id, external_update)
            if status == "finalizing":
                self.cancel(job_id)

    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    store = MemoryStore(tmp_path)
    manager = CancelAtFinalizingManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), store)
    created = manager.submit("demo", [gpt_task("finalizing-race", "race.wav")])

    final = _wait_for_manager_job(manager, created.job_id)

    assert final.status == "cancelled"
    assert final.items[0].status == "cancelled"
    version = store.manifest.lines["finalizing-race"].versions[0]
    assert version.status == "cancelled"
    assert version.audio_path is None
    assert not list(store.project_audio_dir("demo").rglob("*.wav"))


def test_cancel_fails_closed_when_uncommitted_output_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    class OutputThenCancelledClient(RecordingServiceClient):
        def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b"RIFFlocked")
            started.set()
            assert release.wait(3)
            raise SynthesisCancelled(
                "external prompt converged",
                details={"prompt_id": "prompt-locked-output", "converged": True},
            )

    original_unlink = Path.unlink

    def reject_wav_unlink(path: Path, *args, **kwargs):
        if path.suffix == ".wav":
            raise PermissionError("output is still locked password=local-secret")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_wav_unlink)
    client = OutputThenCancelledClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    manager = GenerationJobManager(ServiceGenerationQueue(StaticRouter({"local-gpt": client})), MemoryStore(tmp_path))
    created = manager.submit("demo", [gpt_task("locked-output", "locked.wav")])
    assert started.wait(3)
    assert manager.cancel(created.job_id).status == "cancelling"
    release.set()

    final = _wait_for_manager_job(manager, created.job_id)

    assert final.status == "failed"
    assert final.items[0].status == "failed"
    assert "output cleanup failed" in (final.items[0].error or "")
    version = manager.store.manifest.lines["locked-output"].versions[0]
    assert version.status == "failed"
    assert version.audio_path is None
    assert version.metadata["failure_stage"] == "cancellation_cleanup"
    assert version.metadata["control_code"] == "cancelled"
    assert version.metadata["control_details"]["prompt_id"] == "prompt-locked-output"
    assert "local-secret" not in str(version.metadata)
    assert "password=***" in version.metadata["control_details"]["output_cleanup_error"]
