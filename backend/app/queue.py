from __future__ import annotations

import threading
import uuid
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.adapters.base import EngineAdapter, SynthesisRequest
from app.models import EngineName, GenerationJob, GenerationManifest, GenerationQueueItem, GenerationStatus, GenerationTask, GenerationVersion, ProviderType
from app.services import ServiceRoute, build_load_signature

StatusCallback = Callable[[GenerationTask, GenerationStatus, float, str | None, str | None], None]


def _task_line_uid(task: GenerationTask) -> str:
    return task.line.line_uid or task.line.id


def _safe_line_output_stem(task: GenerationTask) -> str:
    return re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", _task_line_uid(task)).strip("._-") or task.line.id


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
        history = manifest.history_for_line(task.line.id, _task_line_uid(task))
        version_number = len(history.versions) + 1 if history else 1
        version_id = f"v{version_number:03d}"
        output_path = output_dir / task.engine.value / task.profile / f"{_safe_line_output_stem(task)}_{version_id}.wav"
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
                    line_uid=_task_line_uid(task),
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
                    line_uid=_task_line_uid(task),
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
        self._loaded_signatures: dict[str, str] = {}
        self._load_states: dict[str, dict[str, Any]] = {}
        self.status_callback: StatusCallback | None = None

    def load_state(self, service_id: str) -> dict[str, Any]:
        loaded_signature = self._loaded_signatures.get(service_id)
        state = self._load_states.get(service_id, {})
        return {
            "service_id": service_id,
            "loaded_signature": loaded_signature,
            "loaded": loaded_signature is not None,
            "verification_level": state.get("verification_level"),
            "updated_at": state.get("updated_at"),
            "last_error": state.get("last_error"),
            "last_error_at": state.get("last_error_at"),
        }

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
            first_signature = build_load_signature(route.endpoint, first_task.parameters)
            if self._loaded_signatures.get(route.endpoint.service_id) != first_signature:
                try:
                    route.client.load(first_task.profile, first_task.parameters)
                except Exception as exc:
                    self._mark_load_failed(route.endpoint.service_id, exc)
                    for _failed_index, failed_task, failed_route, failed_cluster_key in group:
                        version_id = self._append_failed_version(
                            failed_route,
                            failed_task,
                            manifest,
                            failed_cluster_key,
                            "loading",
                            exc,
                        )
                        self._emit(failed_task, "failed", 1.0, failed_cluster_key, version_id, status_callback)
                    raise
                self._mark_loaded(route.endpoint.service_id, first_signature, "loaded_unverified")
            for _index, task, task_route, task_cluster_key in group:
                task_signature = build_load_signature(task_route.endpoint, task.parameters)
                if self._loaded_signatures.get(task_route.endpoint.service_id) != task_signature:
                    self._emit(task, "loading", 0.12, task_cluster_key, None, status_callback)
                    try:
                        task_route.client.load(task.profile, task.parameters)
                    except Exception as exc:
                        self._mark_load_failed(task_route.endpoint.service_id, exc)
                        version_id = self._append_failed_version(
                            task_route,
                            task,
                            manifest,
                            task_cluster_key,
                            "loading",
                            exc,
                        )
                        self._emit(task, "failed", 1.0, task_cluster_key, version_id, status_callback)
                        raise
                    self._mark_loaded(task_route.endpoint.service_id, task_signature, "loaded_unverified")
                self._run_task(task_route, task, manifest, output_dir, task_cluster_key, status_callback)

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
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            version_number = len(history.versions) + 1 if history else 1
            version_id = f"v{version_number:03d}"
        output_path = (
            output_dir
            / task.engine.value
            / route.endpoint.service_id
            / task.profile
            / f"{_safe_line_output_stem(task)}_{version_id}.wav"
        )
        requested_load_signature = build_load_signature(route.endpoint, task.parameters)
        revision_context = _revision_context(task)
        binding_snapshot = route.binding.model_dump(mode="json") if route.binding else None
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
            load_verification_level = str(result.metadata.get("load_verification_level", "assumed_after_success"))
            verified_load_signature = str(result.metadata.get("verified_load_signature") or requested_load_signature)
            self._mark_loaded(route.endpoint.service_id, verified_load_signature, load_verification_level)
            version = GenerationVersion(
                version_id=version_id,
                line_uid=_task_line_uid(task),
                script_revision_id=revision_context.get("script_revision_id"),
                parse_revision_id=revision_context.get("parse_revision_id"),
                engine=task.engine,
                profile=task.profile,
                service_id=route.endpoint.service_id,
                resource_group=route.endpoint.resource_group,
                provider_type=route.endpoint.provider_type,
                binding_id=task.binding_id or (route.binding.binding_id if route.binding else None),
                binding_snapshot=binding_snapshot,
                requested_load_signature=requested_load_signature,
                verified_load_signature=verified_load_signature,
                status="completed",
                audio_path=str(result.audio_path),
                parameters=task.parameters,
                metadata={
                    **result.metadata,
                    "cluster_key": cluster_key or build_cluster_key(task, route),
                    "requested_load_signature": requested_load_signature,
                    "verified_load_signature": verified_load_signature,
                    "load_verification_level": load_verification_level,
                },
            )
        except Exception as exc:
            failed_version_id = self._append_failed_version(
                route,
                task,
                manifest,
                cluster_key,
                "synthesis",
                exc,
            )
            self._emit(task, "failed", 1.0, cluster_key, failed_version_id, status_callback)
            return
        with self._manifest_lock:
            manifest.append_version(task.line.id, version)
        if version.status == "completed":
            self._emit(task, "completed", 1.0, cluster_key, version_id, status_callback)

    def _append_failed_version(
        self,
        route: ServiceRoute,
        task: GenerationTask,
        manifest: GenerationManifest,
        cluster_key: str | None,
        failure_stage: str,
        exc: Exception,
    ) -> str:
        requested_load_signature = build_load_signature(route.endpoint, task.parameters)
        revision_context = _revision_context(task)
        binding_snapshot = route.binding.model_dump(mode="json") if route.binding else None
        with self._manifest_lock:
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            version_id = f"v{(len(history.versions) if history else 0) + 1:03d}"
            manifest.append_version(
                task.line.id,
                GenerationVersion(
                    version_id=version_id,
                    line_uid=_task_line_uid(task),
                    script_revision_id=revision_context.get("script_revision_id"),
                    parse_revision_id=revision_context.get("parse_revision_id"),
                    engine=task.engine,
                    profile=task.profile,
                    service_id=route.endpoint.service_id,
                    resource_group=route.endpoint.resource_group,
                    provider_type=route.endpoint.provider_type,
                    binding_id=task.binding_id or (route.binding.binding_id if route.binding else None),
                    binding_snapshot=binding_snapshot,
                    requested_load_signature=requested_load_signature,
                    status="failed",
                    parameters=task.parameters,
                    metadata={
                        "cluster_key": cluster_key or build_cluster_key(task, route),
                        "failure_stage": failure_stage,
                        "requested_load_signature": requested_load_signature,
                    },
                    error=str(exc),
                ),
            )
        return version_id

    def _mark_loaded(self, service_id: str, signature: str, verification_level: str) -> None:
        self._loaded_signatures[service_id] = signature
        self._load_states[service_id] = {
            "verification_level": verification_level,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_error": None,
            "last_error_at": None,
        }

    def _mark_load_failed(self, service_id: str, exc: Exception) -> None:
        current = self._load_states.get(service_id, {})
        self._load_states[service_id] = {
            **current,
            "last_error": str(exc),
            "last_error_at": datetime.now(timezone.utc).isoformat(),
        }

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
    if provider == ProviderType.COSYVOICE or task.engine == EngineName.COSYVOICE:
        parts = [
            "provider=cosyvoice",
            f"service_id={service_id}",
            f"mode={params.get('mode', 'zero_shot')}",
            f"speaker_id={params.get('speaker_id', params.get('voice', ''))}",
            f"prompt_audio_path={params.get('prompt_audio_path', params.get('prompt_audio', params.get('reference_audio', '')))}",
            f"prompt_text={params.get('prompt_text', '')}",
            f"instruct_text={params.get('instruct_text', params.get('instruction', ''))}",
            f"speed={params.get('speed', '')}",
            f"seed={params.get('seed', '')}",
        ]
        return "|".join(parts)
    parts = [
        f"provider={provider.value if provider else task.engine.value}",
        f"service_id={service_id}",
        f"model={params.get('model', '')}",
        f"voice={params.get('voice', params.get('voice_id', params.get('voice_name', '')))}",
    ]
    return "|".join(parts)


