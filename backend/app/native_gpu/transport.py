"""Authenticated loopback JSON transport without proxies or redirects."""

from __future__ import annotations

import hmac
import ipaddress
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .protocol import (
    ACTION_FIELDS, CoordinationError, CoordinationTimeout, CoordinationUnavailable,
    LeaseDenied, NativeStatus, ProtocolError, finite_seconds, identifier, validate_action,
)

MAX_JSON_BYTES = 65536


def _loopback_address(host: str) -> str:
    if not isinstance(host, str) or "%" in host:
        raise ProtocolError("control endpoint must use a literal loopback IP address")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ProtocolError("control endpoint must use a literal loopback IP address") from exc
    if not address.is_loopback:
        raise ProtocolError("control endpoint must use a loopback IP address")
    return str(address)


def _endpoint(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
            raise ProtocolError("control endpoint must be plain HTTP without URL credentials")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ProtocolError("control endpoint cannot contain a path, query, or fragment")
        if parsed.hostname is None:
            raise ProtocolError("control endpoint hostname is missing")
        host = _loopback_address(parsed.hostname)
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ProtocolError("invalid control endpoint port")
    except (ValueError, TypeError) as exc:
        raise ProtocolError("invalid control endpoint URL") from exc
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return urlunsplit(("http", authority, "/v1/action", "", ""))


def _auth_secret(token: str) -> str:
    identifier(token, "control token")
    if len(token) < 16 or any(not (char.isascii() and (char.isalnum() or char in "._~+/=-")) for char in token):
        raise ProtocolError("control token must contain at least 16 printable ASCII token characters")
    return token


def _json(data: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError("duplicate JSON fields are not allowed")
            result[key] = value
        return result

    def constant(_value):
        raise ProtocolError("non-finite JSON values are not allowed")

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("invalid UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise ProtocolError("JSON document must be an object")
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ControlClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self._url = _endpoint(base_url)
        self._token = _auth_secret(token)
        self.timeout = finite_seconds(timeout, "timeout", maximum=125)
        if not self.timeout:
            raise ProtocolError("HTTP timeout must be positive")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        action, payload = validate_action(action, {} if payload is None else payload)
        try:
            content = json.dumps({"action": action, "payload": payload}, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as exc:
            raise ProtocolError("control payload is not JSON-safe") from exc
        if len(content) > MAX_JSON_BYTES:
            raise ProtocolError("control request exceeds the JSON size limit")
        request = Request(self._url, data=content, method="POST", headers={
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        timeout = max(self.timeout, float(payload.get("timeout", 0)) + 2)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise CoordinationUnavailable("control endpoint returned an unexpected status")
                raw = response.read(MAX_JSON_BYTES + 1)
                if len(raw) > MAX_JSON_BYTES:
                    raise CoordinationUnavailable("control response exceeds the JSON size limit")
                return _json(raw)
        except HTTPError as exc:
            try:
                document = _json(exc.read(MAX_JSON_BYTES + 1))
                code = (document.get("error") or {}).get("code")
            except (ProtocolError, AttributeError):
                code = None
            if code == CoordinationTimeout.code:
                raise CoordinationTimeout("GPU admission timed out") from None
            if code == LeaseDenied.code:
                raise LeaseDenied("GPU admission was denied") from None
            if code == ProtocolError.code:
                raise ProtocolError("control request was rejected") from None
            raise CoordinationUnavailable("control endpoint rejected the operation") from None
        except (URLError, OSError, TimeoutError) as exc:
            raise CoordinationUnavailable("control endpoint is unavailable") from None


class CoordinatorClient(ControlClient):
    def __init__(self, base_url: str, token: str, *, resource_group: str | None = None, timeout: float = 10.0) -> None:
        super().__init__(base_url, token, timeout=timeout)
        if resource_group is not None:
            identifier(resource_group, "resource_group")
        self.resource_group = resource_group

    @staticmethod
    def _token_result(result: dict[str, Any]) -> str:
        try:
            return identifier(result["token"], "lease token")
        except (KeyError, ProtocolError) as exc:
            raise CoordinationUnavailable("control response did not include a lease token") from None

    @staticmethod
    def _bool_result(result: dict[str, Any], key: str) -> bool:
        value = result.get(key)
        if not isinstance(value, bool):
            raise CoordinationUnavailable("control response does not match its contract")
        return value

    def acquire_native(self, participant: str, timeout: float = 30.0) -> str:
        return self._token_result(self.call("acquire_native", {"participant": participant, "timeout": timeout}))

    def release_native(self, token: str) -> bool:
        return self._bool_result(self.call("release_native", {"token": token}), "released")

    def acquire_comfy(self, holder: str, timeout: float = 30.0) -> str:
        payload: dict[str, Any] = {"holder": holder, "timeout": timeout}
        if self.resource_group is not None:
            payload["resource_group"] = self.resource_group
        return self._token_result(self.call("acquire_comfy", payload))

    def check_comfy(self, token: str) -> bool:
        return self._bool_result(self.call("check_comfy", {"token": token}), "valid")

    def release_comfy(self, token: str, clean: bool) -> bool:
        return self._bool_result(self.call("release_comfy", {"token": token, "clean": clean}), "released")

    def update_native(self, participant: str, status: NativeStatus | dict[str, Any]) -> bool:
        value = status.to_dict() if isinstance(status, NativeStatus) else status
        return self._bool_result(self.call("update_native", {"participant": participant, "status": value}), "updated")

    def snapshot(self) -> dict[str, Any]:
        payload = {"resource_group": self.resource_group} if self.resource_group else {}
        return self.call("snapshot", payload)


class ControlServer:
    """A bounded local server for a fixed dispatch callable.

    Native guards generally enable only ``status`` and ``offload_if_idle``;
    coordinators enable their lease actions. No code or method names from a
    request are evaluated. The caller owns startup and shutdown of this server.
    """

    def __init__(
        self,
        dispatch: Callable[[str, dict[str, Any]], dict[str, Any]],
        token: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        allowed_actions: Iterable[str] | None = None,
        request_timeout: float = 10.0,
        max_workers: int = 16,
    ) -> None:
        host = _loopback_address(host)
        token = _auth_secret(token)
        if not callable(dispatch):
            raise ProtocolError("dispatch must be callable")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ProtocolError("invalid control server port")
        actions = set(ACTION_FIELDS if allowed_actions is None else allowed_actions)
        if not actions or actions - set(ACTION_FIELDS):
            raise ProtocolError("control server action allowlist is invalid")
        request_timeout = finite_seconds(request_timeout, "request_timeout", maximum=125)
        if not request_timeout or isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 64:
            raise ProtocolError("invalid control server resource limit")
        worker_slots = threading.BoundedSemaphore(max_workers)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(request_timeout)

            def log_message(self, format, *args):
                pass

            def _reply(self, status: int, document: dict[str, Any]) -> None:
                try:
                    data = json.dumps(document, allow_nan=False, separators=(",", ":")).encode("utf-8")
                except (ValueError, TypeError, RecursionError):
                    status, data = 500, b'{"error":{"code":"coordination_unavailable"}}'
                if len(data) > MAX_JSON_BYTES:
                    status, data = 500, b'{"error":{"code":"coordination_unavailable"}}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _error(self, status: int, code: str) -> None:
                self._reply(status, {"error": {"code": code}})

            def _reject(self, status: int, code: str) -> None:
                # Closing a socket with an unread, already-sent request body can
                # reset it on Windows before the client receives our response.
                # Discard only a bounded, explicitly framed body. This does not
                # parse JSON or authorize dispatch, and slow senders cannot keep
                # a worker beyond the single overall request timeout.
                lengths = self.headers.get_all("Content-Length", [])
                if (self.headers.get("Transfer-Encoding") is None and len(lengths) == 1
                        and len(lengths[0]) <= 10 and lengths[0].isascii() and lengths[0].isdigit()):
                    remaining = int(lengths[0])
                    if remaining <= MAX_JSON_BYTES:
                        deadline = time.monotonic() + request_timeout
                        try:
                            while remaining:
                                time_left = deadline - time.monotonic()
                                if time_left <= 0:
                                    break
                                self.connection.settimeout(time_left)
                                chunk = self.rfile.read1(min(4096, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                        except OSError:
                            pass
                        finally:
                            self.connection.settimeout(request_timeout)
                self._error(status, code)

            def do_POST(self):
                try:
                    _loopback_address(self.client_address[0])
                except ProtocolError:
                    return self._reject(403, "forbidden")
                if self.path != "/v1/action":
                    return self._reject(404, "not_found")
                credentials = self.headers.get_all("Authorization", [])
                if len(credentials) != 1 or not hmac.compare_digest(credentials[0].encode("utf-8"), f"Bearer {token}".encode("ascii")):
                    return self._reject(401, "unauthorized")
                if self.headers.get("Transfer-Encoding") is not None:
                    return self._reject(400, ProtocolError.code)
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or len(lengths[0]) > 10 or not lengths[0].isascii() or not lengths[0].isdigit():
                    return self._reject(400, ProtocolError.code)
                size = int(lengths[0])
                if size > MAX_JSON_BYTES:
                    return self._reject(413, "request_too_large")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return self._reject(415, "unsupported_media_type")
                try:
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ProtocolError("incomplete JSON body")
                    document = _json(raw)
                    if set(document) != {"action", "payload"}:
                        raise ProtocolError("invalid control document")
                    action, payload = validate_action(document["action"], document["payload"])
                    if action not in actions:
                        raise ProtocolError("action is disabled")
                    result = dispatch(action, payload)
                    if not isinstance(result, dict):
                        raise CoordinationUnavailable("invalid dispatcher response")
                    self._reply(200, result)
                except CoordinationTimeout:
                    self._error(408, CoordinationTimeout.code)
                except LeaseDenied:
                    self._error(409, LeaseDenied.code)
                except ProtocolError:
                    self._error(400, ProtocolError.code)
                except (socket.timeout, TimeoutError):
                    self._error(408, CoordinationUnavailable.code)
                except Exception:
                    self._error(500, CoordinationUnavailable.code)

            def do_GET(self):
                self._error(405, "method_not_allowed")

        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
            daemon_threads = True
            block_on_close = False

            def process_request(self, request, client_address):
                if not worker_slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    worker_slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    worker_slots.release()

        self._server = Server((host, port), Handler)
        self._thread: threading.Thread | None = None
        self._closed = False
        address = self._server.server_address
        authority = f"[{address[0]}]:{address[1]}" if ":" in address[0] else f"{address[0]}:{address[1]}"
        self.base_url = f"http://{authority}"

    def start(self) -> "ControlServer":
        if self._closed:
            raise CoordinationUnavailable("control server is closed")
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="native-gpu-control", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._thread is not None:
                self._server.shutdown()
                self._thread.join(timeout=2)
            self._server.server_close()

    def __enter__(self) -> "ControlServer":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False
