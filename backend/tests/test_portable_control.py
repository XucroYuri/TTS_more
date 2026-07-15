from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app import portable_control
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
        return 0 if self.returncode is None else self.returncode

    def terminate(self) -> None:
        self.terminated = True


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
    environment = os.environ.copy()
    environment["B2_JUNCTION_PATH"] = str(link)
    environment["B2_JUNCTION_TARGET"] = str(target)
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
    if completed.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {completed.stderr}")


def test_controller_executes_exact_root_launcher_with_safe_process_contract(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT 包 & (便携)")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    controller = PortablePackageController(spawn=spawn, environment={"SYSTEMROOT": "C:/Windows", "UNSAFE": "drop"})
    result = controller.start(read_portable_package(package), operation_id=OPERATION_ID, port_override=9980)

    assert calls[0][0] == [
        "cmd.exe", "/d", "/c", str(package / "Start.cmd"),
        "-OperationId", OPERATION_ID, "-ManagedBy", "tts-more", "-NoUi", "-PortOverride", "9980",
    ]
    assert calls[0][1]["cwd"] == package
    assert calls[0][1]["close_fds"] is True
    assert calls[0][1]["env"] == {"SYSTEMROOT": "C:/Windows"}
    assert result == {"status": "starting", "action": "start", "operation_id": OPERATION_ID, "controller_pid": 42}


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
    os.link(outside, original)
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
    with pytest.raises(PortableControlError, match="changed during action"):
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)
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
    with pytest.raises(PortableControlError, match="changed during action"):
        controller.start(read_portable_package(package), operation_id=OPERATION_ID)
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
    with pytest.raises(PortableControlError, match="freshly validated"):
        PortablePackageController(spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn")).start(
            read_portable_package(package), operation_id=OPERATION_ID
        )


def test_stop_repair_and_open_folder_use_only_fixed_commands(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "package")
    calls: list[list[str]] = []
    controller = PortablePackageController(spawn=lambda command, **_kwargs: calls.append(command) or FakeProcess())
    descriptor = read_portable_package(package)

    assert controller.stop(descriptor)["status"] == "stopping"
    assert controller.repair(descriptor)["status"] == "repairing"
    assert controller.open_folder(descriptor)["status"] == "opened"
    assert calls == [
        ["cmd.exe", "/d", "/c", str(package / "Stop.cmd")],
        ["cmd.exe", "/d", "/c", str(package / "Repair.cmd")],
        ["explorer.exe", str(package)],
    ]


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
        "process_created_at": "2026-07-15T00:00:00Z", "executable_path": str(package / "runtime/live/python.exe"),
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
    os.link(outside, operation / "events.jsonl")

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
    assert calls == [["cmd.exe", "/d", "/c", str(package / "Stop.cmd")]]


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
