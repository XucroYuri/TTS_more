import hashlib
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import GenerationManifest, GenerationTask, GenerationVersion, ScriptProject
import app.storage as storage_module
from app.storage import ProjectStore


@pytest.mark.parametrize("project_id", ["../escape", "..\\escape", "/absolute", "C:\\temp\\escape", ""])
def test_project_store_rejects_project_ids_that_escape_data_root(tmp_path: Path, project_id: str) -> None:
    store = ProjectStore(tmp_path)

    with pytest.raises(ValueError):
        store.project_dir(project_id)


@pytest.mark.parametrize(
    "project_id",
    [
        " demo",
        "demo ",
        "demo.",
        "CON",
        "con.txt",
        "NUL.json",
        "COM1.log",
        "LPT9.backup",
        "COM¹.txt",
        "CONIN$",
        "bad?.id",
        "bad|id",
        "control\x01id",
    ],
)
def test_fix_round_3_project_store_rejects_windows_alias_ids_before_filesystem_creation(
    tmp_path: Path, project_id: str
) -> None:
    store = ProjectStore(tmp_path)

    with pytest.raises(ValueError):
        store.project_dir(project_id)
    with pytest.raises(ValueError):
        store.load_manifest(project_id)
    with pytest.raises(ValueError):
        store.update_manifest(project_id, lambda _manifest: None)

    assert store.writable_projects_root().exists() is False


def test_fix_round_3_project_store_case_aliases_share_identity_across_store_instances(tmp_path: Path) -> None:
    first = ProjectStore(tmp_path)
    second = ProjectStore(Path(f"\\\\?\\{tmp_path}"))
    lock_before_project_creation = first._manifest_lock("Demo-Valid_01")
    assert lock_before_project_creation is second._manifest_lock("demo-valid_01")
    first.save_manifest(GenerationManifest(project_id="Demo-Valid_01"))

    loaded = second.load_manifest("demo-valid_01")

    assert loaded.project_id == "Demo-Valid_01"
    assert first.project_dir("Demo-Valid_01").samefile(second.project_dir("demo-valid_01"))


def test_fix_round_4_project_lock_identity_uses_windows_case_rules_without_unicode_casefold(tmp_path: Path) -> None:
    first = ProjectStore(tmp_path)
    second = ProjectStore(tmp_path)

    assert first._manifest_lock("Demo") is second._manifest_lock("demo")
    assert first._manifest_lock("Stra\u00dfe") is not second._manifest_lock("STRASSE")


