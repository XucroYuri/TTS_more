from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import app.local_control as local_control
import app.main as main_module
from app.local_control import FolderSelectionError, select_portable_folder
from app.portable_imports import (
    PortableImportPlanError,
    PortableImportPlanStore,
    load_portable_importer,
    project_import_plan,
    project_import_report,
)
from app.portable_discovery import (
    PortablePackageRegisterRequest,
    endpoint_from_portable_package,
    inspect_locator_candidate,
)
from app.main import create_app
from app.portable_services import PortableServiceStore


OPERATION_ID = "11111111-1111-4111-8111-111111111111"
CONTROL_HEADER = "X-TTS-More-Control"


def _write_package(
    root: Path,
    *,
    component: str = "gpt-sovits",
    package_id: str = "gpt-main",
    controller_range: str = ">=0.2.0,<0.3.0",
    port: int = 9880,
) -> Path:
    root.mkdir(parents=True)
    for launcher in ("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "Build-Package.ps1"):
        (root / launcher).write_text("@echo off\n", encoding="utf-8")
    (root / "tts_more" / "locks").mkdir(parents=True)
    (root / "tts_more" / "locks" / "runtime.lock.json").write_text("{}", encoding="utf-8")
    (root / "tts_more" / "locks" / "models.lock.json").write_text("{}", encoding="utf-8")
    (root / "THIRD_PARTY_NOTICES.json").write_text("{}", encoding="utf-8")
    (root / "SHA256SUMS.txt").write_text("checksums\n", encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "component": component,
        "package_id": package_id,
        "release_version": "0.2.1",
        "version": "0.2.1",
        "build_id": f"{package_id}-build",
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
            "version": "1.0",
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
    manifest_path = root / "package" / "tts-more-package.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _client(
    controller_root: Path,
    *,
    client_host: str = "127.0.0.1",
    base_url: str = "http://127.0.0.1:8000",
) -> TestClient:
    controller_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        data_root=controller_root / "data",
        controller_root=controller_root,
    )
    return TestClient(app, base_url=base_url, client=(client_host, 51000))


def _token(client: TestClient, **headers: str) -> str:
    response = client.get("/api/local-control/token", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _control(token: str) -> dict[str, str]:
    return {CONTROL_HEADER: token}


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def _stored_plan(
    store: PortableImportPlanStore,
    root: Path,
    *,
    digest: str = "a" * 64,
    component: str = "gpt-sovits",
):
    plan = SimpleNamespace(
        new_root=root,
        plan_digest=digest,
        user_files=(
            SimpleNamespace(relative_path="data/user/project.json", size_bytes=9),
        ),
        reusable_assets=(
            SimpleNamespace(relative_path="models/base.safetensors", size_bytes=12),
        ),
        skipped_assets=("models/skipped.bin",),
        already_present=("data/user/existing.json",),
    )
    return store.create(
        plan,
        component=component,
        service_id=f"local-{component}",
        package_id=f"{component}-main",
        build_id="build-two",
    )


def test_portable_import_plan_store_uses_ttl_capacity_single_use_and_component_invalidation(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    store = PortableImportPlanStore(ttl_seconds=5, capacity=2, clock=clock)
    first = _stored_plan(store, tmp_path / "first")
    second = _stored_plan(store, tmp_path / "second", digest="b" * 64)
    third = _stored_plan(store, tmp_path / "third", digest="c" * 64)

    with pytest.raises(PortableImportPlanError) as evicted:
        store.consume(first.plan_id, "a" * 64)
    assert evicted.value.code == "PORTABLE_IMPORT_PLAN_UNAVAILABLE"

    consumed = store.consume(second.plan_id, "b" * 64)
    assert consumed.plan is second.plan
    with pytest.raises(PortableImportPlanError) as replayed:
        store.consume(second.plan_id, "b" * 64)
    assert replayed.value.code == "PORTABLE_IMPORT_PLAN_UNAVAILABLE"

    cosy = _stored_plan(
        store,
        tmp_path / "cosy",
        digest="d" * 64,
        component="cosyvoice",
    )
    store.invalidate_component("gpt-sovits")
    with pytest.raises(PortableImportPlanError):
        store.consume(third.plan_id, "c" * 64)
    assert store.consume(cosy.plan_id, "d" * 64).component == "cosyvoice"

    expired = _stored_plan(store, tmp_path / "expired", digest="e" * 64)
    clock.value += 5.01
    with pytest.raises(PortableImportPlanError) as stale:
        store.consume(expired.plan_id, "e" * 64)
    assert stale.value.code == "PORTABLE_IMPORT_PLAN_UNAVAILABLE"


def test_portable_importer_loads_only_the_fixed_controller_script(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    importer = load_portable_importer(repository_root)

    assert Path(importer.__file__) == repository_root / "scripts" / "import_portable_data.py"
    assert callable(importer.plan_import)
    assert callable(importer.apply_import)

    with pytest.raises(PortableImportPlanError) as unavailable:
        load_portable_importer(tmp_path / "not-a-controller")
    assert unavailable.value.code == "PORTABLE_IMPORT_CORE_UNAVAILABLE"


def test_portable_import_plan_store_compares_digest_in_constant_time_and_keeps_mismatch_unconsumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PortableImportPlanStore()
    stored = _stored_plan(store, tmp_path / "new")
    compared: list[tuple[str, str]] = []

    def compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return left == right

    monkeypatch.setattr("app.portable_imports.hmac.compare_digest", compare)
    with pytest.raises(PortableImportPlanError) as mismatch:
        store.consume(stored.plan_id, "f" * 64)
    assert mismatch.value.code == "PORTABLE_IMPORT_PLAN_UNAVAILABLE"
    assert compared == [("a" * 64, "f" * 64)]
    assert store.consume(stored.plan_id, "a" * 64).plan is stored.plan


def test_portable_import_safe_projections_contain_only_counts_bytes_and_relative_paths(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    store = PortableImportPlanStore(ttl_seconds=300, clock=clock)
    stored = _stored_plan(store, tmp_path / "machine" / "new package")

    planned = project_import_plan(stored, now=clock())
    report = project_import_report(
        SimpleNamespace(
            copied_user_files=1,
            reused_assets=["models/base.safetensors"],
            skipped_assets=["models/skipped.bin"],
            already_present=["data/user/existing.json"],
        )
    )

    assert planned == {
        "plan_id": stored.plan_id,
        "plan_digest": "a" * 64,
        "expires_in_seconds": 300,
        "user_file_count": 1,
        "user_bytes": 9,
        "reusable_assets": ["models/base.safetensors"],
        "reusable_asset_bytes": 12,
        "skipped_assets": ["models/skipped.bin"],
        "already_present": ["data/user/existing.json"],
        "old_package_preserved": True,
    }
    assert report == {
        "copied_user_files": 1,
        "reused_assets": ["models/base.safetensors"],
        "skipped_assets": ["models/skipped.bin"],
        "already_present": ["data/user/existing.json"],
    }
    serialized = json.dumps({"plan": planned, "report": report})
    assert str(tmp_path) not in serialized
    assert not {"path", "old_root", "new_root", "root", "command", "cwd", "env"}.intersection(
        planned
    )


def test_portable_import_projection_clamps_expires_seconds_to_store_ttl(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    store = PortableImportPlanStore(ttl_seconds=300, clock=clock)
    stored = _stored_plan(store, tmp_path / "new package")

    projected = project_import_plan(stored, now=clock() - 0.001)

    assert projected["expires_in_seconds"] == 300


@pytest.mark.parametrize("unsafe", ["C:/secret/model.bin", "../escape", "models\\secret.bin"])
def test_portable_import_safe_projection_rejects_non_relative_or_noncanonical_paths(
    tmp_path: Path, unsafe: str
) -> None:
    store = PortableImportPlanStore()
    stored = _stored_plan(store, tmp_path / "new")
    stored.plan.reusable_assets = (SimpleNamespace(relative_path=unsafe, size_bytes=1),)

    with pytest.raises(PortableImportPlanError) as error:
        project_import_plan(stored)
    assert error.value.code == "PORTABLE_IMPORT_PROJECTION_INVALID"


def _write_suite(root: Path) -> Path:
    """Create four sibling, complete schema-v2 packages without machine dependencies."""

    _write_package(root / "TTS More", component="tts-more", package_id="tts-more", port=8000)
    _write_package(
        root / "GPT-SoVITS",
        component="gpt-sovits",
        package_id="gpt-main",
        port=9880,
    )
    _write_package(
        root / "IndexTTS",
        component="indextts",
        package_id="indextts-main",
        port=9881,
    )
    _write_package(
        root / "CosyVoice",
        component="cosyvoice",
        package_id="cosyvoice-main",
        port=9882,
    )
    return root


def _local_client(tts_more_root: Path) -> tuple[TestClient, str]:
    """Install real routes/token with a deterministic process-free supervisor boundary."""

    client = _client(tts_more_root)
    client.app.state.supervisor = _FakeSupervisor()
    return client, _token(client)


def _raw_asgi_request(
    app,
    *,
    path: str,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    chunks: list[bytes] | None = None,
    client_host: str = "127.0.0.1",
) -> tuple[int, list[tuple[bytes, bytes]], bytes, int]:
    sent: list[dict[str, object]] = []
    bodies = list(chunks or [b""])
    receive_count = 0

    async def receive() -> dict[str, object]:
        nonlocal receive_count
        receive_count += 1
        body = bodies.pop(0) if bodies else b""
        return {"type": "http.request", "body": body, "more_body": bool(bodies)}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [(b"host", b"127.0.0.1:8000")],
        "client": (client_host, 51000),
        "server": ("127.0.0.1", 8000),
    }
    asyncio.run(app(scope, receive, send))
    start = next(item for item in sent if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return int(start["status"]), list(start.get("headers", [])), body, receive_count


@pytest.mark.parametrize(
    ("client_host", "base_url"),
    (
        ("127.0.0.1", "http://127.0.0.1:8000"),
        ("127.9.8.7", "http://127.9.8.7:8000"),
        ("::1", "http://localhost:8000"),
        ("127.0.0.1", "http://localhost:8000"),
    ),
)
def test_token_allows_real_loopback_client_and_loopback_host(
    tmp_path: Path, client_host: str, base_url: str
) -> None:
    response = _client(tmp_path / "TTS More", client_host=client_host, base_url=base_url).get(
        "/api/local-control/token"
    )

    assert response.status_code == 200
    assert len(response.json()["token"]) >= 40
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize(
    "base_url",
    (
        "http://example.test:8000",
        "http://localhost.example:8000",
        "http://127.0.0.1.example:8000",
    ),
)
def test_token_rejects_non_loopback_and_dns_rebinding_hosts(tmp_path: Path, base_url: str) -> None:
    client = _client(tmp_path / "TTS More", base_url=base_url)
    response = client.get("/api/local-control/token", headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "LOCAL_CONTROL_FORBIDDEN", "message": "local control is unavailable"}}


def test_token_ignores_forwarded_loopback_for_lan_client(tmp_path: Path) -> None:
    response = _client(tmp_path / "TTS More", client_host="192.168.2.20").get(
        "/api/local-control/token",
        headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "raw_headers",
    (
        [(b"host", b"127.0.0.1:8000"), (b"host", b"localhost:8000")],
        [(b"host", b"127.0.0.1:8000"), (b"origin", b"http://localhost"), (b"origin", b"http://127.0.0.1")],
        [(b"host", b"127.0.0.1:8000"), (b"host", b"127.0.0.1:8000"), (b"origin", b"http://localhost")],
    ),
)
def test_token_rejects_duplicate_security_headers(
    tmp_path: Path, raw_headers: list[tuple[bytes, bytes]]
) -> None:
    client = _client(tmp_path / "TTS More")
    status, _headers, body, _reads = _raw_asgi_request(
        client.app,
        path="/api/local-control/token",
        headers=raw_headers,
    )
    assert status == 403
    assert json.loads(body)["detail"]["code"] == "LOCAL_CONTROL_FORBIDDEN"


def test_control_route_rejects_duplicate_control_token_header(tmp_path: Path) -> None:
    client = _client(tmp_path / "TTS More")
    token = _token(client).encode("ascii")
    status, _headers, _body, reads = _raw_asgi_request(
        client.app,
        path="/api/local-portable-services",
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"x-tts-more-control", token),
            (b"x-tts-more-control", token),
        ],
    )
    assert status == 403
    assert reads == 0


@pytest.mark.parametrize(
    "header",
    (
        (b"host", b"localhost:0"),
        (b"host", b"localhost:65536"),
        (b"host", b"localhost:%38%30"),
        (b"host", b"localh\xffst:8000"),
        (b"origin", b"http://localhost:0"),
        (b"origin", b"http://localhost:65536"),
        (b"origin", b"http://localh\xffst:5173"),
    ),
)
def test_security_headers_reject_malformed_ports_percent_and_non_ascii(
    tmp_path: Path, header: tuple[bytes, bytes]
) -> None:
    raw = [(b"host", b"127.0.0.1:8000")]
    if header[0] == b"host":
        raw = [header]
    else:
        raw.append(header)
    status, _headers, body, _reads = _raw_asgi_request(
        _client(tmp_path / "TTS More").app,
        path="/api/local-control/token",
        headers=raw,
    )
    assert status == 403
    assert b"Traceback" not in body


@pytest.mark.parametrize(
    "origin",
    (
        "http://127.0.0.1:5173",
        "https://localhost:5173",
        "http://[::1]:5173",
    ),
)
def test_token_allows_http_loopback_origins(tmp_path: Path, origin: str) -> None:
    response = _client(tmp_path / "TTS More").get(
        "/api/local-control/token", headers={"Origin": origin}
    )
    assert response.status_code == 200


def test_token_allows_ipv6_loopback_host_authority(tmp_path: Path) -> None:
    response = _client(tmp_path / "TTS More").get(
        "/api/local-control/token", headers={"Host": "[::1]:8000"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    (
        "null",
        "file://localhost/file",
        "ws://localhost:5173",
        "http://localhost.example:5173",
        "http://127.0.0.1@evil.test:5173",
        "http://user@localhost:5173",
        "http://localhost:5173/not-an-origin",
        "http://localhost:5173?",
        "http://localhost:5173#",
        "http://localhost:5173/",
        "http://[::1",
    ),
)
def test_token_rejects_malformed_non_http_and_non_loopback_origins(
    tmp_path: Path, origin: str
) -> None:
    response = _client(tmp_path / "TTS More").get(
        "/api/local-control/token", headers={"Origin": origin}
    )
    assert response.status_code == 403


def test_token_is_one_process_secret_not_persisted_or_reused(tmp_path: Path, caplog) -> None:
    root = tmp_path / "TTS More"
    first = _client(root)
    first_token = _token(first)
    assert _token(first) == first_token
    second_token = _token(_client(root))

    assert second_token != first_token
    services_file = root / "data" / "local" / "services.json"
    assert not services_file.exists() or first_token not in services_file.read_text(encoding="utf-8")
    assert first_token not in caplog.text
    assert second_token not in caplog.text


def test_control_routes_require_loopback_local_authority_origin_and_constant_time_token(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path / "TTS More")
    token = _token(client)

    assert client.post("/api/local-portable-services/discover", json={}).status_code == 403
    assert client.post(
        "/api/local-portable-services/discover",
        headers=_control("x" * len(token)),
        json={},
    ).status_code == 403
    assert client.post(
        "/api/local-portable-services/discover",
        headers={**_control(token), "Origin": "https://evil.test"},
        json={},
    ).status_code == 403
    assert client.post(
        "/api/local-portable-services/discover", headers=_control(token), json={}
    ).status_code == 200


def test_existing_bearer_auth_cannot_replace_local_control_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "global-bearer")
    client = _client(tmp_path / "TTS More")
    token = _token(client)
    bearer = {"Authorization": "Bearer global-bearer"}

    assert client.post(
        "/api/local-portable-services/discover", headers=bearer, json={}
    ).status_code == 403
    assert client.post(
        "/api/local-portable-services/discover",
        headers={**bearer, **_control(token)},
        json={},
    ).status_code == 200


def test_cors_preflight_never_executes_action_and_needs_no_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "global-bearer")
    client = _client(tmp_path / "TTS More")
    fake = _FakeSupervisor()
    client.app.state.supervisor = fake

    response = client.options(
        "/api/local-portable-services/gpt-sovits/start",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-tts-more-control,content-type",
        },
    )

    assert response.status_code == 200
    assert fake.calls == []


