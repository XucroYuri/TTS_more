from __future__ import annotations

import ctypes
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator, Literal, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import PortableComponent, PortableServiceLocator, TTSServiceEndpoint
from app.portable_discovery import (
    PortablePackageRegisterRequest,
    endpoint_from_portable_package,
    inspect_locator_candidate,
)
from app.portable_services import (
    PortableServiceStore,
    discover_bounded_portable_packages,
    resolve_locator,
)


CONTROL_HEADER = "X-TTS-More-Control"
MAX_LOCAL_CONTROL_BODY_BYTES = 64 * 1024
MAX_FOLDER_OUTPUT_BYTES = 16 * 1024
FOLDER_SELECTOR_TIMEOUT_SECONDS = 120
MAX_OPERATION_EVENTS = 500
_IDENTITY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_INTEGER_RE = re.compile(r"0|[1-9][0-9]*\Z")
_ERROR_MESSAGES = {
    "LOCAL_CONTROL_FOLDER_UNSUPPORTED": "folder selection is unavailable",
    "LOCAL_CONTROL_FOLDER_MISSING": "folder selector is unavailable",
    "LOCAL_CONTROL_FOLDER_UNSAFE": "folder selector failed its integrity check",
    "LOCAL_CONTROL_FOLDER_IDENTITY_CHANGED": "folder selector changed during execution",
    "LOCAL_CONTROL_FOLDER_TIMEOUT": "folder selection timed out",
    "LOCAL_CONTROL_FOLDER_FAILED": "folder selection failed",
    "LOCAL_CONTROL_FOLDER_OUTPUT_INVALID": "folder selector returned invalid output",
}


class FolderSelectionError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or _ERROR_MESSAGES.get(code, "folder selection failed"))


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LocalDiscoverRequest(_StrictRequest):
    roots: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("roots")
    @classmethod
    def validate_roots(cls, roots: list[str]) -> list[str]:
        return [_strict_absolute_path(value, "roots") for value in roots]


class LocalRegisterRequest(_StrictRequest):
    component: PortableComponent
    package_id: str
    path: str
    port_override: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("package_id")
    @classmethod
    def validate_package_id(cls, value: str) -> str:
        return _strict_identity(value, "package_id")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _strict_absolute_path(value, "path")


class LocalSelectFolderRequest(_StrictRequest):
    component: PortableComponent
    package_id: str | None = None

    @field_validator("package_id")
    @classmethod
    def validate_package_id(cls, value: str | None) -> str | None:
        return None if value is None else _strict_identity(value, "package_id")


class LocalActionRequest(_StrictRequest):
    port_override: int | None = Field(default=None, ge=1, le=65535)


RefreshServices = Callable[[Sequence[TTSServiceEndpoint]], None]


