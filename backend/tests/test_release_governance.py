from pathlib import Path
import json
import subprocess

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import GenerationManifest, GenerationVersion, ScriptLine, ScriptProject
from app.storage import ProjectStore


def test_committable_templates_do_not_contain_local_runtime_identifiers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    checked_paths = [
        repo_root / "data" / "services.json",
        repo_root / "data" / "templates" / "services.example.json",
        repo_root / "data" / "templates" / "characters.example.json",
    ]
    forbidden_tokens = [
        "192.168.2.",
        "\\\\192.168.",
        "J:\\",
        "F:\\",
        "电器暴走追逐战",
        "光头TTS",
        "TTS-大鹏眼镜",
    ]

    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in text, f"{path} contains local/private token {token!r}"


def test_repository_does_not_publish_demo_script_templates() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    forbidden_paths = [
        repo_root / "data" / "templates" / "demo-hollywood",
        repo_root / "docs" / "demo-hollywood-prompt.md",
    ]

    for path in forbidden_paths:
        assert not path.exists(), f"{path} must not be published"


def test_local_runtime_paths_are_gitignored() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runtime_paths = [
        "data/local/services.json",
        "data/local/characters.json",
        "data/parser_providers.json",
        "Project/example/.project-id",
        ".env.local",
        "repo/GPT-SoVITS/README.md",
        ".omc/state/example",
        ".omo/run-continuation/example",
        ".omx/state/example",
    ]

    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=repo_root,
        input="\n".join(runtime_paths) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    ignored_paths = set(result.stdout.splitlines())

    assert result.returncode == 0
    assert ignored_paths == set(runtime_paths)


def test_committable_character_template_is_empty() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    template_path = repo_root / "data" / "templates" / "characters.example.json"

    assert json.loads(template_path.read_text(encoding="utf-8")) == []


def test_product_source_does_not_embed_fixed_script_sample_text() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    checked_roots = [
        repo_root / "README.md",
        repo_root / "docs",
        repo_root / "data",
        repo_root / "backend" / "app",
        repo_root / "frontend" / "src",
    ]
    skipped_parts = {
        "tests",
        "superpowers",
        "__pycache__",
    }
    forbidden_tokens = [
        "Signal Over Blackridge",
        "BLACKRIDGE",
        "MARA REYES",
        "JONAH VALE",
        "CAEL ORRIN",
        "角色A（焦急）",
        "角色B（低声）",
        "Character A (urgent)",
        "Character B (low voice)",
        "The wind swallowed the footsteps",
        "风声吞没了街角的脚步",
    ]

    for root in checked_roots:
        paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in paths:
            if skipped_parts.intersection(path.parts):
                continue
            if ".test." in path.name:
                continue
            if path.suffix.lower() not in {".css", ".html", ".json", ".md", ".py", ".ts", ".tsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                assert token not in text, f"{path} contains fixed script sample text {token!r}"


def test_character_library_prefers_local_runtime_config_and_saves_there(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    local_characters = data_root / "local" / "characters.json"
    template_characters = data_root / "templates" / "characters.example.json"
    legacy_characters = data_root / "characters.json"
    local_characters.parent.mkdir(parents=True)
    template_characters.parent.mkdir(parents=True)
    local_characters.write_text(
        """
[
  {
    "id": "local-role",
    "name": "Local Role",
    "profiles": []
  }
]
""",
        encoding="utf-8",
    )
    template_characters.write_text(
        """
[
  {
    "id": "template-role",
    "name": "Template Role",
    "profiles": []
  }
]
""",
        encoding="utf-8",
    )
    legacy_characters.write_text(
        """
[
  {
    "id": "legacy-role",
    "name": "Legacy Role",
    "profiles": []
  }
]
""",
        encoding="utf-8",
    )

    store = ProjectStore(data_root)

    assert [character.id for character in store.load_characters()] == ["local-role"]
    store.save_characters(store.load_characters())
    assert local_characters.exists()
    assert legacy_characters.read_text(encoding="utf-8").find("legacy-role") != -1


def test_default_service_settings_prefers_local_runtime_config(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    local_services = data_root / "local" / "services.json"
    template_services = data_root / "templates" / "services.example.json"
    local_services.parent.mkdir(parents=True)
    template_services.parent.mkdir(parents=True)
    local_services.write_text(
        """
[
  {
    "service_id": "local-runtime-gpt",
    "display_name": "Local Runtime GPT",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "http://127.0.0.1:9880",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    template_services.write_text(
        """
[
  {
    "service_id": "template-gpt",
    "display_name": "Template GPT",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "http://example.invalid:9880",
    "resource_group": "template",
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )

    client = TestClient(create_app(data_root=data_root))

    payload = client.get("/api/settings/services").json()
    service_ids = [service["service_id"] for service in payload["services"]]
    assert service_ids == ["local-runtime-gpt"]


def test_project_store_prefers_local_runtime_projects(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    local_store = ProjectStore(data_root)
    local_project = ScriptProject(
        title="Local Runtime Script",
        default_language="zh",
        lines=[ScriptLine(id="l001", character_id="hero", text="Local line")],
    )
    legacy_project_dir = data_root / "demo"
    legacy_project_dir.mkdir(parents=True)
    (legacy_project_dir / "project.json").write_text(
        ScriptProject(title="Legacy Script", default_language="zh", lines=[]).model_dump_json(),
        encoding="utf-8",
    )

    local_store.save_project("demo", local_project)

    assert local_store.project_path("demo") == tmp_path / "Project" / "Local Runtime Script" / "project.json"
    assert local_store.load_project("demo").title == "Local Runtime Script"
    projects = local_store.list_projects()
    assert len(projects) == 1
    assert projects[0] == {
        "project_id": "demo",
        "title": "Local Runtime Script",
        "default_language": "zh",
        "line_count": 1,
        "character_count": 0,
        "script_revision_count": 1,
        "parse_revision_count": 1,
        "updated_at": projects[0]["updated_at"],
    }


def test_delete_generation_version_falls_back_from_revision_uid_to_legacy_line_id(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    audio = store.project_audio_dir("demo") / "l001-v001.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"RIFFproject")
    manifest = GenerationManifest(project_id="demo")
    manifest.append_version(
        "l001",
        GenerationVersion(
            version_id="v001",
            line_uid="l001",
            engine="gpt-sovits",
            profile="p",
            status="completed",
            audio_path=str(audio),
        ),
    )
    store.save_manifest(manifest)
    client = TestClient(create_app(data_root=tmp_path))

    response = client.delete("/api/projects/demo/manifest/lines/parse-r001:l001/versions/v001")

    assert response.status_code == 200
    assert response.json()["line_key"] == "l001"
    assert audio.exists() is False
    payload = client.get("/api/projects/demo/manifest").json()
    assert payload["lines"]["l001"]["versions"] == []
