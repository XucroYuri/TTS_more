from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import TTSServiceEndpoint
from app.portable_control import (
    PortableControlError,
    PortablePackageController,
    windows_creation_flags,
)
from app.portable_endpoint_trust import trust_resolved_portable_endpoint
from app.portable_services import resolve_locator


def _is_windows() -> bool:
    """Platform check isolated as a function so tests can mock it without
    mutating the global ``os`` module (which would corrupt ``pathlib.Path``
    on non-Windows hosts)."""
    return os.name == "nt"


# Bare executable names that may be invoked as start_command[0] without being
# an in-project path. These are resolved through PATH by the OS. Extend via
# the TTS_MORE_ALLOWED_EXECUTABLES env var (os.pathsep-separated).
_DEFAULT_ALLOWED_EXECUTABLES = {
    "python", "python3", "python3.10", "python3.11", "python.exe",
    "uvicorn", "uvicorn.exe",
    "node", "node.exe",
    "bash", "sh", "zsh",
    "pwsh", "powershell",
}


def _allowed_executables() -> set[str]:
    names = set(_DEFAULT_ALLOWED_EXECUTABLES)
    extra = os.environ.get("TTS_MORE_ALLOWED_EXECUTABLES", "")
    for name in extra.split(os.pathsep):
        name = name.strip()
        if name:
            names.add(name)
    return names