def test_streaming_body_guard_rejects_before_receiving_any_body(tmp_path: Path) -> None:
    client = _client(tmp_path / "TTS More")
    status, _headers, _body, reads = _raw_asgi_request(
        client.app,
        path="/api/local-portable-services/discover",
        method="POST",
        headers=[(b"host", b"127.0.0.1:8000")],
        chunks=[b'{"roots":[]}'],
    )
    assert status == 403
    assert reads == 0


def test_streaming_body_guard_stops_at_first_chunk_over_limit(tmp_path: Path) -> None:
    client = _client(tmp_path / "TTS More")
    token = client.app.state.local_control_token.encode("ascii")
    status, _headers, body, reads = _raw_asgi_request(
        client.app,
        path="/api/local-portable-services/discover",
        method="POST",
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"x-tts-more-control", token),
        ],
        chunks=[b"x" * 40_000, b"y" * 30_000, b"must-not-be-read"],
    )
    assert status == 413
    assert json.loads(body)["detail"]["code"] == "LOCAL_CONTROL_REQUEST_TOO_LARGE"
    assert reads == 2


def test_streaming_body_guard_replays_bounded_multichunk_json(tmp_path: Path) -> None:
    client = _client(tmp_path / "TTS More")
    token = client.app.state.local_control_token.encode("ascii")
    status, _headers, body, reads = _raw_asgi_request(
        client.app,
        path="/api/local-portable-services/discover",
        method="POST",
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"x-tts-more-control", token),
        ],
        chunks=[b'{"roo', b'ts":[]}'],
    )
    assert status == 200
    assert json.loads(body) == {"packages": []}
    assert reads == 2


def test_near_prefix_paths_are_not_treated_as_local_control_routes(tmp_path: Path) -> None:
    client = _client(tmp_path / "TTS More")
    status, _headers, _body, reads = _raw_asgi_request(
        client.app,
        path="/api/local-portable-services-evil",
        method="POST",
        headers=[(b"host", b"evil.test")],
        chunks=[b"not consumed by local control middleware"],
    )
    assert status in {404, 405}
    assert reads == 0


@pytest.mark.parametrize(
    "payload",
    (
        {"roots": "C:/"},
        {"roots": [1]},
        {"include_siblings": True},
        {"command": "calc.exe"},
        {"roots": ["x"] * 17},
        {"roots": ["relative/search-root"]},
    ),
)
def test_discover_payload_is_strict_and_bounded(tmp_path: Path, payload: dict[str, object]) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.post(
        "/api/local-portable-services/discover", headers=_control(_token(client)), json=payload
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {"code": "LOCAL_CONTROL_INVALID_REQUEST", "message": "request validation failed"}
    }


def test_local_control_rejects_oversized_body_before_validation(tmp_path: Path) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.post(
        "/api/local-portable-services/discover",
        headers={**_control(_token(client)), "Content-Type": "application/json"},
        content=json.dumps({"roots": ["x" * 70_000]}),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "LOCAL_CONTROL_REQUEST_TOO_LARGE"


def test_corrupt_local_service_store_returns_stable_non_leaking_error(tmp_path: Path) -> None:
    root = tmp_path / "TTS More"
    client = _client(root)
    token = _token(client)
    store = root / "data/local/services.json"
    store.parent.mkdir(parents=True)
    store.write_text("{broken machine path C:/private/user", encoding="utf-8")
    safe_client = TestClient(
        client.app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 51000),
        raise_server_exceptions=False,
    )

    response = safe_client.get("/api/local-portable-services", headers=_control(token))

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "LOCAL_CONTROL_STORE_INVALID",
            "message": "local portable service settings are unavailable",
        }
    }
    assert "private" not in response.text


