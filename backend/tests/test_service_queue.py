from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName, GenerationManifest, GenerationTask, GenerationVersion, ProviderType, ScriptLine, TTSServiceEndpoint
from app.queue import GenerationJobManager, ServiceGenerationQueue
from app.services import ServiceRoute, build_load_signature
from app.storage import ProjectStore


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


class LoadAndUnloadFailingServiceClient(LoadFailingServiceClient):
    def unload(self) -> None:
        self.calls.append("unload")
        raise RuntimeError("cleanup unload failed")


class FailOnceLoadingServiceClient(RecordingServiceClient):
    def __init__(self, endpoint: TTSServiceEndpoint) -> None:
        super().__init__(endpoint)
        self.remaining_load_failures = 1

    def load(self, profile: str, parameters: dict | None = None) -> None:
        self.calls.append(f"load:{profile}")
        if self.remaining_load_failures:
            self.remaining_load_failures -= 1
            raise RuntimeError("load failed after changing resident resources")


class OutputPathRecordingServiceClient(RecordingServiceClient):
    def __init__(self, endpoint: TTSServiceEndpoint) -> None:
        super().__init__(endpoint)
        self.output_paths: list[Path] = []

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.calls.append(f"synthesize:{request.line.id}")
        self.output_paths.append(request.output_path)
        return SynthesisResult(audio_path=request.output_path, metadata={"service": self.endpoint.service_id})


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


class SnapshotMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._manifests: dict[str, GenerationManifest] = {}
        self._lock = threading.Lock()

    def load_manifest(self, project_id: str) -> GenerationManifest:
        with self._lock:
            manifest = self._manifests.get(project_id, GenerationManifest(project_id=project_id))
            return manifest.model_copy(deep=True)

    def project_audio_dir(self, project_id: str) -> Path:
        path = self.root / project_id / "output" / "audio"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_manifest(self, manifest: GenerationManifest) -> None:
        with self._lock:
            self._manifests[manifest.project_id] = manifest.model_copy(deep=True)

    def manifest(self, project_id: str) -> GenerationManifest:
        with self._lock:
            return self._manifests[project_id].model_copy(deep=True)


class ManifestAppendingQueue:
    def __init__(self, router: StaticRouter, blocking_line_id: str, release: threading.Event) -> None:
        self.router = router
        self.blocking_line_id = blocking_line_id
        self.release = release
        self._started: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def started_event(self, line_id: str) -> threading.Event:
        with self._lock:
            return self._started.setdefault(line_id, threading.Event())

    def run(self, tasks, manifest, output_dir, status_callback=None, cancel_check=None) -> GenerationManifest:
        for current in tasks:
            self.started_event(current.line.id).set()
            if current.line.id == self.blocking_line_id and not self.release.wait(5):
                raise TimeoutError("test timed out waiting to release transaction")
            if cancel_check and cancel_check():
                return manifest
            version_id = "v001"
            if status_callback:
                status_callback(current, "running", 0.5, "test-cluster", version_id)
            manifest.append_version(
                current.line.id,
                GenerationVersion(
                    version_id=version_id,
                    line_uid=current.line.line_uid or current.line.id,
                    engine=current.engine,
                    profile=current.profile,
                    service_id=current.service_id,
                    status="completed",
                    audio_path=str(output_dir / f"{current.line.id}.wav"),
                ),
            )
            if status_callback:
                status_callback(current, "completed", 1.0, "test-cluster", version_id)
        return manifest


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


