from __future__ import annotations

import http.client
import ast
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.native_gpu.coordinator import GPUCoordinator
from app.native_gpu.protocol import CoordinationTimeout, CoordinationUnavailable, NativeStatus, ProtocolError
from app.native_gpu.transport import ControlClient, ControlServer, CoordinatorClient, MAX_JSON_BYTES


SECRET = "unit-test-local-secret-000000000000"


def cpu_status():
    return NativeStatus("native-instance", 321, 0, 0, 0, 300, "cpu", True)


def request(server, body, *, headers=None, path="/v1/action", method="POST"):
    endpoint = urlsplit(server.base_url)
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=2)
    try:
        connection.request(method, path, body=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SECRET}",
            **(headers or {}),
        })
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def test_real_loopback_coordinator_roundtrip_without_gpu_or_native_processes():
    coordinator = GPUCoordinator({"gpu": ["native"]})
    with ControlServer(coordinator.dispatch, SECRET) as server:
        client = CoordinatorClient(server.base_url, SECRET, resource_group="gpu", timeout=2)
        assert client.update_native("native", cpu_status())
        comfy = client.acquire_comfy("workflow", 0)
        assert client.check_comfy(comfy)
        assert client.release_comfy(comfy, False)
        assert not client.check_comfy(comfy)
        with pytest.raises(CoordinationTimeout):
            client.acquire_native("native", 0)
        assert client.release_comfy(comfy, True)
        native = client.acquire_native("native", 0)
        assert client.snapshot()["groups"]["gpu"]["native_active"] == 1
        assert client.release_native(native)


def test_generic_native_server_only_accepts_status_and_atomic_offload():
    calls = []

    def dispatch(action, payload):
        calls.append((action, payload))
        return cpu_status().to_dict()

    with ControlServer(dispatch, SECRET, allowed_actions={"status", "offload_if_idle"}) as server:
        client = ControlClient(server.base_url, SECRET)
        assert client.call("status")["instance_id"] == "native-instance"
        result = client.call("offload_if_idle", {"instance_id": "native-instance", "revision": 0})
        assert result["residency"] == "cpu"
        with pytest.raises(ProtocolError):
            client.call("snapshot")
    assert calls == [("status", {}), ("offload_if_idle", {"instance_id": "native-instance", "revision": 0})]


@pytest.mark.parametrize("url", [
    "http://192.168.1.2:8080", "http://0.0.0.0:8080", "http://localhost:8080",
    "https://127.0.0.1:8080", "http://user:pass@127.0.0.1:8080",
    "http://127.0.0.1:8080/path", "http://127.0.0.1:8080?token=x",
    "http://127.0.0.1:8080#fragment", "http://[::1%eth0]:8080", "http://127.0.0.1:0",
])
def test_client_rejects_nonlocal_or_ambiguous_endpoints_before_connecting(url):
    with pytest.raises(ProtocolError):
        ControlClient(url, SECRET)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "localhost", "::", "::1%scope"])
def test_server_cannot_bind_non_loopback_or_dns_hostnames(host):
    with pytest.raises(ProtocolError):
        ControlServer(lambda *args: {}, SECRET, host=host)


def test_authentication_is_required_before_dispatch_and_private_errors_are_sanitized():
    calls = []

    def dispatch(*args):
        calls.append(args)
        raise RuntimeError("private path or credential")

    with ControlServer(dispatch, SECRET) as server:
        for credential in ("", "Bearer wrong-secret", "Bearer é"):
            status, document = request(server, b'{"action":"status","payload":{}}', headers={"Authorization": credential})
            assert status == 401 and document == {"error": {"code": "unauthorized"}}
        assert calls == []
        status, document = request(server, b'{"action":"status","payload":{}}')
        assert status == 500 and "private" not in str(document)
        with pytest.raises(CoordinationUnavailable, match="rejected"):
            ControlClient(server.base_url, SECRET).call("status")


def test_repeated_invalid_auth_with_sent_body_receives_complete_401_without_dispatch():
    calls = []
    with ControlServer(lambda *args: calls.append(args) or {}, SECRET) as server:
        for index in range(50):
            body = json.dumps({"action": "status", "payload": {}, "padding": "x" * (index * 257)}).encode()
            status, document = request(server, body, headers={"Authorization": "Bearer invalid-secret"})
            assert status == 401 and document == {"error": {"code": "unauthorized"}}
        assert calls == []
        assert ControlClient(server.base_url, SECRET).call("status") == {}


