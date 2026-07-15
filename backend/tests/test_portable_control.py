from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import pytest

from app import portable_control, portable_file_io
from app.portable_control import PortableControlError, PortablePackageController
from app.portable_discovery import read_portable_package


OPERATION_ID = "11111111-1111-4111-8111-111111111111"


class FakeProcess:
    def __init__(self, pid: int = 42, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout=None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-controller", timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


class FlakyPollProcess(FakeProcess):
    def __init__(self, *, poll_failures: int, pid: int = 42) -> None:
        super().__init__(pid=pid)
        self.poll_failures = poll_failures

    def poll(self) -> int | None:
        if self.poll_failures > 0:
            self.poll_failures -= 1
            raise OSError("fixture poll failure")
        return super().poll()


class WaitErrorProcess(FakeProcess):
    def __init__(self, *, wait_failures: int, pid: int = 42) -> None:
        super().__init__(pid=pid)
        self.wait_failures = wait_failures

    def wait(self, timeout=None) -> int:
        if self.wait_failures > 0:
            self.wait_failures -= 1
            raise OSError("fixture wait failure")
        return super().wait(timeout)


class FakeJob:
    def __init__(self, *, fail_assign: bool = False, fail_resume: bool = False) -> None:
        self.fail_assign = fail_assign
        self.fail_resume = fail_resume
        self.assigned: list[FakeProcess] = []
        self.resumed: list[FakeProcess] = []
        self.terminate_calls = 0
        self.close_calls = 0

    def assign(self, process: FakeProcess) -> None:
        self.assigned.append(process)
        if self.fail_assign:
            raise OSError("fixture assign failure")

    def resume(self, process: FakeProcess) -> None:
        self.resumed.append(process)
        if self.fail_resume:
            raise OSError("fixture resume failure")

    def terminate(self) -> None:
        self.terminate_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _write_package(root: Path, *, component: str = "gpt-sovits", package_id: str = "gpt-main") -> Path:
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
        "version": "0.2.0",
        "release_version": "0.2.1",
        "build_id": "build-one",
        "package_profile": "bootstrap",
        "platform": "windows-x64",
        "api_contract": "tts-more-v1",
        "source": {"repository": "https://example.invalid/repo", "revision": "a" * 40},
        "integration": {"version": "2.0.0", "source_revision": "b" * 40, "bundle_sha256": "c" * 64},
        "runtime": {
            "python_version": "3.11",
            "device_profiles": ["auto", "cpu"],
            "lock": "tts_more/locks/runtime.lock.json",
            "state_path": "data/local/install-state.json",
        },
        "models": {"lock": "tts_more/locks/models.lock.json", "required": True},
        "data_root": "data/local",
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
        "protocol": {"name": "tts-more-v1", "version": "1.0", "controller_range": ">=0.2.0,<0.3.0"},
        "data": {
            "user": "data/user",
            "local": "data/local",
            "cache": "data/cache",
            "operations": "data/local/operations",
        },
        "capabilities": ["tts", "artifact-transfer"],
        "sha256_manifest": "SHA256SUMS.txt",
        "licenses": "THIRD_PARTY_NOTICES.json",
    }
    path = root / "package" / "tts-more-package.json"
    path.parent.mkdir()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _rewrite_manifest(root: Path, mutate) -> None:
    path = root / "package" / "tts-more-package.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("directory junction verification is Windows-only")
    environment = os.environ.copy()
    environment["B2_JUNCTION_PATH"] = str(link)
    environment["B2_JUNCTION_TARGET"] = str(target)
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "New-Item -ItemType Junction -Path $env:B2_JUNCTION_PATH -Target $env:B2_JUNCTION_TARGET | Out-Null",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        pytest.fail(f"Windows junction command failed: {exc}")
    if completed.returncode != 0:
        pytest.fail(f"Windows junction creation failed: {completed.stderr}")


def _hardlink(link: Path, target: Path) -> None:
    try:
        os.link(target, link)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"hardlink creation is unavailable: {exc}")
        pytest.fail(f"Windows hardlink creation failed: {exc}")


def _operation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "component": "gpt-sovits",
        "action": "start",
        "initiator": "tts-more",
        "started_at": "2026-07-15T00:00:00Z",
        "status": "starting",
        "exit_code": None,
    }
    payload.update(overrides)
    return payload


def _write_operation_state(package: Path, payload: dict[str, object]) -> Path:
    directory = package / "data" / "local" / "operations" / OPERATION_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "operation.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def _ready_start_batch(*commands: str) -> str:
    operation = (
        '{"operation_id":"%~2","component":"gpt-sovits","action":"start",'
        '"initiator":"tts-more","started_at":"2026-07-15T00:00:00Z",'
        '"status":"ready","exit_code":0,"finished_at":"2026-07-15T00:00:01Z"}'
    )
    lines = [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        *commands,
        'set "OP_DIR=%~dp0data\\local\\operations\\%~2"',
        'if not exist "%OP_DIR%" mkdir "%OP_DIR%"',
        f'> "%OP_DIR%\\operation.json" echo {operation}',
        "exit /b 0",
    ]
    return "\r\n".join(lines) + "\r\n"


def _pid_payload(package: Path, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "pid": 1234,
        "parent_pid": 100,
        "child_pids": [],
        "process_created_at": "2026-07-15T08:00:00+08:00",
        "recorded_at": "2026-07-15T00:00:01Z",
        "executable_path": str(package / "runtime/live/python.exe"),
        "command_sha256": "a" * 64,
        "port": 9880,
        "package_root": str(package.resolve()),
        "build_id": "build-one",
    }
    payload.update(overrides)
    return payload