def test_fix_round_4_unicode_project_ids_keep_distinct_directories_markers_and_listing(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    store.save_project(
        "Stra\u00dfe",
        ScriptProject(title="shared-title", default_language="de", lines=[]),
    )
    first_dir = store.project_dir("Stra\u00dfe")

    store.save_project(
        "STRASSE",
        ScriptProject(title="shared-title", default_language="en", lines=[]),
    )
    second_dir = store.project_dir("STRASSE")

    assert first_dir.samefile(second_dir) is False
    assert (first_dir / ".project-id").read_text(encoding="utf-8") == "Stra\u00dfe"
    assert (second_dir / ".project-id").read_text(encoding="utf-8") == "STRASSE"
    assert store.load_project("Stra\u00dfe").default_language == "de"
    assert store.load_project("STRASSE").default_language == "en"
    listed = {entry["project_id"]: entry["default_language"] for entry in store.list_projects()}
    assert listed == {"Stra\u00dfe": "de", "STRASSE": "en"}


@pytest.mark.parametrize(
    "project_id",
    [
        pytest.param("p" * 121, id="121-ascii-units"),
        pytest.param("p" * 255, id="255-ascii-units"),
        pytest.param(("p" * 253) + "\U0001f600", id="255-units-with-surrogate-pair"),
    ],
)
def test_fix_round_4_project_ids_through_255_utf16_units_persist_without_rewriting(
    tmp_path: Path, project_id: str
) -> None:
    store = ProjectStore(tmp_path)
    replacement = ProjectStore(tmp_path)

    assert store._manifest_lock(project_id) is replacement._manifest_lock(project_id)
    store.save_manifest(GenerationManifest(project_id=project_id))

    project_dir = store.project_dir(project_id)
    assert project_dir.name == project_id
    assert (project_dir / ".project-id").read_text(encoding="utf-8") == project_id
    assert replacement.load_manifest(project_id).project_id == project_id


@pytest.mark.parametrize(
    "project_id",
    [
        pytest.param("p" * 256, id="256-ascii-units"),
        pytest.param(("p" * 254) + "\U0001f600", id="256-units-with-surrogate-pair"),
    ],
)
def test_fix_round_4_project_ids_over_255_utf16_units_are_rejected_before_allocation(
    tmp_path: Path, project_id: str
) -> None:
    store = ProjectStore(tmp_path)

    with pytest.raises(ValueError, match="component length"):
        store._manifest_lock(project_id)
    with pytest.raises(ValueError, match="component length"):
        store.save_manifest(GenerationManifest(project_id=project_id))

    assert store.writable_projects_root().exists() is False


def test_fix_round_5_long_project_id_blank_title_materializes_loads_and_lists(tmp_path: Path) -> None:
    project_id = "p" * 255
    store = ProjectStore(tmp_path)
    project = ScriptProject(title=" ", default_language="en", lines=[])

    store.save_project(project_id, project)

    project_dir = store.project_dir(project_id)
    assert project_dir.name == project_id
    assert store.load_project(project_id).default_language == "en"
    assert (project_dir / ".project-id").read_text(encoding="utf-8") == project_id
    assert (project_dir / "script" / "active.md").is_file()
    assert (project_dir / "output" / "lines.json").read_text(encoding="utf-8") == "[]"
    assert [entry["project_id"] for entry in store.list_projects()] == [project_id]


def test_fix_round_5_long_manifest_directory_renames_to_short_title_and_remains_loadable(tmp_path: Path) -> None:
    project_id = "p" * 255
    store = ProjectStore(tmp_path)
    store.save_manifest(GenerationManifest(project_id=project_id))
    original_dir = store.project_dir(project_id)

    store.save_project(project_id, ScriptProject(title="short-title", default_language="de", lines=[]))

    renamed_dir = store.project_dir(project_id)
    assert renamed_dir.name == "short-title"
    assert original_dir.exists() is False
    assert store.load_project(project_id).default_language == "de"
    assert store.load_manifest(project_id).project_id == project_id
    assert (renamed_dir / ".project-id").read_text(encoding="utf-8") == project_id
    assert [entry["project_id"] for entry in store.list_projects()] == [project_id]


def test_fix_round_5_long_project_id_deletes_to_bounded_collision_safe_trash_children(tmp_path: Path) -> None:
    project_id = "p" * 255
    expected_hash = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    store = ProjectStore(tmp_path)

    trashed_paths: list[Path] = []
    for _index in range(2):
        store.save_manifest(GenerationManifest(project_id=project_id))
        trashed_paths.append(store.delete_project(project_id))

    assert trashed_paths[0] != trashed_paths[1]
    for trashed_path in trashed_paths:
        assert trashed_path.parent.name == ".trash"
        assert trashed_path.is_dir()
        assert trashed_path.name.startswith("pppppppp")
        assert expected_hash in trashed_path.name
        assert all(len(part.encode("utf-16-le")) // 2 <= 255 for part in trashed_path.parts[1:])
        assert (trashed_path / ".project-id").read_text(encoding="utf-8") == project_id


def test_fix_round_5_windows_path_identity_unifies_normal_extended_local_and_unc_spellings() -> None:
    assert storage_module.windows_path_identity(r"C:\Project\Audio") == r"c:\project\audio"
    assert storage_module.windows_path_identity(r"\\?\C:\Project\Audio") == r"c:\project\audio"
    assert storage_module.windows_path_identity(r"\\Server\Share\Project\Audio") == (
        storage_module.windows_path_identity(r"\\?\UNC\Server\Share\Project\Audio")
    )


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


def test_fix_round_2_manifest_transaction_does_not_save_callback_exception(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    original = GenerationManifest(project_id="demo")
    original.append_version(
        "original-line",
        GenerationVersion(
            version_id="v001",
            line_uid="original-line",
            engine="gpt-sovits",
            profile="default",
            status="cancelled",
        ),
    )
    store.save_manifest(original)

    def mutate_then_fail(manifest: GenerationManifest) -> None:
        manifest.append_version(
            "uncommitted-line",
            GenerationVersion(
                version_id="v001",
                line_uid="uncommitted-line",
                engine="gpt-sovits",
                profile="default",
                status="completed",
            ),
        )
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        store.update_manifest("demo", mutate_then_fail)

    durable = store.load_manifest("demo")
    assert set(durable.lines) == {"original-line"}
    assert durable.lines["original-line"].versions[0].status == "cancelled"


def test_fix_round_3_same_project_nested_manifest_transaction_is_rejected_and_guard_recovers(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    replacement_store = ProjectStore(tmp_path)
    store.save_manifest(GenerationManifest(project_id="demo"))
    inner_called = False

    def append(line_id: str):
        def mutation(manifest: GenerationManifest) -> None:
            manifest.append_version(
                line_id,
                GenerationVersion(
                    version_id="v001",
                    line_uid=line_id,
                    engine="gpt-sovits",
                    profile="default",
                    status="completed",
                ),
            )

        return mutation

    def outer(manifest: GenerationManifest) -> None:
        nonlocal inner_called

        def inner(inner_manifest: GenerationManifest) -> None:
            nonlocal inner_called
            inner_called = True
            append("inner-line")(inner_manifest)

        replacement_store.update_manifest("DEMO", inner)
        append("outer-line")(manifest)

    with pytest.raises(RuntimeError, match="nested manifest transaction"):
        store.update_manifest("demo", outer)

    assert inner_called is False
    assert store.load_manifest("demo").lines == {}

    store.update_manifest("demo", append("recovered-line"))
    assert set(store.load_manifest("demo").lines) == {"recovered-line"}


def test_fix_round_3_different_project_nested_manifest_transaction_remains_supported(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    replacement_store = ProjectStore(tmp_path)

    def inner(second_manifest: GenerationManifest) -> None:
        second_manifest.append_version(
            "second-line",
            GenerationVersion(
                version_id="v001",
                line_uid="second-line",
                engine="gpt-sovits",
                profile="default",
                status="completed",
            ),
        )

    def outer(first_manifest: GenerationManifest) -> None:
        replacement_store.update_manifest("second-project", inner)
        first_manifest.append_version(
            "first-line",
            GenerationVersion(
                version_id="v001",
                line_uid="first-line",
                engine="gpt-sovits",
                profile="default",
                status="completed",
            ),
        )

    store.update_manifest("first-project", outer)

    assert set(store.load_manifest("first-project").lines) == {"first-line"}
    assert set(store.load_manifest("second-project").lines) == {"second-line"}


def test_fix_round_2_manifest_temp_files_are_unique_for_concurrent_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path)
    store.save_manifest(GenerationManifest(project_id="demo"))
    replace_guard = threading.Lock()
    manifest_temp_paths: list[Path] = []
    errors: list[BaseException] = []
    original_replace = Path.replace

    def interleaved_replace(path: Path, target: Path):
        if target.name == "manifest.json":
            with replace_guard:
                manifest_temp_paths.append(path)
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", interleaved_replace)

    def save(line_id: str) -> None:
        try:
            manifest = GenerationManifest(project_id="demo")
            manifest.append_version(
                line_id,
                GenerationVersion(
                    version_id="v001",
                    line_uid=line_id,
                    engine="gpt-sovits",
                    profile="default",
                    status="completed",
                ),
            )
            store.save_manifest(manifest)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=save, args=("first-line",), name="manifest-writer-1")
    second = threading.Thread(target=save, args=("second-line",), name="manifest-writer-2")
    first.start()
    second.start()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(set(manifest_temp_paths)) == 2
    assert errors == []
    assert store.load_manifest("demo").lines.keys() in ({"first-line"}, {"second-line"})
    assert not list(store.manifest_path("demo").parent.glob(".manifest.json.*.tmp"))


def test_fix_round_2_failed_atomic_replace_cleans_operation_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path)
    store.save_manifest(GenerationManifest(project_id="demo"))
    original_replace = Path.replace

    def fail_manifest_replace(path: Path, target: Path):
        if target.name == "manifest.json":
            raise PermissionError("replace denied")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_manifest_replace)

    with pytest.raises(PermissionError, match="replace denied"):
        store.save_manifest(GenerationManifest(project_id="demo"))

    assert not list(store.manifest_path("demo").parent.glob(".manifest.json.*.tmp"))


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


def test_fix_round_3_delete_reports_scrubbed_audio_cleanup_warning_after_manifest_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path)
    audio_path = store.project_audio_dir("demo") / "locked.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"RIFFlocked")
    manifest = GenerationManifest(project_id="demo")
    manifest.append_version(
        "locked-line",
        GenerationVersion(
            version_id="v001",
            line_uid="locked-line",
            engine="gpt-sovits",
            profile="default",
            status="completed",
            audio_path=str(audio_path),
        ),
    )
    store.save_manifest(manifest)
    original_unlink = Path.unlink

    def reject_locked_audio(path: Path, *args, **kwargs):
        if path.resolve(strict=False) == audio_path.resolve(strict=False):
            raise PermissionError("audio sharing violation password=delete-secret")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_locked_audio)
    client = TestClient(create_app(data_root=tmp_path), raise_server_exceptions=False)

    response = client.delete("/api/projects/demo/manifest/lines/locked-line/versions/v001")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "deleted"
    assert payload["audio_deleted"] is False
    assert "audio cleanup failed" in payload["warning"]
    assert "password=***" in payload["warning"]
    assert "delete-secret" not in str(payload)
    assert audio_path.is_file()
    assert store.load_manifest("demo").lines["locked-line"].versions == []
    assert client.delete("/api/projects/demo/manifest/lines/locked-line/versions/v001").status_code == 404


