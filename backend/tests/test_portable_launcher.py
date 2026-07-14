from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


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
        "command_sha256": "a" * 64,
        "port": 9880,
        "package_root": str(root),
        "build_id": "gpt-sovits-2.0.0-deadbeef",
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


def test_stop_worker_deletes_record_only_after_process_and_port_are_gone(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "package"
    record_path = root / "data" / "local" / "run" / "worker.pid.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(_record(root)), encoding="utf-8")
    inspections = iter(
        [
            {"pid": 4242, "created_at": "2026-07-14T01:02:03.000000+00:00", "executable_path": str(root / "runtime/live/python.exe")},
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
        launcher.stop_worker(root)
    assert record_path.exists()