def test_discover_uses_bounded_roots_and_keeps_incompatible_package_visible(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "suite"
    controller = suite / "TTS More"
    _write_package(suite / "GPT 包", component="gpt-sovits", package_id="gpt-main")
    _write_package(
        suite / "Index 包",
        component="indextts",
        package_id="index-main",
        controller_range=">=9.0.0,<10.0.0",
    )
    client = _client(controller)
    response = client.post(
        "/api/local-portable-services/discover", headers=_control(_token(client)), json={}
    )

    assert response.status_code == 200
    packages = {item["component"]: item for item in response.json()["packages"]}
    assert set(packages) == {"gpt-sovits", "indextts"}
    assert packages["gpt-sovits"]["manageable"] is True
    assert packages["indextts"]["manageable"] is False
    assert packages["indextts"]["complete_v2"] is True


@pytest.mark.parametrize(
    "payload",
    (
        {"component": "gpt-sovits", "package_id": "gpt-main", "path": 123},
        {"component": "gpt-sovits", "package_id": "GPT MAIN", "path": "C:/GPT"},
        {"component": "gpt-sovits", "package_id": "gpt-main", "path": "C:/GPT", "command": "cmd"},
        {"component": "gpt-sovits", "package_id": "gpt-main", "path": "C:/GPT", "port_override": True},
        {"component": "gpt-sovits", "package_id": "gpt-main", "path": "C:/GPT", "port_override": "9980"},
        {"component": "gpt-sovits", "package_id": "gpt-main", "path": "relative/GPT"},
    ),
)
def test_register_payload_rejects_wrong_types_and_command_fields(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.post(
        "/api/local-portable-services/register", headers=_control(_token(client)), json=payload
    )
    assert response.status_code == 422


def test_register_freshly_validates_identity_persists_locator_and_lists_service(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "suite"
    controller = suite / "TTS More"
    package = _write_package(suite / "GPT 移动包", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    headers = _control(_token(client))
    existing_ids = {
        item["service_id"]
        for item in client.get("/api/local-portable-services", headers=headers).json()["services"]
    }

    registered = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": "gpt-main",
            "path": str(package),
            "port_override": 9980,
        },
    )

    assert registered.status_code == 200, registered.text
    service = registered.json()["service"]
    assert service["component"] == "gpt-sovits"
    assert service["managed"] is True
    assert service["port_override"] == 9980
    listed = client.get("/api/local-portable-services", headers=headers)
    assert listed.status_code == 200
    assert any(item["package_id"] == "gpt-main" for item in listed.json()["services"])
    assert existing_ids.issubset({item["service_id"] for item in listed.json()["services"]})
    persisted = json.loads((controller / "data/local/services.json").read_text(encoding="utf-8"))
    portable = next(item for item in persisted["services"] if item.get("portable_locator"))
    assert portable["portable_locator"]["absolute_path_last_seen"] == str(package.resolve())


def test_register_rejects_identity_mismatch_and_incomplete_package(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="actual")
    client = _client(controller)
    response = client.post(
        "/api/local-portable-services/register",
        headers=_control(_token(client)),
        json={"component": "gpt-sovits", "package_id": "expected", "path": str(package)},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LOCAL_CONTROL_IDENTITY_MISMATCH"


def test_register_replaces_the_previous_package_for_the_same_component(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    first = _write_package(tmp_path / "GPT old", component="gpt-sovits", package_id="gpt-old")
    second = _write_package(tmp_path / "GPT new", component="gpt-sovits", package_id="gpt-new")
    client = _client(controller)
    headers = _control(_token(client))
    for package, package_id in ((first, "gpt-old"), (second, "gpt-new")):
        response = client.post(
            "/api/local-portable-services/register",
            headers=headers,
            json={"component": "gpt-sovits", "package_id": package_id, "path": str(package)},
        )
        assert response.status_code == 200, response.text

    services = client.get("/api/local-portable-services", headers=headers).json()["services"]
    registered = [item for item in services if item["component"] == "gpt-sovits" and item["package_id"]]

    assert [(item["package_id"], item["package_root"]) for item in registered] == [
        ("gpt-new", str(second.resolve()))
    ]


def test_racing_register_publication_cannot_overwrite_newer_persisted_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = tmp_path / "TTS More"
    first = _write_package(tmp_path / "GPT one", component="gpt-sovits", package_id="gpt-one")
    second = _write_package(tmp_path / "GPT two", component="gpt-sovits", package_id="gpt-two")
    first_publish_entered = threading.Event()
    release_first_publish = threading.Event()
    original_apply = main_module._apply_registry

    def ordered_apply(app, registry, store) -> None:
        package_ids = {
            endpoint.portable_locator.package_id
            for endpoint in registry.services
            if endpoint.portable_locator is not None
            and endpoint.portable_locator.component == "gpt-sovits"
        }
        if package_ids == {"gpt-one"}:
            first_publish_entered.set()
            assert release_first_publish.wait(5)
        original_apply(app, registry, store)

    monkeypatch.setattr(main_module, "_apply_registry", ordered_apply)
    client = _client(controller)
    headers = _control(_token(client))

    def register(package: Path, package_id: str):
        return client.post(
            "/api/local-portable-services/register",
            headers=headers,
            json={"component": "gpt-sovits", "package_id": package_id, "path": str(package)},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_request = pool.submit(register, first, "gpt-one")
        assert first_publish_entered.wait(5)
        second_request = pool.submit(register, second, "gpt-two")
        try:
            time.sleep(0.1)
            assert not second_request.done(), "newer registration crossed an older in-flight publication"
        finally:
            release_first_publish.set()
        first_response = first_request.result(timeout=10)
        second_response = second_request.result(timeout=10)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    persisted = PortableServiceStore(controller).load()
    memory = client.app.state.service_registry.services

    def gpt_ids(services) -> list[str]:
        return [
            endpoint.portable_locator.package_id
            for endpoint in services
            if endpoint.portable_locator is not None
            and endpoint.portable_locator.component == "gpt-sovits"
        ]

    assert gpt_ids(persisted) == ["gpt-two"]
    assert gpt_ids(memory) == ["gpt-two"]


def test_register_publication_failure_reports_that_the_service_was_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    headers = _control(_token(client))

    def fail_publication(*_args, **_kwargs) -> None:
        raise ValueError("simulated runtime publication failure")

    monkeypatch.setattr(main_module, "_apply_registry", fail_publication)
    response = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={"component": "gpt-sovits", "package_id": "gpt-main", "path": str(package)},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "LOCAL_CONTROL_PUBLICATION_FAILED",
        "message": "portable service registration was persisted but runtime refresh failed",
    }
    persisted = PortableServiceStore(controller).load()
    assert [
        endpoint.portable_locator.package_id
        for endpoint in persisted
        if endpoint.portable_locator is not None
        and endpoint.portable_locator.component == "gpt-sovits"
    ] == ["gpt-main"]


def test_select_folder_validates_selected_package_and_never_accepts_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    monkeypatch.setattr("app.local_control.select_portable_folder", lambda _root: package)
    response = client.post(
        "/api/local-portable-services/select-folder",
        headers=_control(_token(client)),
        json={"component": "gpt-sovits", "package_id": "gpt-main"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "selected"
    assert response.json()["package"]["package_id"] == "gpt-main"
    rejected = client.post(
        "/api/local-portable-services/select-folder",
        headers=_control(_token(client)),
        json={"component": "gpt-sovits", "command": "powershell evil.ps1"},
    )
    assert rejected.status_code == 422


def test_select_folder_returns_stable_cancelled_and_unsupported_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path / "TTS More")
    headers = _control(_token(client))
    monkeypatch.setattr("app.local_control.select_portable_folder", lambda _root: None)
    cancelled = client.post(
        "/api/local-portable-services/select-folder",
        headers=headers,
        json={"component": "cosyvoice"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {"status": "cancelled"}

    def unsupported(_root: Path) -> None:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_UNSUPPORTED", "folder selection is unavailable")

    monkeypatch.setattr("app.local_control.select_portable_folder", unsupported)
    response = client.post(
        "/api/local-portable-services/select-folder",
        headers=headers,
        json={"component": "cosyvoice"},
    )
    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "LOCAL_CONTROL_FOLDER_UNSUPPORTED"


class _FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def portable_lifecycle_guard(self, _component):
        return nullcontext()

    def start(self, endpoint, *, operation_id=None):
        self.calls.append(("start", endpoint, operation_id))
        return {"status": "starting", "operation_id": operation_id}

    def stop(self, endpoint, *, action_id=None):
        self.calls.append(("stop", endpoint, action_id))
        return {"status": "stopping", "action_id": action_id}

    def repair(self, endpoint, *, proxy_url=None, action_id=None):
        self.calls.append(("repair", endpoint, {"proxy_url": proxy_url, "action_id": action_id}))
        return {"status": "repairing", "action_id": action_id}

    def open_folder(self, endpoint):
        self.calls.append(("open-folder", endpoint, None))
        return {"status": "opened"}

    def status(self, endpoint, *, operation_id=None):
        self.calls.append(("status", endpoint, operation_id))
        return {"status": "ready", "operation": {"operation_id": operation_id}}

    def logs(self, endpoint, *, operation_id=None, after_seq=0, lines=200):
        self.calls.append(("logs", endpoint, operation_id))
        return {"status": "ready", "events": [], "next_seq": after_seq}

    def action_status(self, endpoint, *, action_id=None):
        self.calls.append(("action-status", endpoint, action_id))
        return {"status": "stopped", "action_id": action_id, "action": "stop"}


def _register(client: TestClient, package: Path) -> tuple[str, dict[str, str]]:
    token = _token(client)
    headers = _control(token)
    response = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={"component": "gpt-sovits", "package_id": "gpt-main", "path": str(package)},
    )
    assert response.status_code == 200, response.text
    return token, headers


def _install_portable_import_core(controller: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    scripts = controller / "scripts"
    schema_root = controller / "packaging" / "portable"
    scripts.mkdir(parents=True, exist_ok=True)
    schema_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repository_root / "scripts" / "import_portable_data.py", scripts)
    shutil.copy2(
        repository_root / "packaging" / "portable" / "tts-more-package.schema.json",
        schema_root,
    )


def _portable_import_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, dict[str, str], Path, Path]:
    if os.name != "nt":
        pytest.skip("portable import apply filesystem identity checks target Windows package moves")
    controller = tmp_path / "TTS More"
    _install_portable_import_core(controller)
    old = _write_package(tmp_path / "old worker", component="gpt-sovits", package_id="gpt-main")
    new = _write_package(tmp_path / "new worker", component="gpt-sovits", package_id="gpt-main")
    user_file = old / "data" / "user" / "project.json"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("user-data", encoding="utf-8")
    client = _client(controller)
    _token_value, headers = _register(client, new)
    monkeypatch.setattr("app.local_control.select_portable_folder", lambda _root: old)
    return client, headers, old, new


def test_portable_import_routes_require_existing_local_control_header_and_managed_loopback_service(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path / "TTS More")
    token = _token(client)
    path = "/api/local-portable-services/gpt-sovits/imports/plan"

    assert client.post(path, json={}).status_code == 403
    assert client.post(
        path,
        headers={"X-TTS-More-Local-Control": token},
        json={},
    ).status_code == 403
    assert client.post(
        path,
        headers={"Authorization": "Bearer global-token"},
        json={},
    ).status_code == 403
    unmanaged = client.post(path, headers=_control(token), json={})
    assert unmanaged.status_code == 409
    assert unmanaged.json()["detail"]["code"] == "LOCAL_CONTROL_NOT_MANAGEABLE"

    external_root = tmp_path / "external" / "TTS More"
    external_root.mkdir(parents=True)
    PortableServiceStore(external_root).save(
        [
            {
                "service_id": "lan-gpt",
                "display_name": "LAN GPT-SoVITS",
                "engine": "gpt-sovits",
                "provider_type": "gpt-sovits",
                "catalog_provider": "gpt-sovits",
                "api_contract": "tts-more-v1",
                "base_url": "http://192.168.2.20:9880",
                "mode": "external",
                "network_scope": "lan",
                "managed": False,
                "enabled": True,
                "capabilities": ["tts", "artifact-transfer"],
            }
        ]
    )
    external = _client(external_root)
    external_response = external.post(
        path,
        headers=_control(_token(external)),
        json={},
    )
    assert external_response.status_code == 409
    assert external_response.json()["detail"]["code"] == "LOCAL_CONTROL_NOT_MANAGEABLE"

    lan = TestClient(
        client.app,
        base_url="http://192.168.2.10:8000",
        client=("192.168.2.20", 51000),
    )
    denied = lan.post(path, headers=_control(token), json={})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "LOCAL_CONTROL_FORBIDDEN"


@pytest.mark.parametrize(
    "payload",
    [
        {"old_root": "C:/old"},
        {"new_root": "C:/new"},
        {"path": "C:/worker"},
        {"roots": ["C:/worker"]},
        {"command": ["powershell", "evil.ps1"]},
        {"cwd": "C:/"},
        {"env": {"PATH": "C:/evil"}},
    ],
)
def test_portable_import_plan_request_is_strictly_empty(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan",
        headers=_control(_token(client)),
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "LOCAL_CONTROL_INVALID_REQUEST",
        "message": "request validation failed",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"confirmed": False, "plan_digest": "a" * 64},
        {"confirmed": 1, "plan_digest": "a" * 64},
        {"confirmed": True, "plan_digest": "a" * 63},
        {"confirmed": True, "plan_digest": "g" * 64},
        {"confirmed": True, "plan_digest": "a" * 64, "old_root": "C:/old"},
    ],
)
def test_portable_import_apply_requires_only_literal_confirmation_and_exact_digest(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.post(
        "/api/local-portable-services/gpt-sovits/imports/11111111-1111-4111-8111-111111111111/apply",
        headers=_control(_token(client)),
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "LOCAL_CONTROL_INVALID_REQUEST"


def test_portable_import_openapi_contract_has_no_browser_path_or_process_fields(
    tmp_path: Path,
) -> None:
    schema = _client(tmp_path / "TTS More").get("/openapi.json").json()
    plan_path = schema["paths"]["/api/local-portable-services/{component}/imports/plan"]["post"]
    apply_path = schema["paths"][
        "/api/local-portable-services/{component}/imports/{plan_id}/apply"
    ]["post"]
    contracts = {
        name: schema["components"]["schemas"][name]
        for name in (
            "LocalPortableImportPlanRequest",
            "LocalPortableImportApplyRequest",
            "LocalPortableImportPlanResponse",
            "LocalPortableImportApplyResponse",
            "LocalPortableImportCancelledResponse",
        )
    }
    serialized = json.dumps(contracts)

    for forbidden in ("old_root", "new_root", '"path"', '"roots"', '"command"', '"cwd"', '"env"'):
        assert forbidden not in serialized
    assert '"confirmed"' in serialized
    assert '"plan_digest"' in serialized
    assert plan_path["requestBody"]["required"] is True
    assert apply_path["requestBody"]["required"] is True


def test_portable_import_plan_is_read_only_uses_picker_and_fresh_locator_then_applies_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, old, new = _portable_import_client(tmp_path, monkeypatch)
    destination = new / "data" / "user" / "project.json"
    assert not destination.exists()

    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan",
        headers=headers,
        json={},
    )

    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert set(plan) == {
        "plan_id",
        "plan_digest",
        "expires_in_seconds",
        "user_file_count",
        "user_bytes",
        "reusable_assets",
        "reusable_asset_bytes",
        "skipped_assets",
        "already_present",
        "old_package_preserved",
    }
    assert plan["user_file_count"] == 1
    assert plan["user_bytes"] == len(b"user-data")
    assert plan["old_package_preserved"] is True
    assert not destination.exists()
    assert str(old) not in planned.text
    assert str(new) not in planned.text

    applied = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{plan['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": plan["plan_digest"]},
    )

    assert applied.status_code == 200, applied.text
    assert applied.json() == {
        "copied_user_files": 1,
        "reused_assets": [],
        "skipped_assets": [],
        "already_present": [],
    }
    assert destination.read_text(encoding="utf-8") == "user-data"
    assert (old / "data" / "user" / "project.json").read_text(encoding="utf-8") == "user-data"
    assert str(old) not in applied.text
    assert str(new) not in applied.text


def test_portable_import_digest_mismatch_does_not_consume_but_apply_and_failure_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, new = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    path = f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply"

    mismatch = client.post(
        path,
        headers=headers,
        json={"confirmed": True, "plan_digest": "f" * 64},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"

    pid_record = new / "data" / "local" / "run" / "worker.pid.json"
    pid_record.parent.mkdir(parents=True)
    pid_record.write_text("malformed-but-present", encoding="utf-8")
    blocked = client.post(
        path,
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "LOCAL_CONTROL_IMPORT_BLOCKED",
        "message": "portable import is unavailable",
    }
    assert str(new) not in blocked.text

    pid_record.unlink()
    replayed = client.post(
        path,
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"


def test_portable_import_picker_cancellation_returns_existing_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = tmp_path / "TTS More"
    _install_portable_import_core(controller)
    package = _write_package(tmp_path / "new worker")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    monkeypatch.setattr("app.local_control.select_portable_folder", lambda _root: None)

    response = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}


@pytest.mark.parametrize("drift", ["manifest", "lock", "source", "target"])
def test_portable_import_rejects_manifest_lock_source_and_target_drift_with_redacted_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    client, headers, old, new = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    if drift == "manifest":
        manifest = new / "package" / "tts-more-package.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    elif drift == "lock":
        (new / "tts_more" / "locks" / "models.lock.json").write_text(
            '{"assets": []}\n', encoding="utf-8"
        )
    elif drift == "source":
        (old / "data" / "user" / "project.json").write_text("changed", encoding="utf-8")
    else:
        destination = new / "data" / "user" / "project.json"
        destination.parent.mkdir(parents=True)
        destination.write_text("target appeared", encoding="utf-8")
    path = f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply"

    response = client.post(
        path,
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "LOCAL_CONTROL_IMPORT_FAILED",
        "message": "portable import failed",
    }
    assert str(old) not in response.text
    assert str(new) not in response.text
    replay = client.post(
        path,
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"


@pytest.mark.parametrize("drift", ["component", "package", "build"])
def test_portable_import_rejects_registered_component_package_and_build_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    client, headers, _old, new = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    manifest = new / "package" / "tts-more-package.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if drift == "component":
        payload["component"] = "cosyvoice"
    elif drift == "package":
        payload["package_id"] = "gpt-other"
    else:
        payload["build_id"] = "different-build"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    response = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["message"] in {
        "portable service is not locally manageable",
        "portable import is unavailable",
    }
    assert str(new) not in response.text


def test_portable_import_plan_is_invalidated_when_registered_target_root_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "replacement worker", component="gpt-sovits", package_id="gpt-main"
    )
    registered = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": "gpt-main",
            "path": str(replacement),
        },
    )
    assert registered.status_code == 200

    response = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE",
        "message": "portable import plan is unavailable",
    }
    assert not (original / "data" / "user" / "project.json").exists()
    assert str(original) not in response.text
    assert str(replacement) not in response.text


def test_apply_holds_component_guard_until_copy_finishes_and_blocks_replacement_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    repository_importer = load_portable_importer(tmp_path / "TTS More")
    apply_entered = threading.Event()
    release_apply = threading.Event()

    def blocking_apply(plan):
        apply_entered.set()
        assert release_apply.wait(5)
        return repository_importer.apply_import(plan)

    monkeypatch.setattr(
        local_control,
        "load_portable_importer",
        lambda _root: SimpleNamespace(
            plan_import=repository_importer.plan_import,
            apply_import=blocking_apply,
        ),
    )
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "replacement worker", component="gpt-sovits", package_id="gpt-main"
    )
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    registration_guard_attempted = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        while frame is not None:
            if frame.f_code.co_name == "register_local_portable_service":
                registration_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    def registered_root() -> Path:
        endpoint = next(
            item
            for item in PortableServiceStore(tmp_path / "TTS More").load()
            if item.portable_locator is not None
            and item.portable_locator.component == "gpt-sovits"
        )
        assert endpoint.portable_locator is not None
        assert endpoint.portable_locator.absolute_path_last_seen is not None
        return Path(endpoint.portable_locator.absolute_path_last_seen).resolve()

    with ThreadPoolExecutor(max_workers=2) as executor:
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        assert apply_entered.wait(5)
        registering = executor.submit(
            client.post,
            "/api/local-portable-services/register",
            headers=headers,
            json={
                "component": "gpt-sovits",
                "package_id": "gpt-main",
                "path": str(replacement),
            },
        )
        try:
            assert registration_guard_attempted.wait(2)
            assert not registering.done()
            assert registered_root() == original.resolve()
        finally:
            release_apply.set()
        applied = applying.result(timeout=10)
        registered = registering.result(timeout=10)

    assert applied.status_code == 200, applied.text
    assert registered.status_code == 200, registered.text
    assert registered_root() == replacement.resolve()
    assert (original / "data" / "user" / "project.json").read_text(encoding="utf-8") == "user-data"
    assert not (replacement / "data" / "user" / "project.json").exists()


def test_replacement_registration_holds_component_guard_through_publication_before_stale_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "replacement worker", component="gpt-sovits", package_id="gpt-main"
    )
    registration_published = threading.Event()
    release_registration = threading.Event()
    original_replace = PortableServiceStore.replace_component

    def blocking_replace(store, endpoint, **kwargs):
        services = original_replace(store, endpoint, **kwargs)
        registration_published.set()
        assert release_registration.wait(5)
        return services

    monkeypatch.setattr(PortableServiceStore, "replace_component", blocking_replace)
    monkeypatch.setattr(
        client.app.state.portable_import_plan_store,
        "invalidate_component",
        lambda _component: None,
    )
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    apply_guard_attempted = threading.Event()
    apply_guard_acquired = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        called_by_apply = False
        while frame is not None:
            if frame.f_code.co_name == "apply_local_portable_import":
                called_by_apply = True
                apply_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            if called_by_apply:
                apply_guard_acquired.set()
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    with ThreadPoolExecutor(max_workers=2) as executor:
        registering = executor.submit(
            client.post,
            "/api/local-portable-services/register",
            headers=headers,
            json={
                "component": "gpt-sovits",
                "package_id": "gpt-main",
                "path": str(replacement),
            },
        )
        assert registration_published.wait(5)
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        try:
            assert apply_guard_attempted.wait(2)
            assert not apply_guard_acquired.wait(0.25)
            assert not applying.done()
        finally:
            release_registration.set()
        registered = registering.result(timeout=10)
        applied = applying.result(timeout=10)

    assert registered.status_code == 200, registered.text
    assert applied.status_code == 409
    assert applied.json()["detail"] == {
        "code": "LOCAL_CONTROL_IMPORT_BLOCKED",
        "message": "portable import is unavailable",
    }
    assert str(original) not in applied.text
    assert str(replacement) not in applied.text
    replayed = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"


def test_successful_replacement_registration_invalidates_pending_import_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    pending = [
        client.post(
            "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
        ).json()
        for _index in range(2)
    ]
    replacement = _write_package(
        tmp_path / "replacement worker", component="gpt-sovits", package_id="gpt-main"
    )

    registered = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": "gpt-main",
            "path": str(replacement),
        },
    )
    assert registered.status_code == 200, registered.text
    for planned in pending:
        applied = client.post(
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        assert applied.status_code == 409
        assert applied.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"


def test_failed_replacement_registration_does_not_invalidate_pending_import_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "replacement worker", component="gpt-sovits", package_id="gpt-main"
    )

    def fail_publication(*_args, **_kwargs) -> None:
        raise ValueError("simulated runtime publication failure")

    monkeypatch.setattr(main_module, "_apply_registry", fail_publication)
    registered = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": "gpt-main",
            "path": str(replacement),
        },
    )
    retained = client.app.state.portable_import_plan_store.consume(
        planned["plan_id"], planned["plan_digest"]
    )

    assert registered.status_code == 500
    assert registered.json()["detail"]["code"] == "LOCAL_CONTROL_PUBLICATION_FAILED"
    assert retained.component == "gpt-sovits"


