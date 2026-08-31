from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ScriptProject, ScriptRevision
from app.semantic_models import (
    AnalysisRunQuality,
    AnalysisRunStatus,
    AnalysisWarning,
)
from app.semantic_provider import (
    SemanticProviderContractError,
    SemanticProviderTimeout,
    SemanticProviderUpstream,
)
from app.semantic_source import sha256_source
from app.semantic_storage import SemanticStore
from app.storage import ProjectStore

try:
    from app.semantic_executor import SemanticAnalysisExecutor
    from app.semantic_logging import semantic_event_logger
except ImportError as error:
    _SEMANTIC_API_IMPORT_ERROR: ImportError | None = error
else:
    _SEMANTIC_API_IMPORT_ERROR = None


def _semantic_api() -> None:
    if _SEMANTIC_API_IMPORT_ERROR is not None:
        pytest.fail(f"semantic API modules must be importable: {_SEMANTIC_API_IMPORT_ERROR}")


class FakeSemanticService:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        partial: bool = False,
        gate: threading.Event | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.error = error
        self.partial = partial
        self.gate = gate
        self.delay_seconds = delay_seconds
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def analyze(self, project_id, source_revision, run, draft):
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(3), "test did not release semantic analysis gate"
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        warnings = []
        if self.partial:
            warnings = [
                AnalysisWarning(
                    id="warning-chunk",
                    code="chunk_failed",
                    message="One chunk failed safely.",
                    details={"chunk_id": "chunk-0002"},
                )
            ]
        result = draft.model_copy(
            update={
                "provider": "fake-semantic",
                "model": "fake-model",
                "warnings": warnings,
            },
            deep=True,
        )
        self.finished.set()
        return result


def _seed_project(store: ProjectStore, project_id: str = "demo") -> ScriptProject:
    source_text = "旁白：你好。"
    source = ScriptRevision(
        revision_id="script-r001",
        source_markdown=source_text,
        source_sha256=sha256_source(source_text),
    )
    project = ScriptProject(
        title="Semantic API demo",
        script_revisions=[source],
        active_script_revision_id=source.revision_id,
    )
    store.save_project(project_id, project)
    return store.load_project(project_id)


def _app(tmp_path: Path, service: FakeSemanticService):
    app = create_app(
        data_root=tmp_path,
        env_path=tmp_path / ".env.local",
        semantic_service=service,
    )
    _seed_project(app.state.store)
    return app