@pytest.mark.parametrize(
    ("service_id", "profile", "line_id"),
    [
        ("../escaped-service", "profile", "line/../unsafe"),
        ("/absolute-service", "profile", "line/../unsafe"),
        (r"C:\\temp\\service", r"..\\..\\escaped-profile", "line/../unsafe"),
        ("service", "../../escaped-profile", "line/../unsafe"),
        ("service", "profile", "../"),
    ],
)
def test_service_queue_sanitizes_output_segments_and_contains_paths(
    tmp_path: Path,
    service_id: str,
    profile: str,
    line_id: str,
) -> None:
    output_dir = tmp_path / "audio"
    client = OutputPathRecordingServiceClient(endpoint(service_id, EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({service_id: client}))

    queue.run(
        [task(line_id, EngineName.GPT_SOVITS, profile, service_id)],
        GenerationManifest(project_id="demo"),
        output_dir=output_dir,
    )

    output_path = client.output_paths[0]
    relative = output_path.resolve(strict=False).relative_to(output_dir.resolve(strict=False))
    assert len(relative.parts) == 4
    assert all(part not in {"", ".", ".."} for part in relative.parts)
    assert all(not any(character in part for character in '/\\:') for part in relative.parts)


def test_service_queue_rejects_output_path_resolving_through_symlink_outside_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "audio"
    outside_dir = tmp_path / "outside"
    output_dir.mkdir()
    outside_dir.mkdir()
    (output_dir / EngineName.GPT_SOVITS.value).symlink_to(outside_dir, target_is_directory=True)
    client = OutputPathRecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))

    with pytest.raises(ValueError, match="outside output directory"):
        queue.run(
            [gpt_task("a1", "a.wav")],
            GenerationManifest(project_id="demo"),
            output_dir=output_dir,
        )

    assert client.output_paths == []


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


def test_service_queue_failed_load_invalidates_old_signature_and_active_resource(tmp_path: Path) -> None:
    client = LoadFailingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    queue._loaded_signatures["local-gpt"] = "service_id=local-gpt|logs_name=old"
    queue._active_resource_services["local-gpu-0"] = ("local-gpt", client)
    manifest = GenerationManifest(project_id="demo")

    try:
        queue.run([gpt_task("a1", "new.wav")], manifest, output_dir=tmp_path)
    except RuntimeError:
        pass
    else:
        raise AssertionError("load failure should bubble out of the resource cluster")

    state = queue.load_state("local-gpt")
    assert state["loaded"] is False
    assert state["loaded_signature"] is None
    assert state["last_error"]
    assert "load failed" in state["last_error"]
    assert "local-gpu-0" not in queue._active_resource_services
    assert client.calls == ["load:gpt-role", "unload"]
    failed_version = manifest.lines["a1"].versions[0]
    assert failed_version.status == "failed"
    assert failed_version.error == "load failed for target signature"
    assert failed_version.metadata["failure_stage"] == "loading"
    assert failed_version.requested_load_signature is not None


def test_service_queue_reloads_old_signature_after_partial_load_failure(tmp_path: Path) -> None:
    client = FailOnceLoadingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    old_task = gpt_task("old", "old.wav")
    old_signature = build_load_signature(client.endpoint, old_task.parameters)
    queue._loaded_signatures["local-gpt"] = old_signature
    queue._active_resource_services["local-gpu-0"] = ("local-gpt", client)

    with pytest.raises(RuntimeError, match="changing resident resources"):
        queue.run(
            [gpt_task("new", "new.wav")],
            GenerationManifest(project_id="demo"),
            output_dir=tmp_path,
        )

    retry_manifest = GenerationManifest(project_id="demo")
    queue.run([old_task], retry_manifest, output_dir=tmp_path)

    assert client.calls == ["load:gpt-role", "unload", "load:gpt-role", "synthesize:old"]
    assert queue.load_state("local-gpt")["loaded_signature"] == old_signature
    assert retry_manifest.lines["old"].versions[0].status == "completed"


def test_service_queue_preserves_load_error_when_failed_load_cleanup_unload_also_fails(tmp_path: Path) -> None:
    client = LoadAndUnloadFailingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ServiceGenerationQueue(StaticRouter({"local-gpt": client}))
    queue._loaded_signatures["local-gpt"] = "old-signature"
    queue._active_resource_services["local-gpu-0"] = ("local-gpt", client)

    with pytest.raises(RuntimeError, match="load failed for target signature"):
        queue.run(
            [gpt_task("new", "new.wav")],
            GenerationManifest(project_id="demo"),
            output_dir=tmp_path,
        )

    assert client.calls == ["load:gpt-role", "unload"]
    assert queue.load_state("local-gpt")["loaded"] is False
    assert "local-gpu-0" not in queue._active_resource_services


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


