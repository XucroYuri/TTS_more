from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import GenerationManifest, GenerationVersion, ScriptProject
from app.storage import ProjectStore


@pytest.mark.parametrize("project_id", ["../escape", "..\\escape", "/absolute", "C:\\temp\\escape", ""])
def test_project_store_rejects_project_ids_that_escape_data_root(tmp_path: Path, project_id: str) -> None:
    store = ProjectStore(tmp_path)

    with pytest.raises(ValueError):
        store.project_dir(project_id)


@pytest.mark.parametrize("project_id", ["../escape", "..\\escape", "/absolute", "C:\\temp\\escape", ""])
def test_project_store_rejects_delete_project_ids_that_escape_data_root(tmp_path: Path, project_id: str) -> None:
    store = ProjectStore(tmp_path)

    with pytest.raises(ValueError):
        store.delete_project(project_id)


def test_audio_endpoint_rejects_files_outside_data_root(tmp_path: Path) -> None:
    outside_audio = tmp_path.parent / "outside.wav"
    outside_audio.write_bytes(b"RIFFfake")
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/audio", params={"path": str(outside_audio)})

    assert response.status_code == 400
    assert response.json()["detail"] == "audio path is outside data root"


def test_manifest_uses_output_directory_and_reads_legacy_manifest(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    legacy_dir = store.writable_project_dir("legacy")
    legacy_dir.mkdir(parents=True)
    legacy_manifest = GenerationManifest(project_id="legacy")
    legacy_manifest.append_version(
        "line-1",
        GenerationVersion(
            version_id="v001",
            line_uid="line-1",
            engine="gpt-sovits",
            profile="default",
            status="completed",
        ),
    )
    (legacy_dir / "manifest.json").write_text(legacy_manifest.model_dump_json(), encoding="utf-8")

    loaded = store.load_manifest("legacy")
    store.save_manifest(GenerationManifest(project_id="legacy"))

    assert loaded.lines["line-1"].versions[0].version_id == "v001"
    assert (legacy_dir / "output" / "manifest.json").is_file()


def test_title_named_project_directory_does_not_alias_different_project_id(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    store.save_project(
        "real-id",
        ScriptProject(
            title="demo",
            default_language="zh",
            lines=[],
        ),
    )

    with pytest.raises(FileNotFoundError):
        store.load_project("demo")

    assert store.load_project("real-id").title == "demo"


def test_project_title_avoids_windows_reserved_directory_names(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)

    store.save_project(
        "demo",
        ScriptProject(
            title="CON",
            default_language="zh",
            lines=[],
        ),
    )

    project_path = store.project_path("demo")

    assert project_path.parent.name == "CON_"
    assert project_path.is_file()


def test_audio_endpoint_serves_configured_logs_audio_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    logs_root = tmp_path / "logs"
    audio = logs_root / "demo-mentor-logs" / "5-wav32k" / "yanjing.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"RIFFfake")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        f"""
[
  {{
    "service_id": "lan-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "mock://gpt",
    "resource_group": "lan-gpu",
    "capabilities": ["tts"],
    "default_params": {{"logs_roots": ["{logs_root.as_posix()}"]}}
  }}
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=data_root, services_path=services_path))

    response = client.get("/api/audio", params={"path": str(audio)})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"


def test_audio_endpoint_serves_recording_mime_types(tmp_path: Path) -> None:
    audio = tmp_path / "character_reference_audio" / "role" / "recording.webm"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"webm-audio")
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/audio", params={"path": str(audio)})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/webm"


def test_audio_endpoint_rejects_non_audio_assets_inside_data_root(tmp_path: Path) -> None:
    text_file = tmp_path / "character_reference_audio" / "role" / "notes.txt"
    text_file.parent.mkdir(parents=True)
    text_file.write_text("not audio", encoding="utf-8")
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/audio", params={"path": str(text_file)})

    assert response.status_code == 400
    assert response.json()["detail"] == "asset is not an audio file"