def test_rolled_back_replacement_registration_does_not_invalidate_pending_import_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "replacement worker", component="gpt-sovits", package_id="gpt-main"
    )

    def fail_before_commit(_store, _endpoint, **_kwargs):
        raise OSError("simulated pre-commit failure")

    monkeypatch.setattr(PortableServiceStore, "replace_component", fail_before_commit)
    registered = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": "gpt-main",
            "path": str(replacement),
        },
    )
    retained = client.app.state.portable_import_plan_store.consume(
        planned["plan_id"], planned["plan_digest"]
    )
    endpoint = next(
        item
        for item in PortableServiceStore(tmp_path / "TTS More").load()
        if item.portable_locator is not None
        and item.portable_locator.component == "gpt-sovits"
    )

    assert registered.status_code == 409
    assert registered.json()["detail"]["code"] == "LOCAL_CONTROL_STORE_INVALID"
    assert retained.component == "gpt-sovits"
    assert endpoint.portable_locator is not None
    assert endpoint.portable_locator.absolute_path_last_seen == str(original.resolve())


def test_main_portable_register_blocks_on_apply_component_guard_until_copy_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    importer = load_portable_importer(tmp_path / "TTS More")
    apply_entered = threading.Event()
    release_apply = threading.Event()

    def blocking_apply(plan):
        apply_entered.set()
        assert release_apply.wait(5)
        return importer.apply_import(plan)

    monkeypatch.setattr(
        local_control,
        "load_portable_importer",
        lambda _root: SimpleNamespace(
            plan_import=importer.plan_import,
            apply_import=blocking_apply,
        ),
    )
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "main replacement", component="gpt-sovits", package_id="gpt-main"
    )
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    registration_guard_attempted = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        while frame is not None:
            if frame.f_code.co_name == "portable_package_register":
                registration_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    with ThreadPoolExecutor(max_workers=2) as executor:
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        assert apply_entered.wait(5)
        registering = executor.submit(
            client.post,
            "/api/portable-packages/register",
            json={"package_root": str(replacement)},
        )
        try:
            assert registration_guard_attempted.wait(2)
            assert not registering.done()
            persisted = PortableServiceStore(tmp_path / "TTS More").load()
            current = next(
                endpoint
                for endpoint in persisted
                if endpoint.portable_locator is not None
                and endpoint.portable_locator.component == "gpt-sovits"
            )
            assert current.portable_locator is not None
            assert current.portable_locator.absolute_path_last_seen == str(original.resolve())
        finally:
            release_apply.set()
        applied = applying.result(timeout=10)
        registered = registering.result(timeout=10)

    assert applied.status_code == 200, applied.text
    assert registered.status_code == 200, registered.text
    assert (original / "data" / "user" / "project.json").read_text(encoding="utf-8") == "user-data"
    assert not (replacement / "data" / "user" / "project.json").exists()


def test_main_portable_register_holds_component_guard_through_publication_before_stale_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "main replacement", component="gpt-sovits", package_id="gpt-main"
    )
    registration_published = threading.Event()
    release_registration = threading.Event()
    original_apply_registry = main_module._apply_registry

    def blocking_publication(app, registry, store) -> None:
        original_apply_registry(app, registry, store)
        matching = [
            endpoint
            for endpoint in registry.services
            if endpoint.portable_locator is not None
            and endpoint.portable_locator.component == "gpt-sovits"
            and endpoint.portable_locator.absolute_path_last_seen == str(replacement.resolve())
        ]
        if matching:
            registration_published.set()
            assert release_registration.wait(5)

    monkeypatch.setattr(main_module, "_apply_registry", blocking_publication)
    monkeypatch.setattr(
        client.app.state.portable_import_plan_store,
        "invalidate_component",
        lambda _component: None,
    )
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    apply_guard_attempted = threading.Event()
    apply_guard_acquired = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        called_by_apply = False
        while frame is not None:
            if frame.f_code.co_name == "apply_local_portable_import":
                called_by_apply = True
                apply_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            if called_by_apply:
                apply_guard_acquired.set()
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    with ThreadPoolExecutor(max_workers=2) as executor:
        registering = executor.submit(
            client.post,
            "/api/portable-packages/register",
            json={"package_root": str(replacement)},
        )
        assert registration_published.wait(5)
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        try:
            assert apply_guard_attempted.wait(2)
            assert not apply_guard_acquired.wait(0.25)
            assert not applying.done()
        finally:
            release_registration.set()
        registered = registering.result(timeout=10)
        applied = applying.result(timeout=10)

    assert registered.status_code == 200, registered.text
    assert applied.status_code == 409
    assert applied.json()["detail"] == {
        "code": "LOCAL_CONTROL_IMPORT_BLOCKED",
        "message": "portable import is unavailable",
    }
    assert str(original) not in applied.text
    assert str(replacement) not in applied.text
    replayed = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"


def test_main_portable_register_success_invalidates_all_pending_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    pending = [
        client.post(
            "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
        ).json()
        for _index in range(2)
    ]
    replacement = _write_package(
        tmp_path / "main replacement", component="gpt-sovits", package_id="gpt-main"
    )

    registered = client.post(
        "/api/portable-packages/register", json={"package_root": str(replacement)}
    )

    assert registered.status_code == 200, registered.text
    for planned in pending:
        with pytest.raises(PortableImportPlanError):
            client.app.state.portable_import_plan_store.consume(
                planned["plan_id"], planned["plan_digest"]
            )


def test_main_portable_register_publication_failure_does_not_invalidate_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "main replacement", component="gpt-sovits", package_id="gpt-main"
    )

    def fail_publication(*_args, **_kwargs) -> None:
        raise ValueError("simulated publication failure")

    monkeypatch.setattr(main_module, "_apply_registry", fail_publication)
    registered = client.post(
        "/api/portable-packages/register", json={"package_root": str(replacement)}
    )
    retained = client.app.state.portable_import_plan_store.consume(
        planned["plan_id"], planned["plan_digest"]
    )

    assert registered.status_code == 500
    assert registered.json()["detail"] == {
        "code": "PORTABLE_REGISTRATION_PUBLICATION_FAILED",
        "message": "portable package registration was persisted but publication failed",
    }
    assert retained.component == "gpt-sovits"


