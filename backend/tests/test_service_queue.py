from __future__ import annotations

import threading
from pathlib import Path

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName, GenerationManifest, GenerationTask, ProviderType, ScriptLine, TTSServiceEndpoint
from app.queue import ServiceGenerationQueue
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


class StaticRouter:
    def __init__(self, clients: dict[str, RecordingServiceClient]) -> None:
        self.clients = clients

    def resolve_task(self, task: GenerationTask) -> ServiceRoute:
        assert task.service_id is not None
        client = self.clients[task.service_id]
        return ServiceRoute(endpoint=client.endpoint, client=client)


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

    assert first.calls == ["load:p1", "synthesize:l1", "unload"]
    assert second.calls == ["load:p2", "synthesize:l2", "unload"]
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
