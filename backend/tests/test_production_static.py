from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def test_production_app_serves_frontend_and_spa_routes_without_vite(tmp_path: Path) -> None:
    static_root = tmp_path / "frontend" / "dist"
    (static_root / "assets").mkdir(parents=True)
    (static_root / "index.html").write_text("<html>TTS More production</html>", encoding="utf-8")
    (static_root / "assets" / "app.js").write_text("window.TTS_MORE=true", encoding="utf-8")

    client = TestClient(create_app(data_root=tmp_path / "data", static_root=static_root))

    assert client.get("/").text == "<html>TTS More production</html>"
    assert client.get("/projects/example").text == "<html>TTS More production</html>"
    assert client.get("/assets/app.js").text == "window.TTS_MORE=true"
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/does-not-exist").status_code == 404


def test_missing_production_static_root_fails_at_app_creation(tmp_path: Path) -> None:
    try:
        create_app(data_root=tmp_path / "data", static_root=tmp_path / "missing")
    except RuntimeError as exc:
        assert "frontend static assets are missing" in str(exc)
    else:
        raise AssertionError("production mode must not silently start without its frontend")