def install_local_control(
    app: FastAPI,
    *,
    controller_root: Path,
    refresh_services: RefreshServices,
) -> None:
    """Install the process-private, loopback-only portable control surface."""

    root = Path(os.path.abspath(controller_root))
    token = secrets.token_urlsafe(32)
    app.state.local_control_token = token
    app.state.local_control_root = root

    @app.middleware("http")
    async def bound_local_control_body(request: Request, call_next):
        if _is_local_control_path(request.url.path):
            raw_length = request.headers.get("content-length")
            if raw_length:
                try:
                    if int(raw_length) > MAX_LOCAL_CONTROL_BODY_BYTES:
                        return _error_response(
                            413,
                            "LOCAL_CONTROL_REQUEST_TOO_LARGE",
                            "request body is too large",
                        )
                except ValueError:
                    return _error_response(
                        400,
                        "LOCAL_CONTROL_INVALID_REQUEST",
                        "request validation failed",
                    )
            body = await request.body()
            if len(body) > MAX_LOCAL_CONTROL_BODY_BYTES:
                return _error_response(
                    413,
                    "LOCAL_CONTROL_REQUEST_TOO_LARGE",
                    "request body is too large",
                )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def local_validation_error(request: Request, exc: RequestValidationError):
        if _is_local_control_path(request.url.path):
            return _error_response(
                422,
                "LOCAL_CONTROL_INVALID_REQUEST",
                "request validation failed",
            )
        return await request_validation_exception_handler(request, exc)

    def require_token(request: Request) -> None:
        _require_local_request(request, token=token)

    def current_store() -> PortableServiceStore:
        return PortableServiceStore(root)

    def current_services() -> list[TTSServiceEndpoint]:
        store = current_store()
        try:
            if store.path.exists():
                return store.load()
            registry = getattr(app.state, "service_registry", None)
            return list(registry.services) if registry is not None else []
        except (OSError, ValueError, json.JSONDecodeError):
            _raise_error(
                409,
                "LOCAL_CONTROL_STORE_INVALID",
                "local portable service settings are unavailable",
            )

    def managed_endpoint(component: PortableComponent) -> TTSServiceEndpoint:
        endpoints = [
            endpoint
            for endpoint in current_services()
            if endpoint.portable_locator is not None
            and endpoint.portable_locator.component == component
        ]
        if len(endpoints) != 1:
            _raise_error(
                409,
                "LOCAL_CONTROL_NOT_MANAGEABLE",
                "portable service is not locally manageable",
            )
        endpoint = endpoints[0]
        locator = endpoint.portable_locator
        assert locator is not None
        descriptor = resolve_locator(root, locator, [])
        if descriptor is None or not endpoint.managed:
            _raise_error(
                409,
                "LOCAL_CONTROL_NOT_MANAGEABLE",
                "portable service is not locally manageable",
            )
        return endpoint

    @app.get(
        "/api/local-control/token",
        summary="Create the in-process loopback control capability",
        description="Available only to a true loopback client using a loopback Host and HTTP(S) loopback Origin.",
    )
    def local_control_token(request: Request) -> JSONResponse:
        _require_local_request(request, token=None)
        return JSONResponse(
            {"token": token},
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(
        "/api/local-portable-services",
        summary="List local portable services",
        description="Requires the process-private loopback control capability.",
    )
    def list_local_portable_services(request: Request) -> dict[str, object]:
        require_token(request)
        services = [
            _service_projection(endpoint)
            for endpoint in current_services()
            if endpoint.catalog_provider in {"gpt-sovits", "indextts", "cosyvoice"}
            or endpoint.portable_locator is not None
        ]
        return {"services": services}

    @app.post(
        "/api/local-portable-services/discover",
        summary="Discover bounded local portable packages",
    )
    def discover_local_portable_services(
        payload: LocalDiscoverRequest,
        request: Request,
    ) -> dict[str, object]:
        require_token(request)
        packages = discover_bounded_portable_packages(
            root,
            [Path(item) for item in payload.roots],
        )
        return {"packages": [item.model_dump(mode="json") for item in packages]}

    @app.post(
        "/api/local-portable-services/select-folder",
        summary="Select one portable package folder",
    )
    def select_local_portable_folder(
        payload: LocalSelectFolderRequest,
        request: Request,
    ) -> dict[str, object]:
        require_token(request)
        try:
            selected = select_portable_folder(root)
        except FolderSelectionError as exc:
            status = 501 if exc.code == "LOCAL_CONTROL_FOLDER_UNSUPPORTED" else 409
            _raise_error(status, exc.code, str(exc))
        if selected is None:
            return {"status": "cancelled"}
        descriptor = inspect_locator_candidate(selected)
        if descriptor is None:
            _raise_error(
                400,
                "LOCAL_CONTROL_INVALID_PACKAGE",
                "selected folder is not a complete portable package",
            )
        if descriptor.component != payload.component or (
            payload.package_id is not None and descriptor.package_id != payload.package_id
        ):
            _raise_error(
                409,
                "LOCAL_CONTROL_IDENTITY_MISMATCH",
                "portable package identity does not match the request",
            )
        return {"status": "selected", "package": descriptor.model_dump(mode="json")}

    @app.post(
        "/api/local-portable-services/register",
        summary="Register one freshly validated local portable package",
    )
    def register_local_portable_service(
        payload: LocalRegisterRequest,
        request: Request,
    ) -> dict[str, object]:
        require_token(request)
        descriptor = inspect_locator_candidate(Path(payload.path))
        if descriptor is None:
            _raise_error(
                400,
                "LOCAL_CONTROL_INVALID_PACKAGE",
                "path is not a complete portable package",
            )
        if descriptor.component != payload.component or descriptor.package_id != payload.package_id:
            _raise_error(
                409,
                "LOCAL_CONTROL_IDENTITY_MISMATCH",
                "portable package identity does not match the request",
            )
        endpoint = endpoint_from_portable_package(
            descriptor,
            PortablePackageRegisterRequest(package_root=descriptor.package_root),
        )
        locator = endpoint.portable_locator
        assert locator is not None
        endpoint = endpoint.model_copy(
            update={
                "portable_locator": locator.model_copy(
                    update={
                        "relative_to_tts_more": _relative_sibling(root, Path(descriptor.package_root)),
                        "port_override": payload.port_override,
                    }
                )
            }
        )
        store = current_store()
        try:
            existing = store.load() if store.path.exists() else current_services()
            retained = [
                item
                for item in existing
                if item.portable_locator is None
                or item.portable_locator.component != payload.component
            ]
            if retained != existing or not store.path.exists():
                store.save(retained)
            services = store.upsert(endpoint)
            refresh_services(services)
        except (OSError, ValueError, json.JSONDecodeError):
            _raise_error(
                409,
                "LOCAL_CONTROL_STORE_INVALID",
                "local portable service settings are unavailable",
            )
        stored = next(
            item
            for item in services
            if item.portable_locator is not None
            and item.portable_locator.component == payload.component
            and item.portable_locator.package_id == payload.package_id
        )
        return {
            "package": descriptor.model_dump(mode="json"),
            "service": _service_projection(stored),
        }

    @app.post(
        "/api/local-portable-services/{component}/{action}",
        summary="Run an allowlisted portable lifecycle action",
    )
    def local_portable_action(
        component: PortableComponent,
        action: Literal["start", "stop", "repair", "open-folder"],
        request: Request,
        payload: LocalActionRequest | None = None,
    ) -> dict[str, object]:
        require_token(request)
        payload = payload or LocalActionRequest()
        if action != "start" and payload.port_override is not None:
            _raise_error(
                422,
                "LOCAL_CONTROL_INVALID_REQUEST",
                "request validation failed",
            )
        endpoint = managed_endpoint(component)
        if payload.port_override is not None:
            locator = endpoint.portable_locator
            assert locator is not None
            endpoint = endpoint.model_copy(
                update={
                    "portable_locator": locator.model_copy(
                        update={"port_override": payload.port_override}
                    )
                }
            )
        supervisor = app.state.supervisor
        if action == "start":
            operation_id = str(uuid4())
            result = supervisor.start(endpoint, operation_id=operation_id)
        elif action == "stop":
            result = supervisor.stop(endpoint)
        elif action == "repair":
            result = supervisor.repair(endpoint)
        else:
            result = supervisor.open_folder(endpoint)
        if result.get("status") == "not manageable":
            _raise_error(
                409,
                "LOCAL_CONTROL_NOT_MANAGEABLE",
                "portable service is not locally manageable",
            )
        return {"component": component, "action": action, **result}

    @app.get(
        "/api/local-portable-services/{component}/operations/{operation_id}",
        summary="Read one portable operation",
    )
    def local_portable_operation(
        component: PortableComponent,
        operation_id: str,
        request: Request,
    ) -> dict[str, object]:
        require_token(request)
        operation_id = _operation_id(operation_id)
        endpoint = managed_endpoint(component)
        result = app.state.supervisor.status(endpoint, operation_id=operation_id)
        return _checked_control_result(result)

    @app.get(
        "/api/local-portable-services/{component}/operations/{operation_id}/logs",
        summary="Read a bounded page of portable operation events",
    )
    def local_portable_operation_logs(
        component: PortableComponent,
        operation_id: str,
        request: Request,
        after_seq: str = "0",
        limit: str = "200",
    ) -> dict[str, object]:
        require_token(request)
        operation_id = _operation_id(operation_id)
        after = _bounded_integer(after_seq, minimum=0, maximum=2**31 - 1)
        count = _bounded_integer(limit, minimum=1, maximum=MAX_OPERATION_EVENTS)
        endpoint = managed_endpoint(component)
        result = app.state.supervisor.logs(
            endpoint,
            operation_id=operation_id,
            after_seq=after,
            lines=count,
        )
        return _checked_control_result(result)


def select_portable_folder(
    controller_root: Path,
    *,
    platform_name: str | None = None,
    run: Callable[..., Any] = subprocess.run,
    executable_resolver: Callable[[str], Path] | None = None,
) -> Path | None:
    """Invoke only the fixed package selector and parse one bounded path result."""

    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_UNSUPPORTED")
    root = Path(os.path.abspath(controller_root))
    scripts = root / "scripts"
    selector = scripts / "select-portable-folder.ps1"
    try:
        scripts_identity = _safe_path_identity(scripts, directory=True)
        selector_identity = _safe_path_identity(selector, directory=False)
    except (OSError, ValueError) as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_MISSING") from exc
    resolver = executable_resolver or _windows_system_executable
    powershell = Path(resolver("powershell.exe"))
    if not powershell.is_absolute():
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_UNSAFE")
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(selector.resolve(strict=True)),
    ]
    try:
        with _selector_identity_guard(scripts, selector):
            if _safe_path_identity(scripts, directory=True) != scripts_identity or _safe_path_identity(
                selector, directory=False
            ) != selector_identity:
                raise FolderSelectionError("LOCAL_CONTROL_FOLDER_IDENTITY_CHANGED")
            completed = run(
                command,
                cwd=str(powershell.parent),
                env=_selector_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=FOLDER_SELECTOR_TIMEOUT_SECONDS,
                shell=False,
                check=False,
                creationflags=_no_window_creation_flags(),
            )
    except FolderSelectionError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_TIMEOUT") from exc
    except OSError as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_FAILED") from exc
    try:
        if _safe_path_identity(scripts, directory=True) != scripts_identity or _safe_path_identity(
            selector, directory=False
        ) != selector_identity:
            raise FolderSelectionError("LOCAL_CONTROL_FOLDER_IDENTITY_CHANGED")
    except OSError as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_IDENTITY_CHANGED") from exc
    if completed.returncode != 0:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_FAILED")
    stdout = completed.stdout
    if not isinstance(stdout, bytes) or len(stdout) > MAX_FOLDER_OUTPUT_BYTES:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID")
    try:
        text = stdout.decode("utf-8-sig", errors="strict").strip()
    except UnicodeError as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID") from exc
    if not text:
        return None
    selected: object
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID") from exc
        if payload == {"cancelled": True}:
            return None
        if not isinstance(payload, dict) or set(payload) != {"selected_path"}:
            raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID")
        selected = payload["selected_path"]
    else:
        if "\n" in text or "\r" in text:
            raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID")
        selected = text
    if not isinstance(selected, str):
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID")
    selected_path = Path(_strict_absolute_path(selected, "selected_path"))
    try:
        selected_path = selected_path.resolve(strict=True)
    except OSError as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID") from exc
    if not selected_path.is_dir():
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID")
    return selected_path