def test_controller_executes_exact_root_launcher_with_safe_process_contract(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT 包 & (便携)")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    controller = PortablePackageController(spawn=spawn, environment={"SYSTEMROOT": "C:/Windows", "UNSAFE": "drop"})
    result = controller.start(read_portable_package(package), operation_id=OPERATION_ID, port_override=9980)

    command = calls[0][0]
    assert Path(command[0]).is_absolute()
    assert Path(command[0]).name.casefold() == "cmd.exe"
    assert command == [
        command[0], "/d", "/c", "Start.cmd",
        "-OperationId", OPERATION_ID, "-ManagedBy", "tts-more", "-NoUi", "-PortOverride", "9980",
    ]
    assert calls[0][1]["cwd"] == package
    assert calls[0][1]["close_fds"] is True
    assert calls[0][1]["env"] == {"SYSTEMROOT": "C:/Windows"}
    assert result == {"status": "starting", "action": "start", "operation_id": OPERATION_ID, "controller_pid": 42}


def test_repair_passes_temporary_proxy_only_in_child_environment(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    proxy = "https://proxy-user:proxy-password@proxy.example:8443"
    controller = PortablePackageController(
        spawn=spawn,
        environment={"SYSTEMROOT": "C:/Windows", "HTTP_PROXY": "http://ambient.invalid"},
    )

    result = controller.repair(read_portable_package(package), proxy_url=proxy)

    command, kwargs = calls[0]
    assert command[-1] == "Repair.cmd"
    assert proxy not in " ".join(command)
    assert kwargs["env"] == {
        "SYSTEMROOT": "C:/Windows",
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
    }
    assert proxy not in json.dumps(result)
    assert result["status"] == "repairing"


@pytest.mark.parametrize(
    "proxy",
    (
        "socks5://127.0.0.1:1080",
        "http://proxy.example/path",
        "http://127.0.0.1:70000",
        "http://127.0.0.1:10808\nPATH=C:/evil",
    ),
)
def test_repair_rejects_invalid_proxy_without_spawning(tmp_path: Path, proxy: str) -> None:
    package = _write_package(tmp_path / "GPT")
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn")
    )

    with pytest.raises(PortableControlError, match="proxy"):
        controller.repair(read_portable_package(package), proxy_url=proxy)


@pytest.mark.parametrize(
    ("action", "terminal_status"),
    (("stop", "stopped"), ("repair", "completed")),
)
def test_async_action_status_tracks_real_process_and_keeps_terminal_retryable(
    tmp_path: Path, action: str, terminal_status: str
) -> None:
    package = _write_package(tmp_path / "GPT")
    process = FakeProcess()
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: process)
    descriptor = read_portable_package(package)
    action_id = "22222222-2222-4222-8222-222222222222"

    result = getattr(controller, action)(descriptor, action_id=action_id)
    assert result["action_id"] == action_id
    active_status = {"stop": "stopping", "repair": "repairing"}[action]
    assert controller.action_status(descriptor, action_id=action_id)["status"] == active_status

    process.returncode = 0
    assert controller.action_status(descriptor, action_id=action_id)["status"] == terminal_status
    assert controller.action_status(descriptor, action_id=action_id)["status"] == terminal_status


def test_action_status_is_bound_to_package_identity(tmp_path: Path) -> None:
    first = _write_package(tmp_path / "GPT one", package_id="gpt-one")
    second = _write_package(tmp_path / "GPT two", package_id="gpt-two")
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: FakeProcess())
    action_id = "22222222-2222-4222-8222-222222222222"
    controller.stop(read_portable_package(first), action_id=action_id)

    with pytest.raises(PortableControlError) as error:
        controller.action_status(read_portable_package(second), action_id=action_id)
    assert error.value.code == "PORTABLE_ACTION_NOT_FOUND"
    assert controller.action_status(read_portable_package(first), action_id=action_id)["status"] == "stopping"


