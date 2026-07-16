from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMMAND = ["-m", "uvicorn", "worker:app", "--port", "9880"]


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {result.stderr}")


def _load_launcher():
    module_path = REPO_ROOT / "scripts" / "portable_launcher.py"
    spec = importlib.util.spec_from_file_location("portable_launcher_v2", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(root: Path) -> dict[str, object]:
    return {
        "schema_version": 2,
        "pid": 4242,
        "parent_pid": 100,
        "child_pids": [4243],
        "process_created_at": "2026-07-14T01:02:03.000000+00:00",
        "recorded_at": "2026-07-14T01:02:04.000000+00:00",
        "executable_path": str(root / "runtime" / "live" / "python.exe"),
        "command_sha256": hashlib.sha256("\0".join(DEFAULT_COMMAND).encode()).hexdigest(),
        "port": 9880,
        "package_root": str(root),
        "build_id": "source-checkout",
    }


def test_write_process_record_contains_full_ownership_identity(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package root"
    executable = root / "runtime" / "live" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    path = root / "data" / "local" / "run" / "worker.pid.json"

    launcher.write_process_record(
        path,
        pid=4242,
        parent_pid=100,
        child_pids=[4243],
        process_created_at="2026-07-14T01:02:03.000000+00:00",
        executable_path=executable,
        command=[str(executable), "-m", "uvicorn", "worker:app"],
        port=9880,
        package_root=root,
        build_id="gpt-sovits-2.0.0-deadbeef",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["pid"] == 4242
    assert payload["parent_pid"] == 100
    assert payload["child_pids"] == [4243]
    assert payload["process_created_at"].startswith("2026-07-14")
    assert payload["executable_path"] == str(executable.resolve())
    assert payload["package_root"] == str(root.resolve())
    assert payload["build_id"] == "gpt-sovits-2.0.0-deadbeef"
    assert len(payload["command_sha256"]) == 64
    assert "uvicorn" not in path.read_text(encoding="utf-8")


def test_write_process_record_is_bound_to_fixed_package_path_and_rejects_junction(
    tmp_path: Path,
) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    executable = root / "runtime" / "live" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (root / "data" / "local").mkdir(parents=True)
    _junction(root / "data" / "local" / "run", outside)
    record_path = root / "data" / "local" / "run" / "worker.pid.json"

    with pytest.raises(ValueError, match="reparse|junction|fixed package"):
        launcher.write_process_record(
            record_path,
            pid=4242,
            parent_pid=100,
            child_pids=[],
            process_created_at="2026-07-14T01:02:03+00:00",
            executable_path=executable,
            command=DEFAULT_COMMAND,
            port=9880,
            package_root=root,
            build_id="source-checkout",
        )
    assert not (outside / "worker.pid.json").exists()

    with pytest.raises(ValueError, match="fixed package"):
        launcher.write_process_record(
            tmp_path / "outside-record.json",
            pid=4242,
            parent_pid=100,
            child_pids=[],
            process_created_at="2026-07-14T01:02:03+00:00",
            executable_path=executable,
            command=DEFAULT_COMMAND,
            port=9880,
            package_root=root,
            build_id="source-checkout",
        )
    assert not (tmp_path / "outside-record.json").exists()


def test_stop_worker_does_not_read_or_delete_pid_record_through_junction(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (root / "data" / "local").mkdir(parents=True)
    _junction(root / "data" / "local" / "run", outside)
    external_record = outside / "worker.pid.json"
    external_record.write_text(json.dumps(_record(root)), encoding="utf-8")

    with pytest.raises(ValueError, match="reparse|junction"):
        launcher.stop_worker(
            root,
            inspector=lambda _pid: None,
            port_owner_inspector=lambda _port: set(),
        )
    assert external_record.exists()


def test_stop_worker_deletes_record_only_after_process_and_port_are_gone(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(_record(root)), encoding="utf-8")
    inspections = iter(
        [
            {
                "pid": 4242,
                "parent_pid": 100,
                "created_at": "2026-07-14T01:02:03.000000+00:00",
                "executable_path": str(root / "runtime/live/python.exe"),
                "command_args": DEFAULT_COMMAND,
            },
            None,
        ]
    )
    ports = iter([True, False])
    terminated: list[int] = []

    result = launcher.stop_worker(
        root,
        inspector=lambda _pid: next(inspections),
        terminator=lambda pid: terminated.append(pid),
        port_is_listening=lambda _port: next(ports),
        sleep=lambda _seconds: None,
        timeout_seconds=1,
    )

    assert result == 0
    assert terminated == [4242]
    assert not record_path.exists()


def test_stop_worker_preserves_record_when_port_does_not_release(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(_record(root)), encoding="utf-8")

    result = launcher.stop_worker(
        root,
        inspector=lambda _pid: None,
        terminator=lambda _pid: None,
        port_is_listening=lambda _port: True,
        sleep=lambda _seconds: None,
        timeout_seconds=0,
    )

    assert result == 2
    assert record_path.exists()


def test_stop_worker_never_terminates_when_port_has_unknown_owner(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(_record(root)), encoding="utf-8")

    with pytest.raises(RuntimeError, match="port ownership"):
        launcher.stop_worker(
            root,
            inspector=lambda _pid: {
                "pid": 4242,
                "parent_pid": 100,
                "created_at": "2026-07-14T01:02:03.000000+00:00",
                "executable_path": str(root / "runtime/live/python.exe"),
                "command_args": ["-m", "uvicorn", "worker:app", "--port", "9880"],
            },
            terminator=lambda _pid: pytest.fail("unknown port owner must prevent termination"),
            port_owner_inspector=lambda _port: {4243},
        )
    assert record_path.exists()


def test_stop_worker_validates_build_and_command_identity_before_termination(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    executable = root / "runtime" / "live" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    manifest = root / "package" / "tts-more-package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"build_id": "gpt-sovits-2.0.0-deadbeef"}), encoding="utf-8")
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    payload = _record(root)
    payload["build_id"] = "gpt-sovits-2.0.0-deadbeef"
    command = ["-m", "uvicorn", "worker:app", "--port", "9880"]
    payload["command_sha256"] = hashlib.sha256("\0".join(command).encode()).hexdigest()
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    process = {
        "pid": 4242,
        "parent_pid": 100,
        "created_at": payload["process_created_at"],
        "executable_path": str(executable),
        "command_args": [*command, "--forged"],
    }
    with pytest.raises(RuntimeError, match="command identity"):
        launcher.stop_worker(
            root,
            inspector=lambda _pid: process,
            terminator=lambda _pid: pytest.fail("forged command must not be terminated"),
            port_owner_inspector=lambda _port: {4242},
        )
    assert record_path.exists()

    payload["build_id"] = "forged-build"
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="build identity"):
        launcher.stop_worker(
            root,
            inspector=lambda _pid: {**process, "command_args": command},
            terminator=lambda _pid: pytest.fail("foreign build must not be terminated"),
            port_owner_inspector=lambda _port: {4242},
        )


def test_stop_worker_removes_stale_record_only_when_process_and_port_are_absent(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(_record(root)), encoding="utf-8")

    assert launcher.stop_worker(
        root,
        inspector=lambda _pid: None,
        port_owner_inspector=lambda _port: set(),
    ) == 0
    assert not record_path.exists()
    assert launcher.stop_worker(root) == 0


def test_stop_worker_cleans_dead_stale_build_record_after_liveness_check(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    payload = _record(root)
    payload["build_id"] = "foreign-build"
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    assert launcher.stop_worker(
        root,
        inspector=lambda _pid: None,
        port_owner_inspector=lambda _port: set(),
    ) == 0
    assert not record_path.exists()


def test_stop_worker_rejects_stale_build_record_when_process_or_port_is_live(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    payload = _record(root)
    payload["build_id"] = "foreign-build"
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    process = {
        "pid": 4242,
        "parent_pid": 100,
        "created_at": payload["process_created_at"],
        "executable_path": payload["executable_path"],
        "command_args": DEFAULT_COMMAND,
    }

    with pytest.raises(RuntimeError, match="build identity"):
        launcher.stop_worker(
            root,
            inspector=lambda _pid: process,
            terminator=lambda _pid: pytest.fail("stale build process must not be terminated"),
            port_owner_inspector=lambda _port: {4242},
        )
    assert record_path.exists()


def test_stop_worker_refuses_pid_reuse_or_foreign_executable(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(_record(root)), encoding="utf-8")

    with pytest.raises(RuntimeError, match="identity does not match"):
        launcher.stop_worker(
            root,
            inspector=lambda _pid: {
                "pid": 4242,
                "created_at": "2026-07-14T09:09:09.000000+00:00",
                "executable_path": "C:/Windows/System32/notepad.exe",
            },
            terminator=lambda _pid: pytest.fail("foreign process must not be terminated"),
            port_is_listening=lambda _port: True,
        )
    assert record_path.exists()


def test_stop_worker_rejects_record_from_another_package_root(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    payload = _record(root)
    payload["package_root"] = str(tmp_path / "another-package")
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="different package root"):
        launcher.stop_worker(
            root,
            inspector=lambda _pid: {
                "pid": 4242,
                "parent_pid": 100,
                "created_at": payload["process_created_at"],
                "executable_path": payload["executable_path"],
                "command_args": DEFAULT_COMMAND,
            },
            terminator=lambda _pid: pytest.fail("foreign-root process must not be terminated"),
            port_owner_inspector=lambda _port: {4242},
        )
    assert record_path.exists()


def test_existing_listener_requires_record_build_command_and_process_identity(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    executable = root / "runtime" / "live" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    command = ["-m", "uvicorn", "app.main:app", "--port", "8000"]
    payload = _record(root)
    payload["command_sha256"] = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()
    payload["command_args"] = command
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    process = {
        "pid": 4242,
        "created_at": payload["process_created_at"],
        "executable_path": str(executable),
        "command_args": command,
    }

    assert launcher.listener_is_owned(
        record_path,
        package_root=root,
        port=9880,
        build_id=payload["build_id"],
        executable_path=executable,
        command=command,
        listener_pids={4242},
        inspector=lambda _pid: process,
    )

    for field, forged in (("build_id", "forged-build"), ("command_sha256", "0" * 64)):
        original = payload[field]
        payload[field] = forged
        record_path.write_text(json.dumps(payload), encoding="utf-8")
        assert not launcher.listener_is_owned(
            record_path,
            package_root=root,
            port=9880,
            build_id="source-checkout",
            executable_path=executable,
            command=command,
            listener_pids={4242},
            inspector=lambda _pid: process,
        )
        payload[field] = original


def test_existing_listener_rejects_pid_reuse_and_foreign_runtime(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    executable = root / "runtime" / "live" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    command = ["-m", "uvicorn", "app.main:app", "--port", "8000"]
    payload = _record(root)
    payload["command_sha256"] = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()
    payload["command_args"] = command
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    for process in (
        {
            "pid": 4242,
            "created_at": "2026-07-14T01:05:03.000000+00:00",
            "executable_path": str(executable),
            "command_args": command,
        },
        {
            "pid": 4242,
            "created_at": payload["process_created_at"],
            "executable_path": str(tmp_path / "foreign" / "python.exe"),
            "command_args": command,
        },
    ):
        assert not launcher.listener_is_owned(
            record_path,
            package_root=root,
            port=9880,
            build_id=payload["build_id"],
            executable_path=executable,
            command=command,
            listener_pids={4242},
            inspector=lambda _pid, current=process: current,
        )


def test_windows_process_command_line_is_split_for_identity_comparison() -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows command-line parsing is Windows-only")

    assert launcher._split_windows_command_line(
        '"C:\\Portable Root\\python.exe" -m uvicorn "worker app:api" --port 9880'
    ) == ["C:\\Portable Root\\python.exe", "-m", "uvicorn", "worker app:api", "--port", "9880"]


def test_windows_powershell_51_inspects_current_process_identity() -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")

    process = launcher._inspect_process(os.getpid())

    assert process is not None
    assert process["pid"] == os.getpid()
    executable = Path(str(process["executable_path"])).resolve()
    assert executable.is_file()
    assert executable.name.casefold() == Path(sys.executable).name.casefold()
    assert datetime.fromisoformat(str(process["created_at"]))
    assert isinstance(process["command_args"], list)
    assert process["command_args"]


def test_windows_powershell_51_reports_a_missing_process_without_query_failure() -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")

    assert launcher._inspect_process(2**31 - 1) is None


def test_windows_powershell_51_reports_a_process_missing_after_exit() -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)

    assert launcher._inspect_process(process.pid) is None


def test_process_creation_times_compare_at_utc_microsecond_precision() -> None:
    launcher = _load_launcher()

    assert launcher._same_process_creation_time(
        "2026-07-16T03:32:14.2146700Z", "2026-07-16T03:32:14.2146706+00:00"
    )
    assert not launcher._same_process_creation_time(
        "2026-07-16T03:32:14.2146700Z", "2026-07-16T03:32:14.2146710+00:00"
    )
    assert not launcher._same_process_creation_time("not-a-date", "2026-07-16T03:32:14Z")


def test_windows_cim_creation_time_matches_get_process_record_time() -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"])
    try:
        identity = launcher._inspect_process(process.pid)
        assert identity is not None
        environment = {**os.environ, "TTS_MORE_TEST_PROCESS_PID": str(process.pid)}
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$p=Get-Process -Id ([int]$env:TTS_MORE_TEST_PROCESS_PID) -ErrorAction Stop;"
                "$p.StartTime.ToUniversalTime().ToString('o')",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert completed.returncode == 0, completed.stderr
        assert launcher._same_process_creation_time(
            str(identity["created_at"]), completed.stdout.strip()
        )
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def test_windows_powershell_51_exit_race_is_complete_identity_or_missing() -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    delays = (0.30, 0.34, 0.38) * 10
    for delay in delays:
        process = subprocess.Popen(
            [sys.executable, "-c", f"import time;time.sleep({delay})"]
        )
        try:
            identity = launcher._inspect_process(process.pid)
            if identity is None:
                continue
            assert identity["pid"] == process.pid
            assert identity["created_at"]
            assert identity["executable_path"]
            assert identity["command_args"]
        finally:
            process.wait(timeout=10)


def test_windows_powershell_51_finds_current_loopback_listener_owner() -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell port inspection is Windows-only")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        assert os.getpid() in launcher._listener_pids_for_port(port)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, "", "query failed"),
        (0, "", ""),
        (
            0,
            '{"pid":4242,"parent_pid":1,"created_at":"2026-07-16T00:00:00Z",'
            '"executable_path":"C:\\\\Python\\\\python.exe","command_line":"python.exe"}',
            "unexpected diagnostic",
        ),
        (0, "4242", ""),
    ],
)
def test_windows_process_inspection_fails_closed_on_untrusted_powershell_output(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(RuntimeError, match="process ownership"):
        launcher._inspect_process(4242)


@pytest.mark.parametrize(
    "payload",
    [
        {"found": False, "process": {"pid": 4242}},
        {"found": True, "process": None},
        {"found": True, "process": {"pid": 4242}},
        {
            "found": True,
            "process": {
                "pid": 4242,
                "parent_pid": 1,
                "created_at": "2026-07-16T00:00:00Z",
                "executable_path": None,
                "command_line": "python.exe fixture.py",
            },
        },
        {
            "found": True,
            "process": {
                "pid": 4242,
                "parent_pid": 1,
                "created_at": "not-a-date",
                "executable_path": "C:\\Python\\python.exe",
                "command_line": "python.exe fixture.py",
            },
        },
    ],
)
def test_windows_process_inspection_rejects_inconsistent_found_schema(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(RuntimeError, match="process ownership"):
        launcher._inspect_process(4242)


def test_windows_process_inspection_uses_one_cim_snapshot_for_complete_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    payload = {
        "found": True,
        "process": {
            "pid": 4242,
            "parent_pid": 1,
            "created_at": "2026-07-16T00:00:00Z",
            "executable_path": "C:\\Python\\python.exe",
            "command_line": "python.exe fixture.py",
        },
    }
    captured: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command[-1])
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher._inspect_process(4242) is not None
    assert len(captured) == 1
    assert captured[0].count("Get-CimInstance") == 1
    assert "Get-Process" not in captured[0]


def test_windows_process_inspection_retries_half_snapshot_then_accepts_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    half = {
        "found": True,
        "process": {
            "pid": 4242,
            "parent_pid": 1,
            "created_at": "2026-07-16T00:00:00Z",
            "executable_path": None,
            "command_line": None,
        },
    }
    responses = [half, {"found": False, "process": None}]
    calls: list[int] = []

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess([], 0, json.dumps(responses.pop(0)), "")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    assert launcher._inspect_process(4242) is None
    assert len(calls) == 2


def test_windows_process_inspection_rejects_persistent_half_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    half = {
        "found": True,
        "process": {
            "pid": 4242,
            "parent_pid": 1,
            "created_at": None,
            "executable_path": None,
            "command_line": None,
        },
    }
    calls: list[int] = []

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess([], 0, json.dumps(half), "")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="process ownership"):
        launcher._inspect_process(4242)
    assert len(calls) == 3


def test_windows_process_inspection_does_not_downgrade_retry_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell process inspection is Windows-only")
    half = {
        "found": True,
        "process": {
            "pid": 4242,
            "parent_pid": 1,
            "created_at": "2026-07-16T00:00:00Z",
            "executable_path": None,
            "command_line": None,
        },
    }
    responses = [
        subprocess.CompletedProcess([], 0, json.dumps(half), ""),
        subprocess.CompletedProcess([], 1, "", "CIM failed"),
    ]
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="process ownership"):
        launcher._inspect_process(4242)
    assert not responses


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ('{"listener_pids":[]}', "unexpected diagnostic"),
        ("4242", ""),
    ],
)
def test_windows_port_inspection_fails_closed_on_untrusted_powershell_output(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    stderr: str,
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell port inspection is Windows-only")
    completed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(RuntimeError, match="port ownership"):
        launcher._listener_pids_for_port(9880)


@pytest.mark.parametrize(
    ("function_name", "value"),
    [
        ("_inspect_process", "4242; Write-Output forged"),
        ("_inspect_process", 2**31),
        ("_listener_pids_for_port", 65536),
    ],
)
def test_windows_ownership_queries_reject_unvalidated_values_before_powershell(
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    value: object,
) -> None:
    launcher = _load_launcher()
    if launcher.os.name != "nt":
        pytest.skip("Windows PowerShell ownership inspection is Windows-only")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid values must not reach PowerShell"),
    )

    with pytest.raises(ValueError, match="integer|range"):
        getattr(launcher, function_name)(value)


def test_cli_separator_is_not_part_of_persisted_command_identity(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    executable = root / "runtime" / "live" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    record = root / "data" / "local" / "run" / "worker.pid.json"
    command = ["-m", "uvicorn", "worker:app", "--port", "9880"]

    result = launcher.main(
        [
            "write-process-record",
            "--package-root",
            str(root),
            "--record-path",
            str(record),
            "--pid",
            "4242",
            "--parent-pid",
            "100",
            "--process-created-at",
            "2026-07-14T01:02:03+00:00",
            "--executable",
            str(executable),
            "--port",
            "9880",
            "--build-id",
            "build-id",
            "--",
            *command,
        ]
    )

    assert result == 0
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["command_sha256"] == hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()
