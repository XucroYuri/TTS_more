from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import EngineName, TTSServiceEndpoint
from app.supervisor import ServiceSupervisor


class FakePopen:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = 4321


def test_supervisor_starts_managed_local_service_and_writes_pid_record(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_popen(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return FakePopen(command, **kwargs)

    monkeypatch.setattr("app.supervisor.subprocess.Popen", fake_popen)
    endpoint = TTSServiceEndpoint(
        service_id="local-indextts",
        engine=EngineName.INDEX_TTS,
        base_url="http://127.0.0.1:9881",
        mode="local",
        start_command=["python", "-m", "uvicorn", "app.workers.indextts_worker:app"],
        start_cwd=".",
    )
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")

    result = supervisor.start(endpoint)

    assert result["status"] == "started"
    assert result["pid"] == 4321
    assert calls[0]["command"] == ["python", "-m", "uvicorn", "app.workers.indextts_worker:app"]
    assert calls[0]["cwd"] == tmp_path
    record = json.loads((tmp_path / ".runtime" / "services" / "local-indextts.json").read_text(encoding="utf-8"))
    assert record["pid"] == 4321
    assert Path(record["log_path"]).name == "local-indextts.log"


def test_supervisor_detaches_windows_process_group(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_popen(command, **kwargs):
        calls.append(kwargs)
        return FakePopen(command, **kwargs)

    monkeypatch.setattr("app.supervisor.subprocess.Popen", fake_popen)
    # Cross-platform way to simulate Windows: patch the helper instead of ``os.name``,
    # which would corrupt ``pathlib.Path()`` instantiation on non-Windows hosts.
    # windows_creation_flags() is patched directly because the Windows-only subprocess
    # constants do not exist on POSIX, so the helper would return 0 even with
    # _is_windows() stubbed True.
    monkeypatch.setattr("app.supervisor._is_windows", lambda: True)
    monkeypatch.setattr("app.supervisor.windows_creation_flags", lambda: 0x00000200 | 0x08000000)
    endpoint = TTSServiceEndpoint(
        service_id="local-indextts",
        engine=EngineName.INDEX_TTS,
        base_url="http://127.0.0.1:9881",
        mode="local",
        start_command=["python", "-m", "uvicorn", "app.workers.indextts_worker:app"],
        start_cwd=".",
    )
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")

    supervisor.start(endpoint)

    assert calls[0]["creationflags"] & 0x00000200


def test_supervisor_passes_endpoint_environment_to_process(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_popen(command, **kwargs):
        calls.append(kwargs)
        return FakePopen(command, **kwargs)

    monkeypatch.setattr("app.supervisor.subprocess.Popen", fake_popen)
    monkeypatch.setenv("EXISTING_ENV", "keep")
    endpoint = TTSServiceEndpoint(
        service_id="local-indextts",
        engine=EngineName.INDEX_TTS,
        base_url="http://127.0.0.1:9881",
        mode="local",
        start_command=["python", "-m", "uvicorn", "app.workers.indextts_worker:app"],
        start_cwd=".",
        env={"TTS_MORE_INDEXTTS_MODEL_DIR": "repo/index-tts/checkpoints"},
    )
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")

    supervisor.start(endpoint)

    assert calls[0]["env"]["EXISTING_ENV"] == "keep"
    assert Path(calls[0]["env"]["TTS_MORE_INDEXTTS_MODEL_DIR"]).parts[-3:] == ("repo", "index-tts", "checkpoints")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path normalization only applies on Windows")
def test_supervisor_resolves_path_environment_entries(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_popen(command, **kwargs):
        calls.append(kwargs)
        return FakePopen(command, **kwargs)

    monkeypatch.setattr("app.supervisor.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.supervisor._is_windows", lambda: True)
    monkeypatch.setattr("app.supervisor.subprocess.CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr("app.supervisor.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("PATH", r"C:\System")
    # This test exercises the Windows PATH layout (drive letters, ';'-separated entries,
    # backslash path components). Patch ``os.pathsep`` so the same assertion works on
    # POSIX hosts without polluting ``pathlib`` the way patching ``os.name`` would.
    monkeypatch.setattr("app.supervisor.os.pathsep", ";")
    endpoint = TTSServiceEndpoint(
        service_id="local-gpt-sovits",
        engine=EngineName.GPT_SOVITS,
        base_url="http://127.0.0.1:9880",
        mode="local",
        start_command=["python", "api_v2.py"],
        start_cwd=".",
        env={"PATH": r"repo\GPT-SoVITS\ffmpeg-shared\bin;{PATH}"},
    )
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")

    supervisor.start(endpoint)

    path_entries = calls[0]["env"]["PATH"].split(";")
    assert path_entries[0] == str((tmp_path / "repo/GPT-SoVITS/ffmpeg-shared/bin").resolve(strict=False))
    assert path_entries[-1] == r"C:\System"


def test_supervisor_rejects_external_or_unmanaged_service(tmp_path: Path) -> None:
    endpoint = TTSServiceEndpoint(
        service_id="remote-gpt",
        engine=EngineName.GPT_SOVITS,
        base_url="http://10.0.0.2:9880",
        mode="external",
        start_command=["python", "api_v2.py"],
    )
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")

    result = supervisor.start(endpoint)

    assert result["status"] == "not manageable"
    assert "external" in result["reason"]


def test_supervisor_rejects_disallowed_executable(tmp_path: Path) -> None:
    """A start_command[0] that is an absolute path outside the project or a
    non-allowlisted bare name must be refused before Popen is called."""
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")

    # Absolute path outside project root.
    outside_executable = Path(tmp_path.anchor) / "__tts_more_outside_project__" / "evil"
    endpoint = TTSServiceEndpoint(
        service_id="evil-1",
        engine=EngineName.INDEX_TTS,
        base_url="http://127.0.0.1:9881",
        mode="local",
        start_command=[str(outside_executable)],
        start_cwd=".",
    )
    result = supervisor.start(endpoint)
    assert result["status"] == "not manageable"
    assert "outside project root" in result["reason"]

    # Bare name not in the allowlist.
    endpoint2 = TTSServiceEndpoint(
        service_id="evil-2",
        engine=EngineName.INDEX_TTS,
        base_url="http://127.0.0.1:9881",
        mode="local",
        start_command=["totally-not-a-real-binary"],
        start_cwd=".",
    )
    result2 = supervisor.start(endpoint2)
    assert result2["status"] == "not manageable"
    assert "not allowed" in result2["reason"]


def test_supervisor_allows_in_project_executable(monkeypatch, tmp_path: Path) -> None:
    """An executable path inside the project root is allowed."""
    calls: list[dict] = []

    def fake_popen(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return FakePopen(command, **kwargs)

    monkeypatch.setattr("app.supervisor.subprocess.Popen", fake_popen)
    exe = tmp_path / "bin" / "myrunner"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    endpoint = TTSServiceEndpoint(
        service_id="local-ok",
        engine=EngineName.INDEX_TTS,
        base_url="http://127.0.0.1:9881",
        mode="local",
        start_command=[str(exe)],
        start_cwd=".",
    )
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")

    result = supervisor.start(endpoint)

    assert result["status"] == "started"


def test_supervisor_stop_uses_pid_record_and_removes_it(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")
    record_path = tmp_path / ".runtime" / "services" / "local-indextts.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps({"pid": 1234, "log_path": str(tmp_path / "worker.log")}), encoding="utf-8")

    def fake_run(command, **kwargs):
        commands.append(command)
        class Completed:
            returncode = 0
        return Completed()

    monkeypatch.setattr("app.supervisor.subprocess.run", fake_run)
    monkeypatch.setattr("app.supervisor._is_windows", lambda: True)

    result = supervisor.stop("local-indextts")

    assert result["status"] == "stopped"
    assert commands[0][:4] == ["taskkill", "/PID", "1234", "/T"]
    assert not record_path.exists()


def test_supervisor_tail_logs_reads_last_lines(tmp_path: Path) -> None:
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / ".runtime")
    log_path = tmp_path / ".runtime" / "logs" / "local-gpt.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    record_path = tmp_path / ".runtime" / "services" / "local-gpt.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps({"pid": 55, "log_path": str(log_path)}), encoding="utf-8")

    result = supervisor.logs("local-gpt", lines=2)

    assert result["status"] == "ok"
    assert result["lines"] == ["two", "three"]