def test_generation_job_manager_serializes_full_manifest_transaction_per_project(tmp_path: Path) -> None:
    release = threading.Event()
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ManifestAppendingQueue(StaticRouter({"local-gpt": client}), "first", release)
    store = SnapshotMemoryStore(tmp_path)
    manager = GenerationJobManager(queue, store)
    first_started = queue.started_event("first")
    second_started = queue.started_event("second")

    first = manager.submit("demo", [gpt_task("first", "a.wav")])
    assert first_started.wait(2), "first project transaction did not start"
    second = manager.submit("demo", [gpt_task("second", "b.wav")])
    second_ran_before_release = second_started.wait(0.3)
    release.set()

    first_final = _wait_for_manager_job(manager, first.job_id)
    second_final = _wait_for_manager_job(manager, second.job_id)
    manifest = store.manifest("demo")

    assert second_ran_before_release is False
    assert first_final.status == "completed"
    assert second_final.status == "completed"
    assert set(manifest.lines) == {"first", "second"}


def test_generation_job_manager_allows_different_project_transactions_in_parallel(tmp_path: Path) -> None:
    release = threading.Event()
    client = RecordingServiceClient(endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0"))
    queue = ManifestAppendingQueue(StaticRouter({"local-gpt": client}), "blocked", release)
    store = SnapshotMemoryStore(tmp_path)
    manager = GenerationJobManager(queue, store)
    blocked_started = queue.started_event("blocked")
    other_started = queue.started_event("other")

    blocked = manager.submit("project-a", [gpt_task("blocked", "a.wav")])
    assert blocked_started.wait(2), "blocked project transaction did not start"
    other = manager.submit("project-b", [gpt_task("other", "b.wav")])
    other_ran_in_parallel = other_started.wait(1)
    release.set()

    blocked_final = _wait_for_manager_job(manager, blocked.job_id)
    other_final = _wait_for_manager_job(manager, other.job_id)

    assert other_ran_in_parallel is True
    assert blocked_final.status == "completed"
    assert other_final.status == "completed"
    assert set(store.manifest("project-a").lines) == {"blocked"}
    assert set(store.manifest("project-b").lines) == {"other"}


def test_project_store_atomic_writes_use_unique_temp_paths_under_concurrency(tmp_path: Path, monkeypatch) -> None:
    store = ProjectStore(tmp_path)
    target = tmp_path / "state.json"
    write_barrier = threading.Barrier(2)
    temp_paths: list[Path] = []
    errors: list[BaseException] = []
    paths_lock = threading.Lock()
    original_write_text = Path.write_text
    original_replace = Path.replace
    active_replaces = 0
    max_active_replaces = 0

    def synchronized_write_text(path: Path, text: str, *args, **kwargs):
        result = original_write_text(path, text, *args, **kwargs)
        if path.parent == target.parent and path.name.startswith(f".{target.name}.") and path.name.endswith(".tmp"):
            with paths_lock:
                temp_paths.append(path)
            write_barrier.wait(2)
        return result

    def monitored_replace(path: Path, destination: Path):
        nonlocal active_replaces, max_active_replaces
        with paths_lock:
            active_replaces += 1
            max_active_replaces = max(max_active_replaces, active_replaces)
        try:
            time.sleep(0.05)
            return original_replace(path, destination)
        finally:
            with paths_lock:
                active_replaces -= 1

    def write_payload(payload: str) -> None:
        try:
            store._write_text(target, payload)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, "write_text", synchronized_write_text)
    monkeypatch.setattr(Path, "replace", monitored_replace)
    workers = [threading.Thread(target=write_payload, args=(payload,)) for payload in ("first", "second")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(temp_paths) == 2
    assert len(set(temp_paths)) == 2
    assert max_active_replaces == 1
    assert target.read_text(encoding="utf-8") in {"first", "second"}


def test_project_store_atomic_write_cleans_temp_file_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    store = ProjectStore(tmp_path)
    target = tmp_path / "payload.json"
    original_replace = Path.replace

    def failing_replace(path: Path, destination: Path):
        if path.parent == target.parent and path.name.startswith(f".{target.name}."):
            raise OSError("replace failed")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        store._write_json(target, {"value": 1})

    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


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
            status_callback=lambda task, status, _progress, _cluster_key, _version_id: events.append(f"{task.line.id}:{status}"),
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
