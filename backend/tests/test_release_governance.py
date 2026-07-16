from pathlib import Path
import json
import re
import subprocess

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import GenerationManifest, GenerationVersion, ScriptLine, ScriptProject
from app.storage import ProjectStore


CUDA_PUBLIC_DOCS = (
    "README.md",
    "docs/cuda-e2e-single-node.md",
    "docs/cuda-windows-codex-handoff-prompt.md",
    "docs/cuda-e2e-validation.md",
    "docs/cuda-e2e-acceptance-record.md",
    "docs/ci-architecture.md",
    "docs/deployment.md",
    "deployment/app/README.md",
    "deployment/tts-repos/gpt-sovits/README.md",
    "deployment/tts-repos/indextts/README.md",
    "deployment/tts-repos/cosyvoice/README.md",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iter_product_source_files(repo_root: Path) -> list[Path]:
    """Return release-governed source/template files, not local runtime caches."""

    checked_roots = [
        repo_root / "README.md",
        repo_root / "docs",
        repo_root / "data" / "services.json",
        repo_root / "data" / "templates",
        repo_root / "backend" / "app",
        repo_root / "frontend" / "src",
    ]
    skipped_parts = {
        "tests",
        "superpowers",
        "__pycache__",
    }
    allowed_suffixes = {".css", ".html", ".json", ".md", ".py", ".ts", ".tsx"}
    paths: list[Path] = []

    for root in checked_roots:
        candidates = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in candidates:
            if skipped_parts.intersection(path.parts):
                continue
            if ".test." in path.name:
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            paths.append(path)

    return paths


def test_committable_templates_do_not_contain_local_runtime_identifiers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    checked_paths = [
        repo_root / "data" / "services.json",
        repo_root / "data" / "templates" / "services.example.json",
        repo_root / "data" / "templates" / "characters.example.json",
    ]
    forbidden_tokens = [
        # LAN / private IP subnets (RFC1918) — no real internal hosts in templates.
        "192.168.0.",
        "192.168.1.",
        "192.168.2.",
        "192.168.3.",
        "10.0.",
        "10.1.",
        "172.16.",
        "172.17.",
        "172.18.",
        # UNC paths (\\server\share) — no internal network mounts.
        "\\\\192.168.",
        "\\\\nas",
        "\\\\server",
        # Windows drive letters — no machine-specific absolute paths.
        "C:\\",
        "D:\\",
        "E:\\",
        "F:\\",
        "G:\\",
        "J:\\",
        # POSIX user-home paths — no developer-machine paths.
        "/Users/",
        "/home/",
        # Internal project character names — no real role data.
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
        input=("\n".join(runtime_paths) + "\n").encode("utf-8"),
        capture_output=True,
        check=False,
    )
    ignored_paths = set(result.stdout.decode("utf-8").splitlines())

    assert result.returncode == 0
    assert ignored_paths == set(runtime_paths)


def test_committable_character_template_is_empty() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    template_path = repo_root / "data" / "templates" / "characters.example.json"

    assert json.loads(template_path.read_text(encoding="utf-8")) == []


def test_product_source_scan_excludes_runtime_cache_directories(tmp_path: Path) -> None:
    (tmp_path / "data" / "templates").mkdir(parents=True)
    (tmp_path / "data" / "cache" / "portable" / "python").mkdir(parents=True)
    template = tmp_path / "data" / "templates" / "services.example.json"
    cache_file = tmp_path / "data" / "cache" / "portable" / "python" / "pygettext.py"

    template.write_text("{}\n", encoding="utf-8")
    cache_file.write_bytes(b"\xffSignal Over Blackridge")

    scanned_paths = set(_iter_product_source_files(tmp_path))

    assert template in scanned_paths
    assert cache_file not in scanned_paths


def test_product_source_does_not_embed_fixed_script_sample_text() -> None:
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

    for path in _iter_product_source_files(_repo_root()):
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


def test_cuda_docs_separate_controlled_raw_evidence_from_sanitized_shareable_evidence() -> None:
    root = _repo_root()
    for relative_path in (
        "docs/cuda-e2e-single-node.md",
        "docs/cuda-windows-codex-handoff-prompt.md",
        "docs/cuda-e2e-validation.md",
        "docs/cuda-e2e-acceptance-record.md",
    ):
        text = (root / relative_path).read_text(encoding="utf-8")
        assert "受控原始证据" in text, f"{relative_path} must name the private evidence class"
        assert "脱敏可共享证据" in text, f"{relative_path} must name the shareable evidence class"

    contract = (root / "docs/cuda-e2e-validation.md").read_text(encoding="utf-8")
    for sensitive_artifact in (
        "controller.log",
        "wav/",
        "worker-logs/",
        "Playwright trace",
        "GPU UUID",
        "审核者身份",
    ):
        assert sensitive_artifact in contract
    assert "不得上传" in contract


def test_cuda_acceptance_record_has_separate_raw_and_shareable_locations() -> None:
    record = (_repo_root() / "docs/cuda-e2e-acceptance-record.md").read_text(encoding="utf-8")
    assert "受控原始证据位置" in record
    assert "脱敏可共享证据位置" in record
    assert "不得提交到公开仓库" in record
    assert "Playwright JUnit" in record
    assert "失败 trace/screenshot/video" in record


def test_cuda_docs_do_not_claim_an_html_playwright_report() -> None:
    root = _repo_root()
    combined = "\n".join((root / path).read_text(encoding="utf-8") for path in CUDA_PUBLIC_DOCS)
    assert "Playwright report URL" not in combined
    assert "HTML Playwright" not in combined
    assert "Playwright HTML" not in combined


def test_cuda_document_relative_links_resolve() -> None:
    root = _repo_root()
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for relative_path in CUDA_PUBLIC_DOCS:
        path = root / relative_path
        text = path.read_text(encoding="utf-8")
        for match in link_pattern.finditer(text):
            raw_target = match.group(1)
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (path.parent / target).resolve()
            line = text.count("\n", 0, match.start()) + 1
            assert resolved.exists(), f"{relative_path}:{line} links to missing {target}"


def test_cuda_public_docs_do_not_contain_real_machine_identifiers() -> None:
    root = _repo_root()
    forbidden = (
        "C:\\Users\\",
        "D:\\",
        "E:\\",
        "F:\\",
        "/Users/",
        "/home/",
        "192.168.0.",
        "192.168.1.",
        "10.0.0.",
    )
    for relative_path in CUDA_PUBLIC_DOCS:
        text = (root / relative_path).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{relative_path} contains local/private token {token!r}"
