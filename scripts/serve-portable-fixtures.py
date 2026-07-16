from __future__ import annotations

import argparse
import json
import re
import signal
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class PortableFixtureServer:
    """Serve immutable test assets on a random IPv4 loopback port.

    This server is intentionally test-only.  The first ordinary request can be
    shortened to ``interrupt_after`` bytes so the real portable downloader
    leaves a resumable ``.partial`` file.  Subsequent Range requests receive a
    strict 206 response.  Query modes add deterministic proxy and hash-failure
    scenarios without changing the source fixture.
    """

    host = "127.0.0.1"

    def __init__(
        self,
        root: Path,
        *,
        interrupt_after: int | None = None,
        request_log: Path | None = None,
    ) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)
        if interrupt_after is not None and interrupt_after <= 0:
            raise ValueError("interrupt_after must be positive")
        self.interrupt_after = interrupt_after
        self.request_log = Path(request_log).resolve(strict=False) if request_log else None
        self.requests: list[dict[str, Any]] = []
        self._requests_lock = threading.Lock()
        self._interrupted_paths: set[str] = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PortableFixture/1"
            sys_version = ""

            def do_GET(self) -> None:  # noqa: N802 - HTTP method name
                owner._handle(self)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self._server = _LoopbackHTTPServer((self.host, 0), Handler)
        bound_host, bound_port = self._server.server_address[:2]
        if bound_host != self.host:
            self._server.server_close()
            raise RuntimeError("fixture server did not bind IPv4 loopback")
        self.port = int(bound_port)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"portable-fixture-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def __enter__(self) -> "PortableFixtureServer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()

    def url(self, relative: str, *, mode: str | None = None) -> str:
        encoded = urllib.parse.quote(relative.replace("\\", "/"), safe="/%")
        query = "" if mode is None else "?" + urllib.parse.urlencode({"mode": mode})
        return f"http://{self.host}:{self.port}/{encoded.lstrip('/')}{query}"

    def _record(self, **values: Any) -> None:
        with self._requests_lock:
            self.requests.append(values)
            if self.request_log is not None:
                self.request_log.parent.mkdir(parents=True, exist_ok=True)
                with self.request_log.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(values, ensure_ascii=False, sort_keys=True) + "\n")

    def _resolve(self, raw_path: str) -> tuple[str, Path] | None:
        decoded = urllib.parse.unquote(raw_path)
        relative = Path(decoded.lstrip("/"))
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return None
        candidate = (self.root / relative).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        if not candidate.is_file() or candidate.is_symlink():
            return None
        return relative.as_posix(), candidate

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        resolved = self._resolve(parsed.path)
        range_header = handler.headers.get("Range")
        if resolved is None:
            self._send(handler, 404, b"")
            self._record(path="", range=range_header, status=404, content_range=None)
            return
        relative, path = resolved
        mode = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("mode", [""])[0]
        original = path.read_bytes()
        if mode == "proxy-failure":
            self._send(handler, 503, b"")
            self._record(path=relative, range=range_header, status=503, content_range=None)
            return
        if mode not in {"", "corrupt"}:
            self._send(handler, 400, b"")
            self._record(path=relative, range=range_header, status=400, content_range=None)
            return
        payload = original
        if mode == "corrupt" and payload:
            payload = bytes([payload[0] ^ 0xFF]) + payload[1:]

        if range_header is not None:
            match = _RANGE.fullmatch(range_header)
            if match is None:
                self._send(handler, 400, b"")
                self._record(path=relative, range=range_header, status=400, content_range=None)
                return
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else len(payload) - 1
            if start >= len(payload):
                self._send(handler, 416, b"", {"Content-Range": f"bytes */{len(payload)}"})
                self._record(path=relative, range=range_header, status=416, content_range=f"bytes */{len(payload)}")
                return
            if end < start or end >= len(payload):
                self._send(handler, 416, b"", {"Content-Range": f"bytes */{len(payload)}"})
                self._record(path=relative, range=range_header, status=416, content_range=f"bytes */{len(payload)}")
                return
            content_range = f"bytes {start}-{end}/{len(payload)}"
            body = payload[start : end + 1]
            self._send(handler, 206, body, {"Content-Range": content_range})
            self._record(path=relative, range=range_header, status=206, content_range=content_range)
            return

        should_interrupt = (
            mode == ""
            and self.interrupt_after is not None
            and relative not in self._interrupted_paths
            and self.interrupt_after < len(payload)
        )
        if should_interrupt:
            self._interrupted_paths.add(relative)
            body = payload[: self.interrupt_after]
            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(body)
            handler.wfile.flush()
            handler.close_connection = True
            self._record(path=relative, range=None, status=200, content_range=None, interrupted=True)
            return
        self._send(handler, 200, payload)
        self._record(path=relative, range=None, status=200, content_range=None)

    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if body:
            handler.wfile.write(body)
            handler.wfile.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve deterministic portable first-run fixtures")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--interrupt-after", type=int, default=8)
    parser.add_argument("--request-log", type=Path)
    args = parser.parse_args(argv)
    stopped = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    with PortableFixtureServer(
        args.root,
        interrupt_after=args.interrupt_after,
        request_log=args.request_log,
    ) as server:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(
            json.dumps({"schema_version": 1, "endpoint": f"http://127.0.0.1:{server.port}"}) + "\n",
            encoding="utf-8",
        )
        while not stopped.wait(0.2):
            time.sleep(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
