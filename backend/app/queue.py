from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.adapters.base import EngineAdapter, SynthesisRequest
from app.models import EngineName, GenerationJob, GenerationManifest, GenerationQueueItem, GenerationStatus, GenerationTask, GenerationVersion, ProviderType
from app.services import ServiceRoute

StatusCallback = Callable[[GenerationTask, GenerationStatus, float, str | None, str | None], None]


class GenerationQueue:
    def __init__(self, adapters: dict[EngineName, EngineAdapter]) -> None:
        self.adapters = adapters
        self._gpu_lock = threading.Lock()

    def run(self, tasks: list[GenerationTask], manifest: GenerationManifest, output_dir: Path) -> GenerationManifest:
        grouped: "OrderedDict[tuple[EngineName, str], list[GenerationTask]]" = OrderedDict()
        for task in tasks:
            grouped.setdefault((task.engine, task.profile), []).append(task)

        with self._gpu_lock:
            for (engine, profile), group in grouped.items():
                adapter = self.adapters[engine]
                adapter.load(profile)
                try:
                    for task in group:
                        self._run_task(adapter, task, manifest, output_dir)
                finally:
                    adapter.unload()
        return manifest

    def _run_task(
        self,
        adapter: EngineAdapter,
        task: GenerationTask,
        manifest: GenerationManifest,
        output_dir: Path,
    ) -> None:
        version_number = len(manifest.lines.get(task.line.id, []).versions) + 1 if task.line.id in manifest.lines else 1
        version_id = f"v{version_number:03d}"
        output_path = output_dir / task.engine.value / task.profile / f"{task.line.id}_{version_id}.wav"
        try:
            result = adapter.synthesize(
                SynthesisRequest(
                    line=task.line,
                    profile=task.profile,
                    output_path=output_path,
                    parameters=task.parameters,
                )
            )
            manifest.append_version(
                task.line.id,
                GenerationVersion(
                    version_id=version_id,
                    engine=task.engine,
                    profile=task.profile,
                    status="completed",
                    audio_path=str(result.audio_path),
                    parameters=task.parameters,
                    metadata=result.metadata,
                ),
            )
        except Exception as exc:
            manifest.append_version(
                task.line.id,
                GenerationVersion(
                    version_id=version_id,
                    engine=task.engine,
                    profile=task.profile,
                    status="failed",
                    parameters=task.parameters,
                    error=str(exc),
                ),
            )


