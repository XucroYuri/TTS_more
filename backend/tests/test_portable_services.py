from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import app.portable_services as portable_services
from app.main import create_app
from app.models import TTSServiceEndpoint
from app.portable_discovery import (
    PortablePackageRegisterRequest,
    endpoint_from_portable_package,
    read_portable_package,
)
from app.portable_services import (
    PortableServiceLocator,
    PortableServiceStore,
    resolve_locator,
)
from app.services import ServiceRegistry


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory links are unavailable: {symlink_error}")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"directory junctions are unavailable: {result.stderr or result.stdout}")


def _write_package(
    root: Path,
    *,
    component: str,
    package_id: str,
    port: int = 9880,
    build_id: str = "portable-build",
    controller_range: str = ">=0.2.0,<0.3.0",
    protocol_version: str = "1.0",
) -> Path:
    files = {
        "Initialize.cmd": "@echo off\n",
        "Start.cmd": "@echo off\n",
        "Stop.cmd": "@echo off\n",
        "Repair.cmd": "@echo off\n",
        "Build-Package.ps1": "# package builder\n",
        "tts_more/locks/runtime.lock.json": "{}\n",
        "tts_more/locks/models.lock.json": "{}\n",
        "THIRD_PARTY_NOTICES.json": "{}\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    payload = {
        "schema_version": 2,
        "component": component,
        "package_id": package_id,
        "release_version": "0.2.1",
        "version": "0.2.1",
        "build_id": build_id,
        "package_profile": "bootstrap",
        "platform": "windows-x64",
        "api_contract": "tts-more-v1",
        "source": {"repository": "https://example.invalid/repo", "revision": "a" * 40},
        "integration": {
            "version": "2.0.0",
            "source_revision": "b" * 40,
            "bundle_sha256": "c" * 64,
        },
        "runtime": {
            "python_version": "3.11",
            "device_profiles": ["auto", "cpu"],
            "lock": "tts_more/locks/runtime.lock.json",
            "state_path": "data/local/install-state.json",
        },
        "models": {"lock": "tts_more/locks/models.lock.json", "required": True},
        "data_root": "data/local",
        "protocol": {
            "name": "tts-more-v1",
            "version": protocol_version,
            "controller_range": controller_range,
        },
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
            "default_url": f"http://127.0.0.1:{port}",
            "port": port,
            "health_path": "/health",
            "capabilities_path": "/capabilities",
            "bind_policy": "loopback",
        },
        "capabilities": ["tts", "artifact-transfer"],
        "sha256_manifest": "SHA256SUMS.txt",
        "licenses": "THIRD_PARTY_NOTICES.json",
    }
    manifest = root / "package" / "tts-more-package.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8-sig")
    return root


def _locator(component: str, package_id: str, root: Path, *, port: int | None = None) -> PortableServiceLocator:
    return PortableServiceLocator(
        component=component,
        package_id=package_id,
        absolute_path_last_seen=str(root),
        build_id_last_seen="portable-build",
        port_override=port,
    )


def _endpoint(locator: PortableServiceLocator, *, display_name: str | None = None) -> TTSServiceEndpoint:
    return TTSServiceEndpoint(
        service_id=f"portable-{locator.component}-{locator.package_id}",
        display_name=display_name or locator.package_id,
        provider_type=locator.component,
        api_contract="tts-more-v1",
        base_url="http://127.0.0.1:9880",
        mode="local",
        network_scope="localhost",
        managed=True,
        control_kind="portable-package",
        portable_locator=locator,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("component", "gpt-sovits-dev"),
        ("package_id", ""),
        ("package_id", " gpt-main"),
        ("package_id", "gpt-main\n"),
        ("package_id", 42),
        ("build_id_last_seen", 42),
        ("build_id_last_seen", "build\u200bid"),
        ("absolute_path_last_seen", 42),
        ("port_override", 0),
        ("port_override", 65536),
    ),
)
def test_locator_rejects_ambiguous_identity_and_wrong_types(field: str, value: object) -> None:
    payload: dict[str, object] = {"component": "gpt-sovits", "package_id": "gpt-main"}
    payload[field] = value
    with pytest.raises(ValidationError):
        PortableServiceLocator.model_validate(payload)


def test_locator_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PortableServiceLocator(component="gpt-sovits", package_id="gpt-main", command="evil.cmd")


