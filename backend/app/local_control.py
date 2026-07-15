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
import threading
import time
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
from app.portable_control import PortableControlError, validate_proxy_url
from app.portable_imports import (
    PortableImportPlanError,
    PortableImportPlanStore,
    load_portable_importer,
    project_import_plan,
    project_import_report,
)
from app.portable_services import (
    PortableServiceStore,
    discover_bounded_portable_packages,
    resolve_locator,
)
from app.service_store_io import ServicePostCommitError
from app.windows_job import CREATE_SUSPENDED, WindowsKillOnCloseJob


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
    proxy_url: str | None = Field(default=None, max_length=2048)

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_proxy_url(value)
        except PortableControlError as exc:
            raise ValueError("proxy_url must be a valid HTTP(S) proxy") from exc


class LocalPortableImportPlanRequest(_StrictRequest):
    pass


class LocalPortableImportApplyRequest(_StrictRequest):
    confirmed: Literal[True]
    plan_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("confirmed", mode="before")
    @classmethod
    def validate_literal_confirmation(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("confirmed must be exactly true")
        return value


class LocalPortableImportCancelledResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["cancelled"]


class LocalPortableImportPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan_id: str
    plan_digest: str
    expires_in_seconds: int = Field(ge=0, le=300)
    user_file_count: int = Field(ge=0)
    user_bytes: int = Field(ge=0)
    reusable_assets: list[str]
    reusable_asset_bytes: int = Field(ge=0)
    skipped_assets: list[str]
    already_present: list[str]
    old_package_preserved: Literal[True]


class LocalPortableImportApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    copied_user_files: int = Field(ge=0)
    reused_assets: list[str]
    skipped_assets: list[str]
    already_present: list[str]


RefreshServices = Callable[[Sequence[TTSServiceEndpoint]], None]


class LocalControlMiddleware:
    """Guard local control from raw ASGI metadata before consuming a bounded body."""

    def __init__(self, app: Any, *, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not _is_local_control_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        if str(scope.get("method", "GET")).upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return
        token = None if scope.get("path") == "/api/local-control/token" else self.token
        if not _valid_local_scope(scope, token=token):
            await _error_response(
                403,
                "LOCAL_CONTROL_FORBIDDEN",
                "local control is unavailable",
            )(scope, receive, send)
            return
        length_values = _raw_header_values(scope, b"content-length")
        if len(length_values) > 1:
            await _error_response(
                400,
                "LOCAL_CONTROL_INVALID_REQUEST",
                "request validation failed",
            )(scope, receive, send)
            return
        if length_values:
            raw_length = _ascii_header(length_values[0], maximum=20)
            if raw_length is None or not raw_length.isdigit():
                await _error_response(
                    400,
                    "LOCAL_CONTROL_INVALID_REQUEST",
                    "request validation failed",
                )(scope, receive, send)
                return
            if int(raw_length) > MAX_LOCAL_CONTROL_BODY_BYTES:
                await _error_response(
                    413,
                    "LOCAL_CONTROL_REQUEST_TOO_LARGE",
                    "request body is too large",
                )(scope, receive, send)
                return
        method = str(scope.get("method", "GET")).upper()
        if method not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        buffered: list[dict[str, Any]] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                await _error_response(
                    400,
                    "LOCAL_CONTROL_INVALID_REQUEST",
                    "request body is incomplete",
                )(scope, _empty_receive, send)
                return
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                await _error_response(
                    400,
                    "LOCAL_CONTROL_INVALID_REQUEST",
                    "request validation failed",
                )(scope, _empty_receive, send)
                return
            total += len(body)
            if total > MAX_LOCAL_CONTROL_BODY_BYTES:
                await _error_response(
                    413,
                    "LOCAL_CONTROL_REQUEST_TOO_LARGE",
                    "request body is too large",
                )(scope, _empty_receive, send)
                return
            buffered.append(
                {
                    "type": "http.request",
                    "body": body,
                    "more_body": bool(message.get("more_body", False)),
                }
            )
            if not message.get("more_body", False):
                break
        index = 0

        async def replay() -> dict[str, Any]:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


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
    import_plans = PortableImportPlanStore()
    app.state.portable_import_plan_store = import_plans
    supervisor_start_invalidator = getattr(
        getattr(app.state, "supervisor", None), "set_portable_start_invalidator", None
    )
    if callable(supervisor_start_invalidator):
        supervisor_start_invalidator(import_plans.invalidate_component)
    importer_lock = threading.Lock()
    importer_module: Any | None = None
    app.add_middleware(LocalControlMiddleware, token=token)

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

    def managed_package(component: PortableComponent) -> tuple[TTSServiceEndpoint, Any]:
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
        if (
            descriptor is None
            or not descriptor.manageable
            or not endpoint.managed
            or endpoint.mode != "local"
            or endpoint.network_scope != "localhost"
            or endpoint.api_contract != "tts-more-v1"
            or endpoint.control_kind != "portable-package"
        ):
            _raise_error(
                409,
                "LOCAL_CONTROL_NOT_MANAGEABLE",
                "portable service is not locally manageable",
            )
        return endpoint, descriptor

    def managed_endpoint(component: PortableComponent) -> TTSServiceEndpoint:
        endpoint, _descriptor = managed_package(component)
        return endpoint

    def portable_importer() -> Any:
        nonlocal importer_module
        if importer_module is None:
            with importer_lock:
                if importer_module is None:
                    importer_module = load_portable_importer(root)
        return importer_module

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
            _service_projection(endpoint, root)
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
        supervisor = app.state.supervisor
        try:
            with supervisor.portable_lifecycle_guard(payload.component):
                previous_services = current_services()
                previous_override = next(
                    (
                        item.portable_locator.port_override
                        for item in previous_services
                        if item.portable_locator is not None
                        and item.portable_locator.component == payload.component
                    ),
                    None,
                )
                effective_override = (
                    payload.port_override
                    if payload.port_override is not None
                    else previous_override
                )
                endpoint = endpoint.model_copy(
                    update={
                        "portable_locator": locator.model_copy(
                            update={
                                "relative_to_tts_more": _relative_sibling(
                                    root, Path(descriptor.package_root)
                                ),
                                "port_override": effective_override,
                            }
                        )
                    }
                )
                store = current_store()
                initial_services = previous_services if not store.path.exists() else []
                services = store.replace_component(
                    endpoint,
                    initial_services=initial_services,
                    publish=refresh_services,
                )
                import_plans.invalidate_component(payload.component)
        except ServicePostCommitError:
            _raise_error(
                500,
                "LOCAL_CONTROL_PUBLICATION_FAILED",
                "portable service registration was persisted but runtime refresh failed",
            )
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
            "service": _service_projection(stored, root),
        }

    @app.post(
        "/api/local-portable-services/{component}/imports/plan",
        summary="Plan one local portable worker data import",
        response_model=LocalPortableImportPlanResponse | LocalPortableImportCancelledResponse,
    )
    def plan_local_portable_import(
        component: PortableComponent,
        payload: LocalPortableImportPlanRequest,
        request: Request,
    ) -> dict[str, object]:
        require_token(request)
        endpoint, descriptor = managed_package(component)
        try:
            selected = select_portable_folder(root)
        except FolderSelectionError as exc:
            status = 501 if exc.code == "LOCAL_CONTROL_FOLDER_UNSUPPORTED" else 409
            _raise_error(status, exc.code, str(exc))
        if selected is None:
            return {"status": "cancelled"}
        try:
            importer = portable_importer()
            plan = importer.plan_import(selected, Path(descriptor.package_root))
            stored = import_plans.create(
                plan,
                component=component,
                service_id=endpoint.service_id,
                package_id=descriptor.package_id,
                build_id=descriptor.build_id,
            )
            return project_import_plan(stored)
        except PortableImportPlanError as exc:
            _raise_error(409, "LOCAL_CONTROL_IMPORT_PLAN_FAILED", "portable import plan is unavailable")
            raise AssertionError from exc
        except Exception as exc:
            _raise_error(409, "LOCAL_CONTROL_IMPORT_PLAN_FAILED", "portable import plan is unavailable")
            raise AssertionError from exc

    @app.post(
        "/api/local-portable-services/{component}/imports/{plan_id}/apply",
        summary="Apply one confirmed local portable worker data import",
        response_model=LocalPortableImportApplyResponse,
    )
    def apply_local_portable_import(
        component: PortableComponent,
        plan_id: str,
        payload: LocalPortableImportApplyRequest,
        request: Request,
    ) -> dict[str, object]:
        require_token(request)
        plan_id = _operation_id(plan_id)
        supervisor = app.state.supervisor
        try:
            with supervisor.portable_lifecycle_guard(component):
                try:
                    stored = import_plans.consume(plan_id, payload.plan_digest)
                except PortableImportPlanError as exc:
                    _raise_error(
                        409,
                        "LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE",
                        "portable import plan is unavailable",
                    )
                    raise AssertionError from exc
                endpoint = managed_endpoint(component)
                descriptor = supervisor.require_portable_stopped(endpoint)
                if not _portable_import_identity_matches(stored, endpoint, descriptor):
                    _raise_error(
                        409,
                        "LOCAL_CONTROL_IMPORT_BLOCKED",
                        "portable import is unavailable",
                    )
                importer = portable_importer()
                report = importer.apply_import(stored.plan)
                return project_import_report(report)
        except HTTPException:
            raise
        except (PortableControlError, PortableImportPlanError) as exc:
            _raise_error(409, "LOCAL_CONTROL_IMPORT_BLOCKED", "portable import is unavailable")
            raise AssertionError from exc
        except Exception as exc:
            _raise_error(409, "LOCAL_CONTROL_IMPORT_FAILED", "portable import failed")
            raise AssertionError from exc

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
        if (
            action != "start" and payload.port_override is not None
        ) or (
            action != "repair" and payload.proxy_url is not None
        ):
            _raise_error(
                422,
                "LOCAL_CONTROL_INVALID_REQUEST",
                "request validation failed",
            )
        supervisor = app.state.supervisor
        if action == "start":
            with supervisor.portable_lifecycle_guard(component):
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
                operation_id = str(uuid4())
                result = supervisor.start(endpoint, operation_id=operation_id)
                checked = _checked_control_result(result)
                import_plans.invalidate_component(component)
                return {"component": component, "action": action, **checked}
        endpoint = managed_endpoint(component)
        if action == "stop":
            result = supervisor.stop(endpoint, action_id=str(uuid4()))
        elif action == "repair":
            result = supervisor.repair(
                endpoint,
                proxy_url=payload.proxy_url,
                action_id=str(uuid4()),
            )
        else:
            result = supervisor.open_folder(endpoint)
        checked = _checked_control_result(result)
        return {"component": component, "action": action, **checked}

    @app.get(
        "/api/local-portable-services/{component}/actions/{action_id}",
        summary="Read one in-process portable stop or repair action",
    )
    def local_portable_action_status(
        component: PortableComponent,
        action_id: str,
        request: Request,
    ) -> dict[str, object]:
        require_token(request)
        action_id = _operation_id(action_id)
        endpoint = managed_endpoint(component)
        result = app.state.supervisor.action_status(endpoint, action_id=action_id)
        return _checked_control_result(result)

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


def _run_bounded_selector(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    timeout: float,
    output_limit: int,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
    shell: bool = False,
    check: bool = False,
    creationflags: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    """Run one suspended Windows selector with bounded pipes and a kill-on-close job."""

    del check
    if os.name != "nt" or shell or stdin is not subprocess.DEVNULL:
        raise OSError("unsafe folder selector process configuration")
    if stdout is not subprocess.PIPE or stderr is not subprocess.PIPE:
        raise OSError("folder selector pipes are required")
    if output_limit < 1 or timeout <= 0:
        raise ValueError("selector bounds must be positive")

    job = WindowsKillOnCloseJob()
    process: subprocess.Popen[bytes] | None = None
    threads: list[threading.Thread] = []
    out_buffer = bytearray()
    err_buffer = bytearray()
    overflow = threading.Event()
    wake = threading.Event()
    out_done = threading.Event()
    err_done = threading.Event()
    process_exited = False
    process_completed = False
    process_returncode: int | None = None
    tree_terminated = False
    job_closed = False
    resources_closed = False

    def read_bounded(pipe: Any, target: bytearray, done: threading.Event) -> None:
        try:
            while True:
                reader = getattr(pipe, "read1", pipe.read)
                chunk = reader(4096)
                if not chunk:
                    return
                if len(target) + len(chunk) > output_limit:
                    overflow.set()
                    wake.set()
                    return
                target.extend(chunk)
        finally:
            done.set()
            wake.set()

    def close_job() -> None:
        nonlocal job_closed
        if job_closed:
            return
        job_closed = True
        try:
            job.close()
        except OSError:
            pass

    def close_resources() -> None:
        nonlocal resources_closed
        if resources_closed:
            return
        resources_closed = True
        for pipe in (
            getattr(process, "stdout", None),
            getattr(process, "stderr", None),
        ):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        for thread in threads:
            try:
                thread.join(timeout=1)
            except (OSError, RuntimeError):
                pass

    def terminate_tree() -> None:
        nonlocal tree_terminated
        if tree_terminated:
            return
        tree_terminated = True
        try:
            job.terminate()
        except OSError:
            pass
        close_job()
        if process is not None:
            try:
                process.wait(timeout=0.25)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
        close_resources()

    try:
        flags = int(creationflags)
        flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        flags |= CREATE_SUSPENDED  # Attach the Job Object before selector code runs.
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=flags,
            close_fds=False,
        )
        job.assign(process)
        assert process.stdout is not None
        assert process.stderr is not None
        threads = [
            threading.Thread(
                target=read_bounded,
                args=(process.stdout, out_buffer, out_done),
                daemon=True,
                name="folder-selector-stdout",
            ),
            threading.Thread(
                target=read_bounded,
                args=(process.stderr, err_buffer, err_done),
                daemon=True,
                name="folder-selector-stderr",
            ),
        ]
        for thread in threads:
            thread.start()
        job.resume(process)
        deadline = time.monotonic() + timeout
        while True:
            if overflow.is_set():
                raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID")
            if not process_exited:
                try:
                    returncode = process.poll()
                except (OSError, subprocess.SubprocessError):
                    raise FolderSelectionError("LOCAL_CONTROL_FOLDER_FAILED") from None
                if returncode is not None:
                    process_exited = True
                    process_returncode = int(returncode)
            if process_exited and out_done.is_set() and err_done.is_set():
                process_completed = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FolderSelectionError("LOCAL_CONTROL_FOLDER_TIMEOUT")
            wake.wait(min(remaining, 0.05))
            wake.clear()
        assert process_returncode is not None
        return subprocess.CompletedProcess(
            list(command),
            process_returncode,
            bytes(out_buffer),
            bytes(err_buffer),
        )
    except FolderSelectionError:
        if process is not None and not process_completed:
            terminate_tree()
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        if process is not None and not process_completed:
            terminate_tree()
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_FAILED") from None
    finally:
        if process is not None and not process_completed:
            terminate_tree()
        close_resources()
        close_job()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def select_portable_folder(
    controller_root: Path,
    *,
    platform_name: str | None = None,
    run: Callable[..., Any] | None = None,
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
            runner = run or _run_bounded_selector
            completed = runner(
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
                output_limit=MAX_FOLDER_OUTPUT_BYTES,
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
    try:
        payload = json.loads(text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID") from exc
    if payload == {"cancelled": True}:
        return None
    if not isinstance(payload, dict) or set(payload) != {"selected_path"}:
        raise FolderSelectionError("LOCAL_CONTROL_FOLDER_OUTPUT_INVALID")
    selected: object = payload["selected_path"]
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
    if not _valid_local_scope(request.scope, token=token):
        _raise_error(403, "LOCAL_CONTROL_FORBIDDEN", "local control is unavailable")


def _valid_local_scope(scope: dict[str, Any], *, token: str | None) -> bool:
    client = scope.get("client")
    client_host = client[0] if isinstance(client, (tuple, list)) and client else ""
    if not isinstance(client_host, str) or not _loopback_host(client_host):
        return False
    host_values = _raw_header_values(scope, b"host")
    origin_values = _raw_header_values(scope, b"origin")
    control_values = _raw_header_values(scope, CONTROL_HEADER.casefold().encode("ascii"))
    if len(host_values) != 1 or len(origin_values) > 1:
        return False
    raw_host = _ascii_header(host_values[0], maximum=512)
    if raw_host is None or not _local_authority(raw_host):
        return False
    if origin_values:
        raw_origin = _ascii_header(origin_values[0], maximum=2048)
        if raw_origin is None or not _local_origin(raw_origin):
            return False
    if token is None:
        return len(control_values) == 0
    if len(control_values) != 1:
        return False
    provided = _ascii_header(control_values[0], maximum=256)
    if provided is None:
        return False
    return hmac.compare_digest(provided, token)


def _raw_header_values(scope: dict[str, Any], name: bytes) -> list[bytes]:
    values: list[bytes] = []
    raw_headers = scope.get("headers", [])
    if not isinstance(raw_headers, (tuple, list)):
        return values
    for item in raw_headers:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        raw_name, raw_value = item
        if not isinstance(raw_name, bytes) or not isinstance(raw_value, bytes):
            continue
        if raw_name.lower() == name:
            values.append(raw_value)
    return values


def _ascii_header(raw: bytes, *, maximum: int) -> str | None:
    if not raw or len(raw) > maximum:
        return None
    try:
        value = raw.decode("ascii", errors="strict")
    except UnicodeError:
        return None
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        return None
    return value


def _loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_authority(raw: str) -> bool:
    if not raw or "%" in raw or any(character in raw for character in "@/\\?#"):
        return False
    host: str
    port_text: str | None = None
    if raw.startswith("["):
        closing = raw.find("]")
        if closing <= 1:
            return False
        host = raw[1:closing]
        remainder = raw[closing + 1 :]
        if remainder:
            if not remainder.startswith(":"):
                return False
            port_text = remainder[1:]
        if "]" in remainder or "[" in host or "]" in host:
            return False
    else:
        if raw.count(":") > 1:
            return False
        if ":" in raw:
            host, port_text = raw.rsplit(":", 1)
        else:
            host = raw
    if not host or (port_text is not None and not _valid_port(port_text)):
        return False
    return _local_hostname(host)


def _local_origin(raw: str) -> bool:
    if (
        not raw
        or raw == "null"
        or "%" in raw
        or "\\" in raw
        or "@" in raw
    ):
        return False
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
        and raw.startswith(parsed.scheme + "://")
        and raw == f"{parsed.scheme}://{parsed.netloc}"
        and _local_authority(parsed.netloc)
    )


def _valid_port(raw: str) -> bool:
    return raw.isdigit() and 1 <= int(raw) <= 65535


def _local_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _service_projection(
    endpoint: TTSServiceEndpoint,
    controller_root: Path | None = None,
) -> dict[str, object]:
    locator = endpoint.portable_locator
    component = locator.component if locator is not None else endpoint.catalog_provider
    descriptor = (
        resolve_locator(controller_root, locator, [])
        if controller_root is not None and locator is not None
        else None
    )
    setup_state = endpoint.setup_state
    package_root = endpoint.repo_path if locator is not None else None
    build_id = locator.build_id_last_seen if locator is not None else None
    if locator is not None and controller_root is not None:
        if descriptor is None:
            setup_state = "repo_missing"
        else:
            setup_state = "ready" if descriptor.initialized else "env_missing"
            package_root = descriptor.package_root
            build_id = descriptor.build_id
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
            and (controller_root is None or descriptor is not None)
        ),
        "setup_state": setup_state,
        "package_root": package_root,
        "build_id": build_id,
        "port_override": locator.port_override if locator is not None else None,
    }


def _portable_import_identity_matches(
    stored: Any,
    endpoint: TTSServiceEndpoint,
    descriptor: Any,
) -> bool:
    locator = endpoint.portable_locator
    try:
        descriptor_root = Path(os.path.abspath(Path(descriptor.package_root)))
        planned_root = Path(os.path.abspath(stored.new_root))
    except (OSError, TypeError, ValueError):
        return False
    return bool(
        locator is not None
        and stored.component == locator.component == descriptor.component
        and stored.service_id == endpoint.service_id
        and stored.package_id == locator.package_id == descriptor.package_id
        and stored.build_id == descriptor.build_id
        and _path_key(descriptor_root) == _path_key(planned_root)
    )


def _checked_control_result(result: dict[str, Any]) -> dict[str, object]:
    if result.get("status") not in {"not manageable", "blocked"} and not result.get("error_code"):
        return result
    default_code = (
        "LOCAL_CONTROL_ACTION_FAILED"
        if result.get("status") == "blocked"
        else "LOCAL_CONTROL_NOT_MANAGEABLE"
    )
    error_code = str(result.get("error_code") or default_code)
    status = 404 if error_code in {"PORTABLE_OPERATION_NOT_FOUND", "PORTABLE_ACTION_NOT_FOUND"} else 409
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
    return path == "/api/local-control/token" or path == "/api/local-portable-services" or path.startswith(
        "/api/local-portable-services/"
    )


async def _empty_receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


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