class ServiceGenerationQueue:
    def __init__(self, router: Any) -> None:
        self.router = router
        self._resource_semaphores: dict[str, threading.Semaphore] = {}
        self._resource_guard = threading.Lock()
        self._manifest_lock = threading.Lock()
        self.status_callback: StatusCallback | None = None

    def run(
        self,
        tasks: list[GenerationTask],
        manifest: GenerationManifest,
        output_dir: Path,
        status_callback: StatusCallback | None = None,
    ) -> GenerationManifest:
        grouped: "OrderedDict[str, OrderedDict[str, list[tuple[int, GenerationTask, ServiceRoute, str]]]]" = OrderedDict()
        for index, task in enumerate(tasks):
            route = self.router.resolve_task(task)
            resource_group = route.endpoint.resource_group
            cluster_key = build_cluster_key(task, route)
            cluster_group = grouped.setdefault(resource_group, OrderedDict())
            cluster_group.setdefault(cluster_key, []).append((index, task, route, cluster_key))

        if not grouped:
            return manifest

        work_items: list[tuple[str, list[tuple[str, list[tuple[int, GenerationTask, ServiceRoute, str]]]]]] = []
        for resource_group, cluster_groups in grouped.items():
            ordered_groups = sorted(cluster_groups.items(), key=lambda item: (-len(item[1]), item[1][0][0]))
            work_items.append((resource_group, ordered_groups))

        with ThreadPoolExecutor(max_workers=len(work_items)) as executor:
            futures = [
                executor.submit(self._run_resource_clusters, resource_group, clusters, manifest, output_dir, status_callback)
                for resource_group, clusters in work_items
            ]
            for future in futures:
                future.result()
        return manifest

    def _run_resource_clusters(
        self,
        resource_group: str,
        clusters: list[tuple[str, list[tuple[int, GenerationTask, ServiceRoute, str]]]],
        manifest: GenerationManifest,
        output_dir: Path,
        status_callback: StatusCallback | None,
    ) -> None:
        for cluster_key, group in clusters:
            self._run_service_cluster(resource_group, cluster_key, group, manifest, output_dir, status_callback)

    def _run_service_cluster(
        self,
        resource_group: str,
        cluster_key: str,
        group: list[tuple[int, GenerationTask, ServiceRoute, str]],
        manifest: GenerationManifest,
        output_dir: Path,
        status_callback: StatusCallback | None,
    ) -> None:
        semaphore = self._resource_semaphore(resource_group, capacity=group[0][2].endpoint.capacity)
        with semaphore:
            (_index, first_task, route, _first_cluster) = group[0]
            self._emit(first_task, "loading", 0.05, cluster_key, None, status_callback)
            route.client.load(first_task.profile, first_task.parameters)
            try:
                for _index, task, task_route, task_cluster_key in group:
                    self._run_task(task_route, task, manifest, output_dir, task_cluster_key, status_callback)
            finally:
                route.client.unload()

    def _run_task(
        self,
        route: ServiceRoute,
        task: GenerationTask,
        manifest: GenerationManifest,
        output_dir: Path,
        cluster_key: str | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self._emit(task, "running", 0.35, cluster_key, None, status_callback)
        with self._manifest_lock:
            history = manifest.lines.get(task.line.id)
            version_number = len(history.versions) + 1 if history else 1
            version_id = f"v{version_number:03d}"
        output_path = (
            output_dir
            / task.engine.value
            / route.endpoint.service_id
            / task.profile
            / f"{task.line.id}_{version_id}.wav"
        )
        try:
            result = route.client.synthesize(
                SynthesisRequest(
                    line=task.line,
                    profile=task.profile,
                    output_path=output_path,
                    parameters=task.parameters,
                )
            )
            self._emit(task, "finalizing", 0.9, cluster_key, version_id, status_callback)
            version = GenerationVersion(
                version_id=version_id,
                engine=task.engine,
                profile=task.profile,
                service_id=route.endpoint.service_id,
                resource_group=route.endpoint.resource_group,
                provider_type=route.endpoint.provider_type,
                binding_id=task.binding_id or (route.binding.binding_id if route.binding else None),
                status="completed",
                audio_path=str(result.audio_path),
                parameters=task.parameters,
                metadata={**result.metadata, "cluster_key": cluster_key or build_cluster_key(task, route)},
            )
        except Exception as exc:
            version = GenerationVersion(
                version_id=version_id,
                engine=task.engine,
                profile=task.profile,
                service_id=route.endpoint.service_id,
                resource_group=route.endpoint.resource_group,
                provider_type=route.endpoint.provider_type,
                binding_id=task.binding_id or (route.binding.binding_id if route.binding else None),
                status="failed",
                parameters=task.parameters,
                error=str(exc),
            )
            self._emit(task, "failed", 1.0, cluster_key, version_id, status_callback)
        with self._manifest_lock:
            manifest.append_version(task.line.id, version)
        if version.status == "completed":
            self._emit(task, "completed", 1.0, cluster_key, version_id, status_callback)

    def _resource_semaphore(self, resource_group: str, capacity: int) -> threading.Semaphore:
        with self._resource_guard:
            if resource_group not in self._resource_semaphores:
                self._resource_semaphores[resource_group] = threading.Semaphore(max(1, capacity))
            return self._resource_semaphores[resource_group]

    def _emit(
        self,
        task: GenerationTask,
        status: GenerationStatus,
        progress: float,
        cluster_key: str | None,
        version_id: str | None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        callback = status_callback or self.status_callback
        if callback:
            callback(task, status, progress, cluster_key, version_id)


def build_cluster_key(task: GenerationTask, route: ServiceRoute) -> str:
    provider = route.endpoint.provider_type or task.provider_type
    service_id = route.endpoint.service_id
    params = task.parameters
    if provider == ProviderType.GPT_SOVITS or task.engine == EngineName.GPT_SOVITS:
        parts = [
            f"provider=gpt-sovits",
            f"service_id={service_id}",
            f"gpt_weights_path={params.get('gpt_weights_path', '')}",
            f"sovits_weights_path={params.get('sovits_weights_path', '')}",
            f"aux_ref_audio_paths={','.join(str(item) for item in params.get('aux_ref_audio_paths', []) or [])}",
            f"ref_audio_path={params.get('ref_audio_path', '')}",
        ]
        return "|".join(parts)
    if provider == ProviderType.INDEX_TTS or task.engine == EngineName.INDEX_TTS:
        advanced_keys = [
            "do_sample",
            "top_p",
            "top_k",
            "temperature",
            "length_penalty",
            "num_beams",
            "repetition_penalty",
            "max_mel_tokens",
            "max_text_tokens_per_segment",
        ]
        emotion_mode = str(params.get("emotion_mode", "same_as_voice"))
        emotion_source = {
            "same_as_voice": params.get("voice", ""),
            "emotion_audio": params.get("emotion_audio", ""),
            "emotion_vector": ",".join(str(item) for item in params.get("emotion_vector", []) or []),
            "emotion_text": params.get("emotion_text", ""),
        }.get(emotion_mode, "")
        parts = [
            "provider=indextts",
            f"service_id={service_id}",
            f"voice={params.get('voice', '')}",
            f"emotion_mode={emotion_mode}",
            f"emotion_source={emotion_source}",
            *[f"{key}={params.get(key, '')}" for key in advanced_keys],
        ]
        return "|".join(parts)
    parts = [
        f"provider={provider.value if provider else task.engine.value}",
        f"service_id={service_id}",
        f"model={params.get('model', '')}",
        f"voice={params.get('voice', params.get('voice_id', params.get('voice_name', '')))}",
    ]
    return "|".join(parts)


class GenerationJobManager:
    def __init__(self, queue: ServiceGenerationQueue, store: Any) -> None:
        self.queue = queue
        self.store = store
        self._jobs: dict[str, GenerationJob] = {}
        self._lock = threading.Lock()

    def submit(self, project_id: str, tasks: list[GenerationTask]) -> GenerationJob:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        items = [
            GenerationQueueItem(
                task_id=f"{job_id}-{index + 1}",
                line_id=task.line.id,
                status="queued",
                progress=0.0,
                queue_position=index + 1,
                service_id=task.service_id,
            )
            for index, task in enumerate(tasks)
        ]
        job = GenerationJob(job_id=job_id, project_id=project_id, items=items)
        with self._lock:
            self._jobs[job_id] = job
        worker = threading.Thread(target=self._run_job, args=(job_id, tasks), daemon=True)
        worker.start()
        return job

    def get(self, job_id: str) -> GenerationJob:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return self._jobs[job_id]

    def status(self) -> dict[str, Any]:
        with self._lock:
            jobs = list(self._jobs.values())
        queued = sum(1 for job in jobs for item in job.items if item.status == "queued")
        running = sum(1 for job in jobs for item in job.items if item.status in {"loading", "running", "finalizing"})
        return {"jobs": [job.model_dump(mode="json") for job in jobs], "queued": queued, "running": running}

    def cancel(self, job_id: str) -> GenerationJob:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {"completed", "failed"}:
                return job
            job.status = "cancelled"
            job.updated_at = datetime.now(timezone.utc)
            for item in job.items:
                if item.status == "queued":
                    item.status = "cancelled"
            return job

    def _run_job(self, job_id: str, tasks: list[GenerationTask]) -> None:
        try:
            with self._lock:
                job = self._jobs[job_id]
                if job.status == "cancelled":
                    return
                job.status = "running"
                job.updated_at = datetime.now(timezone.utc)
            manifest = self.store.load_manifest(self.get(job_id).project_id)
            output_dir = self.store.project_dir(self.get(job_id).project_id) / "audio"
            runnable_tasks = self._runnable_tasks(job_id, tasks)
            self.queue.run(
                runnable_tasks,
                manifest,
                output_dir=output_dir,
                status_callback=lambda task, status, progress, cluster_key, version_id: self._update_item(
                    job_id, task, status, progress, cluster_key, version_id
                ),
            )
            self.store.save_manifest(manifest)
            self._finish_job(job_id)
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = str(exc)
                job.updated_at = datetime.now(timezone.utc)

    def _runnable_tasks(self, job_id: str, tasks: list[GenerationTask]) -> list[GenerationTask]:
        with self._lock:
            job = self._jobs[job_id]
            if job.status == "cancelled":
                return []
            cancelled = {item.line_id for item in job.items if item.status == "cancelled"}
        return [task for task in tasks if task.line.id not in cancelled]

    def _update_item(
        self,
        job_id: str,
        task: GenerationTask,
        status: GenerationStatus,
        progress: float,
        cluster_key: str | None,
        version_id: str | None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for item in job.items:
                if item.line_id == task.line.id:
                    item.status = status
                    item.progress = progress
                    item.cluster_key = cluster_key or item.cluster_key
                    item.service_id = task.service_id or item.service_id
                    item.version_id = version_id or item.version_id
            job.progress = sum(item.progress for item in job.items) / max(1, len(job.items))
            job.updated_at = datetime.now(timezone.utc)

    def _finish_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.status == "cancelled":
                return
            if any(item.status == "failed" for item in job.items):
                job.status = "failed"
            else:
                job.status = "completed"
            job.progress = 1.0
            job.updated_at = datetime.now(timezone.utc)
