from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import GenerationManifest, GenerationVersion
from app.storage import ProjectStore


@pytest.mark.parametrize("project_id", ["../escape", "..\\escape", "/absolute", "C:\\temp\\escape", ""])
def test_project_store_rejects_project_ids_that_escape_data_root(tmp_path: Path, project_id: str) -> None:
    store = ProjectStore(tmp_path)

    with pytest.raises(ValueError):
        store.project_dir(project_id)


def test_audio_endpoint_rejects_files_outside_data_root(tmp_path: Path) -> None:
    outside_audio = tmp_path.parent / "outside.wav"
    outside_audio.write_bytes(b"RIFFfake")
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/audio", params={"path": str(outside_audio)})

    assert response.status_code == 400
    assert response.json()["detail"] == "audio path is outside data root"


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
    project_audio = store.project_dir("demo") / "audio" / "l001-v001.wav"
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