def test_main_portable_register_preserves_trusted_lan_endpoint_without_local_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _managed = _portable_import_client(tmp_path, monkeypatch)
    pending = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan",
        headers=headers,
        json={},
    ).json()
    lan_package = _write_package(
        tmp_path / "trusted LAN worker",
        component="cosyvoice",
        package_id="cosy-main",
        port=50000,
    )

    registered = client.post(
        "/api/portable-packages/register",
        json={
            "package_root": str(lan_package),
            "base_url": "http://192.168.2.55:50000",
            "display_name": "CosyVoice trusted LAN",
        },
    )

    assert registered.status_code == 200, registered.text
    service = registered.json()["service"]
    assert {
        key: service[key]
        for key in (
            "service_id",
            "display_name",
            "api_contract",
            "base_url",
            "mode",
            "network_scope",
            "managed",
            "repo_path",
            "start_command",
            "start_cwd",
            "control_kind",
            "portable_locator",
        )
    } == {
        "service_id": "portable-cosyvoice-cosy-main",
        "display_name": "CosyVoice trusted LAN",
        "api_contract": "tts-more-v1",
        "base_url": "http://192.168.2.55:50000",
        "mode": "external",
        "network_scope": "lan",
        "managed": False,
        "repo_path": None,
        "start_command": [],
        "start_cwd": None,
        "control_kind": "generic",
        "portable_locator": None,
    }
    persisted = json.loads(
        Path(client.app.state.writable_services_path).read_text(encoding="utf-8")
    )
    persisted_lan = next(
        item
        for item in persisted["services"]
        if item["service_id"] == "portable-cosyvoice-cosy-main"
    )
    assert persisted_lan["portable_locator"] is None
    published_lan = client.app.state.service_registry.get(
        "portable-cosyvoice-cosy-main"
    )
    assert published_lan.mode == "external"
    assert published_lan.managed is False
    assert published_lan.portable_locator is None
    assert any(
        item.portable_locator is not None
        and item.portable_locator.component == "gpt-sovits"
        for item in client.app.state.service_registry.services
    )
    retained = client.app.state.portable_import_plan_store.consume(
        pending["plan_id"], pending["plan_digest"]
    )
    assert retained.component == "gpt-sovits"

    plan = client.post(
        "/api/local-portable-services/cosyvoice/imports/plan",
        headers=headers,
        json={},
    )
    assert plan.status_code == 409
    assert plan.json()["detail"]["code"] == "LOCAL_CONTROL_NOT_MANAGEABLE"
    forged_plan = _stored_plan(
        client.app.state.portable_import_plan_store,
        (tmp_path / "unreachable LAN import target").resolve(),
        digest="d" * 64,
        component="cosyvoice",
    )
    applied = client.post(
        f"/api/local-portable-services/cosyvoice/imports/{forged_plan.plan_id}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": "d" * 64},
    )
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "LOCAL_CONTROL_NOT_MANAGEABLE"
    for action in ("start", "stop", "repair", "open-folder"):
        response = client.post(
            f"/api/local-portable-services/cosyvoice/{action}",
            headers=headers,
            json={},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "LOCAL_CONTROL_NOT_MANAGEABLE"


def test_trusted_lan_registration_cannot_replace_managed_portable_service_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, managed = _portable_import_client(tmp_path, monkeypatch)
    pending = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan",
        headers=headers,
        json={},
    ).json()
    services_path = Path(client.app.state.writable_services_path)
    before = services_path.read_bytes()

    conflict = client.post(
        "/api/portable-packages/register",
        json={
            "package_root": str(managed),
            "base_url": "http://192.168.2.56:9880",
        },
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    assert services_path.read_bytes() == before
    published = client.app.state.service_registry.get(
        "portable-gpt-sovits-gpt-main"
    )
    assert published.portable_locator is not None
    retained = client.app.state.portable_import_plan_store.consume(
        pending["plan_id"], pending["plan_digest"]
    )
    assert retained.component == "gpt-sovits"


def test_settings_route_cannot_remove_or_replace_managed_portable_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "settings replacement", component="gpt-sovits", package_id="gpt-main"
    )
    current = client.get("/api/settings/services").json()["services"]
    replacement_payload = json.loads(json.dumps(current))
    portable = next(item for item in replacement_payload if item.get("portable_locator"))
    portable["portable_locator"]["absolute_path_last_seen"] = str(replacement.resolve())
    portable["portable_locator"]["relative_to_tts_more"] = None

    removed = client.put("/api/settings/services", json={"services": []})
    replaced = client.put(
        "/api/settings/services", json={"services": replacement_payload}
    )
    retained = client.app.state.portable_import_plan_store.consume(
        planned["plan_id"], planned["plan_digest"]
    )

    expected = {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    assert removed.status_code == 409
    assert removed.json()["detail"] == expected
    assert replaced.status_code == 409
    assert replaced.json()["detail"] == expected
    assert retained.component == "gpt-sovits"
    persisted = PortableServiceStore(tmp_path / "TTS More").load()
    locator = next(item.portable_locator for item in persisted if item.portable_locator is not None)
    assert locator.absolute_path_last_seen == str(original.resolve())


def test_reload_rejects_external_portable_locator_change_and_preserves_published_registry_and_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "reload replacement", component="gpt-sovits", package_id="gpt-main"
    )
    descriptor = inspect_locator_candidate(replacement)
    assert descriptor is not None
    endpoint = endpoint_from_portable_package(
        descriptor,
        PortablePackageRegisterRequest(package_root=str(replacement)),
    )
    PortableServiceStore(tmp_path / "TTS More").replace_component(endpoint)

    reloaded = client.post("/api/settings/services/reload")
    retained = client.app.state.portable_import_plan_store.consume(
        planned["plan_id"], planned["plan_digest"]
    )
    published = next(
        endpoint
        for endpoint in client.app.state.service_registry.services
        if endpoint.portable_locator is not None
    )

    assert reloaded.status_code == 409
    assert reloaded.json()["detail"] == {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    assert retained.component == "gpt-sovits"
    assert published.portable_locator is not None
    assert published.portable_locator.absolute_path_last_seen == str(original.resolve())


def test_reload_and_main_portable_registration_share_deterministic_component_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    replacement = _write_package(
        tmp_path / "reload race replacement", component="gpt-sovits", package_id="gpt-main"
    )
    registration_published = threading.Event()
    release_registration = threading.Event()
    original_apply_registry = main_module._apply_registry

    def blocking_publication(app, registry, store) -> None:
        original_apply_registry(app, registry, store)
        if any(
            endpoint.portable_locator is not None
            and endpoint.portable_locator.absolute_path_last_seen == str(replacement.resolve())
            for endpoint in registry.services
        ):
            registration_published.set()
            assert release_registration.wait(5)

    monkeypatch.setattr(main_module, "_apply_registry", blocking_publication)
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    reload_guard_attempted = threading.Event()
    reload_guard_acquired = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        called_by_reload = False
        while frame is not None:
            if (
                frame.f_code.co_name == "reload_service_settings"
                and component == "gpt-sovits"
            ):
                called_by_reload = True
                reload_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            if called_by_reload:
                reload_guard_acquired.set()
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    with ThreadPoolExecutor(max_workers=2) as executor:
        registering = executor.submit(
            client.post,
            "/api/portable-packages/register",
            json={"package_root": str(replacement)},
        )
        assert registration_published.wait(5)
        reloading = executor.submit(client.post, "/api/settings/services/reload")
        try:
            assert reload_guard_attempted.wait(2)
            assert not reload_guard_acquired.wait(0.25)
            assert not reloading.done()
        finally:
            release_registration.set()
        registered = registering.result(timeout=10)
        reloaded = reloading.result(timeout=10)

    assert registered.status_code == 200, registered.text
    assert reloaded.status_code == 200, reloaded.text


def test_settings_save_waits_for_inflight_apply_component_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    importer = load_portable_importer(tmp_path / "TTS More")
    apply_entered = threading.Event()
    release_apply = threading.Event()

    def blocking_apply(plan):
        apply_entered.set()
        assert release_apply.wait(5)
        return importer.apply_import(plan)

    monkeypatch.setattr(
        local_control,
        "load_portable_importer",
        lambda _root: SimpleNamespace(
            plan_import=importer.plan_import,
            apply_import=blocking_apply,
        ),
    )
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    services = client.get("/api/settings/services").json()["services"]
    services.append(
        {
            "service_id": "external-test",
            "engine": "gpt-sovits",
            "provider_type": "gpt-sovits",
            "base_url": "http://192.168.1.20:9880",
            "mode": "external",
            "network_scope": "lan",
            "managed": False,
        }
    )
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    settings_guard_attempted = threading.Event()
    settings_guard_acquired = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        called_by_settings = False
        while frame is not None:
            if (
                frame.f_code.co_name == "put_service_settings"
                and component == "gpt-sovits"
            ):
                called_by_settings = True
                settings_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            if called_by_settings:
                settings_guard_acquired.set()
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    with ThreadPoolExecutor(max_workers=2) as executor:
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        assert apply_entered.wait(5)
        saving = executor.submit(
            client.put,
            "/api/settings/services",
            json={"services": services},
        )
        try:
            assert settings_guard_attempted.wait(2)
            assert not settings_guard_acquired.wait(0.25)
            assert not saving.done()
        finally:
            release_apply.set()
        applied = applying.result(timeout=10)
        saved = saving.result(timeout=10)

    assert applied.status_code == 200, applied.text
    assert saved.status_code == 200, saved.text


def test_open_source_configure_waits_for_inflight_apply_component_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    importer = load_portable_importer(tmp_path / "TTS More")
    apply_entered = threading.Event()
    release_apply = threading.Event()

    def blocking_apply(plan):
        apply_entered.set()
        assert release_apply.wait(5)
        return importer.apply_import(plan)

    monkeypatch.setattr(
        local_control,
        "load_portable_importer",
        lambda _root: SimpleNamespace(
            plan_import=importer.plan_import,
            apply_import=blocking_apply,
        ),
    )
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    configure_guard_attempted = threading.Event()
    configure_guard_acquired = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        called_by_configure = False
        while frame is not None:
            if (
                frame.f_code.co_name == "open_source_tts_configure"
                and component == "gpt-sovits"
            ):
                called_by_configure = True
                configure_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            if called_by_configure:
                configure_guard_acquired.set()
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    with ThreadPoolExecutor(max_workers=2) as executor:
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        assert apply_entered.wait(5)
        configuring = executor.submit(
            client.post,
            "/api/open-source-tts/configure",
            json={
                "provider_type": "cosyvoice",
                "service_id": "lan-cosyvoice-race",
                "display_name": "CosyVoice LAN",
                "source_profile": "lan_endpoint",
                "base_url": "http://192.168.1.30:50000",
            },
        )
        try:
            assert configure_guard_attempted.wait(2)
            assert not configure_guard_acquired.wait(0.25)
            assert not configuring.done()
        finally:
            release_apply.set()
        applied = applying.result(timeout=10)
        configured = configuring.result(timeout=10)

    assert applied.status_code == 200, applied.text
    assert configured.status_code == 200, configured.text


def test_trusted_lan_registration_waits_for_inflight_apply_component_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _original = _portable_import_client(tmp_path, monkeypatch)
    importer = load_portable_importer(tmp_path / "TTS More")
    apply_entered = threading.Event()
    release_apply = threading.Event()

    def blocking_apply(plan):
        apply_entered.set()
        assert release_apply.wait(5)
        return importer.apply_import(plan)

    monkeypatch.setattr(
        local_control,
        "load_portable_importer",
        lambda _root: SimpleNamespace(
            plan_import=importer.plan_import,
            apply_import=blocking_apply,
        ),
    )
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    lan_package = _write_package(
        tmp_path / "guarded trusted LAN worker",
        component="cosyvoice",
        package_id="cosy-main",
        port=50000,
    )
    supervisor = client.app.state.supervisor
    original_guard = supervisor.portable_lifecycle_guard
    registration_guard_attempted = threading.Event()
    registration_guard_acquired = threading.Event()

    @contextmanager
    def observed_guard(component):
        frame = sys._getframe()
        called_by_registration = False
        while frame is not None:
            if (
                frame.f_code.co_name == "portable_package_register"
                and component == "gpt-sovits"
            ):
                called_by_registration = True
                registration_guard_attempted.set()
                break
            frame = frame.f_back
        with original_guard(component):
            if called_by_registration:
                registration_guard_acquired.set()
            yield

    monkeypatch.setattr(supervisor, "portable_lifecycle_guard", observed_guard)

    with ThreadPoolExecutor(max_workers=2) as executor:
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        assert apply_entered.wait(5)
        registering = executor.submit(
            client.post,
            "/api/portable-packages/register",
            json={
                "package_root": str(lan_package),
                "base_url": "http://192.168.2.55:50000",
            },
        )
        try:
            assert registration_guard_attempted.wait(2)
            assert not registration_guard_acquired.wait(0.25)
            assert not registering.done()
        finally:
            release_apply.set()
        applied = applying.result(timeout=10)
        registered = registering.result(timeout=10)

    assert applied.status_code == 200, applied.text
    assert registered.status_code == 200, registered.text


def test_settings_external_locator_edit_is_rejected_against_published_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "settings external replacement",
        component="gpt-sovits",
        package_id="gpt-main",
    )
    descriptor = inspect_locator_candidate(replacement)
    assert descriptor is not None
    endpoint = endpoint_from_portable_package(
        descriptor,
        PortablePackageRegisterRequest(package_root=str(replacement)),
    )
    store = PortableServiceStore(tmp_path / "TTS More")
    store.replace_component(endpoint)
    services = [item.model_dump(mode="json") for item in store.load()]
    services.append(
        {
            "service_id": "external-settings-test",
            "engine": "gpt-sovits",
            "provider_type": "gpt-sovits",
            "base_url": "http://192.168.1.20:9880",
            "mode": "external",
            "network_scope": "lan",
            "managed": False,
        }
    )

    saved = client.put("/api/settings/services", json={"services": services})

    assert saved.status_code == 409
    assert saved.json()["detail"] == {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    published = next(
        endpoint
        for endpoint in client.app.state.service_registry.services
        if endpoint.portable_locator is not None
    )
    assert published.portable_locator is not None
    assert published.portable_locator.absolute_path_last_seen == str(original.resolve())
    applied = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_BLOCKED"


def test_trusted_lan_registration_rejects_external_locator_edit_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "LAN registration external replacement",
        component="gpt-sovits",
        package_id="gpt-main",
    )
    descriptor = inspect_locator_candidate(replacement)
    assert descriptor is not None
    replacement_endpoint = endpoint_from_portable_package(
        descriptor,
        PortablePackageRegisterRequest(package_root=str(replacement)),
    )
    PortableServiceStore(tmp_path / "TTS More").replace_component(replacement_endpoint)
    lan_package = _write_package(
        tmp_path / "rejected trusted LAN worker",
        component="cosyvoice",
        package_id="cosy-main",
        port=50000,
    )

    registered = client.post(
        "/api/portable-packages/register",
        json={
            "package_root": str(lan_package),
            "base_url": "http://192.168.2.55:50000",
        },
    )

    assert registered.status_code == 409
    assert registered.json()["detail"] == {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    assert all(
        item.service_id != "portable-cosyvoice-cosy-main"
        for item in client.app.state.service_registry.services
    )
    published = client.app.state.service_registry.get(
        "portable-gpt-sovits-gpt-main"
    )
    assert published.portable_locator is not None
    assert published.portable_locator.absolute_path_last_seen == str(original.resolve())
    applied = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_BLOCKED"


def test_configure_external_locator_edit_is_rejected_against_published_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, original = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    replacement = _write_package(
        tmp_path / "configure external replacement",
        component="gpt-sovits",
        package_id="gpt-main",
    )
    descriptor = inspect_locator_candidate(replacement)
    assert descriptor is not None
    endpoint = endpoint_from_portable_package(
        descriptor,
        PortablePackageRegisterRequest(package_root=str(replacement)),
    )
    PortableServiceStore(tmp_path / "TTS More").replace_component(endpoint)

    configured = client.post(
        "/api/open-source-tts/configure",
        json={
            "provider_type": "cosyvoice",
            "service_id": "lan-cosyvoice-external-edit",
            "display_name": "CosyVoice LAN",
            "source_profile": "lan_endpoint",
            "base_url": "http://192.168.1.30:50000",
        },
    )

    assert configured.status_code == 409
    assert configured.json()["detail"] == {
        "code": "MANAGED_PORTABLE_LOCATOR_MUTATION_FORBIDDEN",
        "message": "managed portable locators must use a portable registration route",
    }
    published = next(
        item
        for item in client.app.state.service_registry.services
        if item.portable_locator is not None
    )
    assert published.portable_locator is not None
    assert published.portable_locator.absolute_path_last_seen == str(original.resolve())
    applied = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_BLOCKED"


def test_successful_start_invalidates_component_plans_but_failed_start_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, _new = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    client.app.state.supervisor = _FakeSupervisor()

    started = client.post(
        "/api/local-portable-services/gpt-sovits/start", headers=headers, json={}
    )
    assert started.status_code == 200
    unavailable = client.post(
        f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
        headers=headers,
        json={"confirmed": True, "plan_digest": planned["plan_digest"]},
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"

    client2, headers2, _old2, _new2 = _portable_import_client(
        tmp_path / "failed", monkeypatch
    )
    planned2 = client2.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers2, json={}
    ).json()
    failed = _FakeSupervisor()
    failed.start = lambda _endpoint, operation_id=None: {
        "status": "blocked",
        "error_code": "START_FAILED",
        "operation_id": operation_id,
    }
    client2.app.state.supervisor = failed
    response = client2.post(
        "/api/local-portable-services/gpt-sovits/start", headers=headers2, json={}
    )
    assert response.status_code == 409
    retained = client2.app.state.portable_import_plan_store.consume(
        planned2["plan_id"], planned2["plan_digest"]
    )
    assert retained.component == "gpt-sovits"


class _SerializedImportSupervisor(_FakeSupervisor):
    def __init__(self, descriptor) -> None:
        super().__init__()
        self.descriptor = descriptor
        self.lock = threading.RLock()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()
        self.active = 0
        self.maximum_active = 0

    @contextmanager
    def portable_lifecycle_guard(self, _component):
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                yield
            finally:
                self.active -= 1

    def start(self, endpoint, *, operation_id=None):
        self.calls.append(("start", endpoint, operation_id))
        self.start_entered.set()
        assert self.release_start.wait(5)
        return {"status": "starting", "operation_id": operation_id}

    def require_portable_stopped(self, _endpoint):
        return self.descriptor


def test_start_and_apply_share_one_component_guard_and_cannot_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, _old, new = _portable_import_client(tmp_path, monkeypatch)
    planned = client.post(
        "/api/local-portable-services/gpt-sovits/imports/plan", headers=headers, json={}
    ).json()
    descriptor = inspect_locator_candidate(new)
    assert descriptor is not None
    supervisor = _SerializedImportSupervisor(descriptor)
    client.app.state.supervisor = supervisor

    with ThreadPoolExecutor(max_workers=2) as executor:
        starting = executor.submit(
            client.post,
            "/api/local-portable-services/gpt-sovits/start",
            headers=headers,
            json={},
        )
        assert supervisor.start_entered.wait(2)
        applying = executor.submit(
            client.post,
            f"/api/local-portable-services/gpt-sovits/imports/{planned['plan_id']}/apply",
            headers=headers,
            json={"confirmed": True, "plan_digest": planned["plan_digest"]},
        )
        time.sleep(0.15)
        serialized = not applying.done()
        supervisor.release_start.set()
        started = starting.result(timeout=5)
        applied = applying.result(timeout=5)

    assert serialized is True
    assert supervisor.maximum_active == 1
    assert started.status_code == 200
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE"


def test_moved_four_folder_suite_is_rediscovered_and_started_independently(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "普通用户 目录" / "四仓 套件")
    client, token = _local_client(suite / "TTS More")
    headers = _control(token)

    discovered = client.post(
        "/api/local-portable-services/discover",
        headers=headers,
        json={},
    )

    assert discovered.status_code == 200, discovered.text
    packages = {item["component"]: item for item in discovered.json()["packages"]}
    assert set(packages) == {"gpt-sovits", "indextts", "cosyvoice"}
    assert all(item["complete_v2"] and item["manageable"] for item in packages.values())
    for item in packages.values():
        package_root = Path(item["package_root"])
        package_root.relative_to(suite)
        Path(item["manifest_path"]).relative_to(package_root)
        assert not {"command", "cwd", "env"}.intersection(item)
        assert all(not Path(path).is_absolute() for path in item["launchers"].values())
        assert not Path(item["operations_path"]).is_absolute()
        assert not Path(item["state_path"]).is_absolute()

    gpt = packages["gpt-sovits"]
    registered = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": gpt["package_id"],
            "path": gpt["package_root"],
        },
    )
    assert registered.status_code == 200, registered.text

    started = client.post(
        "/api/local-portable-services/gpt-sovits/start",
        headers=headers,
        json={},
    )
    assert started.status_code == 200, started.text
    assert started.json()["component"] == "gpt-sovits"
    UUID(started.json()["operation_id"])
    assert [(call[0], call[1].portable_locator.component) for call in client.app.state.supervisor.calls] == [
        ("start", "gpt-sovits")
    ]
    assert client.post(
        "/api/local-portable-services/indextts/start", headers=headers, json={}
    ).status_code == 409
    assert client.post(
        "/api/local-portable-services/cosyvoice/start", headers=headers, json={}
    ).status_code == 409
    stored = json.loads(
        (suite / "TTS More/data/local/services.json").read_text(encoding="utf-8")
    )
    gpt_locator = next(
        item["portable_locator"]
        for item in stored["services"]
        if (item.get("portable_locator") or {}).get("component") == "gpt-sovits"
    )
    assert gpt_locator["relative_to_tts_more"] == "../GPT-SoVITS"
    old_absolute_path = Path(gpt_locator["absolute_path_last_seen"])

    client.close()
    moved_suite = tmp_path / "移动后 目录" / "重命名 四仓"
    moved_suite.parent.mkdir(parents=True)
    suite.rename(moved_suite)
    assert not old_absolute_path.exists()
    moved_client, moved_token = _local_client(moved_suite / "TTS More")
    moved_headers = _control(moved_token)

    moved_discovered = moved_client.post(
        "/api/local-portable-services/discover",
        headers=moved_headers,
        json={},
    )
    assert moved_discovered.status_code == 200, moved_discovered.text
    assert {item["component"] for item in moved_discovered.json()["packages"]} == {
        "gpt-sovits",
        "indextts",
        "cosyvoice",
    }

    moved_started = moved_client.post(
        "/api/local-portable-services/gpt-sovits/start",
        headers=moved_headers,
        json={},
    )
    assert moved_started.status_code == 200, moved_started.text
    UUID(moved_started.json()["operation_id"])
    assert [(call[0], call[1].portable_locator.component) for call in moved_client.app.state.supervisor.calls] == [
        ("start", "gpt-sovits")
    ]
    moved_listing = moved_client.get("/api/local-portable-services", headers=moved_headers)
    moved_gpt = next(
        item for item in moved_listing.json()["services"] if item["package_id"] == "gpt-main"
    )
    assert Path(moved_gpt["package_root"]) == (moved_suite / "GPT-SoVITS").resolve()