def _require_local_request(request: Request, *, token: str | None) -> None:
    client_host = request.client.host if request.client is not None else ""
    if not _loopback_host(client_host):
        _raise_error(403, "LOCAL_CONTROL_FORBIDDEN", "local control is unavailable")
    if not _local_authority(request.headers.get("host", "")):
        _raise_error(403, "LOCAL_CONTROL_FORBIDDEN", "local control is unavailable")
    origin = request.headers.get("origin")
    if origin is not None and not _local_origin(origin):
        _raise_error(403, "LOCAL_CONTROL_FORBIDDEN", "local control is unavailable")
    if token is not None:
        provided = request.headers.get(CONTROL_HEADER, "")
        if not hmac.compare_digest(provided, token):
            _raise_error(403, "LOCAL_CONTROL_FORBIDDEN", "local control is unavailable")


def _loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_authority(raw: str) -> bool:
    if not raw or any(character.isspace() or ord(character) < 32 for character in raw):
        return False
    if any(character in raw for character in "@/\\?#"):
        return False
    try:
        parsed = urlsplit(f"http://{raw}")
        _ = parsed.port
    except ValueError:
        return False
    return parsed.username is None and parsed.password is None and _local_hostname(parsed.hostname)


def _local_origin(raw: str) -> bool:
    if not raw or raw == "null" or any(ord(character) < 32 for character in raw):
        return False
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
        and _local_hostname(parsed.hostname)
    )


