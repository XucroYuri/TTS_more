"""FastAPI routes for persistent semantic analysis runs and drafts."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.semantic_executor import SemanticAnalysisExecutor
from app.semantic_logging import SemanticEventLogger
from app.semantic_storage import (
    DraftOperation,
    SemanticConflictError,
    SemanticNotFoundError,
    SemanticStorageError,
    SemanticStore,
    SemanticValidationError,
)
from app.storage import ProjectStore


class AnalysisRunCreateRequest(BaseModel):
    source_revision_id: str


class DraftPatchApiRequest(BaseModel):
    # Deliberately unconstrained: the store owns optimistic-version conflicts,
    # including expected_version=0 -> HTTP 409 rather than schema HTTP 422.
    expected_version: int
    operations: list[DraftOperation]


class DraftConfirmRequest(BaseModel):
    expected_version: int
    idempotency_key: str


class AnalysisReviewSessionResponse(BaseModel):
    run_id: str
    draft_id: str
    source_revision_id: str


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (SemanticNotFoundError, FileNotFoundError)):
        return HTTPException(status_code=404, detail={"code": getattr(exc, "code", "semantic_not_found"), "message": "semantic artifact not found"})
    if isinstance(exc, SemanticConflictError):
        return HTTPException(status_code=409, detail={"code": exc.code, "message": "semantic state conflict"})
    if isinstance(exc, (SemanticValidationError, ValueError)):
        return HTTPException(status_code=422, detail={"code": getattr(exc, "code", "semantic_request_invalid"), "message": "semantic request is invalid"})
    return HTTPException(status_code=500, detail={"code": "semantic_internal_error", "message": "semantic request failed"})


def build_semantic_router(
    project_store: ProjectStore,
    semantic_store: SemanticStore,
    executor: SemanticAnalysisExecutor,
    logger: SemanticEventLogger,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get(
        "/projects/{project_id}/analysis-review-session",
        response_model=AnalysisReviewSessionResponse,
    )
    def get_analysis_review_session(
        project_id: str,
        source_revision_id: str | None = None,
    ) -> AnalysisReviewSessionResponse:
        try:
            run, draft = semantic_store.load_latest_confirmed_review_session(
                project_id,
                source_revision_id,
            )
            return AnalysisReviewSessionResponse(
                run_id=run.id,
                draft_id=draft.id,
                source_revision_id=run.source_revision_id,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post(
        "/projects/{project_id}/analysis-runs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_analysis_run(project_id: str, request: AnalysisRunCreateRequest) -> dict[str, Any]:
        trace_id = f"trace-{uuid.uuid4().hex}"
        try:
            run, draft = semantic_store.create_run_and_draft(
                project_id,
                request.source_revision_id,
                trace_id=trace_id,
            )
            logger.queued(
                run_id=run.id,
                trace_id=run.trace_id,
                project_id=run.project_id,
                source_revision_id=run.source_revision_id,
                draft_id=run.draft_id,
                status=run.status.value,
                progress=run.progress,
            )
            executor.submit(run.id)
            return {
                "run_id": run.id,
                "draft_id": draft.id,
                "status": run.status.value,
                "trace_id": run.trace_id,
            }
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/analysis-runs/{run_id}")
    def get_analysis_run(run_id: str):
        try:
            return semantic_store.load_run(run_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/analysis-drafts/{draft_id}")
    def get_analysis_draft(draft_id: str):
        try:
            return semantic_store.load_draft(draft_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.patch("/analysis-drafts/{draft_id}")
    def patch_analysis_draft(draft_id: str, request: DraftPatchApiRequest):
        try:
            return semantic_store.patch_draft(
                draft_id,
                request.expected_version,
                request.operations,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/analysis-drafts/{draft_id}/confirm")
    def confirm_analysis_draft(draft_id: str, request: DraftConfirmRequest):
        try:
            return semantic_store.confirm_draft(
                draft_id,
                request.expected_version,
                request.idempotency_key,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    return router
