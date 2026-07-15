from __future__ import annotations

import ctypes
import json
import math
import os
import re
import stat
import subprocess
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from app.portable_discovery import PortablePackageDescriptor, inspect_locator_candidate
from app.portable_file_io import PortableFileError, safe_read_bytes
from app.windows_job import CREATE_SUSPENDED, KillOnCloseJob, WindowsKillOnCloseJob


_EXACT_LAUNCHERS = {
    "start": "Start.cmd",
    "stop": "Stop.cmd",
    "repair": "Repair.cmd",
}
_SAFE_ENVIRONMENT_KEYS = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}
_MAX_JSON_BYTES = 64 * 1024
_MAX_EVENT_LINE_BYTES = 64 * 1024
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_EVENTS = 500
_MAX_ACTIVE_ACTIONS = 64
_PID_RECORD_FIELDS = {
    "schema_version",
    "pid",
    "parent_pid",
    "child_pids",
    "process_created_at",
    "recorded_at",
    "executable_path",
    "command_sha256",
    "port",
    "package_root",
    "build_id",
}


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[..., ProcessLike]


class PortableControlError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _ActionContext:
    root: Path
    descriptor: PortablePackageDescriptor
    descriptor_identity: tuple[object, ...]
    effective_port: int
    root_identity: tuple[int, int]
    manifest_identity: tuple[int, int, int, int]
    launcher: Path
    launcher_identity: tuple[int, int, int, int]


@dataclass
class _TrackedAction:
    action: str
    process: ProcessLike
    context: _ActionContext
    job: KillOnCloseJob | None = None
    failure_code: str | None = None
    failure_reason: str | None = None


def windows_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