def _revision_context(task: GenerationTask) -> dict[str, str | None]:
    return {
        "script_revision_id": _string_or_none(task.parameters.get("_script_revision_id")),
        "parse_revision_id": _string_or_none(task.parameters.get("_parse_revision_id")),
    }


def _string_or_none(value: Any) -> str | None:
    return str(value) if value else None


class GenerationJobManager:
    def __init__(self, queue: ServiceGenerationQueue, store: Any) -> None:
        self.queue = queue
        self.store = store
        self._jobs: dict[str, GenerationJob] = {}
        self._lock = threading.Lock()

    def submit(self, project_id: str, tasks: list[GenerationTask]) -> GenerationJob:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        diagnostics = self._task_diagnostics(tasks)
        items = [
            GenerationQueueItem(
                task_id=f"{job_id}-{index + 1}",
                line_id=task.line.id,
                line_uid=_task_line_uid(task),
                status="failed" if diagnostics[index].get("error") else "queued",
                progress=1.0 if diagnostics[index].get("error") else 0.0,
                queue_position=index + 1,
                cluster_key=diagnostics[index].get("cluster_key", ""),
                cluster_size=diagnostics[index].get("cluster_size"),
                cluster_position=diagnostics[index].get("cluster_position"),
                load_signature=diagnostics[index].get("load_signature"),
                service_id=diagnostics[index].get("service_id") or task.service_id,
                resource_group=diagnostics[index].get("resource_group"),
                error=diagnostics[index].get("error"),
            )
            for index, task in enumerate(tasks)
        ]
        job = GenerationJob(job_id=job_id, project_id=project_id, items=items)
        with self._lock:
            self._jobs[job_id] = job
        worker = threading.Thread(target=self._run_job, args=(job_id, tasks), daemon=True)
        worker.start()
        return job

    def _task_diagnostics(self, tasks: list[GenerationTask]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        cluster_counts: dict[str, int] = {}
        cluster_positions: dict[str, int] = {}
        for task in tasks:
            prefail_error = task.parameters.get("_prefail_error")
            if prefail_error:
                output.append({"error": str(prefail_error)})
                continue
            try:
                route = self.queue.router.resolve_task(task)
                cluster_key = build_cluster_key(task, route)
                item = {
                    "cluster_key": cluster_key,
                    "load_signature": build_load_signature(route.endpoint, task.parameters),
                    "service_id": route.endpoint.service_id,
                    "resource_group": route.endpoint.resource_group,
                }
                cluster_counts[cluster_key] = cluster_counts.get(cluster_key, 0) + 1
            except Exception as exc:
                item = {"error": str(exc)}
            output.append(item)
        for item in output:
            cluster_key = item.get("cluster_key")
            if not cluster_key:
                continue
            cluster_positions[cluster_key] = cluster_positions.get(cluster_key, 0) + 1
            item["cluster_position"] = cluster_positions[cluster_key]
            item["cluster_size"] = cluster_counts.get(cluster_key)
        return output

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
        manifest: GenerationManifest | None = None
        try:
            with self._lock:
                job = self._jobs[job_id]
                if job.status == "cancelled":
                    return
                job.status = "running"
                job.updated_at = datetime.now(timezone.utc)
            manifest = self.store.load_manifest(self.get(job_id).project_id)
            output_dir = self.store.project_dir(self.get(job_id).project_id) / "audio"
            self._record_prefailed_items(job_id, tasks, manifest)
            runnable_tasks = self._runnable_tasks(job_id, tasks)
            self.queue.run(
                runnable_tasks,
                manifest,
                output_dir=output_dir,
                status_callback=lambda task, status, progress, cluster_key, version_id: self._update_item(
                    job_id, task, status, progress, cluster_key, version_id
                ),
            )
            self._sync_item_errors_from_manifest(job_id, manifest)
            self.store.save_manifest(manifest)
            self._finish_job(job_id)
        except Exception as exc:
            if manifest is not None:
                self.store.save_manifest(manifest)
            failed_at = datetime.now(timezone.utc)
            with self._lock:
                job = self._jobs[job_id]
                if job.status == "cancelled":
                    return
                job.status = "failed"
                job.error = str(exc)
                job.updated_at = failed_at
                for item in job.items:
                    if item.status not in {"completed", "failed", "cancelled"}:
                        item.status = "failed"
                        item.progress = 1.0
                        item.error = item.error or str(exc)
                job.progress = sum(item.progress for item in job.items) / max(1, len(job.items))

    def _record_prefailed_items(self, job_id: str, tasks: list[GenerationTask], manifest: GenerationManifest) -> None:
        tasks_by_key = {(task.line.id, _task_line_uid(task)): task for task in tasks}
        with self._lock:
            job = self._jobs[job_id]
            failed_items = [item for item in job.items if item.status == "failed" and item.error and item.version_id is None]

        for item in failed_items:
            task = tasks_by_key.get((item.line_id, item.line_uid or item.line_id))
            if task is None:
                continue
            history = manifest.history_for_line(item.line_id, item.line_uid)
            version_id = f"v{(len(history.versions) if history else 0) + 1:03d}"
            revision_context = _revision_context(task)
            manifest.append_version(
                item.line_id,
                GenerationVersion(
                    version_id=version_id,
                    line_uid=_task_line_uid(task),
                    script_revision_id=revision_context.get("script_revision_id"),
                    parse_revision_id=revision_context.get("parse_revision_id"),
                    engine=task.engine,
                    profile=task.profile,
                    service_id=item.service_id or task.service_id,
                    resource_group=item.resource_group,
                    provider_type=task.provider_type,
                    binding_id=task.binding_id,
                    requested_load_signature=item.load_signature,
                    status="failed",
                    parameters=task.parameters,
                    metadata={"failure_stage": "routing", "cluster_key": item.cluster_key},
                    error=item.error,
                ),
            )
            with self._lock:
                for current in self._jobs[job_id].items:
                    if current.task_id == item.task_id:
                        current.version_id = version_id

    def _runnable_tasks(self, job_id: str, tasks: list[GenerationTask]) -> list[GenerationTask]:
        with self._lock:
            job = self._jobs[job_id]
            if job.status == "cancelled":
                return []
            skipped = {(item.line_id, item.line_uid or item.line_id) for item in job.items if item.status in {"cancelled", "failed"}}
        return [task for task in tasks if (task.line.id, _task_line_uid(task)) not in skipped]

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
                if item.line_id == task.line.id and (item.line_uid or item.line_id) == _task_line_uid(task):
                    item.status = status
                    item.progress = progress
                    item.cluster_key = cluster_key or item.cluster_key
                    item.service_id = task.service_id or item.service_id
                    item.version_id = version_id or item.version_id
                    if item.load_signature is None:
                        try:
                            route = self.queue.router.resolve_task(task)
                            item.load_signature = build_load_signature(route.endpoint, task.parameters)
                            item.service_id = item.service_id or route.endpoint.service_id
                            item.resource_group = item.resource_group or route.endpoint.resource_group
                        except Exception:
                            pass
            job.progress = sum(item.progress for item in job.items) / max(1, len(job.items))
            job.updated_at = datetime.now(timezone.utc)

    def _sync_item_errors_from_manifest(self, job_id: str, manifest: GenerationManifest) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for item in job.items:
                if item.status != "failed" or item.error:
                    continue
                history = manifest.history_for_line(item.line_id, item.line_uid)
                if history is None:
                    continue
                version = None
                if item.version_id:
                    version = next((candidate for candidate in history.versions if candidate.version_id == item.version_id), None)
                if version is None:
                    version = next((candidate for candidate in reversed(history.versions) if candidate.status == "failed"), None)
                if version is not None and version.error:
                    item.error = version.error

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