def test_delete_generation_version_removes_manifest_and_project_audio_only(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project_audio = store.project_audio_dir("demo") / "l001-v001.wav"
    project_audio.parent.mkdir(parents=True)
    project_audio.write_bytes(b"RIFFproject")
    outside_audio = tmp_path.parent / "outside-generation.wav"
    outside_audio.write_bytes(b"RIFFoutside")
    manifest = GenerationManifest(project_id="demo")
    manifest.append_version(
        "line-uid-001",
        GenerationVersion(
            version_id="v001",
            line_uid="line-uid-001",
            engine="gpt-sovits",
            profile="p",
            status="completed",
            audio_path=str(project_audio),
        ),
    )
    manifest.append_version(
        "line-uid-001",
        GenerationVersion(
            version_id="v002",
            line_uid="line-uid-001",
            engine="gpt-sovits",
            profile="p",
            status="completed",
            audio_path=str(outside_audio),
        ),
    )
    store.save_manifest(manifest)
    client = TestClient(create_app(data_root=tmp_path))

    first = client.delete("/api/projects/demo/manifest/lines/line-uid-001/versions/v001")
    second = client.delete("/api/projects/demo/manifest/lines/line-uid-001/versions/v002")

    assert first.status_code == 200
    assert first.json()["audio_deleted"] is True
    assert project_audio.exists() is False
    assert second.status_code == 200
    assert second.json()["audio_deleted"] is False
    assert second.json()["warning"] == "audio path is outside project audio directory"
    assert outside_audio.exists() is True
    payload = client.get("/api/projects/demo/manifest").json()
    assert payload["lines"]["line-uid-001"]["versions"] == []


def test_audio_endpoint_rejects_logs_root_outside_project(tmp_path: Path, monkeypatch) -> None:
    """A character config logs_root pointing outside the project/data root
    must NOT widen /api/audio to read arbitrary files."""
    # Disable operator allowlist so the only safe roots are project + data.
    monkeypatch.delenv("TTS_MORE_ALLOWED_DATA_ROOTS", raising=False)
    data_root = tmp_path / "data"
    data_root.mkdir()
    # A secret file outside any allowed root.
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    secret_file = secret_dir / "leak.wav"
    secret_file.write_bytes(b"RIFFsecret")
    client = TestClient(create_app(data_root=data_root))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "evil",
                "name": "Evil",
                "profiles": [
                    {
                        "id": "evil-gpt",
                        "name": "Evil GPT",
                        "engine": "gpt-sovits",
                        "config": {"logs_root": str(secret_dir)},
                    }
                ],
            }
        ],
    )

    response = client.get("/api/audio", params={"path": str(secret_file)})

    assert response.status_code == 400
    assert "outside" in response.json()["detail"]


def test_upload_avatar_rejects_oversized_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_MAX_UPLOAD_BYTES", "16")
    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/characters", json=[{"id": "c1", "name": "C1", "profiles": []}])

    response = client.post(
        "/api/characters/c1/avatar/upload",
        files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"a" * 32, "image/png")},
    )

    assert response.status_code == 413


def test_image_endpoint_rejects_non_image_with_image_extension(tmp_path: Path) -> None:
    """A file named .png but containing non-image bytes must be rejected by
    the magic-byte check, not served."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    fake = data_root / "evil.png"
    fake.write_bytes(b"not-an-image-at-all")
    client = TestClient(create_app(data_root=data_root))

    response = client.get("/api/assets/image", params={"path": str(fake)})

    assert response.status_code == 400
    assert "not an image" in response.json()["detail"]


def test_image_endpoint_serves_real_png(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    # Minimal valid PNG signature + IHDR.
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    img = data_root / "ok.png"
    img.write_bytes(png_bytes)
    client = TestClient(create_app(data_root=data_root))

    response = client.get("/api/assets/image", params={"path": str(img)})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_path_compare_normalizes_mixed_separators() -> None:
    """_normalize_path_for_compare must fold both / and \\ to a single form so
    that an access check works regardless of which separator the config or the
    request used (cross-platform correctness)."""
    from app.services import _endpoint_can_access_path, _normalize_path_for_compare

    # Forward-slash and backslash forms of the same path compare equal.
    assert _normalize_path_for_compare("C:/Users/models") == _normalize_path_for_compare("C:\\Users\\models")
    assert _normalize_path_for_compare("/data/weights") == _normalize_path_for_compare("\\data\\weights")

    # A path inside a root declared with the other separator is accessible.
    endpoint = _make_endpoint_with_roots(["/data/weights"])
    assert _endpoint_can_access_path(endpoint, "/data/weights/role/gpt.ckpt")
    assert _endpoint_can_access_path(endpoint, "\\data\\weights\\role\\gpt.ckpt")
    # Outside the root is rejected.
    assert not _endpoint_can_access_path(endpoint, "/data/other/x.ckpt")


def _make_endpoint_with_roots(roots):
    from app.models import TTSServiceEndpoint
    return TTSServiceEndpoint(
        service_id="t",
        base_url="mock://t",
        default_params={"accessible_path_roots": roots},
    )