class PortablePackageController:
    """Execute only the fixed launchers of a freshly validated portable package."""

    def __init__(
        self,
        *,
        spawn: Spawn | None = None,
        environment: Mapping[str, str] | None = None,
        handshake_seconds: float = 0.75,
        system_executable_resolver: Callable[[str], Path] | None = None,
        job_factory: Callable[[], KillOnCloseJob] | None = None,
    ) -> None:
        self._spawn = spawn or subprocess.Popen
        self._environment = dict(os.environ if environment is None else environment)
        self._handshake_seconds = max(0.05, min(float(handshake_seconds), 2.0))
        self._system_executable_resolver = system_executable_resolver or _windows_system_executable
        self._job_factory = (
            job_factory
            if job_factory is not None
            else (WindowsKillOnCloseJob if spawn is None else None)
        )
        self._actions: OrderedDict[str, _TrackedAction] = OrderedDict()
        self._action_reservations: set[str] = set()
        self._actions_lock = threading.RLock()

    def start(
        self,
        descriptor: PortablePackageDescriptor,
        *,
        operation_id: str,
        port_override: int | None = None,
    ) -> dict[str, object]:
        canonical_id = _canonical_operation_id(operation_id)
        if port_override is not None and (
            type(port_override) is not int or not 1 <= port_override <= 65535
        ):
            raise PortableControlError(
                "PORTABLE_INVALID_PORT", "port_override must be an integer between 1 and 65535"
            )
        arguments = [
            "-OperationId",
            canonical_id,
            "-ManagedBy",
            "tts-more",
            "-NoUi",
        ]
        if port_override is not None:
            arguments.extend(["-PortOverride", str(port_override)])
        return self._launch(descriptor, "start", arguments, operation_id=canonical_id)

    def stop(
        self,
        descriptor: PortablePackageDescriptor,
        *,
        action_id: str | None = None,
    ) -> dict[str, object]:
        return self._launch(descriptor, "stop", [], action_id=action_id)

    def repair(
        self,
        descriptor: PortablePackageDescriptor,
        *,
        proxy_url: str | None = None,
        action_id: str | None = None,
    ) -> dict[str, object]:
        proxy = validate_proxy_url(proxy_url) if proxy_url is not None else None
        environment = None if proxy is None else {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy}
        return self._launch(
            descriptor,
            "repair",
            [],
            action_id=action_id,
            environment_overrides=environment,
        )

    def action_status(
        self,
        descriptor: PortablePackageDescriptor,
        *,
        action_id: str,
    ) -> dict[str, object]:
        canonical_id = _canonical_action_id(action_id)
        with self._actions_lock:
            tracked = self._actions.get(canonical_id)
            if tracked is None:
                raise PortableControlError(
                    "PORTABLE_ACTION_NOT_FOUND", "portable action is unavailable"
                )
            if _descriptor_identity(descriptor) != tracked.context.descriptor_identity:
                raise PortableControlError(
                    "PORTABLE_ACTION_NOT_FOUND", "portable action is unavailable"
                )
            if tracked.failure_code is not None:
                self._actions.move_to_end(canonical_id)
                return self._blocked_action_result(canonical_id, tracked)
            try:
                current = self._action_context(descriptor, tracked.action)
            except PortableControlError:
                return self._block_identity_changed(canonical_id, tracked)
            if (
                current.descriptor_identity != tracked.context.descriptor_identity
                or current.root_identity != tracked.context.root_identity
                or current.launcher_identity != tracked.context.launcher_identity
            ):
                return self._block_identity_changed(canonical_id, tracked)
            try:
                self._assert_context_stable(tracked.context)
            except PortableControlError:
                return self._block_identity_changed(canonical_id, tracked)
            try:
                return_code = tracked.process.poll()
            except (OSError, subprocess.SubprocessError) as exc:
                raise PortableControlError(
                    "PORTABLE_LAUNCH_FAILED", "portable action state could not be checked"
                ) from exc
            self._actions.move_to_end(canonical_id)
            if return_code is None:
                status = {"stop": "stopping", "repair": "repairing"}[tracked.action]
            else:
                self._close_tracked_job(tracked)
                if return_code != 0:
                    return {
                        "status": "blocked",
                        "action": tracked.action,
                        "action_id": canonical_id,
                        "controller_pid": int(tracked.process.pid),
                        "error_code": "PORTABLE_LAUNCH_EXITED",
                        "reason": "portable action launcher exited with a failure",
                    }
                status = {"stop": "stopped", "repair": "completed"}[tracked.action]
            return {
                "status": status,
                "action": tracked.action,
                "action_id": canonical_id,
                "controller_pid": int(tracked.process.pid),
            }

    def open_folder(self, descriptor: PortablePackageDescriptor) -> dict[str, object]:
        context = self._action_context(descriptor, "start")
        explorer = self._system_executable("explorer.exe")
        process, _completed, _job = self._spawn_checked(
            [str(explorer), str(context.root)],
            context,
            action="open_folder",
        )
        return {
            "status": "opened",
            "action": "open_folder",
            "controller_pid": int(process.pid),
        }

    def status(
        self,
        descriptor: PortablePackageDescriptor,
        *,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        context = self._action_context(descriptor, "start")
        root, fresh = context.root, context.descriptor
        if operation_id is not None:
            operation, _directory, _directory_identity = self._read_operation(
                root, fresh, operation_id
            )
            result = {
                "status": operation["status"],
                "operation": operation,
                "running": None,
            }
        else:
            record_path = _contained_path(
                root,
                "data/local/run/worker.pid.json",
                label="PID record",
            )
            record = self._read_process_record(
                record_path, root, fresh, expected_port=context.effective_port
            )
            result = {
                "status": "unknown",
                "process_record": record,
                # A record is observability data only. Stop.cmd performs the actual
                # process/port ownership verification before terminating anything.
                "running": None,
            }
        self._assert_context_stable(context)
        return result

    def logs(
        self,
        descriptor: PortablePackageDescriptor,
        *,
        operation_id: str,
        after_seq: int = 0,
        limit: int = 200,
    ) -> dict[str, object]:
        if type(after_seq) is not int or after_seq < 0:
            raise PortableControlError("PORTABLE_INVALID_CURSOR", "after_seq must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= _MAX_EVENTS:
            raise PortableControlError("PORTABLE_INVALID_LIMIT", f"limit must be between 1 and {_MAX_EVENTS}")
        context = self._action_context(descriptor, "start")
        root, fresh = context.root, context.descriptor
        operation, directory, directory_identity = self._read_operation(root, fresh, operation_id)
        events = _read_events(
            root,
            directory / "events.jsonl",
            after_seq=after_seq,
            limit=limit,
        )
        try:
            directory_stable = _object_identity(directory) == directory_identity
        except OSError:
            directory_stable = False
        if not directory_stable:
            raise PortableControlError(
                "PORTABLE_FILE_CHANGED", "portable operation directory changed during read"
            )
        next_seq = int(events[-1]["seq"]) if events else after_seq
        result = {
            "status": operation["status"],
            "operation_id": operation["operation_id"],
            "events": events,
            "next_seq": next_seq,
        }
        self._assert_context_stable(context)
        return result

    def _launch(
        self,
        descriptor: PortablePackageDescriptor,
        action: str,
        arguments: list[str],
        *,
        operation_id: str | None = None,
        action_id: str | None = None,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        canonical_action_id = _canonical_action_id(action_id) if action_id is not None else None
        context = self._action_context(descriptor, action)
        command_processor = self._system_executable("cmd.exe")
        # `/c` receives only the fixed literal root launcher. The absolute
        # package path remains in cwd and never enters cmd.exe's parser.
        command = [str(command_processor), "/d", "/c", _EXACT_LAUNCHERS[action], *arguments]
        reservation_active = False
        if canonical_action_id is not None:
            self._reserve_action(canonical_action_id)
            reservation_active = True
        try:
            process, completed, job = self._spawn_checked(
                command,
                context,
                action=action,
                environment_overrides=environment_overrides,
                use_action_job=canonical_action_id is not None,
            )
            if completed and action == "start" and operation_id is not None:
                operation, _directory, _identity = self._read_operation(
                    context.root, context.descriptor, operation_id
                )
                operation_status = str(operation["status"])
                if operation_status in _NONTERMINAL_PHASES:
                    raise PortableControlError(
                        "PORTABLE_OPERATION_INCOMPLETE",
                        "portable start launcher exited before the operation reached a terminal state",
                    )
                if operation_status != "ready":
                    raise PortableControlError(
                        "PORTABLE_OPERATION_FAILED",
                        "portable start operation completed without reaching ready",
                    )
                status = "ready"
            elif completed:
                status = {"start": "completed", "stop": "stopped", "repair": "completed"}[action]
            else:
                status = {"start": "starting", "stop": "stopping", "repair": "repairing"}[action]
            result: dict[str, object] = {
                "status": status,
                "action": action,
                "controller_pid": int(process.pid),
            }
            if operation_id is not None:
                result["operation_id"] = operation_id
            if not completed and canonical_action_id is not None:
                committed_action_id = self._commit_action_reservation(
                    canonical_action_id,
                    action,
                    process,
                    context,
                    job,
                )
                reservation_active = False
                result["action_id"] = committed_action_id
            return result
        finally:
            if reservation_active:
                self._release_action_reservation(canonical_action_id)

    def _reserve_action(self, action_id: str) -> None:
        with self._actions_lock:
            if action_id in self._actions or action_id in self._action_reservations:
                raise PortableControlError(
                    "PORTABLE_ACTION_EXISTS", "portable action identifier is already active"
                )
            if len(self._actions) + len(self._action_reservations) >= _MAX_ACTIVE_ACTIONS:
                completed_id = None
                for existing_id, tracked in self._actions.items():
                    if _action_completed(tracked.process):
                        self._close_tracked_job(tracked)
                        completed_id = existing_id
                        break
                if completed_id is not None:
                    self._actions.pop(completed_id, None)
            if len(self._actions) + len(self._action_reservations) >= _MAX_ACTIVE_ACTIONS:
                raise PortableControlError(
                    "PORTABLE_ACTION_CAPACITY", "too many portable actions are active"
                )
            self._action_reservations.add(action_id)

    def _release_action_reservation(self, action_id: str | None) -> None:
        if action_id is None:
            return
        with self._actions_lock:
            self._action_reservations.discard(action_id)

    def _commit_action_reservation(
        self,
        action_id: str,
        action: str,
        process: ProcessLike,
        context: _ActionContext,
        job: KillOnCloseJob | None = None,
    ) -> str:
        with self._actions_lock:
            self._action_reservations.discard(action_id)
            tracked = _TrackedAction(action, process, context, job)
            existing = self._actions.get(action_id)
            if existing is None:
                self._actions[action_id] = tracked
                return action_id
            if existing.process is process:
                if existing.job is None:
                    existing.job = job
                self._actions.move_to_end(action_id)
                return action_id

            # The public reservation protocol makes this collision unreachable:
            # duplicate IDs are rejected while the same lock protects reservations
            # and tracked actions. If internal state is nevertheless disturbed after
            # spawn, preserve both processes and return a queryable rescue ID rather
            # than overwriting the prior process or orphaning the new one.
            rescue_value = UUID(action_id).int
            rescue_id = action_id
            while rescue_id in self._actions or rescue_id in self._action_reservations:
                rescue_value = (rescue_value + 1) % (1 << 128)
                rescue_id = str(UUID(int=rescue_value))
            self._actions[rescue_id] = tracked
            return rescue_id

    def _spawn_checked(
        self,
        command: list[str],
        context: _ActionContext,
        *,
        action: str,
        environment_overrides: Mapping[str, str] | None = None,
        use_action_job: bool = False,
    ) -> tuple[ProcessLike, bool, KillOnCloseJob | None]:
        child_environment = _safe_environment(self._environment)
        if environment_overrides:
            child_environment.update(environment_overrides)
        kwargs: dict[str, object] = {
            "cwd": context.root,
            "env": child_environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        job: KillOnCloseJob | None = None
        if use_action_job and self._job_factory is not None:
            try:
                job = self._job_factory()
            except (OSError, ValueError) as exc:
                raise PortableControlError(
                    "PORTABLE_LAUNCH_FAILED",
                    f"portable {action} launcher process tree could not be secured",
                ) from exc
        flags = windows_creation_flags()
        if job is not None:
            flags |= CREATE_SUSPENDED
        if flags:
            kwargs["creationflags"] = flags
        process: ProcessLike | None = None
        try:
            with _execution_identity_guard(context):
                self._assert_context_stable(context)
                try:
                    process = self._spawn(command, **kwargs)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise PortableControlError(
                        "PORTABLE_LAUNCH_FAILED", f"portable {action} launcher could not be started"
                    ) from exc
                if job is not None:
                    try:
                        job.assign(process)
                        job.resume(process)
                    except (OSError, ValueError) as exc:
                        raise PortableControlError(
                            "PORTABLE_LAUNCH_FAILED",
                            f"portable {action} launcher process tree could not be secured",
                        ) from exc
                try:
                    return_code = process.wait(timeout=self._handshake_seconds)
                    completed = True
                except subprocess.TimeoutExpired:
                    return_code = None
                    completed = False
                except (OSError, subprocess.SubprocessError) as exc:
                    if job is None:
                        raise PortableControlError(
                            "PORTABLE_LAUNCH_FAILED",
                            f"portable {action} launcher state could not be checked",
                        ) from exc
                    return_code = None
                    completed = False
                self._assert_context_stable(context)
                if return_code not in (None, 0):
                    raise PortableControlError(
                        "PORTABLE_LAUNCH_EXITED", f"portable {action} launcher exited with a failure"
                    )
                if completed and job is not None:
                    try:
                        job.close()
                    except OSError as exc:
                        raise PortableControlError(
                            "PORTABLE_LAUNCH_FAILED",
                            f"portable {action} launcher process tree could not be released",
                        ) from exc
                    job = None
        except PortableControlError:
            if process is not None:
                if job is not None:
                    _terminate_job_tree(job, process)
                    job = None
                else:
                    _terminate_controller(process)
            elif job is not None:
                _close_job(job)
                job = None
            raise
        return process, completed, job

    def _close_tracked_job(self, tracked: _TrackedAction) -> None:
        if tracked.job is None:
            return
        try:
            tracked.job.close()
        except OSError as exc:
            raise PortableControlError(
                "PORTABLE_LAUNCH_FAILED",
                "portable action process tree could not be released",
            ) from exc
        tracked.job = None

    def _block_identity_changed(
        self,
        action_id: str,
        tracked: _TrackedAction,
    ) -> dict[str, object]:
        tracked.failure_code = "PORTABLE_IDENTITY_CHANGED"
        tracked.failure_reason = "portable package identity changed while the action was active"
        if tracked.job is not None:
            _terminate_job_tree(tracked.job, tracked.process)
            tracked.job = None
        else:
            _terminate_controller(tracked.process)
        self._actions.move_to_end(action_id)
        return self._blocked_action_result(action_id, tracked)

    @staticmethod
    def _blocked_action_result(
        action_id: str,
        tracked: _TrackedAction,
    ) -> dict[str, object]:
        assert tracked.failure_code is not None
        assert tracked.failure_reason is not None
        return {
            "status": "blocked",
            "action": tracked.action,
            "action_id": action_id,
            "controller_pid": int(tracked.process.pid),
            "error_code": tracked.failure_code,
            "reason": tracked.failure_reason,
        }

    def _system_executable(self, name: str) -> Path:
        try:
            executable = self._system_executable_resolver(name)
            _validate_system_executable(executable, name)
            return executable
        except PortableControlError:
            raise
        except (OSError, ValueError) as exc:
            raise PortableControlError(
                "PORTABLE_SYSTEM_EXECUTABLE_INVALID",
                f"Windows system executable is unavailable: {name}",
            ) from exc

    def _action_context(
        self, descriptor: PortablePackageDescriptor, action: str
    ) -> _ActionContext:
        root, fresh = self._fresh_root(descriptor)
        try:
            expected = _EXACT_LAUNCHERS[action]
            if fresh.launchers.get(action) != expected:
                raise PortableControlError(
                    "PORTABLE_LAUNCHER_INVALID", f"portable {action} launcher is not the fixed root launcher"
                )
            launcher = _contained_path(root, expected, label=f"{action} launcher")
            launcher_file = _regular_file(launcher, f"{action} launcher", required=True)
            assert launcher_file is not None
            manifest = _regular_file(
                root / "package" / "tts-more-package.json", "package manifest", required=True
            )
            assert manifest is not None
            return _ActionContext(
                root=root,
                descriptor=fresh,
                descriptor_identity=_descriptor_identity(fresh),
                effective_port=descriptor.port,
                root_identity=_object_identity(root),
                manifest_identity=_stat_identity(manifest),
                launcher=launcher_file,
                launcher_identity=_stat_identity(launcher_file),
            )
        except PortableControlError:
            raise
        except OSError as exc:
            raise PortableControlError(
                "PORTABLE_PACKAGE_INVALID",
                "portable package metadata could not be safely inspected",
            ) from exc

    def _assert_context_stable(self, context: _ActionContext) -> None:
        try:
            fresh = inspect_locator_candidate(context.root)
            stable = (
                fresh is not None
                and fresh.manageable
                and _descriptor_identity(fresh) == context.descriptor_identity
                and _object_identity(context.root) == context.root_identity
                and _stat_identity(context.root / "package" / "tts-more-package.json")
                == context.manifest_identity
                and _stat_identity(context.launcher) == context.launcher_identity
            )
        except (OSError, ValueError):
            stable = False
        if not stable:
            raise PortableControlError(
                "PORTABLE_IDENTITY_CHANGED", "portable package or launcher changed during action"
            )

    def _fresh_root(
        self, descriptor: PortablePackageDescriptor
    ) -> tuple[Path, PortablePackageDescriptor]:
        try:
            root = Path(os.path.abspath(Path(descriptor.package_root).expanduser()))
            fresh = inspect_locator_candidate(root)
        except (OSError, ValueError) as exc:
            raise PortableControlError(
                "PORTABLE_PACKAGE_INVALID", "portable package could not be freshly validated"
            ) from exc
        if fresh is None:
            raise PortableControlError(
                "PORTABLE_PACKAGE_INVALID", "portable package could not be freshly validated"
            )
        if _descriptor_identity(fresh) != _descriptor_identity(descriptor):
            raise PortableControlError(
                "PORTABLE_IDENTITY_CHANGED", "portable package identity changed before action"
            )
        if not fresh.manageable:
            raise PortableControlError(
                "PORTABLE_PACKAGE_INVALID", "portable package could not be freshly validated"
            )
        return root, fresh

    def _read_operation(
        self,
        root: Path,
        descriptor: PortablePackageDescriptor,
        operation_id: str,
    ) -> tuple[dict[str, object], Path, tuple[int, int]]:
        canonical_id = _canonical_operation_id(operation_id)
        try:
            operations_root = _contained_path(
                root, descriptor.operations_path, label="operations root"
            )
            directory = operations_root / canonical_id
            if not directory.exists():
                raise PortableControlError("PORTABLE_OPERATION_MISSING", "portable operation does not exist")
            if _is_reparse_point(directory):
                raise PortableControlError("PORTABLE_PATH_REPARSE", "portable operation path is a reparse point")
            if not directory.is_dir():
                raise PortableControlError("PORTABLE_OPERATION_INVALID", "portable operation path is not a directory")
            directory.resolve(strict=True).relative_to(root.resolve(strict=True))
        except PortableControlError:
            raise
        except ValueError as exc:
            raise PortableControlError("PORTABLE_PATH_ESCAPE", "portable operation path escapes package root") from exc
        except OSError as exc:
            raise PortableControlError(
                "PORTABLE_FILE_CHANGED", "portable operation metadata changed during read"
            ) from exc
        try:
            directory_identity = _object_identity(directory)
            operation = _project_operation(
                _read_json(root, directory / "operation.json", "operation state"),
                canonical_id,
                descriptor.component,
            )
            directory_stable = _object_identity(directory) == directory_identity
        except PortableControlError:
            raise
        except OSError as exc:
            raise PortableControlError(
                "PORTABLE_FILE_CHANGED", "portable operation metadata changed during read"
            ) from exc
        if not directory_stable:
            raise PortableControlError(
                "PORTABLE_FILE_CHANGED", "portable operation directory changed during read"
            )
        return operation, directory, directory_identity

    @staticmethod
    def _read_process_record(
        path: Path,
        root: Path,
        descriptor: PortablePackageDescriptor,
        *,
        expected_port: int,
    ) -> dict[str, object] | None:
        content = _safe_read_control(
            root,
            path,
            max_bytes=_MAX_JSON_BYTES,
            label="PID record",
            allow_missing=True,
        )
        if content is None:
            return None
        try:
            payload = _decode_json_object(content, "PID record")
        except PortableControlError as exc:
            if exc.code != "PORTABLE_JSON_INVALID":
                raise
            return None
        child_pids = payload.get("child_pids")
        executable_raw = payload.get("executable_path")
        process_created_at = _normalize_iso_timestamp(payload.get("process_created_at"))
        recorded_at = _normalize_iso_timestamp(payload.get("recorded_at"))
        valid_executable = False
        if type(executable_raw) is str and Path(executable_raw).is_absolute():
            try:
                Path(executable_raw).resolve(strict=False).relative_to(root.resolve(strict=True))
                valid_executable = True
            except (OSError, ValueError):
                pass
        if (
            set(payload) != _PID_RECORD_FIELDS
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 2
            or not _positive_integer(payload.get("pid"))
            or not _positive_integer(payload.get("parent_pid"))
            or type(child_pids) is not list
            or any(not _positive_integer(child) for child in child_pids)
            or process_created_at is None
            or recorded_at is None
            or not valid_executable
            or type(payload.get("command_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("command_sha256"))) is None
            or type(payload.get("port")) is not int
            or payload.get("port") != expected_port
            or payload.get("package_root") != str(root.resolve(strict=True))
            or payload.get("build_id") != descriptor.build_id
        ):
            return None
        # Construct the safe projection instead of passing package-controlled
        # values through. This record never grants process authority.
        return {
            "schema_version": 2,
            "pid": int(payload["pid"]),
            "parent_pid": int(payload["parent_pid"]),
            "child_pids": [int(child) for child in child_pids],
            "process_created_at": process_created_at,
            "recorded_at": recorded_at,
            "port": expected_port,
            "build_id": descriptor.build_id,
        }


def _descriptor_identity(descriptor: PortablePackageDescriptor) -> tuple[object, ...]:
    root = Path(os.path.abspath(Path(descriptor.package_root).expanduser()))
    return (
        os.path.normcase(str(root)),
        descriptor.component,
        descriptor.package_id,
        descriptor.build_id,
        descriptor.protocol_version,
        descriptor.controller_range,
        tuple(sorted(descriptor.launchers.items())),
        descriptor.operations_path,
    )


def _canonical_operation_id(value: str) -> str:
    if type(value) is not str:
        raise PortableControlError("PORTABLE_INVALID_OPERATION", "operation_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise PortableControlError("PORTABLE_INVALID_OPERATION", "operation_id must be a canonical UUID") from exc
    if str(parsed) != value:
        raise PortableControlError("PORTABLE_INVALID_OPERATION", "operation_id must be a canonical UUID")
    return value


def _canonical_action_id(value: str) -> str:
    if type(value) is not str:
        raise PortableControlError("PORTABLE_INVALID_ACTION", "action_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise PortableControlError(
            "PORTABLE_INVALID_ACTION", "action_id must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise PortableControlError("PORTABLE_INVALID_ACTION", "action_id must be a canonical UUID")
    return value


def validate_proxy_url(value: str) -> str:
    """Validate an ephemeral HTTP(S) proxy without logging or normalizing credentials."""
    if type(value) is not str or not value or len(value) > 2048 or value != value.strip():
        raise PortableControlError("PORTABLE_INVALID_PROXY", "proxy_url must be a valid HTTP(S) proxy")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PortableControlError("PORTABLE_INVALID_PROXY", "proxy_url must be a valid HTTP(S) proxy")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PortableControlError(
            "PORTABLE_INVALID_PROXY", "proxy_url must be a valid HTTP(S) proxy"
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise PortableControlError("PORTABLE_INVALID_PROXY", "proxy_url must be a valid HTTP(S) proxy")
    return value


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _safe_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if key.upper() in _SAFE_ENVIRONMENT_KEYS and isinstance(value, str)
    }


def _windows_system_executable(name: str) -> Path:
    if os.name != "nt":
        raise PortableControlError(
            "PORTABLE_SYSTEM_EXECUTABLE_INVALID",
            f"Windows system executable lookup is unavailable: {name}",
        )
    if name == "cmd.exe":
        directory = _windows_directory_from_api("GetSystemDirectoryW")
    elif name == "explorer.exe":
        directory = _windows_directory_from_api("GetWindowsDirectoryW")
    else:
        raise PortableControlError(
            "PORTABLE_SYSTEM_EXECUTABLE_INVALID", "unsupported Windows system executable"
        )
    return directory / name


def _windows_directory_from_api(function_name: str) -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = getattr(kernel32, function_name)
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    function.restype = ctypes.c_uint
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = int(function(buffer, size))
    if length == 0 or length >= size:
        error = ctypes.get_last_error()
        raise OSError(error, f"{function_name} failed")
    directory = Path(buffer.value)
    if not directory.is_absolute():
        raise ValueError(f"{function_name} returned a relative path")
    return directory


def _validate_system_executable(path: Path, expected_name: str) -> None:
    executable = Path(path)
    if not executable.is_absolute() or executable.name.casefold() != expected_name.casefold():
        raise PortableControlError(
            "PORTABLE_SYSTEM_EXECUTABLE_INVALID",
            f"Windows system executable path is invalid: {expected_name}",
        )
    try:
        metadata = executable.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse_point(executable)
            or os.path.normcase(str(executable.resolve(strict=True)))
            != os.path.normcase(str(executable))
        ):
            raise ValueError("system executable is not a stable regular file")
    except OSError as exc:
        raise PortableControlError(
            "PORTABLE_SYSTEM_EXECUTABLE_INVALID",
            f"Windows system executable is unavailable: {expected_name}",
        ) from exc


@contextmanager
def _execution_identity_guard(context: _ActionContext) -> Iterator[None]:
    if os.name != "nt":
        yield
        return
    handles: list[int] = []
    try:
        handles.append(
            _open_windows_guard_handle(
                context.root,
                share_mode=0x00000001 | 0x00000002,  # READ | WRITE; deny DELETE/rename.
                directory=True,
            )
        )
        handles.append(
            _open_windows_guard_handle(
                context.root / "package" / "tts-more-package.json",
                share_mode=0x00000001,  # Other readers only; deny write/delete replacement.
                directory=False,
            )
        )
        handles.append(
            _open_windows_guard_handle(
                context.launcher,
                share_mode=0x00000001,
                directory=False,
            )
        )
        yield
    except PortableControlError:
        raise
    except OSError as exc:
        raise PortableControlError(
            "PORTABLE_IDENTITY_GUARD_FAILED",
            "portable package execution identity could not be locked",
        ) from exc
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
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ; makes share-mode write/delete denial effective.
        share_mode,
        None,
        3,  # OPEN_EXISTING
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        error = ctypes.get_last_error()
        raise OSError(error, f"CreateFileW identity guard failed: {path.name}")
    return int(handle)


def _contained_path(root: Path, relative: str, *, label: str) -> Path:
    normalized = relative.replace("\\", "/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ":" in normalized or ".." in path.parts:
        raise PortableControlError("PORTABLE_PATH_ESCAPE", f"{label} is not a contained package path")
    current = root
    for part in path.parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise PortableControlError("PORTABLE_PATH_REPARSE", f"{label} traverses a reparse point")
    try:
        current.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PortableControlError("PORTABLE_PATH_ESCAPE", f"{label} escapes package root") from exc
    return current


def _regular_file(path: Path, label: str, *, required: bool) -> Path | None:
    if not path.exists():
        if required:
            raise PortableControlError("PORTABLE_FILE_MISSING", f"{label} is missing")
        return None
    if _is_reparse_point(path):
        raise PortableControlError("PORTABLE_PATH_REPARSE", f"{label} is a reparse point")
    if not path.is_file():
        raise PortableControlError("PORTABLE_FILE_INVALID", f"{label} is not a regular file")
    if int(path.lstat().st_nlink) != 1:
        raise PortableControlError("PORTABLE_PATH_HARDLINK", f"{label} is a hard link")
    return path


def _safe_read_control(
    root: Path,
    path: Path,
    *,
    max_bytes: int,
    label: str,
    allow_missing: bool = False,
) -> bytes | None:
    try:
        return safe_read_bytes(
            root,
            path,
            max_bytes=max_bytes,
            label=label,
            retries=2,
            allow_missing=allow_missing,
        )
    except PortableFileError as exc:
        raise PortableControlError(exc.code, str(exc)) from exc
    except OSError as exc:
        raise PortableControlError(
            "PORTABLE_FILE_CHANGED", f"{label} could not be read safely"
        ) from exc


def _decode_json_object(content: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableControlError("PORTABLE_JSON_INVALID", f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PortableControlError("PORTABLE_JSON_INVALID", f"{label} must be a JSON object")
    return payload


def _read_json(root: Path, path: Path, label: str) -> dict[str, object]:
    content = _safe_read_control(root, path, max_bytes=_MAX_JSON_BYTES, label=label)
    assert content is not None
    return _decode_json_object(content, label)


def _read_events(
    root: Path,
    path: Path,
    *,
    after_seq: int,
    limit: int,
) -> list[dict[str, object]]:
    content = _safe_read_control(
        root,
        path,
        max_bytes=_MAX_EVENT_BYTES,
        label="operation event log",
        allow_missing=True,
    )
    if content is None:
        return []
    events: list[dict[str, object]] = []
    previous_seq = 0
    for line in content.splitlines(keepends=True):
        if len(line) > _MAX_EVENT_LINE_BYTES:
            raise PortableControlError("PORTABLE_FILE_TOO_LARGE", "operation event log is too large")
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Writers append one fsynced JSON object plus newline. A reader may
            # observe only the in-progress final append.
            if not line.endswith((b"\n", b"\r")):
                break
            raise PortableControlError("PORTABLE_JSON_INVALID", "operation event log is invalid") from exc
        projected = _project_event(event, previous_seq, root)
        previous_seq = int(projected["seq"])
        if previous_seq > after_seq and len(events) < limit:
            events.append(projected)
    return events


_PHASES = {
    "not_initialized", "checking", "downloading", "installing", "validating",
    "starting", "ready", "stopped", "repairable", "blocked",
}
_NONTERMINAL_PHASES = {
    "not_initialized", "checking", "downloading", "installing", "validating", "starting",
}
_OPERATION_REQUIRED_FIELDS = {
    "operation_id", "component", "action", "initiator", "started_at", "status", "exit_code",
}
_EVENT_REQUIRED_FIELDS = {"seq", "timestamp", "phase", "message"}


def _project_operation(
    payload: dict[str, object], operation_id: str, component: str
) -> dict[str, object]:
    fields = set(payload)
    started_at = _normalize_iso_timestamp(payload.get("started_at"))
    if (
        not _OPERATION_REQUIRED_FIELDS.issubset(fields)
        or not fields.issubset(_OPERATION_REQUIRED_FIELDS | {"finished_at"})
        or payload.get("operation_id") != operation_id
        or payload.get("component") != component
        or type(payload.get("action")) is not str
        or payload.get("action") not in {"start", "stop", "repair"}
        or type(payload.get("status")) is not str
        or payload.get("status") not in _PHASES
        or not _bounded_text(payload.get("initiator"), 128)
        or started_at is None
    ):
        raise PortableControlError("PORTABLE_OPERATION_INVALID", "portable operation schema is invalid")
    status = str(payload["status"])
    exit_code = payload["exit_code"]
    has_finished = "finished_at" in payload
    finished_at = (
        _normalize_iso_timestamp(payload.get("finished_at")) if has_finished else None
    )
    if status in _NONTERMINAL_PHASES:
        valid_completion = exit_code is None and not has_finished
    elif status == "ready":
        valid_completion = (
            type(exit_code) is int
            and exit_code == 0
            and has_finished
            and finished_at is not None
        )
    else:
        valid_completion = (
            type(exit_code) is int
            and exit_code != 0
            and has_finished
            and finished_at is not None
        )
    if not valid_completion:
        raise PortableControlError("PORTABLE_OPERATION_INVALID", "portable operation schema is invalid")
    projected = {
        key: payload[key]
        for key in _OPERATION_REQUIRED_FIELDS | {"finished_at"}
        if key in payload
    }
    projected["started_at"] = started_at
    if has_finished:
        projected["finished_at"] = finished_at
    return projected


def _project_event(
    payload: object, previous_seq: int, root: Path
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise PortableControlError("PORTABLE_EVENT_INVALID", "portable operation event must be an object")
    fields = set(payload)
    seq = payload.get("seq")
    percent = payload.get("percent")
    error_code = payload.get("error_code")
    timestamp = _normalize_iso_timestamp(payload.get("timestamp"))
    if (
        not _EVENT_REQUIRED_FIELDS.issubset(fields)
        or not fields.issubset(_EVENT_REQUIRED_FIELDS | {"percent", "error_code"})
        or type(seq) is not int
        or seq != previous_seq + 1
        or timestamp is None
        or type(payload.get("phase")) is not str
        or payload.get("phase") not in _PHASES
        or not _bounded_text(payload.get("message"), 4096)
        or (
            "percent" in payload
            and (
                type(percent) not in {int, float}
                or not math.isfinite(float(percent))
                or not 0 <= float(percent) <= 100
            )
        )
        or (
            "error_code" in payload
            and (
                type(error_code) is not str
                or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code) is None
            )
        )
    ):
        raise PortableControlError("PORTABLE_EVENT_INVALID", "portable operation event schema is invalid")
    projected = {key: payload[key] for key in _EVENT_REQUIRED_FIELDS | {"percent", "error_code"} if key in payload}
    projected["timestamp"] = timestamp
    projected["message"] = _sanitize_event_message(str(payload["message"]), root)
    return projected


def _bounded_text(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _normalize_iso_timestamp(value: object) -> str | None:
    if not _bounded_text(value, 64):
        return None
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC).isoformat()
    except (OverflowError, ValueError):
        return None


def _sanitize_event_message(message: str, root: Path) -> str:
    sanitized = message
    root_text = str(root)
    if root_text:
        sanitized = re.sub(re.escape(root_text), "[REDACTED_PACKAGE_ROOT]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@",
        r"\1[REDACTED_CREDENTIALS]@",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[^\s,;]+",
        "Authorization: Bearer [REDACTED_TOKEN]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[REDACTED_SECRET]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s\"'<>|]+",
        "[REDACTED_PATH]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)\b(?:DESKTOP|LAPTOP)-[A-Z0-9_-]+\b",
        "[REDACTED_COMPUTER]",
        sanitized,
    )
    for key in ("USERNAME", "COMPUTERNAME"):
        identity = os.environ.get(key, "").strip()
        if len(identity) >= 3:
            sanitized = re.sub(re.escape(identity), f"[REDACTED_{key}]", sanitized, flags=re.IGNORECASE)
    return sanitized


def _stat_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = path.stat()
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _object_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return (int(metadata.st_dev), int(metadata.st_ino))


def _is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & flag)


def _action_completed(process: ProcessLike) -> bool:
    try:
        return process.poll() is not None
    except (OSError, subprocess.SubprocessError):
        # Capacity may be reclaimed only from a process proven terminal. A
        # transient query failure is unknown state and must remain tracked.
        return False


def _close_job(job: KillOnCloseJob) -> None:
    try:
        job.close()
    except OSError:
        pass


def _terminate_job_tree(job: KillOnCloseJob, process: ProcessLike) -> None:
    try:
        job.terminate()
    except OSError:
        pass
    _close_job(job)
    # The suspended assign-failure path has no descendants yet and may not be
    # in the Job Object. Terminating the root is therefore a required fallback.
    _terminate_controller(process)


def _terminate_controller(process: ProcessLike) -> None:
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except OSError:
            pass


__all__ = ["PortableControlError", "PortablePackageController", "windows_creation_flags"]