def test_fix_round_4_delete_treats_legacy_nul_audio_path_as_post_commit_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path)
    legacy_audio_path = str(store.project_audio_dir("demo") / "password=delete-secret\x00.wav")
    manifest = GenerationManifest(project_id="demo")
    manifest.append_version(
        "legacy-line",
        GenerationVersion(
            version_id="v001",
            line_uid="legacy-line",
            engine="gpt-sovits",
            profile="default",
            status="completed",
            audio_path=legacy_audio_path,
        ),
    )
    store.save_manifest(manifest)
    original_resolve = Path.resolve

    def reject_legacy_nul(path: Path, *args, **kwargs):
        if "\x00" in str(path):
            raise ValueError("legacy NUL audio path password=delete-secret")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_legacy_nul)
    client = TestClient(create_app(data_root=tmp_path), raise_server_exceptions=False)

    response = client.delete("/api/projects/demo/manifest/lines/legacy-line/versions/v001")

    assert store.load_manifest("demo").lines["legacy-line"].versions == []
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "deleted"
    assert payload["audio_deleted"] is False
    assert "audio cleanup failed" in payload["warning"]
    assert "password=***" in payload["warning"]
    assert "delete-secret" not in str(payload)
    assert client.delete("/api/projects/demo/manifest/lines/legacy-line/versions/v001").status_code == 404


