from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(data_root=tmp_path))


def test_auth_status_open_by_default(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json() == {"auth_required": False}


def test_open_mode_allows_mutating_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TTS_MORE_API_TOKEN", raising=False)
    client = _client(tmp_path)
    # PUT /api/characters is a mutating endpoint; should be allowed with no token.
    response = client.put("/api/characters", json=[])
    assert response.status_code == 200


def test_token_required_blocks_mutating_without_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "secret-xyz")
    client = _client(tmp_path)
    response = client.put("/api/characters", json=[])
    assert response.status_code == 401
    assert "token" in response.json()["detail"].lower()


def test_token_required_accepts_valid_bearer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "secret-xyz")
    client = _client(tmp_path)
    response = client.put(
        "/api/characters",
        json=[],
        headers={"Authorization": "Bearer secret-xyz"},
    )
    assert response.status_code == 200


def test_token_required_rejects_wrong_bearer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "secret-xyz")
    client = _client(tmp_path)
    response = client.put(
        "/api/characters",
        json=[],
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_get_endpoints_stay_open_with_token(tmp_path: Path, monkeypatch) -> None:
    """Read-only GET endpoints (health, projects list) must work without a
    token even when auth is enabled, so the frontend can boot."""
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "secret-xyz")
    client = _client(tmp_path)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_sensitive_get_endpoints_require_token(tmp_path: Path, monkeypatch) -> None:
    """GET endpoints that perform network egress must require a token."""
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "secret-xyz")
    client = _client(tmp_path)
    # /api/open-source-tts/detect is a POST (egress) -> blocked.
    response = client.post(
        "/api/open-source-tts/detect",
        json={"provider_type": "gpt-sovits"},
    )
    assert response.status_code == 401


def test_upload_requires_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "secret-xyz")
    client = _client(tmp_path)
    client.put("/api/characters", json=[{"id": "c1", "name": "C1", "profiles": []}], headers={"Authorization": "Bearer secret-xyz"})
    response = client.post(
        "/api/characters/c1/avatar/upload",
        files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/png")},
    )
    assert response.status_code == 401


def test_delete_requires_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "secret-xyz")
    client = _client(tmp_path)
    response = client.delete("/api/projects/anything")
    assert response.status_code == 401
