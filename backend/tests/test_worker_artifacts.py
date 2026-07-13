from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_artifact_store_allocates_hashes_resolves_and_deletes(tmp_path: Path) -> None:
    from app.workers.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path, max_output_bytes=16)
    artifact_id, output_path = store.allocate_output(".wav")
    output_path.write_bytes(b"RIFFaudio")

    payload = store.describe(artifact_id)

    assert payload == {
        "artifact_id": artifact_id,
        "download_url": f"/artifacts/{artifact_id}",
        "sha256": hashlib.sha256(b"RIFFaudio").hexdigest(),
        "size_bytes": 9,
    }
    assert store.resolve(artifact_id) == output_path
    assert store.delete(artifact_id) is True
    assert store.resolve(artifact_id) is None


def test_artifact_store_rejects_invalid_ids_and_oversized_output(tmp_path: Path) -> None:
    from app.workers.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path, max_output_bytes=4)
    artifact_id, output_path = store.allocate_output(".wav")
    output_path.write_bytes(b"12345")

    assert store.resolve("../secret") is None
    with pytest.raises(ValueError, match="output exceeds"):
        store.describe(artifact_id)


def test_artifact_store_cleans_files_older_than_ttl(tmp_path: Path) -> None:
    from app.workers.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path, ttl_seconds=10)
    artifact_id, output_path = store.allocate_output(".wav")
    output_path.write_bytes(b"old")
    os.utime(output_path, (1, 1))

    assert store.cleanup(now=20) == 1
    assert store.resolve(artifact_id) is None


def test_artifact_store_resolve_expires_stale_file_without_sweep(tmp_path: Path, monkeypatch) -> None:
    from app.workers import artifacts

    store = artifacts.ArtifactStore(tmp_path, ttl_seconds=10)
    artifact_id, output_path = store.allocate_output(".wav")
    output_path.write_bytes(b"old")
    os.utime(output_path, (1, 1))
    monkeypatch.setattr(artifacts.time, "time", lambda: 20)

    assert store.resolve(artifact_id) is None
    assert output_path.exists() is False


def test_worker_health_poll_sweeps_expired_artifacts(tmp_path: Path, monkeypatch) -> None:
    from app.workers import artifacts

    store = artifacts.ArtifactStore(tmp_path, ttl_seconds=10)
    _artifact_id, output_path = store.allocate_output(".wav")
    output_path.write_bytes(b"old")
    os.utime(output_path, (1, 1))
    monkeypatch.setattr(artifacts.time, "time", lambda: 20)
    app = FastAPI()
    artifacts.register_artifact_routes(app, lambda: store)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ready": True}

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert output_path.exists() is False


def test_artifact_download_rejects_output_over_store_limit(tmp_path: Path) -> None:
    from app.workers.artifacts import ArtifactStore, register_artifact_routes

    store = ArtifactStore(tmp_path, max_output_bytes=4)
    artifact_id, output_path = store.allocate_output(".wav")
    output_path.write_bytes(b"12345")
    app = FastAPI()
    register_artifact_routes(app, lambda: store)

    response = TestClient(app).get(f"/artifacts/{artifact_id}")

    assert response.status_code == 413


def test_path_delivery_is_denied_unless_worker_explicitly_allows_it(tmp_path: Path, monkeypatch) -> None:
    from fastapi import HTTPException

    from app.workers.artifacts import ArtifactStore, artifact_output

    store = ArtifactStore(tmp_path / "artifacts")
    requested = tmp_path / "outside.wav"
    monkeypatch.delenv("TTS_MORE_WORKER_ALLOW_PATH_DELIVERY", raising=False)

    with pytest.raises(HTTPException, match="path delivery is disabled"):
        artifact_output(store, "path", requested)

    monkeypatch.setenv("TTS_MORE_WORKER_ALLOW_PATH_DELIVERY", "1")
    assert artifact_output(store, "path", requested) == (requested, None)