@pytest.mark.parametrize(
    "relative",
    (
        "GPT-SoVITS",
        "../../GPT-SoVITS",
        "../nested/GPT-SoVITS",
        "..\\nested\\GPT-SoVITS",
        "C:/GPT-SoVITS",
        "../ GPT-SoVITS",
        "../GPT-SoVITS ",
        "../GPT\u200bSoVITS",
    ),
)
def test_locator_rejects_non_sibling_relative_paths(relative: str) -> None:
    with pytest.raises(ValidationError):
        PortableServiceLocator(
            component="gpt-sovits",
            package_id="gpt-main",
            relative_to_tts_more=relative,
        )


def test_locator_prefers_relative_then_absolute_then_search_identity(tmp_path: Path) -> None:
    controller = tmp_path / "suite" / "TTS More"
    controller.mkdir(parents=True)
    relative = _write_package(
        tmp_path / "suite" / "自定义 GPT 文件夹",
        component="gpt-sovits",
        package_id="gpt-main",
        build_id="relative-build",
    )
    absolute = _write_package(
        tmp_path / "另一磁盘 模拟" / "GPT absolute",
        component="gpt-sovits",
        package_id="gpt-main",
        build_id="absolute-build",
    )
    searched = _write_package(
        tmp_path / "search" / "GPT found",
        component="gpt-sovits",
        package_id="gpt-main",
        build_id="search-build",
    )
    locator = PortableServiceLocator(
        component="gpt-sovits",
        package_id="gpt-main",
        relative_to_tts_more="../自定义 GPT 文件夹",
        absolute_path_last_seen=str(absolute),
        build_id_last_seen="old-build",
    )

    descriptor = resolve_locator(controller, locator, [searched])

    assert descriptor is not None
    assert Path(descriptor.package_root) == relative.resolve()
    assert descriptor.build_id == "relative-build"


def test_locator_falls_back_to_absolute_then_bounded_search_root(tmp_path: Path) -> None:
    controller = tmp_path / "suite" / "TTS More"
    controller.mkdir(parents=True)
    absolute = _write_package(
        tmp_path / "可移动盘 模拟" / "Index 包",
        component="indextts",
        package_id="index-main",
    )
    locator = _locator("indextts", "index-main", absolute)

    assert Path(resolve_locator(controller, locator, []).package_root) == absolute.resolve()  # type: ignore[union-attr]

    absolute.rename(tmp_path / "moved-away")
    container = tmp_path / "explicit search"
    searched = _write_package(container / "中文 Index 新目录", component="indextts", package_id="index-main")
    descriptor = resolve_locator(controller, locator, [container, container, searched])

    assert descriptor is not None
    assert Path(descriptor.package_root) == searched.resolve()


def test_locator_skips_incompatible_relative_candidate_for_compatible_absolute_fallback(tmp_path: Path) -> None:
    controller = tmp_path / "suite" / "TTS More"
    controller.mkdir(parents=True)
    _write_package(
        tmp_path / "suite" / "GPT old",
        component="gpt-sovits",
        package_id="gpt-main",
        controller_range=">=0.3.0,<0.4.0",
    )
    compatible = _write_package(
        tmp_path / "fallback" / "GPT current",
        component="gpt-sovits",
        package_id="gpt-main",
    )
    locator = PortableServiceLocator(
        component="gpt-sovits",
        package_id="gpt-main",
        relative_to_tts_more="../GPT old",
        absolute_path_last_seen=str(compatible),
    )

    descriptor = resolve_locator(controller, locator, [])

    assert descriptor is not None
    assert Path(descriptor.package_root) == compatible.resolve()
    [stored] = PortableServiceStore(controller).upsert(_endpoint(locator))
    assert stored.managed is True
    assert Path(stored.repo_path) == compatible.resolve()  # type: ignore[arg-type]


