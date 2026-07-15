from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import app.portable_locator_mutations as portable_locator_mutations
import app.service_store_io as service_store_io
from app.main import create_app
from app.models import TTSServiceEndpoint
from app.portable_discovery import (
    PortablePackageRegisterRequest,
    endpoint_from_portable_package,
    read_portable_package,
)
from app.portable_endpoint_trust import require_unique_service_identities
from app.portable_locator_mutations import (
    ManagedPortableLocatorMutationError,
    PortableLocatorMutationCoordinator,
)
from app.portable_services import (
    PortableServiceLocator,
    PortableServiceStore,
    resolve_locator,
)
from app.service_config import ServiceSettingsRecord, ServiceSettingsUpdate, save_service_settings
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


def test_registry_save_exposes_no_portable_locator_mutation_authorization_surface() -> None:
    parameters = inspect.signature(ServiceRegistry.save).parameters

    assert not hasattr(portable_locator_mutations, "_PortableLocatorMutationPermit")
    assert all("permit" not in name and "authoriz" not in name for name in parameters)
    assert "expected_published_services" not in parameters


def test_generic_registry_save_rejects_forged_expected_published_snapshot_argument(
    tmp_path: Path,
) -> None:
    controller = tmp_path / "forged snapshot root"
    controller.mkdir()
    package = _write_package(
        tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main"
    )
    endpoint = _endpoint(_locator("gpt-sovits", "gpt-main", package))
    store = PortableServiceStore(controller)
    store.replace_component(endpoint)
    registry = ServiceRegistry.load(store.path)

    with pytest.raises(TypeError):
        registry.save(
            store.path,
            expected_published_services=registry.services,
        )


@pytest.mark.parametrize("coordinator_component", [None, "gpt-sovits", "cosyvoice"])
def test_generic_registry_save_always_rejects_locator_delta_in_every_caller_context(
    tmp_path: Path,
    coordinator_component: str | None,
) -> None:
    controller = tmp_path / "alternate registry root"
    controller.mkdir()
    first_package = _write_package(
        tmp_path / "GPT A", component="gpt-sovits", package_id="gpt-main"
    )
    second_package = _write_package(
        tmp_path / "GPT B", component="gpt-sovits", package_id="gpt-main"
    )
    first = _endpoint(_locator("gpt-sovits", "gpt-main", first_package))
    second = _endpoint(_locator("gpt-sovits", "gpt-main", second_package))
    store = PortableServiceStore(controller)
    store.replace_component(first)
    before = store.path.read_bytes()
    current = ServiceRegistry.load(store.path)
    updated = current.with_services(
        [second if item.service_id == first.service_id else item for item in current.services]
    )

    class NoOpSupervisor:
        def portable_lifecycle_guard(self, _component):
            return nullcontext()

    class Invalidator:
        def invalidate_component(self, _component) -> None:
            raise AssertionError("failed generic save must not invalidate")

    def save() -> None:
        updated.save(store.path)

    with pytest.raises(ManagedPortableLocatorMutationError):
        if coordinator_component is None:
            save()
        else:
            PortableLocatorMutationCoordinator(
                NoOpSupervisor(),
                Invalidator(),
            ).mutate_component(coordinator_component, save)

    assert store.path.read_bytes() == before


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
        "SHA256SUMS.txt": "portable checksum manifest\n",
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

    monkeypatch.setattr("app.service_store_io.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("locked")))
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


def test_replace_component_is_one_atomic_transaction_under_a_barrier(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    gpt_a = _write_package(tmp_path / "GPT A", component="gpt-sovits", package_id="gpt-a")
    gpt_b = _write_package(tmp_path / "GPT B", component="gpt-sovits", package_id="gpt-b")
    index = _write_package(tmp_path / "Index", component="indextts", package_id="index-main")
    legacy = TTSServiceEndpoint(service_id="legacy", base_url="http://127.0.0.1:9000")
    index_endpoint = _endpoint(_locator("indextts", "index-main", index))
    PortableServiceStore(controller).save([legacy, index_endpoint])
    barrier = threading.Barrier(2)

    def replace(package_id: str, package: Path) -> None:
        barrier.wait(timeout=5)
        PortableServiceStore(controller).replace_component(
            _endpoint(_locator("gpt-sovits", package_id, package))
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(replace, "gpt-a", gpt_a),
            pool.submit(replace, "gpt-b", gpt_b),
        ]
        for future in futures:
            future.result(timeout=10)

    loaded = PortableServiceStore(controller).load()
    gpt = [
        item
        for item in loaded
        if item.portable_locator is not None
        and item.portable_locator.component == "gpt-sovits"
    ]
    assert len(gpt) == 1
    assert gpt[0].portable_locator.package_id in {"gpt-a", "gpt-b"}
    assert any(item.service_id == "legacy" for item in loaded)
    assert any(
        item.portable_locator is not None and item.portable_locator.component == "indextts"
        for item in loaded
    )


def test_empty_store_lock_is_initialized_only_after_exclusive_lock(monkeypatch, tmp_path: Path) -> None:
    directory = tmp_path / "lock"
    directory.mkdir()
    lock_path = directory / ".services.lock"
    lock_path.touch()
    original_acquire = service_store_io._acquire_os_lock

    def assert_empty_then_acquire(handle) -> None:
        assert lock_path.read_bytes() == b""
        original_acquire(handle)

    monkeypatch.setattr(service_store_io, "_acquire_os_lock", assert_empty_then_acquire)

    with service_store_io.services_store_lock(directory):
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


# Review regressions: trust boundaries, strict schema, shared writer, identity and budgets.


def test_registry_load_sanitizes_forged_portable_control_fields(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    endpoint = _endpoint(_locator("gpt-sovits", "gpt-main", package))
    raw = endpoint.model_dump(mode="json")
    raw.update(
        managed=True,
        repo_path="C:/forged",
        start_command=["FORGED-COMMAND-MARKER.exe"],
        start_cwd="C:/forged",
    )
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"schema_version": 1, "services": [raw]}), encoding="utf-8")

    [loaded] = ServiceRegistry.load(path).services

    assert loaded.managed is False
    assert loaded.repo_path is None
    assert loaded.start_command == []
    assert loaded.start_cwd is None
    assert "FORGED-COMMAND-MARKER" not in json.dumps(loaded.model_dump(mode="json"))