def _local_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _service_projection(endpoint: TTSServiceEndpoint) -> dict[str, object]:
    locator = endpoint.portable_locator
    component = locator.component if locator is not None else endpoint.catalog_provider
    return {
        "service_id": endpoint.service_id,
        "component": component,
        "package_id": locator.package_id if locator is not None else None,
        "display_name": endpoint.display_name,
        "base_url": endpoint.base_url,
        "mode": endpoint.mode,
        "network_scope": endpoint.network_scope,
        "managed": bool(
            endpoint.managed
            and endpoint.mode == "local"
            and endpoint.network_scope == "localhost"
            and locator is not None
        ),
        "setup_state": endpoint.setup_state,
        "package_root": endpoint.repo_path if locator is not None else None,
        "build_id": locator.build_id_last_seen if locator is not None else None,
        "port_override": locator.port_override if locator is not None else None,
    }


def _checked_control_result(result: dict[str, Any]) -> dict[str, object]:
    if result.get("status") != "not manageable" and not result.get("error_code"):
        return result
    error_code = str(result.get("error_code") or "LOCAL_CONTROL_NOT_MANAGEABLE")
    status = 404 if error_code == "PORTABLE_OPERATION_NOT_FOUND" else 409
    _raise_error(status, error_code, "portable operation is unavailable")