def test_invalid_auth_oversized_declared_body_is_not_drained_or_dispatched():
    calls = []
    with ControlServer(lambda *args: calls.append(args) or {}, SECRET, request_timeout=1) as server:
        endpoint = urlsplit(server.base_url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=1)
        try:
            started = time.monotonic()
            connection.request("POST", "/v1/action", body=b"", headers={
                "Content-Length": str(MAX_JSON_BYTES + 1),
                "Authorization": "Bearer invalid-secret", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            assert response.status == 401
            assert json.loads(response.read()) == {"error": {"code": "unauthorized"}}
            assert time.monotonic() - started < 0.8
            assert calls == []
        finally:
            connection.close()


def test_incomplete_unauthenticated_body_times_out_and_releases_worker_slot():
    calls = []
    with ControlServer(lambda *args: calls.append(args) or {}, SECRET, request_timeout=0.15, max_workers=1) as server:
        endpoint = urlsplit(server.base_url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=1)
        try:
            started = time.monotonic()
            connection.request("POST", "/v1/action", body=b"", headers={
                "Content-Length": "100", "Authorization": "Bearer invalid-secret",
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            assert response.status == 401 and json.loads(response.read())["error"]["code"] == "unauthorized"
            assert time.monotonic() - started < 0.8
            assert calls == []
        finally:
            connection.close()
        # Response receipt can precede the handler's finally by a few scheduler
        # ticks. Poll only our own fixture until its bounded slot is returned.
        deadline = time.monotonic() + 1
        while True:
            try:
                assert ControlClient(server.base_url, SECRET, timeout=0.5).call("status") == {}
                break
            except CoordinationUnavailable:
                if time.monotonic() >= deadline:
                    raise
                threading.Event().wait(0.005)
        assert len(calls) == 1


@pytest.mark.parametrize("body", [
    b'{"action":"evaluate","payload":{"expression":"print(1)"}}',
    b'{"action":"status","payload":{"method":"__class__"}}',
    b'{"action":"status","action":"status","payload":{}}',
    b'{"action":"acquire_native","payload":{"participant":"native","timeout":NaN}}',
    b'{"action":"release_comfy","payload":{"token":"x","clean":"true"}}',
    b'{"action":"status","payload":{},"extra":1}',
    b'[]', b'\xff', b'{',
])
def test_wire_schema_and_json_reject_arbitrary_actions_duplicates_and_nonfinite_values(body):
    calls = []
    with ControlServer(lambda *args: calls.append(args) or {}, SECRET) as server:
        status, _document = request(server, body)
        assert status == 400
    assert calls == []


def test_request_sizes_content_type_paths_and_methods_are_bounded():
    with ControlServer(lambda *args: {}, SECRET) as server:
        assert request(server, b"{}", headers={"Content-Length": str(MAX_JSON_BYTES + 1)})[0] == 413
        assert request(server, b"{}", headers={"Content-Type": "text/plain"})[0] == 415
        assert request(server, b"{}", path="/other")[0] == 404
        assert request(server, None, method="GET")[0] == 405
        with pytest.raises(ProtocolError):
            ControlClient(server.base_url, SECRET).call("acquire_comfy", {"holder": "workflow", "timeout": 121})


def test_client_does_not_use_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with ControlServer(lambda *args: {"healthy": True}, SECRET) as server:
        assert ControlClient(server.base_url, SECRET).call("status") == {"healthy": True}


def test_client_does_not_follow_redirect_even_to_loopback():
    seen = []

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/followed")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        with pytest.raises(CoordinationUnavailable):
            ControlClient(f"http://127.0.0.1:{server.server_address[1]}", SECRET).call("status")
    finally:
        server.shutdown()
        worker.join(2)
        server.server_close()
    assert seen == ["/v1/action"]


def test_oversize_or_invalid_dispatch_response_is_not_exposed():
    with ControlServer(lambda *args: {"data": "x" * MAX_JSON_BYTES}, SECRET) as server:
        with pytest.raises(CoordinationUnavailable):
            ControlClient(server.base_url, SECRET).call("status")
    with ControlServer(lambda *args: {"value": float("nan")}, SECRET) as server:
        with pytest.raises(CoordinationUnavailable):
            ControlClient(server.base_url, SECRET).call("status")


def test_client_validates_lease_and_boolean_response_types():
    with ControlServer(lambda *args: {"token": 123, "valid": "yes"}, SECRET) as server:
        client = CoordinatorClient(server.base_url, SECRET)
        with pytest.raises(CoordinationUnavailable):
            client.acquire_comfy("workflow", 0)
        with pytest.raises(CoordinationUnavailable):
            client.check_comfy("token")


def test_closed_server_and_weak_control_tokens_are_rejected():
    with pytest.raises(ProtocolError):
        ControlClient("http://127.0.0.1:1", "short")
    server = ControlServer(lambda *args: {}, SECRET)
    server.close()
    with pytest.raises(CoordinationUnavailable):
        server.start()


def test_ipv6_loopback_uses_the_same_local_authenticated_contract():
    try:
        server = ControlServer(lambda *args: {"local": True}, SECRET, host="::1")
    except OSError as exc:
        if exc.errno in {socket.EAFNOSUPPORT if hasattr(socket, "EAFNOSUPPORT") else 97, 97, 10047}:
            pytest.skip("IPv6 is unavailable on this host")
        raise
    with server:
        assert server.base_url.startswith("http://[::1]:")
        assert ControlClient(server.base_url, SECRET).call("status") == {"local": True}


@pytest.mark.parametrize("length", ["-1", "²", "9" * 20])
def test_malformed_content_length_does_not_reach_dispatch(length):
    calls = []
    with ControlServer(lambda *args: calls.append(args) or {}, SECRET) as server:
        assert request(server, b"{}", headers={"Content-Length": length})[0] == 400
    assert calls == []


def test_shared_control_modules_use_only_python_310_compatible_syntax():
    directory = Path(__file__).parents[1] / "app" / "native_gpu"
    for name in ("protocol.py", "coordinator.py", "transport.py", "journal.py"):
        source = (directory / name).read_text(encoding="utf-8")
        ast.parse(source, filename=name, feature_version=(3, 10))