def test_upsert_sanitizes_every_retained_portable_record_before_write(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    index_package = _write_package(tmp_path / "Index", component="indextts", package_id="index-main")
    gpt_package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    retained = _endpoint(_locator("indextts", "index-main", index_package))
    raw = retained.model_dump(mode="json")
    raw.update(
        managed=True,
        repo_path="C:/forged",
        start_command=["FORGED-COMMAND-MARKER.exe"],
        start_cwd="C:/forged",
    )
    store = PortableServiceStore(controller)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"schema_version": 1, "services": [raw]}), encoding="utf-8")

    store.upsert(_endpoint(_locator("gpt-sovits", "gpt-main", gpt_package)))

    persisted = store.path.read_text(encoding="utf-8")
    assert "FORGED-COMMAND-MARKER" not in persisted
    loaded = {item.service_id: item for item in store.load()}
    assert loaded[retained.service_id].start_command == []
    assert loaded[retained.service_id].repo_path == str(index_package.resolve())


@pytest.mark.parametrize(
    "endpoint_updates",
    (
        {"mode": "external", "network_scope": "lan", "managed": True},
        {"mode": "local", "network_scope": "lan", "managed": True},
        {"mode": "local", "network_scope": "localhost", "api_contract": "forged-v1", "managed": True},
    ),
)
def test_resolved_factory_never_trusts_nonlocal_or_wrong_contract_endpoint(
    tmp_path: Path, endpoint_updates: dict[str, object]
) -> None:
    from app.portable_endpoint_trust import trust_resolved_portable_endpoint

    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    descriptor = read_portable_package(package)
    endpoint = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_copy(update=endpoint_updates)

    trusted = trust_resolved_portable_endpoint(endpoint, descriptor)

    assert trusted.managed is False
    assert trusted.repo_path is None
    assert trusted.start_command == []
    assert trusted.start_cwd is None


