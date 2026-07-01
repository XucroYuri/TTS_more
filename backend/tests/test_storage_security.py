from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
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
