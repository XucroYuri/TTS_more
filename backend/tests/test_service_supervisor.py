from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import EngineName, PortableServiceLocator, TTSServiceEndpoint
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


class FakePortableController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict[str, object]]] = []

    def start(self, descriptor, **kwargs):
        self.calls.append(("start", descriptor, kwargs))
        return {"status": "starting", "operation_id": kwargs["operation_id"]}

    def stop(self, descriptor, **kwargs):
        self.calls.append(("stop", descriptor, kwargs))
        return {"status": "stopping"}

    def repair(self, descriptor, **kwargs):
        self.calls.append(("repair", descriptor, kwargs))
        return {"status": "repairing"}

    def logs(self, descriptor, **kwargs):
        self.calls.append(("logs", descriptor, kwargs))
        return {"status": "ok", "events": []}

    def status(self, descriptor, **kwargs):
        self.calls.append(("status", descriptor, kwargs))
        return {"status": "ready"}

    def open_folder(self, descriptor, **kwargs):
        self.calls.append(("open_folder", descriptor, kwargs))
        return {"status": "opened"}


def _portable_endpoint(**updates) -> TTSServiceEndpoint:
    values = {
        "service_id": "portable-gpt-main",
        "engine": EngineName.GPT_SOVITS,
        "base_url": "http://127.0.0.1:9880",
        "mode": "local",
        "network_scope": "localhost",
        "managed": True,
        "api_contract": "tts-more-v1",
        "control_kind": "portable-package",
        "portable_locator": PortableServiceLocator(
            component="gpt-sovits",
            package_id="gpt-main",
            absolute_path_last_seen="C:/portable/GPT",
            port_override=9980,
        ),
        # Forged legacy fields must be ignored rather than invoked.
        "repo_path": "C:/evil",
        "start_command": ["evil.exe", "&", "whoami"],
        "start_cwd": "C:/evil",
    }
    values.update(updates)
    return TTSServiceEndpoint(**values)


def _descriptor(**updates):
    values = {
        "component": "gpt-sovits",
        "package_id": "gpt-main",
        "package_root": "C:/portable/GPT",
        "default_url": "http://127.0.0.1:9980",
        "manageable": True,
        "initialized": True,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_supervisor_routes_every_portable_action_through_fresh_resolver(tmp_path: Path) -> None:
    controller = FakePortableController()
    resolved = _descriptor()
    resolutions: list[object] = []

    def resolver(_root, locator, _search):
        resolutions.append(locator)
        return resolved

    supervisor = ServiceSupervisor(
        project_root=tmp_path,
        runtime_root=tmp_path / ".runtime",
        portable_controller=controller,
        portable_resolver=resolver,
    )
    endpoint = _portable_endpoint()

    started = supervisor.start(endpoint, operation_id="11111111-1111-4111-8111-111111111111")
    supervisor.stop(endpoint)
    supervisor.repair(endpoint)
    supervisor.logs(endpoint, operation_id="11111111-1111-4111-8111-111111111111", lines=7)
    supervisor.status(endpoint, operation_id="11111111-1111-4111-8111-111111111111")
    supervisor.open_folder(endpoint)

    assert started["status"] == "starting"
    assert [call[0] for call in controller.calls] == ["start", "stop", "repair", "logs", "status", "open_folder"]
    assert len(resolutions) == 6
    assert controller.calls[0][2]["port_override"] == 9980
    assert all(call[1] is resolved for call in controller.calls)


def test_supervisor_passes_ephemeral_proxy_only_to_repair_controller(tmp_path: Path) -> None:
    controller = FakePortableController()
    supervisor = ServiceSupervisor(
        project_root=tmp_path,
        runtime_root=tmp_path / ".runtime",
        portable_controller=controller,
        portable_resolver=lambda *_args: _descriptor(),
    )
    endpoint = _portable_endpoint()
    proxy = "http://user:password@127.0.0.1:10808"

    result = supervisor.repair(endpoint, proxy_url=proxy)

    assert result["status"] == "repairing"
    assert controller.calls[0][0] == "repair"
    assert controller.calls[0][2]["proxy_url"] == proxy
    assert str(uuid.UUID(str(controller.calls[0][2]["action_id"]))) == controller.calls[0][2]["action_id"]


def test_supervisor_uses_independent_portable_controller_root_for_every_resolution(tmp_path: Path) -> None:
    legacy_project_root = tmp_path / "moved suite" / "app"
    portable_controller_root = legacy_project_root.parent
    resolution_roots: list[Path] = []

    def resolver(root, _locator, _search):
        resolution_roots.append(root)
        return _descriptor(package_root=str(portable_controller_root / "GPT-SoVITS"))

    supervisor = ServiceSupervisor(
        project_root=legacy_project_root,
        portable_controller_root=portable_controller_root,
        runtime_root=tmp_path / ".runtime",
        portable_controller=FakePortableController(),
        portable_resolver=resolver,
    )
    endpoint = _portable_endpoint()

    assert supervisor.start(
        endpoint,
        operation_id="11111111-1111-4111-8111-111111111111",
    )["status"] == "starting"
    assert supervisor.status(endpoint)["status"] == "ready"
    assert resolution_roots == [portable_controller_root.resolve()] * 2
    assert supervisor.project_root == legacy_project_root.resolve()
    assert supervisor.portable_controller_root == portable_controller_root.resolve()


@pytest.mark.parametrize(
    "endpoint,resolved",
    [
        (_portable_endpoint(mode="external", network_scope="lan", base_url="http://192.168.1.2:9880"), _descriptor()),
        (_portable_endpoint(network_scope="lan"), _descriptor()),
        (_portable_endpoint(api_contract="legacy"), _descriptor()),
        (_portable_endpoint(), None),
        (_portable_endpoint(), _descriptor(manageable=False)),
        (_portable_endpoint(), _descriptor(component="indextts")),
    ],
)
def test_supervisor_never_falls_back_to_legacy_for_untrusted_portable_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: TTSServiceEndpoint, resolved
) -> None:
    monkeypatch.setattr("app.supervisor.subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("legacy spawn must not run"))
    controller = FakePortableController()
    supervisor = ServiceSupervisor(
        project_root=tmp_path,
        runtime_root=tmp_path / ".runtime",
        portable_controller=controller,
        portable_resolver=lambda *_args: resolved,
    )

    result = supervisor.start(endpoint, operation_id="11111111-1111-4111-8111-111111111111")

    assert result["status"] == "not manageable"
    assert controller.calls == []


def test_supervisor_ignores_forged_portable_commands_but_preserves_legacy_behavior(tmp_path: Path, monkeypatch) -> None:
    portable = FakePortableController()
    supervisor = ServiceSupervisor(
        project_root=tmp_path,
        runtime_root=tmp_path / ".runtime",
        portable_controller=portable,
        portable_resolver=lambda *_args: _descriptor(),
    )
    portable_result = supervisor.start(_portable_endpoint(), operation_id="11111111-1111-4111-8111-111111111111")
    assert portable_result["status"] == "starting"

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "app.supervisor.subprocess.Popen",
        lambda command, **_kwargs: calls.append(command) or FakePopen(),
    )
    legacy = TTSServiceEndpoint(
        service_id="legacy-local", engine=EngineName.INDEX_TTS,
        base_url="http://127.0.0.1:9881", mode="local",
        start_command=["python", "-m", "uvicorn"], start_cwd=".",
    )
    assert supervisor.start(legacy)["status"] == "started"
    assert calls == [["python", "-m", "uvicorn"]]