@pytest.mark.parametrize("schema_version", (True, "2", 2.0))
def test_manifest_schema_version_requires_exact_json_integer_two(tmp_path: Path, schema_version: object) -> None:
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    manifest = package / "package" / "tts-more-package.json"
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    payload["schema_version"] = schema_version
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version.*exact integer"):
        read_portable_package(package)


@pytest.mark.parametrize("schema_version", (True, "1", 1.0))
def test_services_wrapper_version_requires_exact_json_integer_one(tmp_path: Path, schema_version: object) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    store = PortableServiceStore(controller)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"schema_version": schema_version, "services": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version"):
        store.load()
    with pytest.raises(ValueError, match="versioned|schema_version"):
        ServiceRegistry.load(store.path)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.update(runtime=[]),
        lambda payload: payload["runtime"].update(device_profiles=["cpu", 1]),
        lambda payload: payload["runtime"].update(device_profiles=["future-gpu"]),
        lambda payload: payload["models"].update(required=1),
        lambda payload: payload["endpoint"].update(port="9880"),
        lambda payload: payload.update(capabilities=["tts", 1]),
        lambda payload: payload["protocol"].update(version=1),
        lambda payload: payload["data"].update(local=123),
    ),
)
def test_strict_raw_v2_validator_rejects_nested_coercion_and_unknown_profiles(
    tmp_path: Path, mutation
) -> None:
    from app.portable_manifest import validate_portable_manifest_v2_raw

    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    payload = json.loads((package / "package" / "tts-more-package.json").read_text(encoding="utf-8-sig"))
    mutation(payload)

    model, errors = validate_portable_manifest_v2_raw(payload)

    assert model is None
    assert errors