def _operation_id(raw: str) -> str:
    try:
        value = UUID(raw)
    except (AttributeError, ValueError) as exc:
        _raise_error(422, "LOCAL_CONTROL_INVALID_REQUEST", "request validation failed")
        raise AssertionError from exc
    canonical = str(value)
    if canonical != raw:
        _raise_error(422, "LOCAL_CONTROL_INVALID_REQUEST", "request validation failed")
    return canonical


def _bounded_integer(raw: str, *, minimum: int, maximum: int) -> int:
    if not _INTEGER_RE.fullmatch(raw):
        _raise_error(422, "LOCAL_CONTROL_INVALID_REQUEST", "request validation failed")
    value = int(raw)
    if not minimum <= value <= maximum:
        _raise_error(422, "LOCAL_CONTROL_INVALID_REQUEST", "request validation failed")
    return value


def _strict_identity(value: str, label: str) -> str:
    if not _IDENTITY_RE.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _strict_path(value: str, label: str) -> str:
    if (
        not value
        or len(value) > 4096
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _strict_absolute_path(value: str, label: str) -> str:
    value = _strict_path(value, label)
    if not Path(value).is_absolute() and not PureWindowsPath(value).is_absolute():
        raise ValueError(f"{label} must be absolute")
    return value


def _relative_sibling(controller_root: Path, package_root: Path) -> str | None:
    controller = Path(os.path.abspath(controller_root))
    package = Path(os.path.abspath(package_root))
    if _path_key(controller.parent) == _path_key(package.parent) and _path_key(controller) != _path_key(
        package
    ):
        return f"../{package.name}"
    return None


def _safe_path_identity(path: Path, *, directory: bool) -> tuple[int, int, int, int]:
    lexical = Path(os.path.abspath(path))
    metadata = lexical.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if lexical.is_symlink() or attributes & reparse_flag:
        raise ValueError("path is a link or reparse point")
    if directory and not lexical.is_dir():
        raise ValueError("path is not a directory")
    if not directory and not lexical.is_file():
        raise ValueError("path is not a file")
    if _path_key(lexical.resolve(strict=True)) != _path_key(lexical):
        raise ValueError("path canonical identity changed")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


@contextmanager
def _selector_identity_guard(scripts: Path, selector: Path) -> Iterator[None]:
    if os.name != "nt":
        yield
        return
    handles: list[int] = []
    try:
        handles.append(
            _open_windows_guard_handle(
                scripts,
                share_mode=0x00000001 | 0x00000002,
                directory=True,
            )
        )
        handles.append(
            _open_windows_guard_handle(
                selector,
                share_mode=0x00000001,
                directory=False,
            )
        )
        yield
    except FolderSelectionError:
        raise
    except OSError as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_UNSAFE") from exc
    finally:
        if handles:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            for handle in reversed(handles):
                close_handle(ctypes.c_void_p(handle))


def _open_windows_guard_handle(path: Path, *, share_mode: int, directory: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    flags = 0x00200000
    if directory:
        flags |= 0x02000000
    handle = create_file(
        str(path),
        0x80000000,
        share_mode,
        None,
        3,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "folder selector identity guard failed")
    return int(handle)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(os.path.abspath(path)))).casefold()


def _windows_system_executable(name: str) -> Path:
    if os.name != "nt" or name.casefold() != "powershell.exe":
        raise OSError("Windows system PowerShell is unavailable")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise OSError("GetSystemDirectoryW failed")
    executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_absolute() or not executable.is_file():
        raise OSError("Windows system PowerShell is missing")
    return executable


def _selector_environment() -> dict[str, str]:
    allowed = {
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _no_window_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _is_local_control_path(path: str) -> bool:
    return path == "/api/local-control/token" or path.startswith("/api/local-portable-services")


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def _raise_error(status: int, code: str, message: str) -> None:
    raise HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
        headers={"Cache-Control": "no-store"},
    )