def test_locator_search_is_one_level_only_and_does_not_recurse(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    nested = _write_package(
        tmp_path / "search" / "level-one" / "nested Cosy",
        component="cosyvoice",
        package_id="cosy-main",
    )
    locator = PortableServiceLocator(component="cosyvoice", package_id="cosy-main")

    assert resolve_locator(controller, locator, [tmp_path / "search"]) is None
    assert resolve_locator(controller, locator, [nested]) is not None


def test_locator_search_skips_user_home_and_caps_container_enumeration(tmp_path: Path, monkeypatch) -> None:
    controller = tmp_path / "controller" / "TTS More"
    controller.mkdir(parents=True)
    container = tmp_path / "large search root"
    container.mkdir()
    original_iterdir = Path.iterdir
    enumerated = 0

    def bounded_iterdir(path: Path):
        nonlocal enumerated
        if path == Path.home():
            raise AssertionError("the user home directory must never be scanned")
        if path == container:
            for index in range(1000):
                enumerated += 1
                yield container / f"entry-{index:04d}"
            return
        yield from original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", bounded_iterdir)
    locator = PortableServiceLocator(component="cosyvoice", package_id="cosy-main")

    assert resolve_locator(controller, locator, [Path.home(), container]) is None
    assert enumerated <= 256


def test_locator_uses_package_identity_not_build_id(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    wrong = _write_package(
        tmp_path / "wrong",
        component="gpt-sovits",
        package_id="different-package",
        build_id="remembered-build",
    )
    right = _write_package(
        tmp_path / "right",
        component="gpt-sovits",
        package_id="gpt-main",
        build_id="new-build",
    )
    locator = PortableServiceLocator(
        component="gpt-sovits",
        package_id="gpt-main",
        absolute_path_last_seen=str(wrong),
        build_id_last_seen="remembered-build",
    )

    descriptor = resolve_locator(controller, locator, [right])

    assert descriptor is not None
    assert descriptor.build_id == "new-build"


def test_unicode_and_case_alias_candidates_cannot_bypass_ordered_deduplication(tmp_path: Path) -> None:
    controller = tmp_path / "controller" / "TTS More"
    controller.mkdir(parents=True)
    wrong = _write_package(
        tmp_path / "search" / "GPT",
        component="gpt-sovits",
        package_id="other-package",
    )
    alias = _write_package(
        tmp_path / "search" / "ＧＰＴ",
        component="gpt-sovits",
        package_id="gpt-main",
    )
    locator = PortableServiceLocator(component="gpt-sovits", package_id="gpt-main")

    assert resolve_locator(controller, locator, [wrong, alias]) is None


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.update(schema_version=1),
        lambda payload: payload.pop("package_id"),
        lambda payload: payload["protocol"].update(name="wrong"),
        lambda payload: payload["protocol"].update(version="2.0"),
        lambda payload: payload["protocol"].update(controller_range=">=0.3.0,<0.4.0"),
        lambda payload: payload["launchers"].update(start="../Start.cmd"),
        lambda payload: payload["endpoint"].update(default_url="http://127.0.0.1:9880@evil.example"),
        lambda payload: payload["endpoint"].update(default_url="http://127.0.0.1:9999"),
    ),
)
def test_locator_rejects_incomplete_invalid_or_incompatible_packages(
    tmp_path: Path, mutation
) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    manifest = package / "package" / "tts-more-package.json"
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    mutation(payload)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    locator = _locator("gpt-sovits", "gpt-main", package)

    assert resolve_locator(controller, locator, []) is None
    visible = read_portable_package(package)
    assert visible is not None
    assert visible.manageable is False