def test_supervisor_serializes_racing_actions_for_one_portable_identity(tmp_path: Path) -> None:
    entered = threading.Event()
    released = threading.Event()
    order: list[str] = []

    class BlockingController(FakePortableController):
        def start(self, descriptor, **kwargs):
            order.append("start-enter")
            entered.set()
            assert released.wait(3)
            order.append("start-exit")
            return {"status": "starting", "operation_id": kwargs["operation_id"]}

        def stop(self, descriptor, **kwargs):
            order.append("stop")
            return {"status": "stopping"}

    supervisor = ServiceSupervisor(
        project_root=tmp_path,
        runtime_root=tmp_path / ".runtime",
        portable_controller=BlockingController(),
        portable_resolver=lambda *_args: _descriptor(),
    )
    endpoint = _portable_endpoint()
    start_thread = threading.Thread(
        target=lambda: supervisor.start(endpoint, operation_id="11111111-1111-4111-8111-111111111111")
    )
    stop_thread = threading.Thread(target=lambda: supervisor.stop(endpoint))
    start_thread.start()
    assert entered.wait(2)
    stop_thread.start()
    time.sleep(0.05)
    assert order == ["start-enter"]
    released.set()
    start_thread.join(3)
    stop_thread.join(3)
    assert order == ["start-enter", "start-exit", "stop"]


def test_supervisor_never_rebinds_one_service_id_to_a_different_portable_identity(tmp_path: Path) -> None:
    controller = FakePortableController()

    def resolver(_root, locator, _search):
        return _descriptor(
            component=locator.component,
            package_id=locator.package_id,
            package_root=locator.absolute_path_last_seen,
        )

    supervisor = ServiceSupervisor(
        project_root=tmp_path,
        runtime_root=tmp_path / ".runtime",
        portable_controller=controller,
        portable_resolver=resolver,
    )
    first = _portable_endpoint()
    second = _portable_endpoint(
        portable_locator=PortableServiceLocator(
            component="indextts",
            package_id="index-main",
            absolute_path_last_seen="C:/portable/Index",
        )
    )

    assert supervisor.start(first, operation_id="11111111-1111-4111-8111-111111111111")["status"] == "starting"
    refused = supervisor.start(second, operation_id="22222222-2222-4222-8222-222222222222")

    assert refused["status"] == "not manageable"
    assert "identity" in refused["reason"]
    assert len(controller.calls) == 1