def _create_run(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/projects/demo/analysis-runs",
        json={"source_revision_id": "script-r001"},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _wait_for_terminal(client: TestClient, run_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        response = client.get(f"/api/analysis-runs/{run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "failed", "interrupted"}:
            return payload
        time.sleep(0.01)
    pytest.fail(f"semantic run {run_id} did not become terminal")


def test_analysis_run_is_nonblocking_completes_and_persists_across_app_restart(tmp_path: Path) -> None:
    _semantic_api()
    gate = threading.Event()
    service = FakeSemanticService(gate=gate, delay_seconds=0.2)
    app = _app(tmp_path, service)

    with TestClient(app) as client:
        started_at = time.monotonic()
        created = _create_run(client)
        elapsed = time.monotonic() - started_at
        assert elapsed < 0.5
        assert created["status"] == "queued"
        assert service.started.wait(1)
        running = client.get(f"/api/analysis-runs/{created['run_id']}").json()
        assert running["status"] == "running"
        gate.set()
        terminal = _wait_for_terminal(client, str(created["run_id"]))
        assert terminal["status"] == "completed"
        assert terminal["quality"] == "complete"
        assert terminal["progress"] == 1

    with TestClient(create_app(data_root=tmp_path, env_path=tmp_path / ".env.local", semantic_service=FakeSemanticService())) as client:
        persisted = client.get(f"/api/analysis-runs/{created['run_id']}")
        assert persisted.status_code == 200
        assert persisted.json() == terminal


def test_partial_chunk_warning_sets_partial_quality(tmp_path: Path) -> None:
    _semantic_api()
    with TestClient(_app(tmp_path, FakeSemanticService(partial=True))) as client:
        created = _create_run(client)
        terminal = _wait_for_terminal(client, str(created["run_id"]))

    assert terminal["status"] == "completed"
    assert terminal["quality"] == "partial"
    assert [warning["code"] for warning in terminal["warnings"]] == ["chunk_failed"]


@pytest.mark.parametrize(
    ("error", "http_status", "code", "retryable"),
    [
        (SemanticProviderContractError("safe contract failure"), 422, "semantic_contract_invalid", False),
        (SemanticProviderUpstream("safe upstream failure"), 502, "semantic_provider_upstream", True),
        (SemanticProviderTimeout("safe timeout failure"), 504, "semantic_provider_timeout", True),
        (RuntimeError("SECRET SCRIPT Authorization: Bearer sk-secret"), 500, "semantic_analysis_failed", False),
    ],
)
def test_async_failures_persist_safe_typed_errors_and_keep_get_200(
    tmp_path: Path,
    error: Exception,
    http_status: int,
    code: str,
    retryable: bool,
) -> None:
    _semantic_api()
    app = _app(tmp_path, FakeSemanticService(error=error))
    before = app.state.store.load_project("demo")

    with TestClient(app, raise_server_exceptions=False) as client:
        created = _create_run(client)
        response = client.get(f"/api/analysis-runs/{created['run_id']}")
        terminal = _wait_for_terminal(client, str(created["run_id"]))

    assert response.status_code == 200
    assert terminal["status"] == "failed"
    assert terminal["quality"] is None
    assert terminal["error"]["http_status"] == http_status
    assert terminal["error"]["code"] == code
    assert terminal["error"]["retryable"] is retryable
    assert terminal["error"]["run_id"] == created["run_id"]
    assert terminal["error"]["trace_id"]
    assert terminal["error"]["stage"] == "analysis"
    assert "SECRET SCRIPT" not in str(terminal)
    assert "sk-secret" not in str(terminal)
    after = app.state.store.load_project("demo")
    assert after.active_parse_revision_id == before.active_parse_revision_id
    assert after.lines == before.lines


def test_startup_recovers_only_incomplete_persisted_runs(tmp_path: Path) -> None:
    _semantic_api()
    project_store = ProjectStore(tmp_path)
    _seed_project(project_store)
    semantic_store = SemanticStore(project_store)
    queued, _ = semantic_store.create_run_and_draft("demo", "script-r001", trace_id="trace-queued")
    running, _ = semantic_store.create_run_and_draft("demo", "script-r001", trace_id="trace-running")
    running.status = AnalysisRunStatus.RUNNING
    semantic_store.save_run(running)
    completed, _ = semantic_store.create_run_and_draft("demo", "script-r001", trace_id="trace-completed")
    completed.status = AnalysisRunStatus.COMPLETED
    completed.quality = AnalysisRunQuality.COMPLETE
    semantic_store.save_run(completed)

    app = create_app(data_root=tmp_path, env_path=tmp_path / ".env.local", semantic_service=FakeSemanticService())
    with TestClient(app) as client:
        assert client.get(f"/api/analysis-runs/{queued.id}").json()["status"] == "interrupted"
        assert client.get(f"/api/analysis-runs/{running.id}").json()["status"] == "interrupted"
        assert client.get(f"/api/analysis-runs/{completed.id}").json()["status"] == "completed"


def test_executor_rejects_duplicate_nonqueued_and_post_shutdown_submissions(tmp_path: Path) -> None:
    _semantic_api()
    project_store = ProjectStore(tmp_path)
    _seed_project(project_store)
    semantic_store = SemanticStore(project_store)
    gate = threading.Event()
    service = FakeSemanticService(gate=gate, delay_seconds=0.2)
    executor = SemanticAnalysisExecutor(
        semantic_store,
        project_store,
        service,
        semantic_event_logger(tmp_path),
        max_workers=1,
    )
    run, _ = semantic_store.create_run_and_draft("demo", "script-r001", trace_id="trace-1")

    executor.submit(run.id)
    assert service.started.wait(1)
    executor.submit(run.id)
    executor.shutdown(wait=False)
    gate.set()
    executor.shutdown(wait=True)
    assert service.finished.is_set()
    executor.shutdown()
    assert service.calls == 1
    assert semantic_store.load_run(run.id).status is AnalysisRunStatus.COMPLETED

    after_shutdown, _ = semantic_store.create_run_and_draft("demo", "script-r001", trace_id="trace-2")
    executor.submit(after_shutdown.id)
    assert semantic_store.load_run(after_shutdown.id).status is AnalysisRunStatus.QUEUED


def test_draft_patch_and_confirm_routes_enforce_version_validation_and_idempotency(tmp_path: Path) -> None:
    _semantic_api()
    with TestClient(_app(tmp_path, FakeSemanticService())) as client:
        created = _create_run(client)
        _wait_for_terminal(client, str(created["run_id"]))
        draft_id = str(created["draft_id"])

        fetched = client.get(f"/api/analysis-drafts/{draft_id}")
        assert fetched.status_code == 200
        assert fetched.json()["version"] == 1
        assert client.patch(
            f"/api/analysis-drafts/{draft_id}",
            json={"expected_version": 0, "operations": []},
        ).status_code == 409
        invalid = client.patch(
            f"/api/analysis-drafts/{draft_id}",
            json={
                "expected_version": 1,
                "operations": [{"op": "dismiss_warning", "warning_id": "missing"}],
            },
        )
        assert invalid.status_code == 422
        assert client.get(f"/api/analysis-drafts/{draft_id}").json()["version"] == 1
        malformed = client.patch(
            f"/api/analysis-drafts/{draft_id}",
            json={"expected_version": 1, "operations": [{"op": "invent_operation"}]},
        )
        assert malformed.status_code == 422
        patched = client.patch(
            f"/api/analysis-drafts/{draft_id}",
            json={"expected_version": 1, "operations": []},
        )
        assert patched.status_code == 200
        assert patched.json()["version"] == 2

        first = client.post(
            f"/api/analysis-drafts/{draft_id}/confirm",
            json={"expected_version": 2, "idempotency_key": "confirm-key"},
        )
        assert first.status_code == 200, first.text
        retried = client.post(
            f"/api/analysis-drafts/{draft_id}/confirm",
            json={"expected_version": 0, "idempotency_key": "confirm-key"},
        )
        assert retried.status_code == 200
        assert retried.json()["semantic_revision"]["id"] == first.json()["semantic_revision"]["id"]
        project = retried.json()["project"]
        revision_id = first.json()["parse_revision"]["revision_id"]
        assert sum(item["revision_id"] == revision_id for item in project["parse_revisions"]) == 1
        assert client.post(
            f"/api/analysis-drafts/{draft_id}/confirm",
            json={"expected_version": 2, "idempotency_key": "different-key"},
        ).status_code == 409


def test_running_draft_is_read_only(tmp_path: Path) -> None:
    _semantic_api()
    gate = threading.Event()
    service = FakeSemanticService(gate=gate)
    with TestClient(_app(tmp_path, service)) as client:
        created = _create_run(client)
        assert service.started.wait(1)
        response = client.patch(
            f"/api/analysis-drafts/{created['draft_id']}",
            json={"expected_version": 1, "operations": []},
        )
        gate.set()
        _wait_for_terminal(client, str(created["run_id"]))
    assert response.status_code == 409


def test_semantic_routes_map_missing_and_hostile_ids_without_path_escape(tmp_path: Path) -> None:
    _semantic_api()
    with TestClient(_app(tmp_path, FakeSemanticService()), raise_server_exceptions=False) as client:
        assert client.post(
            "/api/projects/missing/analysis-runs",
            json={"source_revision_id": "script-r001"},
        ).status_code == 404
        assert client.post(
            "/api/projects/demo/analysis-runs",
            json={"source_revision_id": "missing"},
        ).status_code == 404
        assert client.get("/api/analysis-runs/missing").status_code == 404
        assert client.get("/api/analysis-drafts/missing").status_code == 404
        assert client.patch(
            "/api/analysis-drafts/missing",
            json={"expected_version": 1, "operations": []},
        ).status_code == 404
        assert client.post(
            "/api/analysis-drafts/missing/confirm",
            json={"expected_version": 1, "idempotency_key": "key"},
        ).status_code == 404
        hostile = client.get("/api/analysis-runs/..%2F..%2Fescape")
        assert hostile.status_code in {404, 422}

    assert not (tmp_path.parent / "escape.json").exists()
