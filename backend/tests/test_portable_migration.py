from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_importer():
    module_path = REPO_ROOT / "scripts" / "import_portable_data.py"
    assert module_path.is_file(), "portable migration core is missing"
    spec = importlib.util.spec_from_file_location("import_portable_data_for_tests", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _manifest(component: str, build: str, model_lock: str = "package/models.lock.json") -> dict[str, object]:
    return {
        "schema_version": 2,
        "component": component,
        "package_id": component,
        "release_version": build,
        "version": build,
        "build_id": build,
        "package_profile": "full",
        "platform": "windows-x64",
        "api_contract": "tts-more-v1",
        "protocol": {"name": "tts-more-v1", "version": "1.0", "controller_range": ">=0.2.0,<0.3.0"},
        "source": {"repository": "https://example.invalid/repo", "revision": "a" * 40},
        "integration": {"version": "2.0.0", "source_revision": "b" * 40, "bundle_sha256": "c" * 64},
        "runtime": {
            "python_version": "3.11",
            "device_profiles": ["cpu"],
            "lock": "package/runtime.lock.json",
            "state_path": "data/local/install-state.json",
        },
        "models": {"lock": model_lock, "required": True},
        "data_root": "data/local",
        "data": {
            "user": "data/user",
            "local": "data/local",
            "cache": "data/cache",
            "operations": "data/local/operations",
        },
        "launchers": {
            "initialize": "Initialize.cmd",
            "start": "Start.cmd",
            "stop": "Stop.cmd",
            "repair": "Repair.cmd",
            "build": "Build-Package.ps1",
        },
        "endpoint": {
            "default_url": "http://127.0.0.1:9880",
            "port": 9880,
            "health_path": "/health",
            "capabilities_path": "/capabilities",
            "bind_policy": "loopback",
        },
        "capabilities": ["tts"],
        "sha256_manifest": "SHA256SUMS.txt",
        "licenses": "licenses/THIRD_PARTY_NOTICES.json",
    }


def _write_package(
    root: Path,
    *,
    build: str,
    component: str = "gpt-sovits",
    asset_content: bytes = b"locked-model",
    locked_hash: str | None = None,
    locked_size: int | None = None,
    asset_target: str = "models/base.safetensors",
) -> Path:
    root.mkdir(parents=True)
    _write_json(root / "package" / "tts-more-package.json", _manifest(component, build))
    _write_json(root / "package" / "runtime.lock.json", {"schema_version": 1, "component": component})
    _write_json(
        root / "package" / "models.lock.json",
        {
            "schema_version": 1,
            "component": component,
            "complete": True,
            "assets": [
                {
                    "id": "base",
                    "target": asset_target,
                    "sha256": locked_hash or _sha256(asset_content),
                    "size_bytes": len(asset_content) if locked_size is None else locked_size,
                }
            ],
        },
    )
    asset = root / Path(asset_target.replace("/", os.sep))
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(asset_content)
    return root


def _write_version_pair(root: Path, matching_asset: bool) -> tuple[Path, Path]:
    old = _write_package(root / "old package", build="0.1.0")
    new_hash = _sha256(b"locked-model") if matching_asset else _sha256(b"new-version-model")
    new_size = len(b"locked-model") if matching_asset else len(b"new-version-model")
    new = _write_package(
        root / "new package",
        build="0.2.0",
        asset_content=b"new-version-model",
        locked_hash=new_hash,
        locked_size=new_size,
    )
    (new / "models" / "base.safetensors").unlink()
    user_file = old / "data" / "user" / "project.json"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("user-data", encoding="utf-8")
    pid = old / "data" / "local" / "run" / "worker.pid.json"
    pid.parent.mkdir(parents=True)
    pid.write_text('{"pid":1234}', encoding="utf-8")
    operation = old / "data" / "local" / "operations" / "active" / "operation.json"
    operation.parent.mkdir(parents=True)
    operation.write_text('{"status":"starting"}', encoding="utf-8")
    (old / ".venv").mkdir()
    (old / ".venv" / "machine.txt").write_text("never-copy", encoding="utf-8")
    (old / "runtime").mkdir()
    (old / "runtime" / "python.exe").write_bytes(b"never-copy")
    (old / "data" / "cache").mkdir()
    (old / "data" / "cache" / "download.bin").write_bytes(b"never-copy")
    return old, new


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    records: list[tuple[str, str, int, int]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
        digest = _sha256(path.read_bytes()) if kind == "file" else ""
        records.append((relative, f"{kind}:{digest}", metadata.st_size, metadata.st_mtime_ns))
    return tuple(records)


def _hardlink(link: Path, target: Path) -> None:
    try:
        os.link(target, link)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def _junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not link.exists():
        pytest.skip(f"junction creation is unavailable: {completed.stderr or completed.stdout}")


def test_import_copies_user_data_reuses_matching_assets_and_skips_pid_state(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)

    plan = importer.plan_import(old, new)
    report = importer.apply_import(plan)

    assert (new / "data/user/project.json").read_text(encoding="utf-8") == "user-data"
    assert report.copied_user_files == 1
    assert report.reused_assets == ["models/base.safetensors"]
    assert not (new / "data/local/run/worker.pid.json").exists()
    assert not (new / "data/local/operations").exists()
    assert not (new / ".venv").exists()
    assert not (new / "runtime").exists()
    assert not (new / "data/cache").exists()
    assert (old / "data/user/project.json").exists()


def test_hash_mismatch_is_not_reused(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=False)

    plan = importer.plan_import(old, new)
    report = importer.apply_import(plan)

    assert report.reused_assets == []
    assert not (new / "models/base.safetensors").exists()
    assert "models/base.safetensors" in report.skipped_assets


def test_plan_is_strictly_read_only_and_has_a_stable_digest(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    before = (_tree_snapshot(old), _tree_snapshot(new))

    first = importer.plan_import(old, new)
    second = importer.plan_import(old, new)

    assert (_tree_snapshot(old), _tree_snapshot(new)) == before
    assert first.plan_digest == second.plan_digest
    assert len(first.plan_digest) == 64


def test_existing_identical_destination_is_idempotent_but_different_content_blocks(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    destination = new / "data" / "user" / "project.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("user-data", encoding="utf-8")

    report = importer.apply_import(importer.plan_import(old, new))

    assert report.copied_user_files == 0
    assert "data/user/project.json" in report.already_present
    destination.write_text("different", encoding="utf-8")
    with pytest.raises(importer.PortableMigrationError, match="conflict|different"):
        importer.plan_import(old, new)


@pytest.mark.parametrize("relation", ("same", "old-contains-new", "new-contains-old"))
def test_same_or_nested_roots_are_rejected(tmp_path: Path, relation: str) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    if relation == "same":
        new = old
    elif relation == "old-contains-new":
        new = old / "nested"
        new.mkdir()
    else:
        old = new / "nested"
        old.mkdir()

    with pytest.raises(importer.PortableMigrationError, match="same|contain|nested"):
        importer.plan_import(old, new)


def test_schema_and_component_mismatch_are_rejected(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    manifest_path = old / "package" / "tts-more-package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    _write_json(manifest_path, manifest)
    with pytest.raises(importer.PortableMigrationError, match="schema"):
        importer.plan_import(old, new)

    manifest["schema_version"] = 2
    manifest["component"] = "cosyvoice"
    _write_json(manifest_path, manifest)
    with pytest.raises(importer.PortableMigrationError, match="component"):
        importer.plan_import(old, new)


def test_both_manifests_must_satisfy_the_complete_schema_v2_contract(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    manifest_path = old / "package" / "tts-more-package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["endpoint"]
    _write_json(manifest_path, manifest)

    with pytest.raises(importer.PortableMigrationError, match="schema|endpoint|manifest"):
        importer.plan_import(old, new)


def test_manifest_user_and_model_targets_cannot_enter_prohibited_areas(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path / "user-path", matching_asset=True)
    old_manifest_path = old / "package" / "tts-more-package.json"
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    old_manifest["data"]["user"] = "data/local"
    _write_json(old_manifest_path, old_manifest)

    with pytest.raises(importer.PortableMigrationError, match="data/user|prohibited|user data"):
        importer.plan_import(old, new)

    old, new = _write_version_pair(tmp_path / "model-path", matching_asset=True)
    for package in (old, new):
        lock_path = package / "package" / "models.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["assets"][0]["target"] = "data/local/install-state.json"
        _write_json(lock_path, lock)

    with pytest.raises(importer.PortableMigrationError, match="prohibited|model asset target"):
        importer.plan_import(old, new)


def test_empty_and_unsupported_components_are_rejected(tmp_path: Path) -> None:
    importer = _load_importer()
    for component in ("", "unsupported-worker"):
        old, new = _write_version_pair(tmp_path / (component or "empty"), matching_asset=True)
        for package in (old, new):
            manifest_path = package / "package" / "tts-more-package.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["component"] = component
            _write_json(manifest_path, manifest)
        with pytest.raises(importer.PortableMigrationError, match="component"):
            importer.plan_import(old, new)


@pytest.mark.parametrize(
    "unsafe_target",
    (
        "C:/outside/model.bin",
        "../outside/model.bin",
        "models/stream:evil.bin",
        "models／escape.bin",
        "models/CON.txt",
        "models/trailing. /model.bin",
    ),
)
def test_unsafe_model_targets_are_rejected(tmp_path: Path, unsafe_target: str) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    for package in (old, new):
        lock_path = package / "package" / "models.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["assets"][0]["target"] = unsafe_target
        _write_json(lock_path, lock)

    with pytest.raises(importer.PortableMigrationError, match="unsafe|relative|reserved|ADS|target"):
        importer.plan_import(old, new)


@pytest.mark.parametrize("collision", ("case", "nfkc"))
def test_user_path_normalization_collisions_are_rejected(tmp_path: Path, collision: str) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    user = old / "data" / "user"
    if collision == "case":
        first, second = user / "Voice.txt", user / "voice.TXT"
    else:
        first, second = user / "K.txt", user / "Ｋ.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    if len({path.name for path in user.iterdir() if path.name.casefold() == first.name.casefold()}) < 2 and collision == "case":
        pytest.skip("filesystem is case-insensitive and cannot create the collision fixture")

    with pytest.raises(importer.PortableMigrationError, match="collision"):
        importer.plan_import(old, new)


def test_manifest_lock_and_user_hardlinks_are_rejected(tmp_path: Path) -> None:
    importer = _load_importer()
    for relative in (
        "package/tts-more-package.json",
        "package/models.lock.json",
        "data/user/project.json",
    ):
        case_root = tmp_path / relative.replace("/", "-")
        old, new = _write_version_pair(case_root, matching_asset=True)
        path = old / relative
        outside = case_root / "outside.bin"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        _hardlink(path, outside)
        with pytest.raises(importer.PortableMigrationError, match="hard.?link"):
            importer.plan_import(old, new)


def test_model_hardlink_is_skipped_without_reading_or_copying(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    asset = old / "models" / "base.safetensors"
    outside = tmp_path / "outside-model.bin"
    outside.write_bytes(asset.read_bytes())
    asset.unlink()
    _hardlink(asset, outside)

    plan = importer.plan_import(old, new)
    report = importer.apply_import(plan)

    assert report.reused_assets == []
    assert report.skipped_assets == ["models/base.safetensors"]
    assert not (new / "models/base.safetensors").exists()


def test_user_reparse_is_rejected_and_model_reparse_is_skipped(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path / "user-case", matching_asset=True)
    user_file = old / "data" / "user" / "project.json"
    outside_user = tmp_path / "outside-user.json"
    outside_user.write_text("user-data", encoding="utf-8")
    user_file.unlink()
    _symlink(user_file, outside_user)
    with pytest.raises(importer.PortableMigrationError, match="reparse|link"):
        importer.plan_import(old, new)

    old, new = _write_version_pair(tmp_path / "model-case", matching_asset=True)
    model = old / "models" / "base.safetensors"
    outside_model = tmp_path / "outside-model.bin"
    outside_model.write_bytes(model.read_bytes())
    model.unlink()
    _symlink(model, outside_model)
    report = importer.apply_import(importer.plan_import(old, new))
    assert report.skipped_assets == ["models/base.safetensors"]
    assert not (new / "models/base.safetensors").exists()


def test_real_junction_user_path_is_rejected_and_model_path_is_skipped(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path / "user-junction", matching_asset=True)
    outside_user = tmp_path / "outside-user"
    outside_user.mkdir()
    (outside_user / "project.json").write_text("user-data", encoding="utf-8")
    user = old / "data" / "user"
    shutil.rmtree(user)
    _junction(user, outside_user)
    try:
        with pytest.raises(importer.PortableMigrationError, match="reparse|link"):
            importer.plan_import(old, new)
    finally:
        if user.exists():
            os.rmdir(user)

    old, new = _write_version_pair(tmp_path / "model-junction", matching_asset=True)
    outside_models = tmp_path / "outside-models"
    outside_models.mkdir()
    source_model = old / "models" / "base.safetensors"
    shutil.copy2(source_model, outside_models / source_model.name)
    shutil.rmtree(old / "models")
    _junction(old / "models", outside_models)
    try:
        report = importer.apply_import(importer.plan_import(old, new))
        assert report.skipped_assets == ["models/base.safetensors"]
        assert report.reused_assets == []
        assert not (new / "models/base.safetensors").exists()
    finally:
        if (old / "models").exists():
            os.rmdir(old / "models")


def test_manifest_and_lock_ancestors_cannot_be_junctions(tmp_path: Path) -> None:
    importer = _load_importer()
    for protected_name in ("package", "locks"):
        case_root = tmp_path / protected_name
        old, new = _write_version_pair(case_root, matching_asset=True)
        if protected_name == "package":
            protected = old / "package"
        else:
            protected = old / "locks"
            protected.mkdir()
            shutil.move(old / "package" / "models.lock.json", protected / "models.lock.json")
            manifest_path = old / "package" / "tts-more-package.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["models"]["lock"] = "locks/models.lock.json"
            _write_json(manifest_path, manifest)
        outside = case_root / "outside"
        shutil.copytree(protected, outside)
        shutil.rmtree(protected)
        _junction(protected, outside)
        try:
            with pytest.raises(importer.PortableMigrationError, match="ancestor|reparse|junction|link"):
                importer.plan_import(old, new)
        finally:
            if protected.exists():
                os.rmdir(protected)


def test_manifest_and_lock_json_are_parsed_from_the_verified_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    original = Path.read_text

    def forbid_second_path_open(path: Path, *args, **kwargs):
        if path.name in {"tts-more-package.json", "models.lock.json"}:
            raise AssertionError("JSON was reopened by path after verification")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_second_path_open)

    plan = importer.plan_import(old, new)

    assert plan.user_files


@pytest.mark.parametrize("old_target", ("Models/base.safetensors", "models\\base.safetensors"))
def test_model_reuse_requires_exact_target_spelling(tmp_path: Path, old_target: str) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    old_lock_path = old / "package" / "models.lock.json"
    old_lock = json.loads(old_lock_path.read_text(encoding="utf-8"))
    old_lock["assets"][0]["target"] = old_target
    _write_json(old_lock_path, old_lock)

    plan = importer.plan_import(old, new)

    assert plan.reusable_assets == ()
    assert plan.skipped_assets == ("models/base.safetensors",)


def test_target_appearing_during_copy_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)
    original = importer._copy_source_to_temporary

    def racing_copy(item, temporary):
        result = original(item, temporary)
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        item.destination.write_text("racing-target", encoding="utf-8")
        return result

    monkeypatch.setattr(importer, "_copy_source_to_temporary", racing_copy)
    with pytest.raises(importer.PortableMigrationError, match="target"):
        importer.apply_import(plan)
    destination = new / "data" / "user" / "project.json"
    assert destination.read_text(encoding="utf-8") == "racing-target"
    assert not list(new.rglob("*.tts-more-import-*.tmp"))


def test_apply_rejects_same_content_destination_identity_replacement(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    destination = new / "data" / "user" / "project.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("user-data", encoding="utf-8")
    plan = importer.plan_import(old, new)
    destination.unlink()
    destination.write_text("user-data", encoding="utf-8")

    with pytest.raises(importer.PortableMigrationError, match="changed|drift|identity"):
        importer.apply_import(plan)


def test_apply_rejects_destination_parent_created_after_planning(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)
    destination_parent = new / "data" / "user"
    destination_parent.mkdir(parents=True)

    with pytest.raises(importer.PortableMigrationError, match="changed|drift|directory|identity"):
        importer.apply_import(plan)


def test_parent_junction_race_cannot_publish_outside_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)
    original = importer._copy_source_to_temporary
    outside = tmp_path / "outside-target"
    outside.mkdir()
    detached = new / "detached-user"
    raced_parent: Path | None = None

    def racing_copy(item, temporary):
        nonlocal raced_parent
        if raced_parent is not None:
            return original(item, temporary)
        raced_parent = item.destination.parent
        raced_parent.rename(detached)
        _junction(raced_parent, outside)
        return original(item, temporary)

    monkeypatch.setattr(importer, "_copy_source_to_temporary", racing_copy)
    try:
        with pytest.raises(importer.PortableMigrationError, match="changed|drift|escape|unsafe|identity"):
            importer.apply_import(plan)
        assert not (outside / "project.json").exists()
    finally:
        if raced_parent is not None and raced_parent.exists():
            os.rmdir(raced_parent)


def test_publish_parent_race_uses_frozen_directory_and_temp_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)
    original = importer._publish_no_replace
    outside = tmp_path / "outside-publish"
    outside.mkdir()
    detached = new / "detached-publish-parent"
    raced_parent: Path | None = None

    def racing_publish(temporary: Path, destination: Path, **kwargs) -> None:
        nonlocal raced_parent
        if raced_parent is not None:
            return original(temporary, destination, **kwargs)
        raced_parent = destination.parent
        raced_parent.rename(detached)
        _junction(raced_parent, outside)
        shutil.copy2(detached / temporary.name, outside / temporary.name)
        return original(temporary, destination, **kwargs)

    monkeypatch.setattr(importer, "_publish_no_replace", racing_publish)
    try:
        with pytest.raises(importer.PortableMigrationError, match="changed|drift|escape|unsafe|identity"):
            importer.apply_import(plan)
        assert not (outside / "project.json").exists()
    finally:
        if raced_parent is not None and raced_parent.exists():
            os.rmdir(raced_parent)


def test_temp_cleanup_never_unlinks_a_replacement_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)
    replacement: list[Path] = []

    def replace_then_fail(temporary: Path, destination: Path, **kwargs) -> None:
        del destination
        del kwargs
        temporary.unlink()
        temporary.write_text("not-created-by-this-import", encoding="utf-8")
        replacement.append(temporary)
        raise importer.PortableMigrationError("forced publish failure")

    monkeypatch.setattr(importer, "_publish_no_replace", replace_then_fail)

    with pytest.raises(importer.PortableMigrationError, match="forced publish failure"):
        importer.apply_import(plan)
    assert replacement and replacement[0].read_text(encoding="utf-8") == "not-created-by-this-import"
    replacement[0].unlink()


@pytest.mark.parametrize("changed", ("manifest", "lock", "source", "target"))
def test_apply_rejects_every_post_plan_drift(tmp_path: Path, changed: str) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)
    if changed == "manifest":
        path = old / "package" / "tts-more-package.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif changed == "lock":
        path = new / "package" / "models.lock.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif changed == "source":
        (old / "data" / "user" / "project.json").write_text("changed", encoding="utf-8")
    else:
        target = new / "data" / "user" / "project.json"
        target.parent.mkdir(parents=True)
        target.write_text("appeared", encoding="utf-8")

    with pytest.raises(importer.PortableMigrationError, match="changed|drift|target|identity"):
        importer.apply_import(plan)
    assert (old / "data" / "user" / "project.json").exists()


def test_apply_detects_source_change_while_copying_and_never_publishes_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)
    original = importer._copy_source_to_temporary

    def changing_copy(item, temporary):
        result = original(item, temporary)
        item.source.write_text("changed-during-copy", encoding="utf-8")
        return result

    monkeypatch.setattr(importer, "_copy_source_to_temporary", changing_copy)

    with pytest.raises(importer.PortableMigrationError, match="changed|identity"):
        importer.apply_import(plan)
    assert not (new / "data" / "user" / "project.json").exists()
    assert not list(new.rglob("*.tts-more-import-*.tmp"))
    assert (old / "data" / "user" / "project.json").exists()


def test_cli_requires_explicit_confirmation_digest(tmp_path: Path) -> None:
    importer = _load_importer()
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    plan = importer.plan_import(old, new)

    assert importer.main(["apply", "--old-root", str(old), "--new-root", str(new)]) != 0
    assert importer.main(
        [
            "apply",
            "--old-root",
            str(old),
            "--new-root",
            str(new),
            "--confirmed-digest",
            "0" * 64,
        ]
    ) != 0
    assert not (new / "data" / "user" / "project.json").exists()
    assert importer.main(
        [
            "apply",
            "--old-root",
            str(old),
            "--new-root",
            str(new),
            "--confirmed-digest",
            plan.plan_digest,
        ]
    ) == 0
    assert (new / "data" / "user" / "project.json").exists()