def test_sha256_manifest_must_be_an_existing_regular_contained_file(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    locator = _locator("gpt-sovits", "gpt-main", package)
    sums = package / "SHA256SUMS.txt"
    sums.unlink()
    sums.mkdir()

    assert resolve_locator(controller, locator, []) is None

    sums.rmdir()
    external = tmp_path / "external sums"
    external.mkdir()
    (external / "SHA256SUMS.txt").write_text("outside\n", encoding="utf-8")
    _make_directory_link(package / "checksums", external)
    manifest = package / "package" / "tts-more-package.json"
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    payload["sha256_manifest"] = "checksums/SHA256SUMS.txt"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    assert resolve_locator(controller, locator, []) is None


def test_stale_registry_delta_preserves_concurrent_add_and_applies_delete(tmp_path: Path) -> None:
    path = tmp_path / "data" / "local" / "services.json"
    initial = [
        TTSServiceEndpoint(service_id="delete-me", base_url="http://127.0.0.1:9001"),
        TTSServiceEndpoint(service_id="keep-me", base_url="http://127.0.0.1:9002"),
    ]
    ServiceRegistry(initial).save(path)
    stale = ServiceRegistry.load(path)
    desired = [service for service in stale.services if service.service_id != "delete-me"]
    desired.append(TTSServiceEndpoint(service_id="registry-add", base_url="http://127.0.0.1:9003"))
    ServiceRegistry([*initial, TTSServiceEndpoint(service_id="concurrent-add", base_url="http://127.0.0.1:9004")]).save(path)

    stale.with_services(desired).save(path)

    assert {item.service_id for item in ServiceRegistry.load(path).services} == {
        "keep-me",
        "registry-add",
        "concurrent-add",
    }


def test_stale_portable_store_save_does_not_revive_a_concurrent_delete(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    store = PortableServiceStore(controller)
    initial = [
        TTSServiceEndpoint(service_id="delete-me", base_url="http://127.0.0.1:9001"),
        TTSServiceEndpoint(service_id="keep-me", base_url="http://127.0.0.1:9002"),
    ]
    store.save(initial)
    stale = store.load()
    ServiceRegistry.load(store.path).with_services([initial[1]]).save(store.path)

    store.save([*stale, TTSServiceEndpoint(service_id="portable-add", base_url="http://127.0.0.1:9003")])

    assert {item.service_id for item in ServiceRegistry.load(store.path).services} == {
        "keep-me",
        "portable-add",
    }


def test_registry_and_portable_upsert_interleave_across_processes_without_lost_update(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    store = PortableServiceStore(controller)
    store.save(
        [
            TTSServiceEndpoint(service_id="delete-me", base_url="http://127.0.0.1:9001"),
            TTSServiceEndpoint(service_id="keep-me", base_url="http://127.0.0.1:9002"),
        ]
    )
    ready = tmp_path / "registry.ready"
    go = tmp_path / "go"
    upsert_done = tmp_path / "upsert.done"
    backend_root = Path(__file__).resolve().parents[1]
    registry_code = """
import sys, time
from pathlib import Path
from app.models import TTSServiceEndpoint
from app.services import ServiceRegistry
path, ready, go, done = map(Path, sys.argv[1:5])
registry = ServiceRegistry.load(path)
desired = [item for item in registry.services if item.service_id != 'delete-me']
desired.append(TTSServiceEndpoint(service_id='registry-add', base_url='http://127.0.0.1:9003'))
ready.write_text('ready')
while not go.exists(): time.sleep(0.005)
while not done.exists(): time.sleep(0.005)
registry.with_services(desired).save(path)
"""
    upsert_code = """
import sys, time
from pathlib import Path
from app.models import PortableServiceLocator, TTSServiceEndpoint
from app.portable_services import PortableServiceStore
controller, package, go, done = map(Path, sys.argv[1:5])
while not go.exists(): time.sleep(0.005)
locator = PortableServiceLocator(component='gpt-sovits', package_id='gpt-main', absolute_path_last_seen=str(package))
endpoint = TTSServiceEndpoint(service_id='portable-gpt-sovits-gpt-main', provider_type='gpt-sovits', base_url='http://127.0.0.1:9880', control_kind='portable-package', portable_locator=locator)
PortableServiceStore(controller).upsert(endpoint)
done.write_text('done')
"""
    registry_process = subprocess.Popen(
        [sys.executable, "-c", registry_code, str(store.path), str(ready), str(go), str(upsert_done)],
        cwd=backend_root,
    )
    upsert_process = subprocess.Popen(
        [sys.executable, "-c", upsert_code, str(controller), str(package), str(go), str(upsert_done)],
        cwd=backend_root,
    )
    try:
        for _ in range(1000):
            if ready.exists():
                break
            __import__("time").sleep(0.005)
        assert ready.exists()
        go.write_text("go")
        assert upsert_process.wait(timeout=30) == 0
        assert registry_process.wait(timeout=30) == 0
    finally:
        for process in (registry_process, upsert_process):
            if process.poll() is None:
                process.kill()

    assert {item.service_id for item in ServiceRegistry.load(store.path).services} == {
        "keep-me",
        "registry-add",
        "portable-gpt-sovits-gpt-main",
    }


@pytest.mark.parametrize("package_id", ("Foo", "foo\u200b", "Ｆｏｏ"))
def test_portable_package_identity_requires_canonical_lowercase(package_id: str) -> None:
    with pytest.raises(ValidationError):
        PortableServiceLocator(component="gpt-sovits", package_id=package_id)


def test_store_fails_closed_on_duplicate_service_id(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    store = PortableServiceStore(controller)
    raw = [
        TTSServiceEndpoint(service_id="duplicate", base_url="http://127.0.0.1:9001").model_dump(mode="json"),
        TTSServiceEndpoint(service_id="duplicate", base_url="http://127.0.0.1:9002").model_dump(mode="json"),
    ]
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"schema_version": 1, "services": raw}), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate service_id"):
        store.load()
    with pytest.raises(ValueError, match="duplicate service_id"):
        ServiceRegistry.load(store.path)


def test_store_fails_closed_on_duplicate_portable_package_identity(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    first = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_dump(mode="json")
    second = {**first, "service_id": "alternate-service-id"}
    store = PortableServiceStore(controller)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"schema_version": 1, "services": [first, second]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate portable package identity"):
        store.load()
    with pytest.raises(ValueError, match="duplicate portable package identity"):
        ServiceRegistry.load(store.path)


def test_search_roots_and_total_candidate_enumeration_are_globally_bounded(tmp_path: Path, monkeypatch) -> None:
    controller = tmp_path / "controller" / "TTS More"
    controller.mkdir(parents=True)
    roots = [tmp_path / "roots" / f"root-{index:03d}" for index in range(100)]
    for root in roots:
        root.mkdir(parents=True)
    original_iterdir = Path.iterdir
    touched_roots: set[Path] = set()
    enumerated = 0

    def counted_iterdir(path: Path):
        nonlocal enumerated
        if path in roots:
            touched_roots.add(path)
            for index in range(256):
                enumerated += 1
                yield path / f"entry-{index:03d}"
            return
        yield from original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", counted_iterdir)
    locator = PortableServiceLocator(component="cosyvoice", package_id="cosy-main")

    assert resolve_locator(controller, locator, roots) is None
    assert len(touched_roots) <= 16
    assert enumerated <= 512


def test_settings_save_rejects_forged_portable_runtime_authority(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    endpoint = _endpoint(_locator("gpt-sovits", "gpt-main", package))
    record = ServiceSettingsRecord.model_validate(
        endpoint.model_copy(
            update={
                "managed": True,
                "repo_path": "C:/forged",
                "start_command": ["FORGED-COMMAND-MARKER.exe"],
                "start_cwd": "C:/forged",
            }
        ).model_dump(mode="python")
    )
    path = tmp_path / "data" / "local" / "services.json"

    with pytest.raises(ManagedPortableLocatorMutationError):
        save_service_settings(
            path,
            tmp_path / ".env.local",
            ServiceSettingsUpdate(services=[record]),
        )

    assert not path.exists()


def test_settings_api_forbids_creating_a_managed_portable_locator(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    raw = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_dump(mode="json")
    raw.update(
        managed=True,
        repo_path="C:/forged",
        start_command=["FORGED-COMMAND-MARKER.exe"],
        start_cwd="C:/forged",
    )
    app = create_app(data_root=tmp_path / "controller-data", env_path=tmp_path / ".env.local")

    response = TestClient(app).put("/api/settings/services", json={"services": [raw]})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    assert all(endpoint.portable_locator is None for endpoint in app.state.service_registry.services)


def test_open_source_configure_cannot_replace_a_managed_portable_locator_by_service_id(
    tmp_path: Path,
) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(
        tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main"
    )
    app = create_app(
        data_root=controller / "data",
        controller_root=controller,
        env_path=tmp_path / ".env.local",
    )
    client = TestClient(app)
    registered = client.post(
        "/api/portable-packages/register", json={"package_root": str(package)}
    )
    assert registered.status_code == 200, registered.text
    service_id = registered.json()["service"]["service_id"]

    configured = client.post(
        "/api/open-source-tts/configure",
        json={
            "provider_type": "gpt-sovits",
            "service_id": service_id,
            "display_name": "replacement generic endpoint",
            "source_profile": "local_endpoint",
            "base_url": "http://127.0.0.1:9872",
            "api_contract": "gradio-gpt-sovits-webui",
        },
    )

    assert configured.status_code == 409
    assert configured.json()["detail"] == {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    persisted = next(
        endpoint
        for endpoint in PortableServiceStore(controller).load()
        if endpoint.portable_locator is not None
    )
    assert persisted.portable_locator is not None
    assert persisted.portable_locator.package_id == "gpt-main"


def test_portable_store_save_returns_only_sanitized_merged_endpoints(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    forged = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_copy(
        update={
            "managed": True,
            "repo_path": "C:/forged",
            "start_command": ["FORGED-COMMAND-MARKER.exe"],
            "start_cwd": "C:/forged",
        }
    )

    [returned] = PortableServiceStore(controller).save([forged])

    assert returned.managed is False
    assert returned.repo_path is None
    assert returned.start_command == []
    assert returned.start_cwd is None


def test_missing_registry_load_merges_first_portable_writer_without_losing_defaults(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    path = controller / "data" / "local" / "services.json"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    stale = ServiceRegistry.load(path)
    default_ids = {item.service_id for item in stale.services}
    desired = [
        *stale.services,
        TTSServiceEndpoint(service_id="registry-add", base_url="http://127.0.0.1:9003"),
    ]
    go = tmp_path / "go"
    done = tmp_path / "done"
    backend_root = Path(__file__).resolve().parents[1]
    upsert_code = """
import sys, time
from pathlib import Path
from app.models import PortableServiceLocator, TTSServiceEndpoint
from app.portable_services import PortableServiceStore
controller, package, go, done = map(Path, sys.argv[1:5])
while not go.exists(): time.sleep(0.005)
locator = PortableServiceLocator(component='gpt-sovits', package_id='gpt-main', absolute_path_last_seen=str(package))
endpoint = TTSServiceEndpoint(service_id='portable-gpt-sovits-gpt-main', provider_type='gpt-sovits', base_url='http://127.0.0.1:9880', control_kind='portable-package', portable_locator=locator)
PortableServiceStore(controller).upsert(endpoint)
done.write_text('done')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", upsert_code, str(controller), str(package), str(go), str(done)],
        cwd=backend_root,
    )
    try:
        go.write_text("go")
        assert process.wait(timeout=30) == 0
        assert done.exists()
        stale.with_services(desired).save(path)
    finally:
        if process.poll() is None:
            process.kill()

    loaded = ServiceRegistry.load(path).services
    ids = [item.service_id for item in loaded]
    assert "portable-gpt-sovits-gpt-main" in ids
    assert "registry-add" in ids
    assert default_ids.issubset(ids)
    assert len(ids) == len(set(ids))


def test_post_merge_portable_identity_collision_fails_before_replace(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    seed = TTSServiceEndpoint(service_id="seed", base_url="http://127.0.0.1:9001")
    ServiceRegistry([seed]).save(controller / "data" / "local" / "services.json")
    registry = ServiceRegistry.load(controller / "data" / "local" / "services.json")
    store = PortableServiceStore(controller)
    store_baseline = store.load()
    first = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_copy(
        update={"service_id": "first-portable"}
    )
    second = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_copy(
        update={"service_id": "second-portable"}
    )
    PortableServiceStore(controller).replace_component(first, initial_services=registry.services)
    before = store.path.read_bytes()

    with pytest.raises(ValueError, match="duplicate portable package identity"):
        store.save([*store_baseline, second])

    assert store.path.read_bytes() == before
    assert len(ServiceRegistry.load(store.path).services) == 2


@pytest.mark.parametrize("alias", ("GPT-MAIN", "ｇｐｔ-main", "gpt-main\u200b"))
def test_noncanonical_portable_identity_from_stale_writer_fails_closed(
    tmp_path: Path, alias: str
) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    path = controller / "data" / "local" / "services.json"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    canonical = _endpoint(_locator("gpt-sovits", "gpt-main", package)).model_dump(mode="json")
    forged = {**canonical, "service_id": "alias-portable"}
    forged["portable_locator"] = {**forged["portable_locator"], "package_id": alias}
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "services": [forged]}), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises((ValueError, ValidationError)):
        ServiceRegistry([TTSServiceEndpoint.model_validate(canonical)]).save(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize("alias", ("GPT-MAIN", "ｇｐｔ-main", "gpt-main\u200b"))
def test_shared_identity_validator_rejects_case_and_unicode_package_aliases(
    tmp_path: Path, alias: str
) -> None:
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    canonical = _endpoint(_locator("gpt-sovits", "gpt-main", package))
    alias_locator = PortableServiceLocator.model_construct(
        component="gpt-sovits",
        package_id=alias,
        absolute_path_last_seen=str(package),
    )
    forged = canonical.model_copy(
        update={"service_id": "alias-portable", "portable_locator": alias_locator}
    )

    with pytest.raises(ValueError, match="portable package identity"):
        require_unique_service_identities([canonical, forged])


@pytest.mark.parametrize("component", ("GPT-SOVITS", "ｇｐｔ-sovits"))
def test_shared_identity_validator_requires_exact_lower_ascii_component(
    tmp_path: Path, component: str
) -> None:
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    endpoint = _endpoint(_locator("gpt-sovits", "gpt-main", package))
    forged_locator = PortableServiceLocator.model_construct(
        component=component,
        package_id="gpt-main",
        absolute_path_last_seen=str(package),
    )
    forged = endpoint.model_copy(update={"portable_locator": forged_locator})

    with pytest.raises(ValueError, match="portable package component"):
        require_unique_service_identities([forged])


@pytest.mark.parametrize("service_id", ("alpha\u200b", "alpha\ue000", "ａｌｐｈａ"))
def test_shared_identity_validator_rejects_noncanonical_or_category_c_service_id(
    service_id: str,
) -> None:
    endpoint = TTSServiceEndpoint(service_id=service_id, base_url="http://127.0.0.1:9001")

    with pytest.raises(ValueError, match="service_id"):
        require_unique_service_identities([endpoint])


def test_registry_writer_preflights_model_construct_before_atomic_replace(tmp_path: Path) -> None:
    path = tmp_path / "data" / "local" / "services.json"
    seed = TTSServiceEndpoint(service_id="seed", base_url="http://127.0.0.1:9001")
    ServiceRegistry([seed]).save(path)
    before = path.read_bytes()
    forged_locator = PortableServiceLocator.model_construct(
        component="GPT-SOVITS",
        package_id="gpt-main",
        absolute_path_last_seen=str(tmp_path / "GPT"),
    )
    forged = TTSServiceEndpoint(
        service_id="forged-portable",
        base_url="http://127.0.0.1:9880",
        control_kind="portable-package",
        portable_locator=PortableServiceLocator(
            component="gpt-sovits",
            package_id="gpt-main",
            absolute_path_last_seen=str(tmp_path / "GPT"),
        ),
    ).model_copy(update={"portable_locator": forged_locator})

    with pytest.raises((ValueError, ValidationError)):
        ServiceRegistry([forged]).save(path)

    assert path.read_bytes() == before
    assert [item.service_id for item in ServiceRegistry.load(path).services] == ["seed"]


@pytest.mark.parametrize(
    ("component", "service_id"),
    (("ｇｐｔ-sovits", "portable-one"), ("gpt-sovits", "alpha\u200b")),
)
def test_portable_store_writer_preflights_model_copy_before_atomic_replace(
    tmp_path: Path, component: str, service_id: str
) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    store = PortableServiceStore(controller)
    seed = TTSServiceEndpoint(service_id="seed", base_url="http://127.0.0.1:9001")
    store.save([seed])
    before = store.path.read_bytes()
    locator = PortableServiceLocator.model_construct(
        component=component,
        package_id="gpt-main",
        absolute_path_last_seen=str(tmp_path / "GPT"),
    )
    forged = TTSServiceEndpoint(
        service_id="portable-one",
        base_url="http://127.0.0.1:9880",
        control_kind="portable-package",
        portable_locator=PortableServiceLocator(
            component="gpt-sovits",
            package_id="gpt-main",
            absolute_path_last_seen=str(tmp_path / "GPT"),
        ),
    ).model_copy(update={"service_id": service_id, "portable_locator": locator})

    with pytest.raises((ValueError, ValidationError)):
        store.save([forged])

    assert store.path.read_bytes() == before
    assert [item.service_id for item in ServiceRegistry.load(store.path).services] == ["seed"]


def test_writer_preflight_accepts_legal_lowercase_portable_identity(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    endpoint = _endpoint(_locator("gpt-sovits", "gpt-main", package))

    saved = PortableServiceStore(controller).save([endpoint])

    assert [item.service_id for item in saved] == [endpoint.service_id]
    assert ServiceRegistry.load(controller / "data" / "local" / "services.json").services


@pytest.mark.parametrize("writer", ("registry", "portable-store"))
def test_writer_preflight_roundtrip_rejects_invalid_model_copy_before_replace(
    tmp_path: Path, writer: str
) -> None:
    controller = tmp_path / "TTS More"
    controller.mkdir()
    path = controller / "data" / "local" / "services.json"
    seed = TTSServiceEndpoint(service_id="seed", base_url="http://127.0.0.1:9001")
    ServiceRegistry([seed]).save(path)
    before = path.read_bytes()
    forged = TTSServiceEndpoint(
        service_id="forged",
        base_url="http://127.0.0.1:9002",
    ).model_copy(update={"mode": "forged-mode"})

    with pytest.raises((ValueError, ValidationError)):
        if writer == "registry":
            ServiceRegistry([forged]).save(path)
        else:
            PortableServiceStore(controller).save([forged])

    assert path.read_bytes() == before
    assert [item.service_id for item in ServiceRegistry.load(path).services] == ["seed"]