def test_lan_service_remains_usable_but_local_lifecycle_and_browse_are_denied(
    tmp_path: Path,
) -> None:
    controller = tmp_path / "可信 LAN 套件" / "TTS More"
    controller.mkdir(parents=True)
    PortableServiceStore(controller).save(
        [
            {
                "service_id": "lan-gpt",
                "display_name": "LAN GPT-SoVITS",
                "engine": "gpt-sovits",
                "provider_type": "gpt-sovits",
                "catalog_provider": "gpt-sovits",
                "api_contract": "tts-more-v1",
                "base_url": "mock://lan-gpt",
                "mode": "external",
                "network_scope": "lan",
                "managed": True,
                "enabled": True,
                "capabilities": ["tts", "artifact-transfer"],
            }
        ]
    )
    client, token = _local_client(controller)
    headers = _control(token)

    ordinary_services = client.get("/api/services")
    assert ordinary_services.status_code == 200
    lan_service = ordinary_services.json()["services"][0]
    assert lan_service["service_id"] == "lan-gpt"
    assert lan_service["ready"] is True
    local_listing = client.get("/api/local-portable-services", headers=headers)
    assert local_listing.status_code == 200
    assert local_listing.json()["services"][0]["managed"] is False

    for action in ("start", "stop", "repair", "open-folder"):
        response = client.post(
            f"/api/local-portable-services/gpt-sovits/{action}",
            headers=headers,
            json={},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "LOCAL_CONTROL_NOT_MANAGEABLE"
    assert client.app.state.supervisor.calls == []

    lan_client = TestClient(
        client.app,
        base_url="http://192.168.2.10:8000",
        client=("192.168.2.20", 51000),
    )
    assert lan_client.get("/api/services").status_code == 200
    denied_requests = [
        ("/api/local-portable-services/select-folder", {"component": "gpt-sovits"}),
        ("/api/local-portable-services/gpt-sovits/start", {}),
        ("/api/local-portable-services/gpt-sovits/stop", {}),
        ("/api/local-portable-services/gpt-sovits/repair", {}),
        ("/api/local-portable-services/gpt-sovits/open-folder", {}),
    ]
    for path, payload in denied_requests:
        response = lan_client.post(path, headers=headers, json=payload)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "LOCAL_CONTROL_FORBIDDEN"


def test_replacing_package_path_preserves_existing_port_override(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    first = _write_package(tmp_path / "GPT old", component="gpt-sovits", package_id="gpt-main")
    replacement = _write_package(tmp_path / "GPT new", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    headers = _control(_token(client))

    first_response = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": "gpt-main",
            "path": str(first),
            "port_override": 9988,
        },
    )
    replacement_response = client.post(
        "/api/local-portable-services/register",
        headers=headers,
        json={
            "component": "gpt-sovits",
            "package_id": "gpt-main",
            "path": str(replacement),
        },
    )

    assert first_response.status_code == 200, first_response.text
    assert replacement_response.status_code == 200, replacement_response.text
    assert replacement_response.json()["service"]["package_root"] == str(replacement.resolve())
    assert replacement_response.json()["service"]["port_override"] == 9988


def test_local_service_list_refreshes_install_state_from_fresh_package(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)

    before = client.get("/api/local-portable-services", headers=headers)
    state_path = package / "data/local/install-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    after = client.get("/api/local-portable-services", headers=headers)

    assert before.status_code == 200
    before_service = next(
        service for service in before.json()["services"] if service["package_id"] == "gpt-main"
    )
    after_service = next(
        service for service in after.json()["services"] if service["package_id"] == "gpt-main"
    )
    assert before_service["setup_state"] == "env_missing"
    assert after.status_code == 200
    assert after_service["setup_state"] == "ready"


@pytest.mark.parametrize("action", ("start", "stop", "repair", "open-folder"))
def test_actions_use_only_action_enum_and_fresh_stored_locator(
    tmp_path: Path, action: str
) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    fake = _FakeSupervisor()
    client.app.state.supervisor = fake

    response = client.post(
        f"/api/local-portable-services/gpt-sovits/{action}",
        headers=headers,
        json={"port_override": 9981} if action == "start" else {},
    )

    assert response.status_code == 200, response.text
    assert fake.calls[0][0] == action
    endpoint = fake.calls[0][1]
    assert endpoint.portable_locator.component == "gpt-sovits"
    if action == "start":
        assert endpoint.portable_locator.port_override == 9981
        assert fake.calls[0][2]
    elif action in {"stop", "repair"}:
        action_id = response.json()["action_id"]
        assert action_id
        assert fake.calls[0][2]


def test_action_converts_semantic_controller_failure_to_http_error(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    fake = _FakeSupervisor()
    fake.start = lambda _endpoint, operation_id=None: {
        "status": "blocked",
        "error_code": "CUDA_PROBE_FAILED",
        "reason": "probe failed",
        "operation_id": operation_id,
    }
    client.app.state.supervisor = fake

    response = client.post(
        "/api/local-portable-services/gpt-sovits/start",
        headers=headers,
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CUDA_PROBE_FAILED"


def test_repair_accepts_only_strict_http_proxy_and_never_echoes_credentials(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    fake = _FakeSupervisor()
    client.app.state.supervisor = fake
    proxy = "http://proxy-user:proxy-password@127.0.0.1:10808"

    response = client.post(
        "/api/local-portable-services/gpt-sovits/repair",
        headers=headers,
        json={"proxy_url": proxy},
    )

    assert response.status_code == 200, response.text
    assert fake.calls[0][0] == "repair"
    assert fake.calls[0][2]["proxy_url"] == proxy
    assert fake.calls[0][2]["action_id"]
    assert proxy not in response.text
    assert "proxy-password" not in response.text
    services_file = controller / "data/local/services.json"
    assert proxy not in services_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("action", "proxy_url"),
    (
        ("start", "http://127.0.0.1:10808"),
        ("stop", "http://127.0.0.1:10808"),
        ("open-folder", "http://127.0.0.1:10808"),
        ("repair", "socks5://127.0.0.1:1080"),
        ("repair", "http://proxy.example/path?secret=1"),
        ("repair", "http://127.0.0.1:70000"),
        ("repair", "http://127.0.0.1:10808\nINJECTED=1"),
    ),
)
def test_actions_reject_proxy_outside_strict_repair_contract(
    tmp_path: Path, action: str, proxy_url: str
) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.post(
        f"/api/local-portable-services/gpt-sovits/{action}",
        headers=_control(_token(client)),
        json={"proxy_url": proxy_url},
    )
    assert response.status_code == 422


def test_start_action_accepts_empty_body_for_the_one_click_client(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    fake = _FakeSupervisor()
    client.app.state.supervisor = fake

    response = client.post(
        "/api/local-portable-services/gpt-sovits/start",
        headers=headers,
    )

    assert response.status_code == 200
    assert fake.calls[0][0] == "start"


@pytest.mark.parametrize(
    ("action", "payload"),
    (
        ("start", {"command": "calc.exe"}),
        ("start", {"cwd": "C:/"}),
        ("start", {"env": {"PATH": "evil"}}),
        ("start", {"executable": "evil.exe"}),
        ("stop", {"port_override": 9980}),
        ("repair", {"port_override": 9980}),
    ),
)
def test_actions_reject_process_control_fields_and_wrong_action_payload(
    tmp_path: Path, action: str, payload: dict[str, object]
) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.post(
        f"/api/local-portable-services/gpt-sovits/{action}",
        headers=_control(_token(client)),
        json=payload,
    )
    assert response.status_code == 422


def test_action_rejects_missing_or_identity_drifted_local_package(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    manifest_path = package / "package/tts-more-package.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["package_id"] = "replacement"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    response = client.post(
        "/api/local-portable-services/gpt-sovits/start", headers=headers, json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LOCAL_CONTROL_NOT_MANAGEABLE"


def test_action_status_route_is_token_and_fresh_identity_protected(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    fake = _FakeSupervisor()
    client.app.state.supervisor = fake
    action_id = "22222222-2222-4222-8222-222222222222"

    assert client.get(
        f"/api/local-portable-services/gpt-sovits/actions/{action_id}"
    ).status_code == 403
    response = client.get(
        f"/api/local-portable-services/gpt-sovits/actions/{action_id}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "stopped", "action_id": action_id, "action": "stop"}
    assert fake.calls[-1][0] == "action-status"


def test_operation_and_logs_routes_delegate_to_strict_portable_reader(tmp_path: Path) -> None:
    controller = tmp_path / "TTS More"
    package = _write_package(tmp_path / "GPT", component="gpt-sovits", package_id="gpt-main")
    client = _client(controller)
    _token_value, headers = _register(client, package)
    operation = package / "data/local/operations" / OPERATION_ID
    operation.mkdir(parents=True)
    (operation / "operation.json").write_text(
        json.dumps(
            {
                "operation_id": OPERATION_ID,
                "component": "gpt-sovits",
                "action": "start",
                "initiator": "tts-more",
                "started_at": "2026-07-15T00:00:00Z",
                "status": "ready",
                "exit_code": 0,
                "finished_at": "2026-07-15T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )
    (operation / "events.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "timestamp": "2026-07-15T00:00:00Z",
                "phase": "ready",
                "message": "done",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    status = client.get(
        f"/api/local-portable-services/gpt-sovits/operations/{OPERATION_ID}", headers=headers
    )
    logs = client.get(
        f"/api/local-portable-services/gpt-sovits/operations/{OPERATION_ID}/logs?after_seq=0&limit=1",
        headers=headers,
    )

    assert status.status_code == 200, status.text
    assert status.json()["operation"]["operation_id"] == OPERATION_ID
    assert logs.status_code == 200, logs.text
    assert "events" in logs.json(), logs.json()
    assert logs.json()["events"][0]["seq"] == 1


@pytest.mark.parametrize(
    "suffix",
    (
        "not-a-uuid",
        f"{OPERATION_ID}/logs?after_seq=-1&limit=1",
        f"{OPERATION_ID}/logs?after_seq=0&limit=501",
        f"{OPERATION_ID}/logs?after_seq=true&limit=1",
    ),
)
def test_operation_routes_reject_invalid_uuid_and_unbounded_cursors(
    tmp_path: Path, suffix: str
) -> None:
    client = _client(tmp_path / "TTS More")
    response = client.get(
        f"/api/local-portable-services/gpt-sovits/operations/{suffix}",
        headers=_control(_token(client)),
    )
    assert response.status_code == 422


def _selector_root(tmp_path: Path) -> Path:
    root = tmp_path / "TTS More"
    script = root / "scripts/select-portable-folder.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("# fixed selector\n", encoding="utf-8")
    return root


def _fake_powershell(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-system" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake powershell")
    return executable


def _raw_http_request(port: int, request: bytes) -> tuple[int, bytes, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        response = bytearray()
        while True:
            chunk = connection.recv(8192)
            if not chunk:
                break
            response.extend(chunk)
    headers, _, body = bytes(response).partition(b"\r\n\r\n")
    status = int(headers.split(b"\r\n", 1)[0].split()[1])
    return status, headers, body


def test_real_httptools_rejects_ambiguous_headers_and_allows_bearer_cors_preflight(
    tmp_path: Path,
) -> None:
    controller_root = tmp_path / "TTS More"
    controller_root.mkdir()
    data_root = tmp_path / "data"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    backend_root = Path(__file__).resolve().parents[1]
    server_code = (
        "import sys,uvicorn;"
        "from pathlib import Path;"
        "from app.main import create_app;"
        "app=create_app(data_root=Path(sys.argv[1]),controller_root=Path(sys.argv[2]));"
        "uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[3]),http='httptools',"
        "log_level='critical',access_log=False)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_root)
    env["TTS_MORE_API_TOKEN"] = "integration-secret"
    server = subprocess.Popen(
        [sys.executable, "-c", server_code, str(data_root), str(controller_root), str(port)],
        cwd=str(backend_root.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            if server.poll() is not None:
                pytest.fail(f"uvicorn exited early with {server.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("uvicorn did not accept loopback connections")
                time.sleep(0.05)

        token_status, _, token_body = _raw_http_request(
            port,
            (
                f"GET /api/local-control/token HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"),
        )
        assert token_status == 200
        control_token = json.loads(token_body)["token"]

        cors_status, cors_headers, _ = _raw_http_request(
            port,
            (
                f"OPTIONS /api/local-portable-services HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Origin: http://localhost:5173\r\n"
                "Access-Control-Request-Method: GET\r\n"
                "Access-Control-Request-Headers: authorization,x-tts-more-control\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"),
        )
        assert cors_status == 200
        assert b"access-control-allow-origin: http://localhost:5173" in cors_headers.lower()

        duplicate_host, _, duplicate_host_body = _raw_http_request(
            port,
            (
                f"GET /api/local-control/token HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Host: evil.test\r\nConnection: close\r\n\r\n"
            ).encode("ascii"),
        )
        assert duplicate_host in {400, 403}
        assert b'"token"' not in duplicate_host_body

        protected_base = (
            f"GET /api/local-portable-services HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Origin: http://localhost:5173\r\n"
            "Authorization: Bearer integration-secret\r\n"
        )
        duplicate_origin, _, _ = _raw_http_request(
            port,
            (
                protected_base
                + "Origin: http://evil.test\r\n"
                + f"{CONTROL_HEADER}: {control_token}\r\nConnection: close\r\n\r\n"
            ).encode("ascii"),
        )
        assert duplicate_origin == 403

        duplicate_control, _, _ = _raw_http_request(
            port,
            (
                protected_base
                + f"{CONTROL_HEADER}: {control_token}\r\n"
                + f"{CONTROL_HEADER}: {control_token}\r\nConnection: close\r\n\r\n"
            ).encode("ascii"),
        )
        assert duplicate_control == 403
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def test_folder_selector_uses_absolute_system_powershell_and_fixed_script_only(
    tmp_path: Path,
) -> None:
    root = _selector_root(tmp_path)
    selected = tmp_path / "用户选择"
    selected.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"selected_path": str(selected)}).encode("utf-8"),
            stderr=b"",
        )

    result = select_portable_folder(
        root,
        platform_name="nt",
        run=run,
        executable_resolver=lambda _name: _fake_powershell(tmp_path),
    )

    assert result == selected.resolve()
    command, kwargs = calls[0]
    assert Path(command[0]).is_absolute()
    assert command[-1] == str((root / "scripts/select-portable-folder.ps1").resolve())
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["shell"] is False
    assert kwargs["timeout"] <= 120
    assert "input" not in kwargs


@pytest.mark.parametrize(
    ("platform_name", "completed", "code"),
    (
        ("posix", None, "LOCAL_CONTROL_FOLDER_UNSUPPORTED"),
        ("nt", SimpleNamespace(returncode=7, stdout=b"", stderr=b"secret"), "LOCAL_CONTROL_FOLDER_FAILED"),
        ("nt", SimpleNamespace(returncode=0, stdout=b"{" + b"x" * 20_000, stderr=b""), "LOCAL_CONTROL_FOLDER_OUTPUT_INVALID"),
        ("nt", SimpleNamespace(returncode=0, stdout=b"two\npaths\n", stderr=b""), "LOCAL_CONTROL_FOLDER_OUTPUT_INVALID"),
    ),
)
def test_folder_selector_has_stable_platform_exit_and_output_errors(
    tmp_path: Path, platform_name: str, completed, code: str
) -> None:
    root = _selector_root(tmp_path)

    def run(*_args, **_kwargs):
        assert completed is not None
        return completed

    with pytest.raises(FolderSelectionError) as caught:
        select_portable_folder(
            root,
            platform_name=platform_name,
            run=run,
            executable_resolver=lambda _name: _fake_powershell(tmp_path),
        )
    assert caught.value.code == code
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("case", ("raw", "duplicate-path", "duplicate-cancelled", "cancelled-extra", "path-extra", "empty"))
def test_folder_selector_accepts_only_one_strict_json_object(
    tmp_path: Path, case: str
) -> None:
    root = _selector_root(tmp_path)
    selected = tmp_path / "selected"
    selected.mkdir()
    encoded_path = json.dumps(str(selected))
    outputs = {
        "raw": str(selected).encode("utf-8"),
        "duplicate-path": f'{{"selected_path":{encoded_path},"selected_path":{encoded_path}}}'.encode("utf-8"),
        "duplicate-cancelled": b'{"cancelled":true,"cancelled":true}',
        "cancelled-extra": b'{"cancelled":true,"extra":false}',
        "path-extra": f'{{"selected_path":{encoded_path},"extra":false}}'.encode("utf-8"),
        "empty": b"",
    }

    with pytest.raises(FolderSelectionError) as caught:
        select_portable_folder(
            root,
            platform_name="nt",
            run=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=outputs[case],
                stderr=b"",
            ),
            executable_resolver=lambda _name: _fake_powershell(tmp_path),
        )

    assert caught.value.code == "LOCAL_CONTROL_FOLDER_OUTPUT_INVALID"


class _SelectorPipe:
    def __init__(self, chunks: list[bytes], *, close_error: bool = False) -> None:
        self.chunks = list(chunks)
        self.close_error = close_error
        self.close_calls = 0

    def read1(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, size: int) -> bytes:
        return self.read1(size)

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise OSError("secret pipe close failure")


class _SelectorProcess:
    def __init__(
        self,
        *,
        poll_result: int | None = None,
        poll_error: bool = False,
        wait_error: bool = False,
        kill_error: bool = False,
        stdout_chunks: list[bytes] | None = None,
        stderr_chunks: list[bytes] | None = None,
        pipe_close_error: bool = False,
    ) -> None:
        self.pid = 4200
        self.returncode = poll_result
        self.poll_error = poll_error
        self.wait_error = wait_error
        self.kill_error = kill_error
        self.poll_calls = 0
        self.wait_calls = 0
        self.kill_calls = 0
        self.stdout = _SelectorPipe(stdout_chunks or [], close_error=pipe_close_error)
        self.stderr = _SelectorPipe(stderr_chunks or [], close_error=pipe_close_error)

    def poll(self) -> int | None:
        self.poll_calls += 1
        if self.poll_error:
            raise OSError("secret poll failure")
        return self.returncode

    def wait(self, timeout=None) -> int:
        self.wait_calls += 1
        if self.wait_error:
            raise OSError("secret wait failure")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("secret selector", timeout)
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_error:
            raise OSError("secret kill failure")
        self.returncode = 1


class _SelectorJob:
    def __init__(self, *, terminate_error: bool = False, close_error: bool = False) -> None:
        self.terminate_error = terminate_error
        self.close_error = close_error
        self.assign_calls = 0
        self.resume_calls = 0
        self.terminate_calls = 0
        self.close_calls = 0

    def assign(self, _process) -> None:
        self.assign_calls += 1

    def resume(self, _process) -> None:
        self.resume_calls += 1

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error:
            raise OSError("secret terminate failure")

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise OSError("secret job close failure")


class _InlineSelectorThread:
    instances: list["_InlineSelectorThread"] = []

    def __init__(self, *, target, args, **_kwargs) -> None:
        self.target = target
        self.args = args
        self.join_calls = 0
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.target(*self.args)

    def join(self, timeout=None) -> None:
        self.join_calls += 1


def _install_fake_selector_runtime(
    monkeypatch: pytest.MonkeyPatch,
    process: _SelectorProcess,
    job: _SelectorJob,
) -> None:
    _InlineSelectorThread.instances = []
    monkeypatch.setattr(local_control.os, "name", "nt")
    monkeypatch.setattr(local_control, "WindowsKillOnCloseJob", lambda: job)
    monkeypatch.setattr(local_control.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(local_control.threading, "Thread", _InlineSelectorThread)


def test_bounded_selector_poll_error_is_structured_and_cleans_every_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _SelectorProcess(poll_error=True)
    job = _SelectorJob()
    _install_fake_selector_runtime(monkeypatch, process, job)

    with pytest.raises(FolderSelectionError) as failure:
        local_control._run_bounded_selector(
            ["selector.exe"],
            cwd=tmp_path,
            env={},
            timeout=1,
            output_limit=1024,
        )

    assert failure.value.code == "LOCAL_CONTROL_FOLDER_FAILED"
    assert "secret" not in str(failure.value)
    assert job.assign_calls == 1
    assert job.resume_calls == 1
    assert job.terminate_calls == 1
    assert job.close_calls == 1
    assert process.stdout.close_calls == 1
    assert process.stderr.close_calls == 1
    assert [thread.join_calls for thread in _InlineSelectorThread.instances] == [1, 1]


def test_bounded_selector_cleanup_continues_when_every_termination_step_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _SelectorProcess(
        poll_error=True,
        wait_error=True,
        kill_error=True,
        pipe_close_error=True,
    )
    job = _SelectorJob(terminate_error=True, close_error=True)
    _install_fake_selector_runtime(monkeypatch, process, job)

    with pytest.raises(FolderSelectionError) as failure:
        local_control._run_bounded_selector(
            ["selector.exe"],
            cwd=tmp_path,
            env={},
            timeout=1,
            output_limit=1024,
        )

    assert failure.value.code == "LOCAL_CONTROL_FOLDER_FAILED"
    assert "secret" not in str(failure.value)
    assert job.terminate_calls == 1
    assert job.close_calls == 1
    assert process.wait_calls >= 1
    assert process.kill_calls == 1
    assert process.stdout.close_calls == 1
    assert process.stderr.close_calls == 1
    assert [thread.join_calls for thread in _InlineSelectorThread.instances] == [1, 1]


@pytest.mark.parametrize(
    ("scenario", "expected_code", "expected_terminate"),
    [
        ("normal", None, 0),
        ("overflow", "LOCAL_CONTROL_FOLDER_OUTPUT_INVALID", 1),
        ("timeout", "LOCAL_CONTROL_FOLDER_TIMEOUT", 1),
    ],
)
def test_bounded_selector_cleanup_is_idempotent_for_every_exit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_code: str | None,
    expected_terminate: int,
) -> None:
    process = _SelectorProcess(
        poll_result=0 if scenario == "normal" else None,
        stdout_chunks=[b"ok"] if scenario == "normal" else ([b"x" * 2048] if scenario == "overflow" else []),
    )
    job = _SelectorJob()
    _install_fake_selector_runtime(monkeypatch, process, job)

    if expected_code is None:
        completed = local_control._run_bounded_selector(
            ["selector.exe"], cwd=tmp_path, env={}, timeout=1, output_limit=1024
        )
        assert completed.returncode == 0
    else:
        with pytest.raises(FolderSelectionError) as failure:
            local_control._run_bounded_selector(
                ["selector.exe"],
                cwd=tmp_path,
                env={},
                timeout=0.001 if scenario == "timeout" else 1,
                output_limit=1024,
            )
        assert failure.value.code == expected_code

    assert job.terminate_calls == expected_terminate
    assert job.close_calls == 1
    assert process.stdout.close_calls == 1
    assert process.stderr.close_calls == 1
    assert [thread.join_calls for thread in _InlineSelectorThread.instances] == [1, 1]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree control is platform-specific")
@pytest.mark.parametrize("stream", ("stdout", "stderr"))
def test_bounded_selector_terminates_immediately_when_either_stream_overflows(
    tmp_path: Path, stream: str
) -> None:
    target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
    command = [
        sys.executable,
        "-c",
        f"import sys,time; {target}.write(b'x'*4096); {target}.flush(); time.sleep(30)",
    ]
    started = time.monotonic()

    with pytest.raises(FolderSelectionError) as caught:
        local_control._run_bounded_selector(
            command,
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=10,
            output_limit=1024,
        )

    assert caught.value.code == "LOCAL_CONTROL_FOLDER_OUTPUT_INVALID"
    assert time.monotonic() - started < 5


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree control is platform-specific")
def test_bounded_selector_returns_both_bounded_streams_without_communicate(tmp_path: Path) -> None:
    completed = local_control._run_bounded_selector(
        [
            sys.executable,
            "-c",
            "import sys;sys.stdout.buffer.write(b'out');sys.stderr.buffer.write(b'err')",
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=5,
        output_limit=1024,
    )

    assert completed.returncode == 0
    assert completed.stdout == b"out"
    assert completed.stderr == b"err"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree control is platform-specific")
def test_bounded_selector_timeout_kills_descendant_process_tree(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid),encoding='ascii');"
        "time.sleep(30)"
    )

    with pytest.raises(FolderSelectionError) as caught:
        local_control._run_bounded_selector(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=1,
            output_limit=1024,
        )

    assert caught.value.code == "LOCAL_CONTROL_FOLDER_TIMEOUT"
    assert child_pid_file.is_file()
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _windows_process_is_running(child_pid):
        time.sleep(0.05)
    assert not _windows_process_is_running(child_pid)


def _windows_process_is_running(pid: int) -> bool:
    ctypes = __import__("ctypes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    exit_code = ctypes.c_uint32()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def test_folder_selector_reports_timeout_without_echoing_process_output(tmp_path: Path) -> None:
    root = _selector_root(tmp_path)

    def run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("secret command", 120, output=b"C:/private")

    with pytest.raises(FolderSelectionError) as caught:
        select_portable_folder(
            root,
            platform_name="nt",
            run=run,
            executable_resolver=lambda _name: _fake_powershell(tmp_path),
        )

    assert caught.value.code == "LOCAL_CONTROL_FOLDER_TIMEOUT"
    assert "private" not in str(caught.value)


def test_folder_selector_detects_script_identity_change_during_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _selector_root(tmp_path)
    script = root / "scripts/select-portable-folder.ps1"
    from contextlib import nullcontext

    monkeypatch.setattr("app.local_control._selector_identity_guard", lambda *_args: nullcontext())

    def run(*_args, **_kwargs):
        script.write_text("# replaced selector with different length\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with pytest.raises(FolderSelectionError) as caught:
        select_portable_folder(
            root,
            platform_name="nt",
            run=run,
            executable_resolver=lambda _name: _fake_powershell(tmp_path),
        )
    assert caught.value.code == "LOCAL_CONTROL_FOLDER_IDENTITY_CHANGED"


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode guard is platform-specific")
def test_folder_selector_holds_script_against_write_or_replacement_during_execution(
    tmp_path: Path,
) -> None:
    root = _selector_root(tmp_path)
    script = root / "scripts/select-portable-folder.ps1"
    selected = tmp_path / "selected"
    selected.mkdir()
    blocked: list[bool] = []

    def run(*_args, **_kwargs):
        try:
            script.write_text("# attacker replacement\n", encoding="utf-8")
        except PermissionError:
            blocked.append(True)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"selected_path": str(selected)}).encode("utf-8"),
            stderr=b"",
        )

    result = select_portable_folder(
        root,
        platform_name="nt",
        run=run,
        executable_resolver=lambda _name: Path(
            "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        ),
    )

    assert result == selected.resolve()
    assert blocked == [True]


def test_selector_script_and_package_builder_keep_fixed_selector_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/select-portable-folder.ps1").read_text(encoding="utf-8")
    builder = (root / "Build-Package.ps1").read_text(encoding="utf-8")

    assert "FolderBrowserDialog" in script
    assert "Invoke-Expression" not in script
    assert "Start-Process" not in script
    assert "select-portable-folder.ps1" in builder
    assert builder.count('"select-portable-folder.ps1"') == 1


def test_openapi_documents_local_routes_without_secret_examples(tmp_path: Path) -> None:
    client = _client(tmp_path / "TTS More")
    schema = client.get("/openapi.json").json()
    assert "/api/local-control/token" in schema["paths"]
    assert "/api/local-portable-services/{component}/{action}" in schema["paths"]
    assert "example" not in json.dumps(schema["paths"]["/api/local-control/token"]).casefold()
