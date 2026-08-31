"""Persistent, bounded background execution for semantic analysis runs."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from app.semantic_logging import SemanticEventLogger
from app.semantic_models import (
    AnalysisError,
    AnalysisRun,
    AnalysisRunQuality,
    AnalysisRunStatus,
)
from app.semantic_provider import SemanticProviderError
from app.semantic_storage import SemanticStorageError, SemanticStore
from app.storage import ProjectStore


_PROVIDER_ERROR_MESSAGES = {
    "semantic_contract_invalid": "Semantic provider response failed validation.",
    "semantic_provider_unavailable": "Semantic analysis provider is unavailable.",
    "semantic_provider_upstream": "Semantic analysis provider request failed.",
    "semantic_provider_timeout": "Semantic analysis provider timed out.",
}
_PROVIDER_ERROR_FALLBACK = "Semantic analysis provider failed."


def _worker_count(explicit: int | None) -> int:
    if explicit is not None:
        candidate = explicit
    else:
        try:
            candidate = int(os.environ.get("TTS_MORE_SEMANTIC_WORKERS", "2"))
        except (TypeError, ValueError):
            candidate = 2
    if candidate <= 0 or candidate > 32:
        return 2
    return candidate


def _service_metadata(service: object) -> tuple[str, str]:
    provider = getattr(service, "provider", service)
    return (
        str(getattr(provider, "name", "unknown")),
        str(getattr(provider, "model", "unknown")),
    )


class SemanticAnalysisExecutor:
    def __init__(
        self,
        store: SemanticStore,
        project_store: ProjectStore,
        service: object,
        logger: SemanticEventLogger,
        *,
        max_workers: int | None = None,
    ) -> None:
        self.store = store
        self.project_store = project_store
        self.logger = logger
        self._service = service
        self._pool = ThreadPoolExecutor(
            max_workers=_worker_count(max_workers),
            thread_name_prefix="semantic-analysis",
        )
        self._lock = threading.RLock()
        self._accepting = True
        self._submitted: set[str] = set()
        self._futures: set[Future[None]] = set()

    def set_service(self, service: object) -> None:
        with self._lock:
            self._service = service

    def submit(self, run_id: str) -> None:
        with self._lock:
            if not self._accepting or run_id in self._submitted:
                return
            try:
                run = self.store.load_run(run_id)
            except (SemanticStorageError, ValueError, OSError):
                return
            if run.status is not AnalysisRunStatus.QUEUED:
                return
            self._submitted.add(run_id)
            try:
                future = self._pool.submit(self._execute, run_id)
            except RuntimeError:
                self._submitted.discard(run_id)
                return
            self._futures.add(future)
            future.add_done_callback(lambda completed, identity=run_id: self._forget(identity, completed))

    def recover_interrupted(self) -> int:
        try:
            index = self.store._load_index()
        except (SemanticStorageError, ValueError, OSError):
            return 0
        changed = 0
        for run_id in list(index.get("runs", {})):
            try:
                interrupted = self.store.transition_incomplete_run_to_interrupted(run_id)
            except (SemanticStorageError, ValueError, OSError):
                continue
            if interrupted is None:
                continue
            self.logger.interrupted(
                run_id=interrupted.id,
                trace_id=interrupted.trace_id,
                project_id=interrupted.project_id,
                source_revision_id=interrupted.source_revision_id,
                draft_id=interrupted.draft_id,
                status=AnalysisRunStatus.INTERRUPTED.value,
                progress=interrupted.progress,
            )
            changed += 1
        return changed

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._accepting = False
        # ThreadPoolExecutor.shutdown is itself idempotent. Calling it again
        # with wait=True must still drain a pool previously stopped with
        # wait=False.
        self._pool.shutdown(wait=wait)

    def _forget(self, run_id: str, future: Future[None]) -> None:
        with self._lock:
            self._submitted.discard(run_id)
            self._futures.discard(future)

    def _execute(self, run_id: str) -> None:
        started_at = time.perf_counter()
        run: AnalysisRun | None = None
        source_text: str | None = None
        with self._lock:
            service = self._service
        provider, model = _service_metadata(service)
        try:
            run = self.store.load_run(run_id)
            if run.status is not AnalysisRunStatus.QUEUED:
                return
            draft = self.store.load_draft(run.draft_id)
            project = self.project_store.load_project(run.project_id)
            source = next(
                (item for item in project.script_revisions if item.revision_id == run.source_revision_id),
                None,
            )
            if source is None:
                raise ValueError("semantic source revision is missing")
            source_text = source.source_markdown
            run = run.model_copy(
                update={
                    "status": AnalysisRunStatus.RUNNING,
                    "quality": None,
                    "progress": 0,
                    "warnings": [],
                    "error": None,
                    "updated_at": datetime.now(timezone.utc),
                },
                deep=True,
            )
            self.store.save_run(run)
            self.logger.started(
                run_id=run.id,
                trace_id=run.trace_id,
                project_id=run.project_id,
                source_revision_id=run.source_revision_id,
                draft_id=run.draft_id,
                provider=provider,
                model=model,
                status=run.status.value,
                progress=run.progress,
                source_char_count=len(source.source_markdown),
                source_sha256=source.source_sha256,
            )
            replacement = service.analyze(run.project_id, source, run, draft)
            saved_draft = self.store.replace_analysis_result(run.id, replacement)
            quality = (
                AnalysisRunQuality.PARTIAL
                if any(warning.code == "chunk_failed" for warning in saved_draft.warnings)
                else AnalysisRunQuality.COMPLETE
            )
            completed = run.model_copy(
                update={
                    "status": AnalysisRunStatus.COMPLETED,
                    "quality": quality,
                    "progress": 1,
                    "warnings": saved_draft.warnings,
                    "error": None,
                    "updated_at": datetime.now(timezone.utc),
                },
                deep=True,
            )
            self.store.save_run(completed)
            self.logger.completed(
                run_id=completed.id,
                trace_id=completed.trace_id,
                project_id=completed.project_id,
                source_revision_id=completed.source_revision_id,
                draft_id=completed.draft_id,
                provider=saved_draft.provider or provider,
                model=saved_draft.model or model,
                status=completed.status.value,
                quality=quality.value,
                progress=1,
                warning_count=len(saved_draft.warnings),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        except Exception as exc:
            if run is None:
                return
            error = self._analysis_error(run, exc)
            try:
                failed = run.model_copy(
                    update={
                        "status": AnalysisRunStatus.FAILED,
                        "quality": None,
                        "error": error,
                        "updated_at": datetime.now(timezone.utc),
                    },
                    deep=True,
                )
                self.store.save_run(failed)
            except Exception:
                return
            self.logger.failed(
                run_id=failed.id,
                trace_id=failed.trace_id,
                project_id=failed.project_id,
                source_revision_id=failed.source_revision_id,
                draft_id=failed.draft_id,
                provider=provider,
                model=model,
                status=failed.status.value,
                progress=failed.progress,
                stage=error.stage,
                error_code=error.code,
                http_status=error.http_status,
                retryable=error.retryable,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                exception=exc,
                source_text=source_text,
            )

    @staticmethod
    def _analysis_error(run: AnalysisRun, exc: Exception) -> AnalysisError:
        if isinstance(exc, SemanticProviderError):
            code = exc.code
            http_status = exc.http_status
            retryable = exc.retryable
            message = _PROVIDER_ERROR_MESSAGES.get(str(code), _PROVIDER_ERROR_FALLBACK)
        elif isinstance(exc, (SemanticStorageError, ValueError)):
            code = getattr(exc, "code", "semantic_validation_failed")
            http_status = 422
            retryable = False
            message = "Semantic analysis failed validation."
        else:
            code = "semantic_analysis_failed"
            http_status = 500
            retryable = False
            message = "Semantic analysis failed unexpectedly."
        return AnalysisError(
            code=str(code),
            http_status=http_status,
            stage="analysis",
            message=message,
            retryable=retryable,
            run_id=run.id,
            trace_id=run.trace_id,
            details={},
        )