def test_fix_round_2_delete_then_manager_append_does_not_resurrect_deleted_version(tmp_path: Path) -> None:
    from app.queue import GenerationJobManager

    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    app = create_app(data_root=tmp_path, services_path=services_path)
    client = TestClient(app)
    store = app.state.store
    original_audio = store.project_audio_dir("demo") / "original.wav"
    original_audio.parent.mkdir(parents=True, exist_ok=True)
    original_audio.write_bytes(b"RIFForiginal")
    manifest = GenerationManifest(project_id="demo")
    manifest.append_version(
        "shared-line",
        GenerationVersion(
            version_id="v001",
            line_uid="shared-line",
            engine="gpt-sovits",
            profile="default",
            service_id="mock-gpt",
            status="completed",
            audio_path=str(original_audio),
        ),
    )
    store.save_manifest(manifest)
    manager_snapshot_loaded = threading.Event()
    release_manager = threading.Event()
    manager_finished = threading.Event()
    original_queue = app.state.queue

    class BlockingQueue:
        router = original_queue.router

        def run(self, *args, **kwargs):
            manager_snapshot_loaded.set()
            assert release_manager.wait(3)
            return original_queue.run(*args, **kwargs)

    class ObservedManager(GenerationJobManager):
        def _finish_job(self, job_id: str) -> None:
            super()._finish_job(job_id)
            manager_finished.set()

    manager = ObservedManager(BlockingQueue(), store)
    task = GenerationTask.model_validate(
        {
            "line": {"id": "shared-line", "character_id": "role", "text": "new"},
            "engine": "gpt-sovits",
            "profile": "default",
            "service_id": "mock-gpt",
            "parameters": {
                "gpt_weights_path": "voice.ckpt",
                "sovits_weights_path": "voice.pth",
                "ref_audio_path": "voice.wav",
                "prompt_text": "参考文本",
            },
        }
    )
    created = manager.submit("demo", [task])
    assert manager_snapshot_loaded.wait(3)

    deleted = client.delete("/api/projects/demo/manifest/lines/shared-line/versions/v001")
    assert deleted.status_code == 200
    release_manager.set()
    assert manager_finished.wait(3)
    assert manager.get(created.job_id).status == "completed"

    versions = store.load_manifest("demo").lines["shared-line"].versions
    assert [(version.version_id, version.status) for version in versions] == [("v002", "completed")]
    assert original_audio.exists() is False


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
