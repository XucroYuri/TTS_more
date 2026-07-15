from __future__ import annotations

import json
import os
import socket
import subprocess
import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.local_control as local_control
import app.main as main_module
from app.local_control import FolderSelectionError, select_portable_folder
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
            "default_url": "http://127.0.0.1:9880",
            "port": 9880,
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

    def start(self, endpoint, *, operation_id=None):
        self.calls.append(("start", endpoint, operation_id))
        return {"status": "starting", "operation_id": operation_id}

    def stop(self, endpoint):
        self.calls.append(("stop", endpoint, None))
        return {"status": "stopping"}

    def repair(self, endpoint):
        self.calls.append(("repair", endpoint, None))
        return {"status": "repairing"}

    def open_folder(self, endpoint):
        self.calls.append(("open-folder", endpoint, None))
        return {"status": "opened"}

    def status(self, endpoint, *, operation_id=None):
        self.calls.append(("status", endpoint, operation_id))
        return {"status": "ready", "operation": {"operation_id": operation_id}}

    def logs(self, endpoint, *, operation_id=None, after_seq=0, lines=200):
        self.calls.append(("logs", endpoint, operation_id))
        return {"status": "ready", "events": [], "next_seq": after_seq}


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
        executable_resolver=lambda _name: Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
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
            executable_resolver=lambda _name: Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
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
            executable_resolver=lambda _name: Path(
                "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
            ),
        )

    assert caught.value.code == "LOCAL_CONTROL_FOLDER_OUTPUT_INVALID"


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
            executable_resolver=lambda _name: Path(
                "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
            ),
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
            executable_resolver=lambda _name: Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
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