class ServiceSupervisor:
    def __init__(
        self,
        project_root: Path,
        runtime_root: Path,
        *,
        portable_controller: PortablePackageController | None = None,
        portable_resolver=None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runtime_root = runtime_root
        self.records_dir = runtime_root / "services"
        self.logs_dir = runtime_root / "logs"
        self._portable_controller = portable_controller or PortablePackageController()
        self._portable_resolver = portable_resolver or resolve_locator
        self._portable_locks_guard = threading.Lock()
        self._portable_locks: dict[tuple[str, str], threading.RLock] = {}
        self._portable_service_identities: dict[str, tuple[str, str]] = {}

    def status(
        self,
        endpoint: TTSServiceEndpoint,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if endpoint.control_kind == "portable-package":
            return self._portable_action(
                endpoint,
                "status",
                operation_id=operation_id,
            )
        record = self._read_record(endpoint.service_id)
        return {
            "service_id": endpoint.service_id,
            "manageable": self._manageable(endpoint),
            "record": record,
            "running": self._is_pid_running(record.get("pid")) if record else False,
        }

    def start(
        self,
        endpoint: TTSServiceEndpoint,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if endpoint.control_kind == "portable-package":
            return self._portable_action(
                endpoint,
                "start",
                operation_id=operation_id or str(uuid.uuid4()),
            )
        if endpoint.mode != "local" or not endpoint.managed:
            return {"status": "not manageable", "reason": f"{endpoint.service_id} is {endpoint.mode}"}
        if not endpoint.start_command:
            return {"status": "not manageable", "reason": f"{endpoint.service_id} has no start_command"}

        existing = self._read_record(endpoint.service_id)
        if existing and self._is_pid_running(existing.get("pid")):
            return {"status": "already running", **existing}

        try:
            command = self._resolve_command(endpoint.start_command)
        except ValueError as exc:
            return {"status": "not manageable", "reason": str(exc)}
        cwd = self._resolve_cwd(endpoint)
        log_path = self.logs_dir / f"{endpoint.service_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_path.open("ab")
        popen_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": {**os.environ, **self._resolve_env(endpoint.env)},
            "close_fds": False,
        }
        creation_flags = windows_creation_flags()
        if creation_flags:
            popen_kwargs["creationflags"] = creation_flags
        process = subprocess.Popen(command, **popen_kwargs)
        log_file.close()
        record = {
            "service_id": endpoint.service_id,
            "pid": process.pid,
            "command": command,
            "cwd": str(cwd),
            "log_path": str(log_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._record_path(endpoint.service_id).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "started", **record}

    def stop(self, service_id: str | TTSServiceEndpoint) -> dict[str, Any]:
        if isinstance(service_id, TTSServiceEndpoint):
            if service_id.control_kind == "portable-package":
                return self._portable_action(service_id, "stop")
            service_id = service_id.service_id
        record = self._read_record(service_id)
        if not record:
            return {"status": "not running", "service_id": service_id}
        pid = record.get("pid")
        if pid:
            if _is_windows():
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
            else:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
        self._record_path(service_id).unlink(missing_ok=True)
        return {"status": "stopped", "service_id": service_id, "pid": pid}

    def logs(
        self,
        service_id: str | TTSServiceEndpoint,
        lines: int = 120,
        *,
        operation_id: str | None = None,
        after_seq: int = 0,
    ) -> dict[str, Any]:
        if isinstance(service_id, TTSServiceEndpoint):
            if service_id.control_kind == "portable-package":
                if operation_id is None:
                    return {
                        "status": "not manageable",
                        "reason": "portable operation_id is required for logs",
                    }
                return self._portable_action(
                    service_id,
                    "logs",
                    operation_id=operation_id,
                    after_seq=after_seq,
                    limit=lines,
                )
            service_id = service_id.service_id
        record = self._read_record(service_id)
        if not record:
            return {"status": "missing", "service_id": service_id, "lines": []}
        log_path = Path(record.get("log_path", ""))
        if not log_path.exists():
            return {"status": "missing log", "service_id": service_id, "lines": []}
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"status": "ok", "service_id": service_id, "log_path": str(log_path), "lines": content[-max(1, lines):]}

    def repair(self, endpoint: TTSServiceEndpoint) -> dict[str, Any]:
        if endpoint.control_kind != "portable-package":
            return {"status": "not manageable", "reason": "repair is only available for portable packages"}
        return self._portable_action(endpoint, "repair")

    def open_folder(self, endpoint: TTSServiceEndpoint) -> dict[str, Any]:
        if endpoint.control_kind != "portable-package":
            return {"status": "not manageable", "reason": "folder access is only available for portable packages"}
        return self._portable_action(endpoint, "open_folder")

    def _portable_action(
        self,
        endpoint: TTSServiceEndpoint,
        action: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        locator = endpoint.portable_locator
        if (
            locator is None
            or not endpoint.managed
            or endpoint.mode != "local"
            or endpoint.network_scope != "localhost"
            or endpoint.api_contract != "tts-more-v1"
        ):
            return {
                "status": "not manageable",
                "manageable": False,
                "reason": "portable endpoint is not trusted local tts-more-v1",
            }
        identity = (locator.component, locator.package_id)
        lock = self._portable_lock(identity)
        with lock:
            try:
                descriptor = self._portable_resolver(self.project_root, locator, [])
                if (
                    descriptor is None
                    or not descriptor.manageable
                    or descriptor.component != locator.component
                    or descriptor.package_id != locator.package_id
                ):
                    return {
                        "status": "not manageable",
                        "manageable": False,
                        "reason": "portable package could not be freshly resolved",
                    }
                trusted = trust_resolved_portable_endpoint(endpoint, descriptor)
                if not trusted.managed:
                    return {
                        "status": "not manageable",
                        "manageable": False,
                        "reason": "portable endpoint trust validation failed",
                    }
                if not self._bind_portable_service_identity(endpoint.service_id, identity):
                    return {
                        "status": "not manageable",
                        "manageable": False,
                        "reason": "portable service identity changed during this supervisor lifetime",
                    }
                method = getattr(self._portable_controller, action)
                if action == "start":
                    kwargs["port_override"] = locator.port_override
                result = method(descriptor, **kwargs)
                if action == "status":
                    result.setdefault("manageable", True)
                return result
            except PortableControlError as exc:
                return {
                    "status": "blocked",
                    "manageable": False,
                    "error_code": exc.code,
                    "reason": str(exc),
                }
            except (OSError, ValueError) as exc:
                return {
                    "status": "not manageable",
                    "manageable": False,
                    "reason": f"portable package validation failed: {type(exc).__name__}",
                }

    def _portable_lock(self, identity: tuple[str, str]) -> threading.RLock:
        with self._portable_locks_guard:
            return self._portable_locks.setdefault(identity, threading.RLock())

    def _bind_portable_service_identity(
        self, service_id: str, identity: tuple[str, str]
    ) -> bool:
        with self._portable_locks_guard:
            existing = self._portable_service_identities.get(service_id)
            if existing is not None and existing != identity:
                return False
            self._portable_service_identities[service_id] = identity
            return True

    def _manageable(self, endpoint: TTSServiceEndpoint) -> bool:
        return endpoint.mode == "local" and endpoint.managed and bool(endpoint.start_command)

    def _record_path(self, service_id: str) -> Path:
        safe = service_id.replace("/", "_").replace("\\", "_")
        return self.records_dir / f"{safe}.json"

    def _read_record(self, service_id: str) -> dict[str, Any] | None:
        path = self._record_path(service_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _resolve_command(self, command: list[str]) -> list[str]:
        if not command:
            return command
        executable = Path(command[0])
        if not executable.is_absolute() and ("\\" in command[0] or "/" in command[0]):
            candidate = (self.project_root / executable).resolve()
            if self._inside_project(candidate):
                return [str(candidate), *command[1:]]
        # Validate the executable: it must be either a bare allowed name
        # (resolved via PATH) or an absolute path inside the project. This
        # prevents a attacker-controlled start_command from running arbitrary
        # binaries (e.g. /tmp/evil or C:\Tools\payload.exe) when service
        # settings are writable.
        self._validate_executable(command[0])
        return command

    def _validate_executable(self, raw: str) -> None:
        """Raise ValueError if ``raw`` is not an allowed start_command[0]."""
        executable = Path(raw)
        if executable.is_absolute():
            resolved = executable.resolve(strict=False)
            if not self._inside_project(resolved):
                raise ValueError(
                    f"start_command executable is outside project root: {raw}"
                )
            return
        # Relative path with a separator must already have been resolved into
        # the project above; a relative path that escapes is rejected here.
        if "\\" in raw or "/" in raw:
            candidate = (self.project_root / raw).resolve(strict=False)
            if not self._inside_project(candidate):
                raise ValueError(
                    f"start_command executable escapes project root: {raw}"
                )
            return
        # Bare name: must be in the allowlist.
        if raw not in _allowed_executables():
            raise ValueError(
                f"start_command executable not allowed: {raw!r} "
                f"(allowed: bare names in TTS_MORE_ALLOWED_EXECUTABLES, "
                f"or a path inside the project root)"
            )

    def _resolve_cwd(self, endpoint: TTSServiceEndpoint) -> Path:
        raw = endpoint.start_cwd or ("." if endpoint.service_id in {"local-indextts"} else endpoint.repo_path) or "."
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        resolved = candidate.resolve(strict=False)
        if not self._inside_project(resolved):
            raise ValueError(f"start_cwd escapes project root: {raw}")
        return resolved

    def _resolve_env(self, env: dict[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for key, value in env.items():
            if key.upper() == "PATH":
                resolved[key] = self._resolve_path_env(value)
                continue
            if key.endswith("_PATH") or key.endswith("_DIR") or key.endswith("_PYTHON"):
                path = Path(value)
                if not path.is_absolute() and ("\\" in value or "/" in value):
                    candidate = (self.project_root / path).resolve(strict=False)
                    if self._inside_project(candidate):
                        resolved[key] = str(candidate)
                        continue
            resolved[key] = value
        return resolved

    def _resolve_path_env(self, value: str) -> str:
        # The PATH value follows the convention of the *target* service config
        # (Windows uses ";", POSIX uses ":"), not the host running this code.
        # Using the host's os.pathsep would break PATH values authored for a
        # different platform (e.g. a macOS host managing a Windows service).
        separator = ";" if _is_windows() else ":"
        parts: list[str] = []
        for item in value.replace("%PATH%", "{PATH}").split(separator):
            if item == "{PATH}":
                parts.append(os.environ.get("PATH", ""))
                continue
            # Normalize backslashes to forward slashes so that Path() treats
            # Windows-style config values ("repo\GPT-SoVITS\bin") as a real
            # path hierarchy on every host platform.
            normalized = item.replace("\\", "/")
            path = Path(normalized)
            if normalized and not path.is_absolute() and ("/" in normalized):
                candidate = (self.project_root / path).resolve(strict=False)
                if self._inside_project(candidate):
                    parts.append(str(candidate))
                    continue
            parts.append(item)
        return separator.join(part for part in parts if part)

    def _inside_project(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self.project_root)
            return True
        except ValueError:
            return False

    def _is_pid_running(self, pid: Any) -> bool:
        if not pid:
            return False
        try:
            if _is_windows():
                result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}"], capture_output=True, text=True, check=False)
                return str(pid) in result.stdout
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False