def test_action_tracker_rejects_more_than_bounded_concurrent_actions(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    processes: list[FakeProcess] = []

    def spawn(*_args, **_kwargs):
        process = FakeProcess(pid=100 + len(processes))
        processes.append(process)
        return process

    controller = PortablePackageController(spawn=spawn)
    for index in range(64):
        controller.stop(descriptor, action_id=f"00000000-0000-4000-8000-{index:012d}")

    with pytest.raises(PortableControlError) as error:
        controller.stop(descriptor, action_id="00000000-0000-4000-8000-000000000064")
    assert error.value.code == "PORTABLE_ACTION_CAPACITY"
    assert len(processes) == 64
    assert all(process.terminated is False for process in processes)


def test_action_tracker_reserves_capacity_before_concurrent_spawn(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    processes: list[FakeProcess] = []
    processes_lock = threading.Lock()

    def spawn(*_args, **_kwargs):
        with processes_lock:
            process = FakeProcess(pid=1000 + len(processes))
            processes.append(process)
            return process

    controller = PortablePackageController(spawn=spawn)

    def launch(index: int) -> str:
        try:
            controller.stop(
                descriptor,
                action_id=f"10000000-0000-4000-8000-{index:012d}",
            )
            return "spawned"
        except PortableControlError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=80) as executor:
        results = list(executor.map(launch, range(80)))

    assert results.count("spawned") == 64
    assert results.count("PORTABLE_ACTION_CAPACITY") == 16
    assert len(processes) == 64
    assert all(process.terminated is False for process in processes)


def test_action_tracker_does_not_evict_process_when_poll_state_is_unknown(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    processes: list[FakeProcess] = []

    def spawn(*_args, **_kwargs):
        if not processes:
            process: FakeProcess = FlakyPollProcess(poll_failures=2, pid=1500)
        else:
            process = FakeProcess(pid=1500 + len(processes))
        processes.append(process)
        return process

    controller = PortablePackageController(spawn=spawn)
    action_ids = [f"15000000-0000-4000-8000-{index:012d}" for index in range(65)]
    for action_id in action_ids[:64]:
        controller.stop(descriptor, action_id=action_id)

    with pytest.raises(PortableControlError) as capacity:
        controller.stop(descriptor, action_id=action_ids[64])
    assert capacity.value.code == "PORTABLE_ACTION_CAPACITY"
    assert len(processes) == 64
    assert all(process.terminated is False for process in processes)

    with pytest.raises(PortableControlError) as unavailable:
        controller.action_status(descriptor, action_id=action_ids[0])
    assert unavailable.value.code == "PORTABLE_LAUNCH_FAILED"
    assert controller.action_status(descriptor, action_id=action_ids[0])["status"] == "stopping"


def test_action_status_keeps_record_after_transient_poll_failure(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = FlakyPollProcess(poll_failures=1)
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: process)
    action_id = "15111111-1111-4111-8111-111111111111"
    controller.stop(descriptor, action_id=action_id)

    with pytest.raises(PortableControlError) as unavailable:
        controller.action_status(descriptor, action_id=action_id)
    assert unavailable.value.code == "PORTABLE_LAUNCH_FAILED"

    assert controller.action_status(descriptor, action_id=action_id)["status"] == "stopping"


def test_duplicate_action_id_is_rejected_before_spawn(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    processes: list[FakeProcess] = []

    def spawn(*_args, **_kwargs):
        process = FakeProcess(pid=2000 + len(processes))
        processes.append(process)
        return process

    controller = PortablePackageController(spawn=spawn)
    action_id = "22222222-2222-4222-8222-222222222222"
    controller.stop(descriptor, action_id=action_id)

    with pytest.raises(PortableControlError) as error:
        controller.stop(descriptor, action_id=action_id)

    assert error.value.code == "PORTABLE_ACTION_EXISTS"
    assert len(processes) == 1
    assert processes[0].terminated is False


def test_duplicate_action_id_is_rejected_while_first_spawn_is_reserved(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    spawn_entered = threading.Event()
    allow_spawn = threading.Event()
    processes: list[FakeProcess] = []

    def spawn(*_args, **_kwargs):
        process = FakeProcess(pid=2100)
        processes.append(process)
        spawn_entered.set()
        assert allow_spawn.wait(timeout=2)
        return process

    controller = PortablePackageController(spawn=spawn)
    action_id = "22222222-2222-4222-8222-222222222222"
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(controller.stop, descriptor, action_id=action_id)
        assert spawn_entered.wait(timeout=2)

        with pytest.raises(PortableControlError) as error:
            controller.stop(descriptor, action_id=action_id)
        assert error.value.code == "PORTABLE_ACTION_EXISTS"
        assert len(processes) == 1

        allow_spawn.set()
        assert first.result(timeout=2)["status"] == "stopping"


def test_spawn_failure_releases_action_reservation_for_retry(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    calls = 0

    def spawn(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fixture spawn failure")
        return FakeProcess()

    controller = PortablePackageController(spawn=spawn)
    action_id = "22222222-2222-4222-8222-222222222222"

    with pytest.raises(PortableControlError) as error:
        controller.stop(descriptor, action_id=action_id)
    assert error.value.code == "PORTABLE_LAUNCH_FAILED"

    result = controller.stop(descriptor, action_id=action_id)
    assert result["status"] == "stopping"
    assert calls == 2


def test_synchronous_completion_releases_action_reservation(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    processes = [FakeProcess(returncode=0), FakeProcess()]
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: processes.pop(0))
    action_id = "22222222-2222-4222-8222-222222222222"

    assert controller.stop(descriptor, action_id=action_id)["status"] == "stopped"
    retry = controller.stop(descriptor, action_id=action_id)

    assert retry["status"] == "stopping"
    assert retry["action_id"] == action_id


def test_commit_self_heals_when_reservation_is_missing_after_spawn(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = FakeProcess(pid=2500)
    controller: PortablePackageController

    def spawn(*_args, **_kwargs):
        with controller._actions_lock:
            controller._action_reservations.clear()
        return process

    controller = PortablePackageController(spawn=spawn)
    action_id = "25222222-2222-4222-8222-222222222222"

    result = controller.stop(descriptor, action_id=action_id)

    assert result["action_id"] == action_id
    assert controller.action_status(descriptor, action_id=action_id)["controller_pid"] == 2500
    assert process.terminated is False


def test_commit_is_idempotent_for_the_same_tracked_process(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = FakeProcess(pid=2550)
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: process)
    action_id = "25555555-5555-4555-8555-555555555555"
    controller.stop(descriptor, action_id=action_id)
    tracked = controller._actions[action_id]

    committed_id = controller._commit_action_reservation(
        action_id,
        tracked.action,
        process,
        tracked.context,
    )

    assert committed_id == action_id
    assert list(controller._actions) == [action_id]
    assert controller.action_status(descriptor, action_id=action_id)["controller_pid"] == 2550


def test_commit_rescues_new_process_without_overwriting_unexpected_collision(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    old_process = FakeProcess(pid=2600)
    new_process = FakeProcess(pid=2601)
    source_id = "26000000-0000-4000-8000-000000000000"
    requested_id = "26000000-0000-4000-8000-000000000001"
    calls = 0
    controller: PortablePackageController

    def spawn(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return old_process
        with controller._actions_lock:
            controller._actions[requested_id] = controller._actions.pop(source_id)
            controller._action_reservations.discard(requested_id)
        return new_process

    controller = PortablePackageController(spawn=spawn)
    controller.stop(descriptor, action_id=source_id)

    result = controller.stop(descriptor, action_id=requested_id)

    rescue_id = str(result["action_id"])
    assert rescue_id != requested_id
    assert controller.action_status(descriptor, action_id=requested_id)["controller_pid"] == 2600
    assert controller.action_status(descriptor, action_id=rescue_id)["controller_pid"] == 2601
    assert old_process.terminated is False
    assert new_process.terminated is False


@pytest.mark.parametrize(
    ("action", "expected_status", "terminal_status"),
    [("stop", "stopping", "stopped"), ("repair", "repairing", "completed")],
)
def test_wait_error_is_tracked_as_unknown_active_action(
    tmp_path: Path,
    action: str,
    expected_status: str,
    terminal_status: str,
) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = WaitErrorProcess(wait_failures=1, pid=2700)
    job = FakeJob()
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: process,
        job_factory=lambda: job,
    )
    action_id = "27000000-0000-4000-8000-000000000000"

    result = getattr(controller, action)(descriptor, action_id=action_id)

    assert result["status"] == expected_status
    assert result["action_id"] == action_id
    assert controller.action_status(descriptor, action_id=action_id)["status"] == expected_status
    assert job.close_calls == 0
    process.returncode = 0
    assert controller.action_status(descriptor, action_id=action_id)["status"] == terminal_status
    assert job.close_calls == 1


def test_post_spawn_identity_failure_terminates_job_tree_and_releases_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = FakeProcess(pid=2750)
    job = FakeJob()
    monkeypatch.setattr(portable_control, "_execution_identity_guard", lambda _context: nullcontext())

    def spawn(*_args, **_kwargs):
        (package / "Stop.cmd").write_text("@echo changed\n", encoding="utf-8")
        return process

    controller = PortablePackageController(spawn=spawn, job_factory=lambda: job)
    action_id = "27500000-0000-4000-8000-000000000000"

    with pytest.raises(PortableControlError) as failure:
        controller.stop(descriptor, action_id=action_id)

    assert failure.value.code == "PORTABLE_IDENTITY_CHANGED"
    assert job.terminate_calls == 1
    assert job.close_calls == 1
    assert action_id not in controller._actions
    assert action_id not in controller._action_reservations


@pytest.mark.parametrize("failure_point", ["assign", "resume"])
def test_job_setup_failure_terminates_tree_before_releasing_reservation(
    tmp_path: Path,
    failure_point: str,
) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = FakeProcess(pid=2800)
    job = FakeJob(
        fail_assign=failure_point == "assign",
        fail_resume=failure_point == "resume",
    )
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: process,
        job_factory=lambda: job,
    )
    action_id = "28000000-0000-4000-8000-000000000000"

    with pytest.raises(PortableControlError) as failure:
        controller.stop(descriptor, action_id=action_id)

    assert failure.value.code == "PORTABLE_LAUNCH_FAILED"
    assert job.terminate_calls == 1
    assert job.close_calls == 1
    assert action_id not in controller._actions
    assert action_id not in controller._action_reservations


def test_async_action_holds_job_until_terminal_and_closes_it_once(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = FakeProcess(pid=2850)
    job = FakeJob()
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: process,
        job_factory=lambda: job,
    )
    action_id = "28500000-0000-4000-8000-000000000000"

    result = controller.stop(descriptor, action_id=action_id)

    assert result["status"] == "stopping"
    assert job.assigned == [process]
    assert job.resumed == [process]
    assert job.terminate_calls == 0
    assert job.close_calls == 0
    process.returncode = 0
    assert controller.action_status(descriptor, action_id=action_id)["status"] == "stopped"
    assert controller.action_status(descriptor, action_id=action_id)["status"] == "stopped"
    assert job.close_calls == 1


def test_action_status_identity_drift_blocks_and_terminates_job_tree_repeatably(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    process = FakeProcess(pid=2875)
    job = FakeJob()
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: process,
        job_factory=lambda: job,
    )
    action_id = "28750000-0000-4000-8000-000000000000"
    controller.stop(descriptor, action_id=action_id)
    (package / "Stop.cmd").write_text("@echo identity drift\n", encoding="utf-8")

    first = controller.action_status(descriptor, action_id=action_id)
    second = controller.action_status(descriptor, action_id=action_id)

    assert first == second
    assert first["status"] == "blocked"
    assert first["error_code"] == "PORTABLE_IDENTITY_CHANGED"
    assert job.terminate_calls == 1
    assert job.close_calls == 1
    assert action_id in controller._actions


def test_capacity_lru_eviction_closes_terminal_action_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portable_control, "_MAX_ACTIVE_ACTIONS", 2)
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    processes = [FakeProcess(pid=2900 + index) for index in range(3)]
    jobs = [FakeJob() for _index in range(3)]
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: processes.pop(0),
        job_factory=lambda: jobs.pop(0),
    )
    action_ids = [f"29000000-0000-4000-8000-{index:012d}" for index in range(3)]

    controller.stop(descriptor, action_id=action_ids[0])
    first_job = controller._actions[action_ids[0]].job
    controller.stop(descriptor, action_id=action_ids[1])
    processes_by_id = {
        action_id: controller._actions[action_id].process for action_id in action_ids[:2]
    }
    processes_by_id[action_ids[0]].returncode = 0

    controller.stop(descriptor, action_id=action_ids[2])

    assert isinstance(first_job, FakeJob)
    assert first_job.close_calls == 1
    with pytest.raises(PortableControlError) as evicted:
        controller.action_status(descriptor, action_id=action_ids[0])
    assert evicted.value.code == "PORTABLE_ACTION_NOT_FOUND"


def test_start_does_not_create_kill_on_close_job(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT")
    _write_operation_state(package, _operation_payload(status="starting"))
    process = FakeProcess(pid=2950)
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: process,
        job_factory=lambda: pytest.fail("Start must not create a kill-on-close job"),
    )

    result = controller.start(read_portable_package(package), operation_id=OPERATION_ID)

    assert result["status"] == "starting"


def test_action_tracker_lru_evicts_only_oldest_terminal_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portable_control, "_MAX_ACTIVE_ACTIONS", 3)
    package = _write_package(tmp_path / "GPT")
    descriptor = read_portable_package(package)
    processes: list[FakeProcess] = []

    def spawn(*_args, **_kwargs):
        process = FakeProcess(pid=3000 + len(processes))
        processes.append(process)
        return process

    controller = PortablePackageController(spawn=spawn)
    action_ids = [
        "30000000-0000-4000-8000-000000000000",
        "30000000-0000-4000-8000-000000000001",
        "30000000-0000-4000-8000-000000000002",
        "30000000-0000-4000-8000-000000000003",
    ]
    for action_id in action_ids[:3]:
        controller.stop(descriptor, action_id=action_id)
    processes[0].returncode = 0
    processes[1].returncode = 0

    # Refresh the oldest terminal record so the second record becomes the LRU.
    assert controller.action_status(descriptor, action_id=action_ids[0])["status"] == "stopped"
    controller.stop(descriptor, action_id=action_ids[3])

    with pytest.raises(PortableControlError) as evicted:
        controller.action_status(descriptor, action_id=action_ids[1])
    assert evicted.value.code == "PORTABLE_ACTION_NOT_FOUND"
    assert controller.action_status(descriptor, action_id=action_ids[0])["status"] == "stopped"
    assert controller.action_status(descriptor, action_id=action_ids[2])["status"] == "stopping"
    assert controller.action_status(descriptor, action_id=action_ids[3])["status"] == "stopping"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree control is platform-specific")
def test_real_windows_known_failure_job_kills_powershell_descendant(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "job-tree-package")
    child_pid_file = package / "job-child.pid"
    (package / "spawn-child.ps1").write_text(
        "$child = Start-Process -FilePath \"$env:SystemRoot\\System32\\cmd.exe\" "
        "-ArgumentList @('/d','/c','ping.exe -n 31 127.0.0.1 > nul') -PassThru\n"
        "[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'job-child.pid'), [string]$child.Id)\n"
        "exit 9\n",
        encoding="utf-8",
    )
    (package / "Stop.cmd").write_text(
        "@echo off\n"
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0spawn-child.ps1"\n'
        "@exit /b %errorlevel%\n",
        encoding="utf-8",
    )
    action_id = "29999999-9999-4999-8999-999999999999"

    with pytest.raises(PortableControlError) as failure:
        PortablePackageController(handshake_seconds=2.0).stop(
            read_portable_package(package),
            action_id=action_id,
        )

    assert failure.value.code == "PORTABLE_LAUNCH_EXITED"
    assert child_pid_file.is_file()
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _windows_process_is_running(child_pid):
        time.sleep(0.05)
    survived = _windows_process_is_running(child_pid)
    if survived:
        taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        subprocess.run(
            [str(taskkill), "/PID", str(child_pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    assert survived is False


def _windows_process_is_running(pid: int) -> bool:
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


@pytest.mark.skipif(os.name != "nt", reason="real cmd.exe contract is Windows-only")
@pytest.mark.parametrize(
    "directory_name",
    [
        "普通 package with spaces",
        "中文 & caret^ (括号) %百分% !感叹!",
    ],
)
def test_real_windows_cmd_runs_fixed_literal_launcher_from_special_cwd(
    tmp_path: Path, directory_name: str
) -> None:
    package = _write_package(tmp_path / directory_name)
    marker = package / "real-start.marker"
    (package / "Start.cmd").write_text(
        _ready_start_batch('> "%~dp0real-start.marker" echo SAFE_MARKER'),
        encoding="utf-8",
    )

    result = PortablePackageController().start(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert marker.read_text(encoding="utf-8").strip() == "SAFE_MARKER"
    assert result["status"] == "ready"
    assert result["status"] != "starting"


@pytest.mark.skipif(os.name != "nt", reason="real cmd.exe contract is Windows-only")
def test_real_windows_zero_exit_with_marker_but_no_operation_is_rejected(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path / "marker-no-operation")
    marker = package / "ran.marker"
    (package / "Start.cmd").write_text(
        '@echo off\r\n> "%~dp0ran.marker" echo RAN\r\nexit /b 0\r\n',
        encoding="utf-8",
    )

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().start(
            read_portable_package(package), operation_id=OPERATION_ID
        )

    assert marker.read_text(encoding="utf-8").strip() == "RAN"
    assert error.value.code == "PORTABLE_OPERATION_MISSING"


@pytest.mark.skipif(os.name != "nt", reason="Windows system executable lookup is Windows-only")
def test_real_windows_cmd_ignores_cwd_and_path_name_hijacks(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    marker = package / "system-cmd.marker"
    (package / "Start.cmd").write_text(
        _ready_start_batch("> system-cmd.marker echo SYSTEM_CMD"),
        encoding="utf-8",
    )
    (package / "cmd.exe").write_bytes(b"MZ-not-a-real-system-command")
    path_hijack = tmp_path / "path-hijack"
    path_hijack.mkdir()
    (path_hijack / "cmd.exe").write_bytes(b"MZ-not-a-real-system-command")
    environment = dict(os.environ)
    environment["PATH"] = f"{path_hijack};{environment.get('PATH', '')}"

    result = PortablePackageController(environment=environment).start(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert marker.read_text(encoding="utf-8").strip() == "SYSTEM_CMD"
    assert result["status"] == "ready"


@pytest.mark.skipif(os.name != "nt", reason="real cmd.exe timing is Windows-only")
def test_real_windows_short_delayed_failure_is_never_reported_starting(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    (package / "Start.cmd").write_text(
        "@echo off\r\n"
        "powershell -NoProfile -NonInteractive -Command \"Start-Sleep -Milliseconds 80\"\r\n"
        "exit /b 9\r\n",
        encoding="utf-8",
    )

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().start(
            read_portable_package(package), operation_id=OPERATION_ID
        )

    assert error.value.code == "PORTABLE_LAUNCH_EXITED"


@pytest.mark.skipif(os.name != "nt", reason="real cmd.exe timing is Windows-only")
def test_real_windows_long_controller_returns_starting_after_bounded_handshake(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    (package / "Start.cmd").write_text(
        "@echo off\r\n"
        "powershell -NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 2\"\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )

    started_at = time.monotonic()
    result = PortablePackageController().start(
        read_portable_package(package), operation_id=OPERATION_ID
    )
    elapsed = time.monotonic() - started_at

    assert result["status"] == "starting"
    assert elapsed < 1.0
    time.sleep(2.2)


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode guard is Windows-only")
@pytest.mark.parametrize("target", ["launcher", "manifest"])
def test_real_windows_execution_guard_blocks_launcher_or_manifest_replacement(
    tmp_path: Path, target: str
) -> None:
    package = _write_package(tmp_path / f"guard-{target}")
    if target == "launcher":
        command = '> "%~f0" echo REPLACED'
    else:
        command = 'del /f /q "package\\tts-more-package.json"'
    original_launcher = _ready_start_batch(
        command,
        "if errorlevel 1 exit /b 17",
    )
    (package / "Start.cmd").write_text(original_launcher, encoding="utf-8")
    original_launcher_bytes = (package / "Start.cmd").read_bytes()

    result = PortablePackageController().start(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert result["status"] == "ready"
    assert (package / "Start.cmd").read_bytes() == original_launcher_bytes
    assert (package / "package" / "tts-more-package.json").is_file()


@pytest.mark.parametrize("operation_id", ["../escape", "{11111111-1111-4111-8111-111111111111}", "11111111-1111-4111-8111-111111111111 & whoami", 7])
def test_controller_rejects_noncanonical_operation_id_without_spawning(tmp_path: Path, operation_id: object) -> None:
    package = _write_package(tmp_path / "package")
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn"))
    with pytest.raises(PortableControlError, match="canonical UUID"):
        controller.start(read_portable_package(package), operation_id=operation_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", [0, 65536, True, "9880", "9880 & whoami"])
def test_controller_rejects_noninteger_or_out_of_range_port_without_spawning(tmp_path: Path, port: object) -> None:
    package = _write_package(tmp_path / "package")
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn"))
    with pytest.raises(PortableControlError, match="port_override"):
        controller.start(read_portable_package(package), operation_id=OPERATION_ID, port_override=port)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__("package_id", "gpt-other"),
        lambda payload: payload.__setitem__("build_id", "build-two"),
        lambda payload: payload["protocol"].__setitem__("version", "2.0"),
        lambda payload: payload["protocol"].__setitem__("controller_range", ">=0.3.0,<0.4.0"),
    ],
)
def test_controller_fails_closed_when_descriptor_identity_drifts(tmp_path: Path, mutation) -> None:
    package = _write_package(tmp_path / "package")
    descriptor = read_portable_package(package)
    _rewrite_manifest(package, mutation)
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn"))

    with pytest.raises(PortableControlError, match="identity changed"):
        controller.start(descriptor, operation_id=OPERATION_ID)


@pytest.mark.parametrize("action", ["start", "stop", "repair"])
def test_controller_rejects_launcher_hardlinks(tmp_path: Path, action: str) -> None:
    package = _write_package(tmp_path / "package")
    descriptor = read_portable_package(package)
    original = package / f"{action.title()}.cmd"
    outside = tmp_path / "outside.cmd"
    outside.write_text("@echo off\n", encoding="utf-8")
    original.unlink()
    _hardlink(original, outside)
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn"))

    with pytest.raises(PortableControlError, match="package|launcher|hard link"):
        getattr(controller, action)(descriptor, **({"operation_id": OPERATION_ID} if action == "start" else {}))


def test_controller_detects_launcher_swap_during_spawn_and_terminates_controller(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    process = FakeProcess()

    def swap_during_spawn(_command, **_kwargs):
        launcher = package / "Start.cmd"
        launcher.write_text("@echo changed\n", encoding="utf-8")
        return process

    controller = PortablePackageController(spawn=swap_during_spawn)
    with pytest.raises(PortableControlError) as error:
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)
    if os.name == "nt":
        assert error.value.code == "PORTABLE_LAUNCH_FAILED"
        assert process.terminated is False
        assert (package / "Start.cmd").read_text(encoding="utf-8") == "@echo off\n"
    else:
        assert error.value.code == "PORTABLE_IDENTITY_CHANGED"
        assert process.terminated is True


def test_controller_detects_package_root_swap_during_spawn(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    moved = tmp_path / "moved-original"
    process = FakeProcess()

    def swap_root(_command, **_kwargs):
        package.rename(moved)
        _write_package(package)
        return process

    controller = PortablePackageController(spawn=swap_root)
    with pytest.raises(PortableControlError) as error:
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)
    if os.name == "nt":
        assert error.value.code == "PORTABLE_LAUNCH_FAILED"
        assert process.terminated is False
        assert not moved.exists()
    else:
        assert error.value.code == "PORTABLE_IDENTITY_CHANGED"
        assert process.terminated is True


def test_controller_allows_launcher_to_create_package_data_children(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")

    def create_runtime_data(_command, **_kwargs):
        (package / "data" / "local" / "operations").mkdir(parents=True)
        return FakeProcess()

    result = PortablePackageController(spawn=create_runtime_data).start(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert result["status"] == "starting"


def test_controller_rejects_package_root_and_manifest_directory_junctions(tmp_path: Path) -> None:
    physical = _write_package(tmp_path / "physical")
    linked = tmp_path / "linked"
    _junction(linked, physical)
    descriptor = read_portable_package(physical).model_copy(update={"package_root": str(linked)})
    with pytest.raises(PortableControlError, match="freshly validated"):
        PortablePackageController(spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn")).start(
            descriptor, operation_id=OPERATION_ID
        )

    package = _write_package(tmp_path / "manifest-package")
    manifest_directory = package / "package"
    outside_manifest = tmp_path / "outside-manifest"
    manifest_directory.rename(outside_manifest)
    _junction(manifest_directory, outside_manifest)
    with pytest.raises(OSError, match="reparse"):
        read_portable_package(package)


def test_stop_repair_and_open_folder_use_only_fixed_commands(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    calls: list[list[str]] = []
    controller = PortablePackageController(spawn=lambda command, **_kwargs: calls.append(command) or FakeProcess())
    descriptor = read_portable_package(package)

    assert controller.stop(descriptor)["status"] == "stopping"
    assert controller.repair(descriptor)["status"] == "repairing"
    assert controller.open_folder(descriptor)["status"] == "opened"
    assert all(Path(command[0]).is_absolute() for command in calls)
    assert [Path(command[0]).name.casefold() for command in calls] == ["cmd.exe", "cmd.exe", "explorer.exe"]
    assert calls[0][1:] == ["/d", "/c", "Stop.cmd"]
    assert calls[1][1:] == ["/d", "/c", "Repair.cmd"]
    assert calls[2][1:] == [str(package)]


def test_spawn_failure_has_stable_error_and_never_reports_starting(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")

    def fail(*_args, **_kwargs):
        raise OSError("localized operating system detail")

    with pytest.raises(PortableControlError) as error:
        PortablePackageController(spawn=fail).start(read_portable_package(package), operation_id=OPERATION_ID)
    assert error.value.code == "PORTABLE_LAUNCH_FAILED"
    assert str(error.value) == "portable start launcher could not be started"


def test_immediate_launcher_failure_is_blocked_not_starting(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: FakeProcess(returncode=9))
    with pytest.raises(PortableControlError) as error:
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)
    assert error.value.code == "PORTABLE_LAUNCH_EXITED"


def test_immediate_success_reports_terminal_operation_status(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    _write_operation_state(package, _operation_payload(
        status="ready", exit_code=0, finished_at="2026-07-15T00:00:01Z"
    ))
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: FakeProcess(returncode=0)
    )

    result = controller.start(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert result["status"] == "ready"


def test_immediate_zero_exit_without_operation_is_never_success(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: FakeProcess(returncode=0)
    )

    with pytest.raises(PortableControlError) as error:
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)

    assert error.value.code == "PORTABLE_OPERATION_MISSING"


def test_immediate_success_never_reports_completed_for_nonterminal_operation(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path / "package")
    _write_operation_state(package, _operation_payload(status="starting"))
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: FakeProcess(returncode=0)
    )

    with pytest.raises(PortableControlError) as error:
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)

    assert error.value.code == "PORTABLE_OPERATION_INCOMPLETE"


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (_operation_payload(status="blocked", exit_code=22, finished_at="2026-07-15T00:00:01Z"), "PORTABLE_OPERATION_FAILED"),
        (_operation_payload(status="repairable", exit_code=21, finished_at="2026-07-15T00:00:01Z"), "PORTABLE_OPERATION_FAILED"),
        (_operation_payload(status="stopped", exit_code=20, finished_at="2026-07-15T00:00:01Z"), "PORTABLE_OPERATION_FAILED"),
        (_operation_payload(status="ready", exit_code=7, finished_at="2026-07-15T00:00:01Z"), "PORTABLE_OPERATION_INVALID"),
    ],
    ids=["blocked", "repairable", "stopped", "inconsistent-ready"],
)
def test_immediate_zero_exit_accepts_only_strict_ready_operation(
    tmp_path: Path, payload: dict[str, object], expected_code: str
) -> None:
    package = _write_package(tmp_path / "package")
    _write_operation_state(package, payload)
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: FakeProcess(returncode=0)
    )

    with pytest.raises(PortableControlError) as error:
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)

    assert error.value.code == expected_code


def test_running_controller_with_valid_nonterminal_operation_returns_starting(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path / "package")
    _write_operation_state(package, _operation_payload(status="starting"))
    controller = PortablePackageController(
        spawn=lambda *_args, **_kwargs: FakeProcess(returncode=None)
    )

    result = controller.start(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert result["status"] == "starting"


def test_status_and_logs_read_only_schema_bound_package_data(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    operations = package / "data" / "local" / "operations" / OPERATION_ID
    operations.mkdir(parents=True)
    (operations / "operation.json").write_text(
        json.dumps({
            "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
            "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z", "status": "starting", "exit_code": None,
        }), encoding="utf-8",
    )
    (operations / "events.jsonl").write_text(
        "\n".join(json.dumps({"seq": i, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": f"event-{i}"}) for i in range(1, 6)) + "\n",
        encoding="utf-8",
    )
    record = package / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({
        "schema_version": 2, "pid": 1234, "parent_pid": 100, "child_pids": [],
        "process_created_at": "2026-07-15T00:00:00Z", "recorded_at": "2026-07-15T00:00:01Z",
        "executable_path": str(package / "runtime/live/python.exe"),
        "command_sha256": "a" * 64, "port": 9880, "package_root": str(package.resolve()), "build_id": "build-one",
    }), encoding="utf-8")
    controller = PortablePackageController(spawn=lambda *_args, **_kwargs: pytest.fail("reads must not spawn"))
    descriptor = read_portable_package(package)

    assert controller.status(descriptor, operation_id=OPERATION_ID)["operation"]["status"] == "starting"
    logs = controller.logs(descriptor, operation_id=OPERATION_ID, after_seq=2, limit=2)
    assert [event["seq"] for event in logs["events"]] == [3, 4]
    assert logs["next_seq"] == 4
    pid_status = controller.status(descriptor)
    assert pid_status["process_record"]["pid"] == 1234
    assert pid_status["running"] is None


def test_status_normalizes_operation_timestamps_before_projection(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    _write_operation_state(package, _operation_payload(
        started_at="2026-07-15T08:00:00+08:00",
        status="ready",
        exit_code=0,
        finished_at="2026-07-15T08:00:01+08:00",
    ))

    operation = PortablePackageController().status(
        read_portable_package(package), operation_id=OPERATION_ID
    )["operation"]

    assert operation["started_at"] == "2026-07-15T00:00:00+00:00"
    assert operation["finished_at"] == "2026-07-15T00:00:01+00:00"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("initiator"),
        lambda payload: payload.update(exit_code="0", status="ready", finished_at="2026-07-15T00:00:01Z"),
        lambda payload: payload.update(status="ready", exit_code=0),
        lambda payload: payload.update(exit_code=7),
        lambda payload: payload.update(secret="must-not-cross-api"),
        lambda payload: payload.update(initiator="tts-more\nheader-injection"),
        lambda payload: payload.update(action=["start"]),
    ],
    ids=[
        "missing-initiator",
        "string-exit-code",
        "terminal-without-finished-at",
        "nonterminal-with-exit-code",
        "unknown-sensitive-field",
        "control-character-initiator",
        "non-string-action",
    ],
)
def test_status_rejects_non_exact_operation_protocol(
    tmp_path: Path, mutation
) -> None:
    package = _write_package(tmp_path / "package")
    payload = _operation_payload()
    mutation(payload)
    _write_operation_state(package, payload)

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().status(
            read_portable_package(package), operation_id=OPERATION_ID
        )

    assert error.value.code == "PORTABLE_OPERATION_INVALID"


@pytest.mark.parametrize(
    "event",
    [
        {"seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": "ok", "secret": "token"},
        {"seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": "ok", "percent": True},
        {"seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": "ok", "percent": 101},
        {"seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": "ok", "error_code": "secret value"},
        {"seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": ["checking"], "message": "ok"},
    ],
    ids=["unknown-field", "boolean-percent", "percent-out-of-range", "unsafe-error-code", "non-string-phase"],
)
def test_logs_reject_non_exact_event_protocol(tmp_path: Path, event: dict[str, object]) -> None:
    package = _write_package(tmp_path / "package")
    operation = _write_operation_state(package, _operation_payload())
    (operation / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().logs(
            read_portable_package(package), operation_id=OPERATION_ID
        )

    assert error.value.code == "PORTABLE_EVENT_INVALID"


def test_logs_project_only_protocol_fields_and_redact_sensitive_message_data(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path / "package")
    operation = _write_operation_state(package, _operation_payload())
    raw_message = (
        rf"loaded {package}\private\voice.wav from C:\Users\xuyu_\secret.wav "
        "Authorization: Bearer abc.def.ghi api_key=top-secret "
        "https://alice:password@example.invalid/path DESKTOP-SECRET"
    )
    event = {
        "seq": 1,
        "timestamp": "2026-07-15T00:00:00Z",
        "phase": "checking",
        "message": raw_message,
        "percent": 42.5,
        "error_code": "CUDA_PROBE_FAILED",
    }
    (operation / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    returned = PortablePackageController().logs(
        read_portable_package(package), operation_id=OPERATION_ID
    )["events"][0]

    assert set(returned) == {"seq", "timestamp", "phase", "message", "percent", "error_code"}
    message = str(returned["message"])
    for secret in (str(package), "C:\\Users\\xuyu_", "abc.def.ghi", "top-secret", "alice", "password", "DESKTOP-SECRET"):
        assert secret.casefold() not in message.casefold()
    assert "[REDACTED" in message


def test_pid_status_never_returns_paths_or_command_digest(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    record = package / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({
        "schema_version": 2,
        "pid": 1234,
        "parent_pid": 100,
        "child_pids": [1235],
        "process_created_at": "2026-07-15T00:00:00Z",
        "recorded_at": "2026-07-15T00:00:01Z",
        "executable_path": str(package / "runtime/live/python.exe"),
        "command_sha256": "a" * 64,
        "port": 9880,
        "package_root": str(package.resolve()),
        "build_id": "build-one",
    }), encoding="utf-8")

    result = PortablePackageController().status(read_portable_package(package))["process_record"]

    assert set(result) == {
        "schema_version", "pid", "parent_pid", "child_pids", "process_created_at",
        "recorded_at", "port", "build_id",
    }
    assert result["process_created_at"] == "2026-07-15T00:00:00+00:00"
    assert result["recorded_at"] == "2026-07-15T00:00:01+00:00"


def test_require_stopped_allows_only_an_absent_fixed_pid_record(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    descriptor = read_portable_package(package)
    controller = PortablePackageController()

    controller.require_stopped(descriptor)

    record = package / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text("not-json", encoding="utf-8")
    with pytest.raises(PortableControlError) as present:
        controller.require_stopped(descriptor)
    assert present.value.code == "PORTABLE_WORKER_NOT_STOPPED"


def test_require_stopped_blocks_identity_drifted_pid_record_and_reparse(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path / "package")
    descriptor = read_portable_package(package)
    record = package / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({**_pid_payload(package), "build_id": "wrong-build"}), encoding="utf-8")

    with pytest.raises(PortableControlError) as drifted:
        PortablePackageController().require_stopped(descriptor)
    assert drifted.value.code == "PORTABLE_WORKER_NOT_STOPPED"

    record.unlink()
    record.parent.rmdir()
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (outside / "worker.pid.json").write_text("{}", encoding="utf-8")
    _junction(record.parent, outside)
    with pytest.raises(PortableControlError) as linked:
        PortablePackageController().require_stopped(descriptor)
    assert linked.value.code in {"PORTABLE_PATH_REPARSE", "PORTABLE_WORKER_NOT_STOPPED"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("process_created_at"),
        lambda payload: payload.pop("recorded_at"),
        lambda payload: payload.update(process_created_at="not-a-timestamp"),
        lambda payload: payload.update(process_created_at="2026-07-15T00:00:00"),
        lambda payload: payload.update(recorded_at=1720000000),
        lambda payload: payload.update(extra={"token": "must-not-be-accepted"}),
    ],
    ids=[
        "missing-created-at",
        "missing-recorded-at",
        "malformed-created-at",
        "naive-created-at",
        "numeric-recorded-at",
        "unknown-extra-structure",
    ],
)
def test_pid_status_rejects_non_exact_writer_schema(
    tmp_path: Path, mutation
) -> None:
    package = _write_package(tmp_path / "package")
    payload = _pid_payload(package)
    mutation(payload)
    record = package / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(payload), encoding="utf-8")

    result = PortablePackageController().status(read_portable_package(package))

    assert result["process_record"] is None
    assert result["running"] is None


@pytest.mark.parametrize("operation_id", ["../secret", "not-a-uuid"])
def test_logs_reject_operation_traversal(tmp_path: Path, operation_id: str) -> None:
    descriptor = read_portable_package(_write_package(tmp_path / "package"))
    with pytest.raises(PortableControlError, match="canonical UUID"):
        PortablePackageController().logs(descriptor, operation_id=operation_id)


def test_logs_reject_operation_directory_junction(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    outside = tmp_path / "outside" / OPERATION_ID
    outside.mkdir(parents=True)
    (outside / "operation.json").write_text("{}", encoding="utf-8")
    operations_root = package / "data" / "local" / "operations"
    operations_root.mkdir(parents=True)
    _junction(operations_root / OPERATION_ID, outside)

    with pytest.raises(PortableControlError, match="reparse"):
        PortablePackageController().logs(
            read_portable_package(package), operation_id=OPERATION_ID
        )


def test_logs_never_read_an_operation_event_hardlink(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    operation = package / "data" / "local" / "operations" / OPERATION_ID
    operation.mkdir(parents=True)
    (operation / "operation.json").write_text(json.dumps({
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z", "status": "starting", "exit_code": None,
    }), encoding="utf-8")
    outside = tmp_path / "outside-events.jsonl"
    outside.write_text(json.dumps({
        "seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": "outside",
    }) + "\n", encoding="utf-8")
    _hardlink(operation / "events.jsonl", outside)

    with pytest.raises(PortableControlError, match="hard link"):
        PortablePackageController().logs(
            read_portable_package(package), operation_id=OPERATION_ID
        )


def test_logs_are_size_bounded(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    operation = package / "data" / "local" / "operations" / OPERATION_ID
    operation.mkdir(parents=True)
    (operation / "operation.json").write_text(json.dumps({
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z", "status": "starting", "exit_code": None,
    }), encoding="utf-8")
    (operation / "events.jsonl").write_bytes(b"x" * (70 * 1024) + b"\n")
    with pytest.raises(PortableControlError, match="too large"):
        PortablePackageController().logs(read_portable_package(package), operation_id=OPERATION_ID)


def test_logs_ignore_only_a_torn_final_event_and_keep_completed_events(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    operation = package / "data" / "local" / "operations" / OPERATION_ID
    operation.mkdir(parents=True)
    (operation / "operation.json").write_text(json.dumps({
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z", "status": "starting", "exit_code": None,
    }), encoding="utf-8")
    complete = json.dumps({
        "seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": "complete",
    }).encode("utf-8") + b"\n"
    (operation / "events.jsonl").write_bytes(complete + b'{"seq":2')

    result = PortablePackageController().logs(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert [event["seq"] for event in result["events"]] == [1]


def test_logs_fail_closed_when_package_identity_drifts_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _write_package(tmp_path / "package")
    operation = package / "data" / "local" / "operations" / OPERATION_ID
    operation.mkdir(parents=True)
    (operation / "operation.json").write_text(json.dumps({
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z", "status": "starting", "exit_code": None,
    }), encoding="utf-8")
    (operation / "events.jsonl").write_text(json.dumps({
        "seq": 1, "timestamp": "2026-07-15T00:00:00Z", "phase": "checking", "message": "complete",
    }) + "\n", encoding="utf-8")
    original = portable_control._read_events

    def mutate_during_read(*args, **kwargs):
        result = original(*args, **kwargs)
        (package / "Start.cmd").write_text("@echo changed during read\n", encoding="utf-8")
        return result

    monkeypatch.setattr(portable_control, "_read_events", mutate_during_read)

    with pytest.raises(PortableControlError, match="changed during action"):
        PortablePackageController().logs(
            read_portable_package(package), operation_id=OPERATION_ID
        )


def test_status_retries_atomic_operation_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _write_package(tmp_path / "package")
    operation = package / "data" / "local" / "operations" / OPERATION_ID
    operation.mkdir(parents=True)
    operation_path = operation / "operation.json"
    operation_path.write_text(json.dumps({
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z",
        "status": "not_initialized", "exit_code": None,
    }), encoding="utf-8")
    replacement = operation / "operation.next.json"
    replacement.write_text(json.dumps({
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z",
        "status": "ready", "exit_code": 0, "finished_at": "2026-07-15T00:00:01Z",
    }), encoding="utf-8")
    original_open = portable_file_io._open_binary
    swapped = False

    def replace_before_open(current: Path):
        nonlocal swapped
        if not swapped and current.name == "operation.json":
            swapped = True
            os.replace(replacement, current)
        return original_open(current)

    monkeypatch.setattr(portable_file_io, "_open_binary", replace_before_open)

    result = PortablePackageController().status(
        read_portable_package(package), operation_id=OPERATION_ID
    )

    assert result["status"] == "ready"


def test_logs_never_return_after_operation_parent_switches_to_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _write_package(tmp_path / "package")
    operations_root = package / "data" / "local" / "operations"
    operation = operations_root / OPERATION_ID
    operation.mkdir(parents=True)
    payload = {
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z",
        "status": "not_initialized", "exit_code": None,
    }
    (operation / "operation.json").write_text(json.dumps(payload), encoding="utf-8")
    (operation / "events.jsonl").write_text("", encoding="utf-8")
    outside = tmp_path / "outside-operation"
    outside.mkdir()
    (outside / "operation.json").write_text(json.dumps(payload), encoding="utf-8")
    (outside / "events.jsonl").write_text("", encoding="utf-8")
    moved = operations_root / f"{OPERATION_ID}-old"
    original_open = portable_file_io._open_binary
    swapped = False

    def swap_parent_before_open(current: Path):
        nonlocal swapped
        if not swapped and current.name == "operation.json":
            swapped = True
            operation.rename(moved)
            _junction(operation, outside)
        return original_open(current)

    monkeypatch.setattr(portable_file_io, "_open_binary", swap_parent_before_open)

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().logs(
            read_portable_package(package), operation_id=OPERATION_ID
        )

    assert error.value.code in {"PORTABLE_PATH_REPARSE", "PORTABLE_FILE_CHANGED", "PORTABLE_PATH_ESCAPE"}


def test_status_maps_low_level_read_errors_to_stable_control_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _write_package(tmp_path / "package")
    operation = package / "data" / "local" / "operations" / OPERATION_ID
    operation.mkdir(parents=True)
    (operation / "operation.json").write_text(json.dumps({
        "operation_id": OPERATION_ID, "component": "gpt-sovits", "action": "start",
        "initiator": "tts-more", "started_at": "2026-07-15T00:00:00Z",
        "status": "not_initialized", "exit_code": None,
    }), encoding="utf-8")
    descriptor = read_portable_package(package)

    original_open = portable_file_io._open_binary

    def fail_operation_only(current: Path):
        if current.name == "operation.json":
            raise OSError("localized filesystem error")
        return original_open(current)

    monkeypatch.setattr(portable_file_io, "_open_binary", fail_operation_only)

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().status(
            descriptor, operation_id=OPERATION_ID
        )

    assert error.value.code == "PORTABLE_FILE_CHANGED"
    assert "localized filesystem error" not in str(error.value)


def test_action_context_maps_launcher_metadata_errors_to_stable_control_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _write_package(tmp_path / "package")
    descriptor = read_portable_package(package)
    original_lstat = Path.lstat

    def fail_launcher_only(current: Path):
        if current == package / "Start.cmd":
            raise OSError("localized launcher metadata detail")
        return original_lstat(current)

    monkeypatch.setattr(Path, "lstat", fail_launcher_only)

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().start(descriptor, operation_id=OPERATION_ID)

    assert error.value.code == "PORTABLE_PACKAGE_INVALID"
    assert "localized launcher metadata detail" not in str(error.value)


def test_operation_directory_metadata_errors_are_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _write_package(tmp_path / "package")
    operation = _write_operation_state(package, _operation_payload())
    descriptor = read_portable_package(package)
    original_exists = Path.exists

    def fail_operation_only(current: Path):
        if current == operation:
            raise OSError("localized operation metadata detail")
        return original_exists(current)

    monkeypatch.setattr(Path, "exists", fail_operation_only)

    with pytest.raises(PortableControlError) as error:
        PortablePackageController().status(descriptor, operation_id=OPERATION_ID)

    assert error.value.code == "PORTABLE_FILE_CHANGED"
    assert "localized operation metadata detail" not in str(error.value)


def test_pid_record_never_authorizes_running_or_stop(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    record = package / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"schema_version": 2, "pid": 999999}), encoding="utf-8")
    calls: list[list[str]] = []
    controller = PortablePackageController(spawn=lambda command, **_kwargs: calls.append(command) or FakeProcess())
    descriptor = read_portable_package(package)

    status = controller.status(descriptor)
    assert status["running"] is None
    controller.stop(descriptor)
    assert len(calls) == 1
    assert Path(calls[0][0]).is_absolute()
    assert Path(calls[0][0]).name.casefold() == "cmd.exe"
    assert calls[0][1:] == ["/d", "/c", "Stop.cmd"]


def test_status_rejects_a_partially_forged_pid_record_schema(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    record = package / "data" / "local" / "run" / "worker.pid.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({
        "schema_version": 2,
        "pid": 1234,
        "parent_pid": "not-an-integer",
        "child_pids": [],
        "process_created_at": "2026-07-15T00:00:00Z",
        "executable_path": str(package / "runtime/live/python.exe"),
        "command_sha256": "a" * 64,
        "port": 9880,
        "package_root": str(package.resolve()),
        "build_id": "build-one",
    }), encoding="utf-8")

    result = PortablePackageController().status(read_portable_package(package))

    assert result["process_record"] is None
    assert result["running"] is None
