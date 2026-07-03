from __future__ import annotations

import threading
import time
from pathlib import Path

from app.adapters.base import SynthesisRequest, SynthesisResult
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

    assert first.calls == ["load:p1", "synthesize:l1"]
    assert second.calls == ["load:p2", "synthesize:l2"]
    assert manifest.lines["l1"].versions[0].service_id == "local-gpt"
    assert manifest.lines["l2"].versions[0].resource_group == "local-gpu-0"


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


def _wait_for_manager_job(manager: GenerationJobManager, job_id: str):
    for _ in range(40):
        payload = manager.get(job_id)
        if payload.status in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("job did not finish")


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
            status_callback=lambda task, status, _progress, _cluster_key, _version_id: events.append(f"{task.line.id}:{status}"),
        )
    except BaseException as exc:
        errors.append(exc)