def test_locator_requires_every_manifest_root_launcher(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    (package / "Stop.cmd").unlink()

    assert resolve_locator(controller, _locator("gpt-sovits", "gpt-main", package), []) is None


def test_locator_applies_port_override_without_mutating_manifest(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main", port=9880)

    descriptor = resolve_locator(
        controller,
        _locator("gpt-sovits", "gpt-main", package, port=19980),
        [],
    )

    assert descriptor is not None
    assert descriptor.port == 19980
    assert descriptor.default_url == "http://127.0.0.1:19980"
    assert read_portable_package(package).port == 9880


def test_relative_sibling_reparse_point_is_rejected(tmp_path: Path) -> None:
    controller = tmp_path / "suite" / "TTS More"
    controller.mkdir(parents=True)
    real = _write_package(tmp_path / "real GPT", component="gpt-sovits", package_id="gpt-main")
    link = tmp_path / "suite" / "GPT link"
    _make_directory_link(link, real)
    locator = PortableServiceLocator(
        component="gpt-sovits",
        package_id="gpt-main",
        relative_to_tts_more="../GPT link",
    )

    assert resolve_locator(controller, locator, []) is None


def test_locator_rejects_reparse_manifest_and_data_paths(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    manifest_link_package = _write_package(
        tmp_path / "manifest link package",
        component="gpt-sovits",
        package_id="gpt-main",
    )
    external_manifest = tmp_path / "external manifest"
    (manifest_link_package / "package").rename(external_manifest)
    _make_directory_link(manifest_link_package / "package", external_manifest)
    data_link_package = _write_package(
        tmp_path / "data link package",
        component="indextts",
        package_id="index-main",
    )
    external_data = tmp_path / "external data"
    external_data.mkdir()
    _make_directory_link(data_link_package / "data", external_data)

    assert resolve_locator(
        controller,
        _locator("gpt-sovits", "gpt-main", manifest_link_package),
        [],
    ) is None
    assert resolve_locator(
        controller,
        _locator("indextts", "index-main", data_link_package),
        [],
    ) is None


def test_incompatible_package_remains_visible_but_endpoint_is_unmanaged(tmp_path: Path) -> None:
    package = _write_package(
        tmp_path / "GPT",
        component="gpt-sovits",
        package_id="gpt-main",
        controller_range=">=0.3.0,<0.4.0",
    )
    descriptor = read_portable_package(package)

    endpoint = endpoint_from_portable_package(
        descriptor,
        request=PortablePackageRegisterRequest(package_root=str(package)),
    )

    assert descriptor.valid is True
    assert descriptor.controller_compatible is False
    assert descriptor.manageable is False
    assert endpoint.control_kind == "portable-package"
    assert endpoint.managed is False


def test_endpoint_identity_is_stable_across_builds(tmp_path: Path) -> None:
    first_root = _write_package(
        tmp_path / "GPT first",
        component="gpt-sovits",
        package_id="gpt-main",
        build_id="build-one",
    )
    second_root = _write_package(
        tmp_path / "GPT second",
        component="gpt-sovits",
        package_id="gpt-main",
        build_id="build-two",
    )

    first = endpoint_from_portable_package(
        read_portable_package(first_root),
        PortablePackageRegisterRequest(package_root=str(first_root)),
    )
    second = endpoint_from_portable_package(
        read_portable_package(second_root),
        PortablePackageRegisterRequest(package_root=str(second_root)),
    )

    assert first.service_id == second.service_id == "portable-gpt-sovits-gpt-main"


def test_store_missing_file_returns_safe_default(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    store = PortableServiceStore(controller)

    assert store.path == controller / "data" / "local" / "services.json"
    assert store.load() == []
    assert not store.path.exists()


def test_store_writes_versioned_document_and_roundtrips_unknown_endpoint_data(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    store = PortableServiceStore(controller)
    raw = {
        **_endpoint(_locator("gpt-sovits", "gpt-main", package)).model_dump(mode="json"),
        "future_endpoint_field": {"keep": True},
    }
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps([raw]), encoding="utf-8")

    loaded = store.load()
    store.save(loaded)

    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["services"][0]["future_endpoint_field"] == {"keep": True}
    assert loaded[0].portable_locator.package_id == "gpt-main"  # type: ignore[union-attr]
    assert TTSServiceEndpoint.model_validate(saved["services"][0]).model_dump(mode="json") == saved["services"][0]


@pytest.mark.parametrize("payload", ("{broken", "{}", '{"schema_version":1,"services":"wrong"}'))
def test_store_corruption_fails_closed_and_upsert_does_not_overwrite(tmp_path: Path, payload: str) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    store = PortableServiceStore(controller)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError):
        store.upsert(_endpoint(_locator("gpt-sovits", "gpt-main", package)))

    assert store.path.read_text(encoding="utf-8") == payload


def test_store_save_does_not_overwrite_an_existing_corrupt_document(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    store = PortableServiceStore(controller)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="unreadable"):
        store.save([])

    assert store.path.read_text(encoding="utf-8") == "{broken"


def test_store_atomic_replace_failure_preserves_old_readable_document(tmp_path: Path, monkeypatch) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    store = PortableServiceStore(controller)
    existing = _endpoint(_locator("gpt-sovits", "gpt-main", package), display_name="before")
    store.save([existing])
    before = store.path.read_bytes()

    monkeypatch.setattr("app.portable_services.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(OSError, match="locked"):
        store.save([existing.model_copy(update={"display_name": "after"})])

    assert store.path.read_bytes() == before
    assert json.loads(before)["services"][0]["display_name"] == "before"
    assert not list(store.path.parent.glob(".services.json.*.tmp"))


def test_store_upsert_is_concurrent_unique_and_deterministic(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    packages = {
        component: _write_package(tmp_path / component, component=component, package_id=f"{component}-main")
        for component in ("gpt-sovits", "indextts", "cosyvoice")
    }

    def add(component: str) -> None:
        locator = _locator(component, f"{component}-main", packages[component])
        PortableServiceStore(controller).upsert(_endpoint(locator))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(add, ("gpt-sovits", "indextts", "cosyvoice", "gpt-sovits", "indextts", "cosyvoice")))

    loaded = PortableServiceStore(controller).load()
    identities = [(item.portable_locator.component, item.portable_locator.package_id) for item in loaded]  # type: ignore[union-attr]
    assert identities == sorted(set(identities))
    assert len(identities) == 3


def test_empty_store_lock_is_initialized_only_after_exclusive_lock(monkeypatch, tmp_path: Path) -> None:
    directory = tmp_path / "lock"
    directory.mkdir()
    lock_path = directory / ".services.lock"
    lock_path.touch()
    original_acquire = portable_services._acquire_os_lock

    def assert_empty_then_acquire(handle) -> None:
        assert lock_path.read_bytes() == b""
        original_acquire(handle)

    monkeypatch.setattr(portable_services, "_acquire_os_lock", assert_empty_then_acquire)

    with portable_services._store_lock(directory):
        pass
    assert lock_path.read_bytes() == b"\0"


def test_store_rejects_data_directory_reparse_escape(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_directory_link(controller / "data", outside)
    store = PortableServiceStore(controller)

    with pytest.raises(ValueError, match="reparse|link"):
        store.save([])

    assert not (outside / "local" / "services.json").exists()


def test_store_does_not_trust_persisted_portable_control_fields(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(
        tmp_path / "GPT",
        component="gpt-sovits",
        package_id="gpt-main",
        controller_range=">=0.3.0,<0.4.0",
    )
    store = PortableServiceStore(controller)
    raw = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_dump(mode="json")
    raw.update(
        {
            "managed": True,
            "repo_path": "C:/untrusted",
            "start_command": ["evil.exe"],
            "start_cwd": "C:/untrusted",
        }
    )
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"schema_version": 1, "services": [raw]}), encoding="utf-8")

    [loaded] = store.load()

    assert loaded.managed is False
    assert loaded.repo_path is None
    assert loaded.start_command == []
    assert loaded.start_cwd is None


def test_legacy_endpoint_without_portable_fields_roundtrips_unchanged() -> None:
    legacy = {
        "service_id": "remote-gpt",
        "engine": "gpt-sovits",
        "base_url": "http://192.168.1.20:9880",
        "mode": "external",
        "managed": False,
        "future_endpoint_field": "preserved",
    }

    endpoint = TTSServiceEndpoint.model_validate(legacy)
    roundtrip = endpoint.model_dump(mode="json", exclude_defaults=True)

    assert endpoint.portable_locator is None
    assert endpoint.control_kind == "generic"
    assert roundtrip["future_endpoint_field"] == "preserved"


def test_versioned_store_remains_compatible_with_registry_and_existing_api(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    raw_endpoint = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_dump(mode="json")
    raw_endpoint["future_endpoint_field"] = {"keep": True}
    endpoint = TTSServiceEndpoint.model_validate(raw_endpoint)
    store = PortableServiceStore(controller)
    store.save([endpoint])

    registry = ServiceRegistry.load(store.path)
    registry.save(store.path)
    client = TestClient(create_app(data_root=controller / "data", env_path=tmp_path / ".env.local"))

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["services"][0]["future_endpoint_field"] == {"keep": True}
    assert registry.get(endpoint.service_id).portable_locator.package_id == "gpt-main"  # type: ignore[union-attr]
    assert client.get("/api/settings/services").status_code == 200


def test_legacy_registry_save_cannot_overwrite_a_corrupt_shared_store(tmp_path: Path) -> None:
    path = tmp_path / "data" / "local" / "services.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    registry = ServiceRegistry(
        [
            TTSServiceEndpoint(
                service_id="legacy",
                base_url="http://127.0.0.1:9880",
            )
        ]
    )

    with pytest.raises(ValueError, match="unreadable|decode|JSON"):
        registry.save(path)

    assert path.read_text(encoding="utf-8") == "{broken"
