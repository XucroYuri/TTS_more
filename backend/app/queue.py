from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.adapters.base import SynthesisCancelled, SynthesisRequest, SynthesisTimeout
from app.models import EngineName, GenerationJob, GenerationManifest, GenerationQueueItem, GenerationStatus, GenerationTask, GenerationVersion, ProviderType
from app.net_guard import scrub_error
from app.services import COMFYUI_TTS_CONTRACTS, ServiceRoute, build_load_signature

ExternalStatusUpdate = dict[str, Any]
StatusCallback = Callable[[GenerationTask, GenerationStatus, float, str | None, str | None, ExternalStatusUpdate | None], None]

logger = logging.getLogger(__name__)


def _task_line_uid(task: GenerationTask) -> str:
    return task.line.line_uid or task.line.id


def _safe_line_output_stem(task: GenerationTask) -> str:
    return re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", _task_line_uid(task)).strip("._-") or task.line.id


def _safe_output_namespace(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._-") or "job"


class ServiceGenerationQueue:
    def __init__(self, router: Any) -> None:
        self.router = router
        self._resource_semaphores: dict[str, threading.Semaphore] = {}
        self._resource_guard = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._loaded_signatures: dict[str, str] = {}
        self._load_states: dict[str, dict[str, Any]] = {}
        self._active_resource_services: dict[str, tuple[str, Any]] = {}
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
        cancel_check: Callable[[], bool] | None = None,
        output_namespace: str | None = None,
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
                executor.submit(
                    self._run_resource_clusters,
                    resource_group,
                    clusters,
                    manifest,
                    output_dir,
                    status_callback,
                    cancel_check,
                    output_namespace,
                )
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
        cancel_check: Callable[[], bool] | None = None,
        output_namespace: str | None = None,
    ) -> None:
        for cluster_key, group in clusters:
            if cancel_check and cancel_check():
                return
            self._run_service_cluster(
                resource_group,
                cluster_key,
                group,
                manifest,
                output_dir,
                status_callback,
                cancel_check,
                output_namespace,
            )

    def _run_service_cluster(
        self,
        resource_group: str,
        cluster_key: str,
        group: list[tuple[int, GenerationTask, ServiceRoute, str]],
        manifest: GenerationManifest,
        output_dir: Path,
        status_callback: StatusCallback | None,
        cancel_check: Callable[[], bool] | None = None,
        output_namespace: str | None = None,
    ) -> None:
        semaphore = self._resource_semaphore(resource_group, capacity=group[0][2].endpoint.capacity)
        with semaphore:
            if cancel_check and cancel_check():
                return
            (_index, first_task, route, _first_cluster) = group[0]
            self._emit(first_task, "loading", 0.05, cluster_key, None, status_callback)
            first_signature = build_load_signature(route.endpoint, first_task.parameters)
            try:
                self._evict_other_service(resource_group, route)
            except Exception as exc:
                self._mark_load_failed(route.endpoint.service_id, exc)
                for _failed_index, failed_task, failed_route, failed_cluster_key in group:
                    version_id = self._append_failed_version(
                        failed_route, failed_task, manifest, failed_cluster_key, "unloading", exc
                    )
                    self._emit(failed_task, "failed", 1.0, failed_cluster_key, version_id, status_callback)
                raise
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
            self._active_resource_services[resource_group] = (route.endpoint.service_id, route.client)
            for _index, task, task_route, task_cluster_key in group:
                # Stop dispatching new lines in this cluster if the job was cancelled.
                if cancel_check and cancel_check():
                    return
                task_signature = build_load_signature(task_route.endpoint, task.parameters)
                try:
                    self._evict_other_service(resource_group, task_route)
                except Exception as exc:
                    self._mark_load_failed(task_route.endpoint.service_id, exc)
                    version_id = self._append_failed_version(
                        task_route, task, manifest, task_cluster_key, "unloading", exc
                    )
                    self._emit(task, "failed", 1.0, task_cluster_key, version_id, status_callback)
                    raise
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
                self._active_resource_services[resource_group] = (task_route.endpoint.service_id, task_route.client)
                self._run_task(
                    task_route,
                    task,
                    manifest,
                    output_dir,
                    task_cluster_key,
                    status_callback,
                    cancel_check,
                    output_namespace,
                )

    def _run_task(
        self,
        route: ServiceRoute,
        task: GenerationTask,
        manifest: GenerationManifest,
        output_dir: Path,
        cluster_key: str | None = None,
        status_callback: StatusCallback | None = None,
        cancel_check: Callable[[], bool] | None = None,
        output_namespace: str | None = None,
    ) -> None:
        self._emit(task, "running", 0.35, cluster_key, None, status_callback)
        with self._manifest_lock:
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            version_number = len(history.versions) + 1 if history else 1
            version_id = f"v{version_number:03d}"
        versioned_stem = f"{_safe_line_output_stem(task)}_{version_id}"
        if output_namespace is not None:
            versioned_stem = f"{_safe_line_output_stem(task)}_{_safe_output_namespace(output_namespace)}_{version_id}"
        output_path = (
            output_dir
            / task.engine.value
            / route.endpoint.service_id
            / task.profile
            / f"{versioned_stem}.wav"
        )
        requested_load_signature = build_load_signature(route.endpoint, task.parameters)
        revision_context = _revision_context(task)
        binding_snapshot = route.binding.model_dump(mode="json") if route.binding else None

        def progress_callback(update: ExternalStatusUpdate) -> None:
            external_progress = update.get("progress")
            mapped_progress = 0.45
            if isinstance(external_progress, (int, float)):
                mapped_progress = max(0.35, min(0.88, float(external_progress)))
            self._emit(task, "running", mapped_progress, cluster_key, None, status_callback, update)

        try:
            result = route.client.synthesize(
                SynthesisRequest(
                    line=task.line,
                    profile=task.profile,
                    output_path=output_path,
                    parameters=task.parameters,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )
            )
            if cancel_check and cancel_check():
                self._record_cancelled_outcome(
                    route,
                    task,
                    manifest,
                    cluster_key,
                    output_paths=(output_path, result.audio_path),
                    details=result.metadata,
                    status_callback=status_callback,
                )
                return
            self._emit(task, "finalizing", 0.9, cluster_key, version_id, status_callback)
            if cancel_check and cancel_check():
                self._record_cancelled_outcome(
                    route,
                    task,
                    manifest,
                    cluster_key,
                    output_paths=(output_path, result.audio_path),
                    details=result.metadata,
                    status_callback=status_callback,
                )
                return
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
        except SynthesisCancelled as exc:
            cleanup_errors = self._discard_uncommitted_output(output_path)
            if exc.details.get("converged") is True:
                self._record_cancelled_outcome(
                    route,
                    task,
                    manifest,
                    cluster_key,
                    output_paths=(),
                    details=self._with_output_cleanup_errors(exc.details, cleanup_errors),
                    status_callback=status_callback,
                    error=exc,
                    force_failed=bool(cleanup_errors),
                )
            else:
                failed_version_id = self._append_failed_version(
                    route,
                    task,
                    manifest,
                    cluster_key,
                    "cancellation_cleanup",
                    exc,
                    control_code=exc.code,
                    control_details=self._with_output_cleanup_errors(exc.details, cleanup_errors),
                )
                self._emit(task, "failed", 1.0, cluster_key, failed_version_id, status_callback)
            return
        except SynthesisTimeout as exc:
            cleanup_errors = self._discard_uncommitted_output(output_path)
            failed_version_id = self._append_failed_version(
                route,
                task,
                manifest,
                cluster_key,
                "timeout",
                exc,
                control_code=exc.code,
                control_details=self._with_output_cleanup_errors(exc.details, cleanup_errors),
            )
            self._emit(task, "failed", 1.0, cluster_key, failed_version_id, status_callback)
            return
        except Exception as exc:
            cleanup_errors = self._discard_uncommitted_output(output_path)
            failed_version_id = self._append_failed_version(
                route,
                task,
                manifest,
                cluster_key,
                "synthesis",
                exc,
                extra_metadata=self._with_output_cleanup_errors({}, cleanup_errors),
            )
            self._emit(task, "failed", 1.0, cluster_key, failed_version_id, status_callback)
            return
        with self._manifest_lock:
            manifest.append_version(task.line.id, version)
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            committed_version_id = history.versions[-1].version_id if history else version_id
        if version.status == "completed":
            self._emit(task, "completed", 1.0, cluster_key, committed_version_id, status_callback)
            if cancel_check and cancel_check():
                self._remove_manifest_version(manifest, task, committed_version_id)
                self._record_cancelled_outcome(
                    route,
                    task,
                    manifest,
                    cluster_key,
                    output_paths=(output_path, result.audio_path),
                    details=result.metadata,
                    status_callback=status_callback,
                )

    def _append_failed_version(
        self,
        route: ServiceRoute,
        task: GenerationTask,
        manifest: GenerationManifest,
        cluster_key: str | None,
        failure_stage: str,
        exc: Exception,
        *,
        control_code: str | None = None,
        control_details: dict[str, Any] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> str:
        requested_load_signature = build_load_signature(route.endpoint, task.parameters)
        revision_context = _revision_context(task)
        binding_snapshot = route.binding.model_dump(mode="json") if route.binding else None
        with self._manifest_lock:
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            version_id = f"v{(len(history.versions) if history else 0) + 1:03d}"
            metadata: dict[str, Any] = {
                "cluster_key": cluster_key or build_cluster_key(task, route),
                "failure_stage": failure_stage,
                "requested_load_signature": requested_load_signature,
            }
            if control_code is not None:
                metadata["control_code"] = control_code
            if control_details is not None:
                metadata["control_details"] = self._sanitize_control_details(control_details)
            if extra_metadata:
                metadata.update(self._sanitize_control_details(extra_metadata))
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
                    metadata=metadata,
                    error=scrub_error(exc, route.endpoint.base_url),
                ),
            )
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            return history.versions[-1].version_id if history else version_id

    def _append_cancelled_version(
        self,
        route: ServiceRoute,
        task: GenerationTask,
        manifest: GenerationManifest,
        cluster_key: str | None,
        *,
        details: dict[str, Any],
        error: Exception | None = None,
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
                    status="cancelled",
                    parameters=task.parameters,
                    metadata={
                        "cluster_key": cluster_key or build_cluster_key(task, route),
                        "requested_load_signature": requested_load_signature,
                        "control_code": "cancelled",
                        "control_details": self._sanitize_control_details(details),
                    },
                    error=scrub_error(error, route.endpoint.base_url) if error is not None else None,
                ),
            )
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            return history.versions[-1].version_id if history else version_id

    def _record_cancelled_outcome(
        self,
        route: ServiceRoute,
        task: GenerationTask,
        manifest: GenerationManifest,
        cluster_key: str | None,
        *,
        output_paths: tuple[Path, ...],
        details: dict[str, Any],
        status_callback: StatusCallback | None,
        error: Exception | None = None,
        force_failed: bool = False,
    ) -> None:
        cleanup_errors = self._discard_uncommitted_output(*output_paths)
        control_details = self._with_output_cleanup_errors(details, cleanup_errors)
        if force_failed or cleanup_errors:
            prior_cleanup_error = control_details.get("output_cleanup_error")
            if cleanup_errors:
                cleanup_error = RuntimeError(f"uncommitted output cleanup failed: {'; '.join(cleanup_errors)}")
            elif prior_cleanup_error:
                cleanup_error = RuntimeError(f"uncommitted output cleanup failed: {prior_cleanup_error}")
            else:
                cleanup_error = error or RuntimeError("uncommitted output cleanup failed")
            version_id = self._append_failed_version(
                route,
                task,
                manifest,
                cluster_key,
                "cancellation_cleanup",
                cleanup_error,
                control_code="cancelled",
                control_details=control_details,
            )
            self._emit(task, "failed", 1.0, cluster_key, version_id, status_callback)
            return
        version_id = self._append_cancelled_version(
            route,
            task,
            manifest,
            cluster_key,
            details=control_details,
            error=error,
        )
        self._emit(task, "cancelled", 1.0, cluster_key, version_id, status_callback)

    @staticmethod
    def _discard_uncommitted_output(*paths: Path) -> list[str]:
        errors: list[str] = []
        for path in set(paths):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                diagnostic = scrub_error(exc)
                errors.append(f"{path.name}: {diagnostic}")
                logger.warning("Failed to discard uncommitted generation output %s: %s", path, diagnostic)
        return errors

    @staticmethod
    def _with_output_cleanup_errors(details: dict[str, Any], cleanup_errors: list[str]) -> dict[str, Any]:
        output = dict(details)
        if cleanup_errors:
            output["output_cleanup_error"] = "; ".join(cleanup_errors)
        return output

    def _remove_manifest_version(self, manifest: GenerationManifest, task: GenerationTask, version_id: str) -> None:
        with self._manifest_lock:
            history = manifest.history_for_line(task.line.id, _task_line_uid(task))
            if history is not None:
                history.versions = [version for version in history.versions if version.version_id != version_id]

    @classmethod
    def _sanitize_control_details(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {scrub_error(str(key)): cls._sanitize_control_details(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._sanitize_control_details(item) for item in value]
        if isinstance(value, str):
            return scrub_error(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return scrub_error(str(value))

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

    def _evict_other_service(self, resource_group: str, target_route: ServiceRoute) -> None:
        active = self._active_resource_services.get(resource_group)
        if active is None or active[0] == target_route.endpoint.service_id:
            return
        active_service_id, active_client = active
        active_client.unload()
        self._active_resource_services.pop(resource_group, None)
        self._loaded_signatures.pop(active_service_id, None)
        self._load_states[active_service_id] = {
            "verification_level": "unloaded_after_resource_switch",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_error": None,
            "last_error_at": None,
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
        external_update: ExternalStatusUpdate | None = None,
    ) -> None:
        callback = status_callback or self.status_callback
        if callback:
            callback(task, status, progress, cluster_key, version_id, external_update)


def build_cluster_key(task: GenerationTask, route: ServiceRoute) -> str:
    provider = route.endpoint.provider_type or task.provider_type
    service_id = route.endpoint.service_id
    params = task.parameters
    if route.endpoint.api_contract in COMFYUI_TTS_CONTRACTS:
        effective_engine = str(params.get("engine") or (route.endpoint.engine.value if route.endpoint.engine else task.engine.value))
        reference_audio = params.get(
            "reference_audio",
            params.get("ref_audio_path", params.get("prompt_audio_path", "")),
        )
        parts = [
            f"provider={provider.value if provider else task.engine.value}",
            f"engine={effective_engine}",
            f"service_id={service_id}",
            f"resource_id={params.get('resource_id', route.endpoint.default_params.get('resource_id', ''))}",
            f"reference_audio={reference_audio}",
            f"prompt_text={params.get('prompt_text', '')}",
            f"instruct_text={params.get('instruct_text', params.get('instruction', ''))}",
            f"speed={params.get('speed', '')}",
            f"seed={params.get('seed', '')}",
        ]
        return "|".join(parts)
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
    if provider == ProviderType.COMFYUI or task.engine == EngineName.COMFYUI:
        effective_engine = str(params.get("engine", task.engine.value))
        parts = [
            f"provider=comfyui",
            f"engine={effective_engine}",
            f"service_id={service_id}",
            f"model_path={params.get('model_path', '')}",
            f"reference_audio={params.get('reference_audio', params.get('prompt_audio_path', ''))}",
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
    # Bounded job store and concurrency to prevent unbounded memory/thread
    # growth from a flood of generation requests.
    MAX_JOBS = int(os.environ.get("TTS_MORE_MAX_JOBS", "200"))
    MAX_ACTIVE_JOBS = int(os.environ.get("TTS_MORE_MAX_ACTIVE_JOBS", "8"))
    # Completed/failed jobs are evicted after this many seconds.
    JOB_RETENTION_SECONDS = int(os.environ.get("TTS_MORE_JOB_RETENTION_SECONDS", "3600"))

    def __init__(self, queue: ServiceGenerationQueue, store: Any) -> None:
        self.queue = queue
        self.store = store
        self._jobs: "OrderedDict[str, GenerationJob]" = OrderedDict()
        self._lock = threading.Lock()
        self._project_persistence_locks: dict[str, threading.Lock] = {}
        self._project_persistence_guard = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=self.MAX_ACTIVE_JOBS, thread_name_prefix="tts-job")

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
            self._evict_locked()
            if len(self._jobs) >= self.MAX_JOBS:
                job.status = "failed"
                job.error = "job queue is full"
                job.updated_at = datetime.now(timezone.utc)
                self._jobs[job_id] = job
                return job
            self._jobs[job_id] = job
        self._executor.submit(self._run_job, job_id, tasks)
        return job

    def _evict_locked(self) -> None:
        """Drop finished jobs older than JOB_RETENTION_SECONDS. Caller holds the lock."""
        if not self._jobs:
            return
        cutoff = datetime.now(timezone.utc).timestamp() - self.JOB_RETENTION_SECONDS
        stale: list[str] = []
        for job_id, job in self._jobs.items():
            if job.status in {"completed", "failed", "cancelled"}:
                ts = job.updated_at.timestamp() if job.updated_at else 0
                if ts and ts < cutoff:
                    stale.append(job_id)
        for job_id in stale:
            self._jobs.pop(job_id, None)

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
            self._evict_locked()
            jobs = list(self._jobs.values())
        queued = sum(1 for job in jobs for item in job.items if item.status == "queued")
        running = sum(1 for job in jobs for item in job.items if item.status in {"loading", "running", "finalizing", "cancelling"})
        return {"jobs": [job.model_dump(mode="json") for job in jobs], "queued": queued, "running": running}

    def cancel(self, job_id: str) -> GenerationJob:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {"completed", "failed", "cancelled", "cancelling"}:
                return job
            has_active = False
            for item in job.items:
                if item.status == "queued":
                    item.status = "cancelled"
                    item.progress = 1.0
                elif item.status in {"loading", "running", "finalizing"}:
                    item.status = "cancelling"
                    has_active = True
            if has_active:
                job.status = "cancelling"
            elif any(item.status == "failed" for item in job.items):
                job.status = "failed"
            elif job.items and all(item.status == "completed" for item in job.items):
                job.status = "completed"
                job.progress = 1.0
            else:
                job.status = "cancelled"
                job.progress = 1.0
            job.updated_at = datetime.now(timezone.utc)
            return job

    def _is_cancelled(self, job_id: str) -> bool:
        """Check whether a job has been cancelled (called between line dispatches)."""
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.status in {"cancelling", "cancelled"})

    def _run_job(self, job_id: str, tasks: list[GenerationTask]) -> None:
        manifest: GenerationManifest | None = None
        baseline: GenerationManifest | None = None
        persistence_attempted = False
        try:
            with self._lock:
                job = self._jobs[job_id]
                if job.status in {"cancelling", "cancelled"}:
                    return
                job.status = "running"
                job.updated_at = datetime.now(timezone.utc)
            project_id = self.get(job_id).project_id
            loaded_manifest = self.store.load_manifest(project_id)
            baseline = loaded_manifest.model_copy(deep=True)
            manifest = loaded_manifest.model_copy(deep=True)
            output_dir = self.store.project_audio_dir(project_id)
            self._record_prefailed_items(job_id, tasks, manifest)
            runnable_tasks = self._runnable_tasks(job_id, tasks)
            self.queue.run(
                runnable_tasks,
                manifest,
                output_dir=output_dir,
                status_callback=lambda task, status, progress, cluster_key, version_id, external_update: self._update_item(
                    job_id, task, status, progress, cluster_key, version_id, external_update
                ),
                cancel_check=lambda: self._is_cancelled(job_id),
                output_namespace=job_id,
            )
            self._sync_item_errors_from_manifest(job_id, manifest)
            persistence_attempted = True
            self._persist_manifest_delta(job_id, baseline, manifest)
            self._finish_job(job_id)
        except Exception as exc:
            failure_message = scrub_error(exc)
            persistence_failed = persistence_attempted
            if manifest is not None and baseline is not None and not persistence_attempted:
                try:
                    persistence_attempted = True
                    self._persist_manifest_delta(job_id, baseline, manifest)
                except Exception as persistence_exc:
                    persistence_failed = True
                    failure_message = (
                        f"{failure_message}; manifest persistence failed: {scrub_error(persistence_exc)}"
                    )
            elif persistence_failed:
                failure_message = f"manifest persistence failed: {failure_message}"
            self._fail_job(job_id, failure_message, persistence_failed=persistence_failed)

    def _project_persistence_lock(self, project_id: str) -> threading.Lock:
        with self._project_persistence_guard:
            return self._project_persistence_locks.setdefault(project_id, threading.Lock())

    def _persist_manifest_delta(
        self,
        job_id: str,
        baseline: GenerationManifest,
        updated: GenerationManifest,
    ) -> None:
        project_id = updated.project_id
        with self._project_persistence_lock(project_id):
            current = self.store.load_manifest(project_id).model_copy(deep=True)
            for line_key, updated_history in updated.lines.items():
                baseline_history = baseline.lines.get(line_key)
                baseline_versions = baseline_history.versions if baseline_history else []
                if len(updated_history.versions) < len(baseline_versions):
                    raise RuntimeError(f"manifest line {line_key} removed baseline generation versions")
                if updated_history.versions[: len(baseline_versions)] != baseline_versions:
                    raise RuntimeError(f"manifest line {line_key} changed baseline generation versions")
                for version in updated_history.versions[len(baseline_versions) :]:
                    provisional_id = version.version_id
                    current.append_version(updated_history.line_id, version)
                    current_history = current.history_for_line(updated_history.line_id, version.line_uid)
                    actual_id = current_history.versions[-1].version_id if current_history else provisional_id
                    if actual_id != provisional_id:
                        self._replace_item_version_id(
                            job_id,
                            line_id=updated_history.line_id,
                            line_uid=version.line_uid,
                            provisional_id=provisional_id,
                            actual_id=actual_id,
                        )
            self.store.save_manifest(current)

    def _replace_item_version_id(
        self,
        job_id: str,
        *,
        line_id: str,
        line_uid: str | None,
        provisional_id: str,
        actual_id: str,
    ) -> None:
        with self._lock:
            for item in self._jobs[job_id].items:
                if (
                    item.line_id == line_id
                    and (item.line_uid or item.line_id) == (line_uid or line_id)
                    and item.version_id == provisional_id
                ):
                    item.version_id = actual_id

    def _fail_job(self, job_id: str, message: str, *, persistence_failed: bool) -> None:
        failed_at = datetime.now(timezone.utc)
        with self._lock:
            job = self._jobs[job_id]
            job.status = "failed"
            job.error = message
            job.updated_at = failed_at
            for item in job.items:
                if item.status not in {"completed", "failed", "cancelled"}:
                    item.status = "failed"
                item.progress = 1.0
                if persistence_failed or item.status == "failed":
                    item.error = item.error or message
            job.progress = 1.0

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
            if job.status in {"cancelling", "cancelled"}:
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
        external_update: ExternalStatusUpdate | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for item in job.items:
                if item.line_id == task.line.id and (item.line_uid or item.line_id) == _task_line_uid(task):
                    if item.status == "cancelling" and status not in {"cancelled", "failed"}:
                        item.progress = max(item.progress, progress)
                    elif item.status not in {"completed", "failed", "cancelled"}:
                        item.status = status
                        item.progress = progress
                    item.cluster_key = cluster_key or item.cluster_key
                    item.service_id = task.service_id or item.service_id
                    item.version_id = version_id or item.version_id
                    if external_update:
                        external_job_id = external_update.get("external_job_id")
                        external_status = external_update.get("external_status")
                        if external_job_id is not None:
                            item.external_job_id = str(external_job_id)
                        if external_status is not None:
                            item.external_status = str(external_status)
                    if item.load_signature is None:
                        try:
                            route = self.queue.router.resolve_task(task)
                            item.load_signature = build_load_signature(route.endpoint, task.parameters)
                            item.service_id = item.service_id or route.endpoint.service_id
                            item.resource_group = item.resource_group or route.endpoint.resource_group
                        except Exception:
                            logger.warning("Failed to resolve load signature for line %s", task.line.id, exc_info=True)
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
            for item in job.items:
                if item.status == "cancelling":
                    item.status = "cancelled"
                    item.progress = 1.0
            if any(item.status == "failed" for item in job.items):
                job.status = "failed"
            elif job.status == "cancelling" or any(item.status == "cancelled" for item in job.items):
                job.status = "cancelled"
            else:
                job.status = "completed"
            job.progress = 1.0
            job.updated_at = datetime.now(timezone.utc)
