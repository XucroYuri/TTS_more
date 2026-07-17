from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cuda_validation import CUDAValidationRunner
from app.lan_nodes import NodeProbe
from app.lan_orchestration import (
    DeploymentMode,
    LanOrchestrator,
    LanRunOptions,
    controller_commit,
    controller_id_sha256,
    control_plane,
    parse_args,
    render_external_services,
    run_fault_recovery,
    run_core_cuda_validation,
    run_workstation_e2e,
    validate_network_identities,
    validate_node_probes,
    wait_http_ready,
    wait_for_services,
    write_preflight,
)
from app.lan_topology import LanMode, LanPolicy, load_lan_policy


COMMIT = "a" * 40


def _topology_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "name": "shared-validation",
        "app_node": "controller",
        "nodes": {
            "controller": {
                "role": "app",
                "host": "controller.lan",
                "bind_host": "127.0.0.1",
                "services": [],
                "resource_group": "controller",
                "capacity": 1,
            },
            "gpu-worker": {
                "role": "worker",
                "host": "gpu-worker.lan",
                "bind_host": "0.0.0.0",
                "services": [
                    "local-gpt-sovits-main",
                    "local-indextts",
                    "local-cosyvoice",
                ],
                "resource_group": "gpu-0",
                "capacity": 1,
            },
        },
    }


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    topology = tmp_path / "topology.json"
    topology.write_text(json.dumps(_topology_payload()), encoding="utf-8")
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}", encoding="utf-8")
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host gpu-worker\n", encoding="utf-8")
    return topology, fixture, ssh_config


def _options(tmp_path: Path, **overrides: object) -> LanRunOptions:
    topology, fixture, ssh_config = _write_inputs(tmp_path)
    values: dict[str, object] = {
        "mode": LanMode.SHARED,
        "deployment": DeploymentMode.CLEAN,
        "topology": topology,
        "fixture": fixture,
        "ssh_config": ssh_config,
        "remote_root": r"C:\TTS\TTS_more",
        "output": tmp_path / "run-001",
        "require_baseline": False,
    }
    values.update(overrides)
    return LanRunOptions(**values)  # type: ignore[arg-type]


def _write_services(path: Path) -> Path:
    providers = {
        "local-gpt-sovits-main": ("gpt-sovits", "gpt-sovits", 9880),
        "local-indextts": ("indextts", "indextts", 9881),
        "local-cosyvoice": ("cosyvoice", "cosyvoice", 9882),
    }
    path.write_text(
        json.dumps(
            [
                {
                    "service_id": service_id,
                    "display_name": service_id,
                    "engine": engine,
                    "provider_type": provider,
                    "api_contract": "tts-more-v1",
                    "base_url": f"http://worker.example.test:{port}",
                    "mode": "external",
                    "network_scope": "lan",
                    "managed": False,
                    "enabled": True,
                    "resource_group": service_id,
                    "capabilities": ["tts", "artifact-transfer"],
                }
                for service_id, (engine, provider, port) in providers.items()
            ]
        ),
        encoding="utf-8",
    )
    return path


class _FakeAppProcess:
    def __init__(
        self,
        events: list[str],
        *,
        require_kill: bool = False,
        exit_code: int | None = None,
    ) -> None:
        self.events = events
        self.require_kill = require_kill
        self.exit_code = exit_code
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.events.append("terminate-child")

    def kill(self) -> None:
        self.events.append("kill-child")

    def wait(self, timeout: int) -> int:
        self.wait_calls += 1
        self.events.append(f"wait-child:{timeout}")
        if self.require_kill and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("uvicorn", timeout)
        return 0


def test_control_plane_uses_exact_argv_and_only_terminates_its_child(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "run"
    output.mkdir()
    services = _write_services(tmp_path / "services.json")
    events: list[str] = []
    captured: dict[str, object] = {}
    child = _FakeAppProcess(events, require_kill=True)

    monkeypatch.setattr(
        "app.lan_orchestration._assert_fixed_loopback_port_available",
        lambda port: events.append(f"port-free:{port}"),
    )
    def backend_ready(*_args: object, **kwargs: object) -> None:
        captured["expected_instance_id"] = kwargs["expected_instance_id"]
        events.append("backend-ready")

    monkeypatch.setattr("app.lan_orchestration.wait_http_ready", backend_ready)

    def popen_factory(argv: list[str], **kwargs: object) -> _FakeAppProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return child

    with control_plane(services, output, popen_factory=popen_factory):
        events.append("application-loop")

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[1:] == [
        "-m",
        "uvicorn",
        "app.main:app",
        "--app-dir",
        "backend",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    assert captured["shell"] is False
    assert captured["cwd"] == Path(__file__).resolve().parents[2]
    assert captured["env"]["TTS_MORE_SERVICE_MODE"] == "real"  # type: ignore[index]
    assert captured["env"]["TTS_MORE_SERVICES_PATH"] == str(services)  # type: ignore[index]
    assert captured["env"]["TTS_MORE_INSTANCE_ID"] == captured["expected_instance_id"]  # type: ignore[index]
    assert events == [
        "port-free:8000",
        "backend-ready",
        "application-loop",
        "terminate-child",
        "wait-child:15",
        "kill-child",
        "wait-child:15",
    ]


def test_control_plane_refuses_unknown_fixed_port_without_starting_or_killing(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "run"
    output.mkdir()
    services = _write_services(tmp_path / "services.json")
    calls: list[str] = []

    def occupied(_port: int) -> None:
        raise RuntimeError("fixed loopback port is already in use")

    monkeypatch.setattr(
        "app.lan_orchestration._assert_fixed_loopback_port_available", occupied
    )

    with pytest.raises(RuntimeError, match="already in use"):
        with control_plane(
            services,
            output,
            popen_factory=lambda *_args, **_kwargs: calls.append("popen"),
        ):
            pass
    assert calls == []


def test_control_plane_rejects_health_from_a_process_it_did_not_start(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "run"
    output.mkdir()
    services = _write_services(tmp_path / "services.json")
    events: list[str] = []
    child = _FakeAppProcess(events, exit_code=1)
    monkeypatch.setattr(
        "app.lan_orchestration._assert_fixed_loopback_port_available", lambda _port: None
    )
    monkeypatch.setattr(
        "app.lan_orchestration.wait_http_ready", lambda *_args, **_kwargs: None
    )

    with pytest.raises(RuntimeError, match="backend child exited"):
        with control_plane(
            services, output, popen_factory=lambda *_args, **_kwargs: child
        ):
            pass

    assert events == []


def test_wait_http_ready_rejects_a_healthy_response_from_the_wrong_instance() -> None:
    class Response:
        is_success = True

        def __init__(self, instance_id: str) -> None:
            self.instance_id = instance_id

        def json(self) -> dict[str, str]:
            return {"status": "ok", "instance_id": self.instance_id}

    responses = iter((Response("wrong-instance"), Response("expected-instance")))
    now = [0.0]

    wait_http_ready(
        "http://127.0.0.1:8000/api/health",
        timeout_seconds=2,
        expected_instance_id="expected-instance",
        http_get=lambda *_args, **_kwargs: next(responses),
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert now[0] > 0


@pytest.mark.parametrize(
    ("mode", "expected_mode"),
    [
        (LanMode.SHARED, "lan-shared"),
        (LanMode.DISTRIBUTED, "lan-distributed"),
    ],
)
def test_workstation_e2e_uses_formal_mode_environment_credentials_and_run_specific_results(
    tmp_path: Path,
    monkeypatch,
    mode: LanMode,
    expected_mode: str,
) -> None:
    options = _options(tmp_path, mode=mode)
    options.output.mkdir()
    monkeypatch.setenv("TTS_MORE_API_TOKEN", "private-api-token")
    checked_ports: list[int] = []
    monkeypatch.setattr(
        "app.lan_orchestration._assert_fixed_loopback_port_available",
        checked_ports.append,
    )
    captured: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        env = kwargs["env"]
        assert isinstance(env, dict)
        Path(env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"]).write_text(
            '<testsuite tests="1" failures="0"/>\n', encoding="utf-8"
        )
        kwargs["stdout"].write("controlled stdout\n")  # type: ignore[union-attr]
        kwargs["stderr"].write("controlled stderr\n")  # type: ignore[union-attr]
        return subprocess.CompletedProcess(argv, 0, "", "")

    run_workstation_e2e(options, runner=runner)

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:4] == ["pnpm", "--dir", "frontend", "cuda:e2e"]
    assert "private-api-token" not in " ".join(argv)
    assert "--output" in argv
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["TTS_MORE_API_TOKEN"] == "private-api-token"
    assert env["TTS_MORE_CUDA_E2E_PROJECT_ID"] == "cuda-e2e-run-001"
    assert env["TTS_MORE_CUDA_VALIDATION_MODE"] == expected_mode
    assert Path(env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"]) == (
        options.output / "playwright-junit.xml"
    )
    assert checked_ports == [5173]
    assert (options.output / "playwright-junit.xml").is_file()


class _FaultManager:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.stopped: set[str] = set()

    def stop_service(self, node: str, port: int) -> None:
        service_id = {9880: "local-gpt-sovits-main", 9881: "local-indextts", 9882: "local-cosyvoice"}[port]
        self.events.append(f"stop:{node}:{service_id}")
        self.stopped.add(service_id)

    def stop_all_services(self, node: str, ports: tuple[int, ...]) -> None:
        self.events.append(f"stop-all:{node}")
        for port in ports:
            self.stop_service(node, port)

    def restart_services(
        self, node: str, remote_root: str, service_ids: tuple[str, ...]
    ) -> None:
        self.events.append(f"restart:{node}:{','.join(service_ids)}")
        self.stopped.difference_update(service_ids)


def _patch_fault_probes(monkeypatch, manager: _FaultManager) -> None:
    monkeypatch.setattr(
        "app.lan_orchestration.wait_service_state",
        lambda service_id, *, ready, timeout_seconds: (
            1.25 if not ready and service_id in manager.stopped else 0.5
        ),
    )
    monkeypatch.setattr(
        "app.lan_orchestration.current_service_ready",
        lambda service_id: service_id not in manager.stopped,
    )
    monkeypatch.setattr("app.lan_orchestration.application_ready", lambda: True)
    monkeypatch.setattr(
        "app.lan_orchestration.wait_for_services",
        lambda *_args, **_kwargs: manager.events.append("workers-ready"),
    )
    monkeypatch.setattr(
        "app.lan_orchestration.run_core_cuda_validation",
        lambda *_args, **kwargs: manager.events.append(
            f"core:{Path(kwargs['output_dir']).name}"
        ),
    )


def test_shared_fault_stops_one_then_all_exact_listeners_and_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path)
    options.output.mkdir()
    services = _write_services(options.output / "services.external.json")
    preflight = options.output / "orchestration-preflight.json"
    preflight.write_text("{}", encoding="utf-8")
    policy = LanPolicy(
        LanMode.SHARED,
        "controller",
        ("shared-worker",),
        {
            "local-gpt-sovits-main": "shared-worker",
            "local-indextts": "shared-worker",
            "local-cosyvoice": "shared-worker",
        },
        1,
        False,
    )
    manager = _FaultManager()
    _patch_fault_probes(monkeypatch, manager)

    report = run_fault_recovery(
        options, policy, manager, services, preflight, "private-token"
    )

    assert report["degraded_within_seconds"] == 1.25
    assert report["other_services_ready"] is True
    assert report["all_services_degraded"] is True
    assert report["application_survived"] is True
    assert report["retry_passed"] is True
    assert report["recovery_passed"] is True
    assert manager.events == [
        "stop:shared-worker:local-gpt-sovits-main",
        "restart:shared-worker:local-gpt-sovits-main",
        "workers-ready",
        "core:retry",
        "stop:shared-worker:local-gpt-sovits-main",
        "stop:shared-worker:local-indextts",
        "stop:shared-worker:local-cosyvoice",
        "restart:shared-worker:local-gpt-sovits-main,local-indextts,local-cosyvoice",
        "workers-ready",
        "core:recovery",
    ]
    assert "private-token" not in (options.output / "fault-recovery.json").read_text(
        encoding="utf-8"
    )


def test_shared_fault_partial_stop_restores_only_confirmed_stops(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path)
    options.output.mkdir()
    services = _write_services(options.output / "services.external.json")
    preflight = options.output / "orchestration-preflight.json"
    preflight.write_text("{}", encoding="utf-8")
    policy = LanPolicy(
        LanMode.SHARED,
        "controller",
        ("shared-worker",),
        {
            "local-gpt-sovits-main": "shared-worker",
            "local-indextts": "shared-worker",
            "local-cosyvoice": "shared-worker",
        },
        1,
        False,
    )

    class PartialStopManager(_FaultManager):
        def stop_service(self, node: str, port: int) -> None:
            service_id = {
                9880: "local-gpt-sovits-main",
                9881: "local-indextts",
                9882: "local-cosyvoice",
            }[port]
            if service_id == "local-indextts":
                self.events.append(f"stop-failed:{node}:{service_id}")
                raise RuntimeError("second listener stop failed")
            super().stop_service(node, port)

    manager = PartialStopManager()
    _patch_fault_probes(monkeypatch, manager)

    with pytest.raises(RuntimeError, match="second listener stop failed"):
        run_fault_recovery(
            options, policy, manager, services, preflight, "private-token"
        )

    assert manager.events[-4:] == [
        "stop:shared-worker:local-gpt-sovits-main",
        "stop-failed:shared-worker:local-indextts",
        "restart:shared-worker:local-gpt-sovits-main",
        "workers-ready",
    ]


def test_fault_recovery_does_not_restart_an_unconfirmed_initial_stop(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path, mode=LanMode.DISTRIBUTED)
    options.output.mkdir()
    services = _write_services(options.output / "services.external.json")
    preflight = options.output / "orchestration-preflight.json"
    preflight.write_text("{}", encoding="utf-8")
    policy = LanPolicy(
        LanMode.DISTRIBUTED,
        "controller",
        ("gpt-worker", "index-worker", "cosy-worker"),
        {
            "local-gpt-sovits-main": "gpt-worker",
            "local-indextts": "index-worker",
            "local-cosyvoice": "cosy-worker",
        },
        3,
        True,
    )

    class FailedInitialStopManager(_FaultManager):
        def stop_service(self, node: str, port: int) -> None:
            self.events.append("initial-stop-failed")
            raise RuntimeError("initial listener stop failed")

    manager = FailedInitialStopManager()
    _patch_fault_probes(monkeypatch, manager)
    monkeypatch.setenv("TTS_MORE_VALIDATION_FAULT_NODE", "gpt-worker")

    with pytest.raises(RuntimeError, match="initial listener stop failed"):
        run_fault_recovery(
            options, policy, manager, services, preflight, "private-token"
        )

    assert manager.events == ["initial-stop-failed"]


def test_distributed_fault_keeps_other_workers_ready_and_restarts_selected_node(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path, mode=LanMode.DISTRIBUTED)
    options.output.mkdir()
    services = _write_services(options.output / "services.external.json")
    preflight = options.output / "orchestration-preflight.json"
    preflight.write_text("{}", encoding="utf-8")
    policy = LanPolicy(
        LanMode.DISTRIBUTED,
        "controller",
        ("gpt-worker", "index-worker", "cosy-worker"),
        {
            "local-gpt-sovits-main": "gpt-worker",
            "local-indextts": "index-worker",
            "local-cosyvoice": "cosy-worker",
        },
        3,
        True,
    )
    manager = _FaultManager()
    _patch_fault_probes(monkeypatch, manager)
    monkeypatch.setenv("TTS_MORE_VALIDATION_FAULT_NODE", "gpt-worker")

    report = run_fault_recovery(
        options, policy, manager, services, preflight, "private-token"
    )

    assert report["fault_node"] == "gpt-worker"
    assert report["other_services_ready"] is True
    assert report["all_services_degraded"] is None
    assert report["recovery_passed"] is True
    assert manager.events == [
        "stop:gpt-worker:local-gpt-sovits-main",
        "restart:gpt-worker:local-gpt-sovits-main",
        "workers-ready",
        "core:recovery",
    ]


def test_fault_gate_failure_restores_only_the_injected_listener(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path, mode=LanMode.DISTRIBUTED)
    options.output.mkdir()
    services = _write_services(options.output / "services.external.json")
    preflight = options.output / "orchestration-preflight.json"
    preflight.write_text("{}", encoding="utf-8")
    policy = LanPolicy(
        LanMode.DISTRIBUTED,
        "controller",
        ("gpt-worker", "index-worker", "cosy-worker"),
        {
            "local-gpt-sovits-main": "gpt-worker",
            "local-indextts": "index-worker",
            "local-cosyvoice": "cosy-worker",
        },
        3,
        True,
    )
    manager = _FaultManager()
    _patch_fault_probes(monkeypatch, manager)
    monkeypatch.setenv("TTS_MORE_VALIDATION_FAULT_NODE", "gpt-worker")
    monkeypatch.setattr("app.lan_orchestration.application_ready", lambda: False)

    with pytest.raises(RuntimeError, match="fault isolation"):
        run_fault_recovery(
            options, policy, manager, services, preflight, "private-token"
        )

    assert manager.events == [
        "stop:gpt-worker:local-gpt-sovits-main",
        "restart:gpt-worker:local-gpt-sovits-main",
        "workers-ready",
    ]
    report = json.loads(
        (options.output / "fault-recovery.json").read_text(encoding="utf-8")
    )
    assert report["application_survived"] is False
    assert report["recovery_passed"] is False


def test_cli_requires_explicit_deployment_mode(tmp_path: Path) -> None:
    topology, fixture, ssh_config = _write_inputs(tmp_path)
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                "lan-shared",
                "--topology",
                str(topology),
                "--fixture",
                str(fixture),
                "--ssh-config",
                str(ssh_config),
                "--remote-root",
                r"C:\TTS\TTS_more",
                "--output",
                str(tmp_path / "run-001"),
            ]
        )


def test_cli_rejects_skip_deploy(tmp_path: Path) -> None:
    topology, fixture, ssh_config = _write_inputs(tmp_path)
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                "lan-shared",
                "--deployment",
                "clean",
                "--topology",
                str(topology),
                "--fixture",
                str(fixture),
                "--ssh-config",
                str(ssh_config),
                "--remote-root",
                r"C:\TTS\TTS_more",
                "--output",
                str(tmp_path / "run-001"),
                "--skip-deploy",
            ]
        )


@pytest.mark.parametrize(
    ("deployment", "require_baseline", "message"),
    [
        (DeploymentMode.RELEASE, False, "release deployment requires an approved baseline"),
        (DeploymentMode.CLEAN, True, "clean certification establishes a baseline"),
    ],
)
def test_deployment_mode_has_an_explicit_baseline_gate(
    tmp_path: Path,
    deployment: DeploymentMode,
    require_baseline: bool,
    message: str,
) -> None:
    options = _options(
        tmp_path,
        deployment=deployment,
        require_baseline=require_baseline,
    )
    with pytest.raises(ValueError, match=message):
        options.validate()


@pytest.mark.parametrize("field", ["topology", "fixture", "ssh_config"])
def test_options_reject_symlinked_inputs(tmp_path: Path, field: str) -> None:
    real = tmp_path / f"real-{field}"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / f"linked-{field}"
    link.symlink_to(real)
    options = _options(tmp_path, **{field: link})
    with pytest.raises(ValueError, match="regular file without symlinks"):
        options.validate()


def test_cli_preserves_symlink_identity_until_validation(tmp_path: Path) -> None:
    topology, fixture, ssh_config = _write_inputs(tmp_path)
    linked_fixture = tmp_path / "fixture-link.json"
    linked_fixture.symlink_to(fixture)
    options = parse_args(
        [
            "--mode",
            "lan-shared",
            "--deployment",
            "clean",
            "--topology",
            str(topology),
            "--fixture",
            str(linked_fixture),
            "--ssh-config",
            str(ssh_config),
            "--remote-root",
            r"C:\TTS\TTS_more",
            "--output",
            str(tmp_path / "run-001"),
        ]
    )
    with pytest.raises(ValueError, match="without symlinks"):
        options.validate()


def test_options_require_new_absolute_nonsymlinked_output(tmp_path: Path) -> None:
    options = _options(tmp_path, output=Path("relative-output"))
    with pytest.raises(ValueError, match="absolute new directory"):
        options.validate()

    existing = _options(tmp_path).output
    existing.mkdir()
    with pytest.raises(ValueError, match="absolute new directory"):
        _options(tmp_path, output=existing).validate()

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _options(tmp_path, output=linked_parent / "run-001").validate()


@pytest.mark.parametrize("field", ["topology", "fixture", "ssh_config", "output"])
def test_options_reject_non_path_field_types(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match="Path"):
        _options(tmp_path, **{field: "not-a-path-object"}).validate()


@pytest.mark.parametrize(
    "remote_root",
    [
        r"TTS\TTS_more",
        r"C:\TTS\..\escape",
        r"C:\TTS\NUL",
        r"C:\TTS Folder\TTS_more",
        "C:\\TTS\\bad\nroot",
    ],
)
def test_options_reject_unsafe_remote_roots(tmp_path: Path, remote_root: str) -> None:
    with pytest.raises(ValueError, match="remote root"):
        _options(tmp_path, remote_root=remote_root).validate()


def test_controller_commit_confirms_complete_clean_repository(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []
    outputs = iter([str(tmp_path), COMMIT, ""])
    monkeypatch.setattr(
        "app.lan_orchestration._trusted_executable", lambda path: str(path)
    )

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(argv, 0, next(outputs), "")

    assert controller_commit(tmp_path, process_runner=runner) == COMMIT
    assert calls == [
        [str(Path("/usr/bin/git")), "rev-parse", "--show-toplevel"],
        [str(Path("/usr/bin/git")), "rev-parse", "HEAD"],
        [
            str(Path("/usr/bin/git")),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
    ]


def test_controller_commit_rejects_repository_subdirectory(
    tmp_path: Path, monkeypatch
) -> None:
    subdirectory = tmp_path / "backend"
    subdirectory.mkdir()
    monkeypatch.setattr(
        "app.lan_orchestration._trusted_executable", lambda path: str(path)
    )

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, str(tmp_path), "")

    with pytest.raises(ValueError, match="repository root"):
        controller_commit(subdirectory, process_runner=runner)


def test_controller_identity_hashes_raw_uuid_without_exposing_it(monkeypatch) -> None:
    raw_uuid = "ABCD-0123-secret-platform-uuid"
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            argv,
            0,
            f'"IOPlatformUUID" = "{raw_uuid}"',
            "",
        )

    monkeypatch.setattr("app.lan_orchestration.sys.platform", "darwin")
    monkeypatch.setattr(
        "app.lan_orchestration._trusted_executable", lambda path: str(path)
    )
    digest = controller_id_sha256(b"s" * 32, process_runner=runner)
    assert digest == hashlib.sha256(b"s" * 32 + b"\0" + raw_uuid.encode()).hexdigest()
    assert raw_uuid not in digest
    assert calls == [
        [str(Path("/usr/sbin/ioreg")), "-rd1", "-c", "IOPlatformExpertDevice"]
    ]


def test_network_and_probe_identities_are_distinct(tmp_path: Path) -> None:
    topology_path, _, _ = _write_inputs(tmp_path)
    topology, policy = load_lan_policy(topology_path, LanMode.SHARED)

    def resolver(host: str, _port: object, **_kwargs: object) -> list[tuple]:
        address = {"controller.lan": "192.0.2.10", "gpu-worker.lan": "192.0.2.20"}[host]
        return [(2, 1, 6, "", (address, 0))]

    validate_network_identities(topology, policy, resolver=resolver)
    probe = NodeProbe(
        node="gpu-worker",
        commit=COMMIT,
        host_key_sha256="b" * 64,
        machine_id_sha256="c" * 64,
        gpu_uuid_sha256=("d" * 64,),
        cuda_runtime="12.8",
        memory_total_mib=24_000,
    )
    validate_node_probes(policy, "e" * 64, [probe])


def test_network_identity_rejects_shared_dns_address(tmp_path: Path) -> None:
    topology_path, _, _ = _write_inputs(tmp_path)
    topology, policy = load_lan_policy(topology_path, LanMode.SHARED)

    def resolver(*_args: object, **_kwargs: object) -> list[tuple]:
        return [(2, 1, 6, "", ("192.0.2.10", 0))]

    with pytest.raises(ValueError, match="same address"):
        validate_network_identities(topology, policy, resolver=resolver)


def test_network_identity_binds_ssh_target_to_topology_dns(tmp_path: Path) -> None:
    topology_path, _, _ = _write_inputs(tmp_path)
    topology, policy = load_lan_policy(topology_path, LanMode.SHARED)

    def resolver(host: str, _port: object, **_kwargs: object) -> list[tuple]:
        address = {"controller.lan": "192.0.2.10", "gpu-worker.lan": "192.0.2.20"}[host]
        return [(2, 1, 6, "", (address, 0))]

    with pytest.raises(ValueError, match="does not match topology DNS"):
        validate_network_identities(
            topology,
            policy,
            resolver=resolver,
            ssh_targets={
                "gpu-worker": SimpleNamespace(address="192.0.2.99"),
            },
        )


def test_probe_identity_rejects_controller_worker_collision(tmp_path: Path) -> None:
    topology_path, _, _ = _write_inputs(tmp_path)
    _, policy = load_lan_policy(topology_path, LanMode.SHARED)
    probe = NodeProbe(
        node="gpu-worker",
        commit=COMMIT,
        host_key_sha256="b" * 64,
        machine_id_sha256="c" * 64,
        gpu_uuid_sha256=("d" * 64,),
        cuda_runtime="12.8",
        memory_total_mib=24_000,
    )
    with pytest.raises(ValueError, match="machine identities must be distinct"):
        validate_node_probes(policy, "c" * 64, [probe])


def test_probe_identity_rejects_malformed_hashes(tmp_path: Path) -> None:
    topology_path, _, _ = _write_inputs(tmp_path)
    _, policy = load_lan_policy(topology_path, LanMode.SHARED)
    probe = NodeProbe(
        node="gpu-worker",
        commit=COMMIT,
        host_key_sha256="not-a-hash",
        machine_id_sha256="c" * 64,
        gpu_uuid_sha256=("d" * 64,),
        cuda_runtime="12.8",
        memory_total_mib=24_000,
    )
    with pytest.raises(ValueError, match="probe identity hash is invalid"):
        validate_node_probes(policy, "e" * 64, [probe])


def test_write_preflight_emits_schema_v2_and_only_token_hash(tmp_path: Path) -> None:
    options = _options(tmp_path)
    options.output.mkdir()
    token = "one-time-secret-token"
    probe = NodeProbe(
        node="gpu-worker",
        commit=COMMIT,
        host_key_sha256="b" * 64,
        machine_id_sha256="c" * 64,
        gpu_uuid_sha256=("d" * 64,),
        cuda_runtime="12.8",
        memory_total_mib=24_000,
    )

    path = write_preflight(options, COMMIT, "e" * 64, [probe], token)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["mode"] == "lan-shared"
    assert payload["controller_commit"] == COMMIT
    assert payload["controller_id_sha256"] == hashlib.sha256(
        ("e" * 64).encode("utf-8")
    ).hexdigest()
    assert payload["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in path.read_text(encoding="utf-8")
    assert set(payload["nodes"]) == {"gpu-worker"}


def test_write_preflight_rejects_duplicate_probe_nodes(tmp_path: Path) -> None:
    options = _options(tmp_path)
    options.output.mkdir()
    probe = NodeProbe(
        node="gpu-worker",
        commit=COMMIT,
        host_key_sha256="b" * 64,
        machine_id_sha256="c" * 64,
        gpu_uuid_sha256=("d" * 64,),
        cuda_runtime="12.8",
        memory_total_mib=24_000,
    )
    with pytest.raises(ValueError, match="probe set"):
        write_preflight(
            options,
            COMMIT,
            "e" * 64,
            [probe, probe],
            "one-time-secret-token",
        )


def test_written_preflight_matches_current_cuda_schema_v2_contract(
    tmp_path: Path,
) -> None:
    options = _options(tmp_path)
    options.output.mkdir()
    token = "one-time-secret-token"
    controller_identity = "e" * 64
    probe = NodeProbe(
        node="gpu-worker",
        commit=COMMIT,
        host_key_sha256="b" * 64,
        machine_id_sha256="c" * 64,
        gpu_uuid_sha256=("d" * 64,),
        cuda_runtime="12.8",
        memory_total_mib=24_000,
    )
    preflight = write_preflight(
        options,
        COMMIT,
        controller_identity,
        [probe],
        token,
    )
    runner = CUDAValidationRunner(
        mode=options.mode.value,
        services_path=options.output / "services.external.json",
        fixture_path=options.fixture,
        output_dir=options.output,
        topology_path=options.topology,
        expected_commit=COMMIT,
        require_baseline=False,
        orchestration_preflight_path=preflight,
        orchestration_token=token,
        controller_identity_provider=lambda: controller_identity,
    )

    assert runner._verify_orchestration() == ""


def test_render_external_services_uses_trusted_argument_array(tmp_path: Path) -> None:
    options = _options(tmp_path)
    options.output.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        output = Path(argv[argv.index("--output") + 1])
        output.write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    services = render_external_services(
        options,
        "controller",
        process_runner=runner,
    )

    assert services == options.output / "services.external.json"
    argv, kwargs = calls[0]
    assert argv[1:6] == [
        str(Path(__file__).resolve().parents[2] / "scripts" / "tts_more_deploy.py"),
        "render-services",
        "--profile",
        "app-only",
        "--platform",
    ]
    assert argv[6] == "posix"
    assert argv[argv.index("--topology") + 1] == str(options.topology)
    assert argv[argv.index("--node") + 1] == "controller"
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == Path(__file__).resolve().parents[2]


def test_wait_for_services_requires_real_nonempty_registry(tmp_path: Path) -> None:
    missing = tmp_path / "missing-services.json"
    with pytest.raises(ValueError, match="services registry"):
        wait_for_services(missing, timeout_seconds=1)

    empty = tmp_path / "empty-services.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="services registry"):
        wait_for_services(empty, timeout_seconds=1)


def test_core_runner_receives_token_in_memory_not_argv(tmp_path: Path) -> None:
    options = _options(tmp_path)
    options.output.mkdir()
    services = options.output / "services.external.json"
    services.write_text("[]", encoding="utf-8")
    preflight = options.output / "orchestration-preflight.json"
    preflight.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> dict[str, bool]:
            return {"passed": True}

    run_core_cuda_validation(
        options,
        services,
        preflight,
        "one-time-secret-token",
        expected_commit=COMMIT,
        controller_identity="e" * 64,
        runner_factory=FakeRunner,
    )

    assert captured["mode"] == "lan-shared"
    assert captured["expected_commit"] == COMMIT
    assert captured["orchestration_token"] == "one-time-secret-token"
    assert captured["orchestration_preflight_path"] == preflight
    assert captured["controller_identity_provider"]() == "e" * 64


class _FakeExecutor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def resolve(self, node: str) -> SimpleNamespace:
        self.events.append(f"ssh-resolve:{node}")
        return SimpleNamespace(hostname=f"{node}.lan", address="192.0.2.20")

    def with_pinned_targets(self, targets: dict[str, SimpleNamespace]) -> "_FakeExecutor":
        self.events.append("ssh-pin:" + ",".join(sorted(targets)))
        return self


class _FakeManager:
    def __init__(self, events: list[str], *, fail_collect: bool = False) -> None:
        self.events = events
        self.fail_collect = fail_collect

    def sync_checkout(self, node: str, remote_root: str, commit: str) -> None:
        self.events.append(f"sync:{node}")

    def deploy(self, node: str, remote_root: str, topology: Path, *, clean: bool) -> None:
        self.events.append(f"deploy:{node}:{clean}")

    def start_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
        self.events.append(f"monitor-start:{node}")

    def start(self, node: str, remote_root: str) -> None:
        self.events.append(f"worker-start:{node}")

    def inspect(self, node: str, remote_root: str, commit: str) -> NodeProbe:
        self.events.append(f"inspect:{node}")
        return NodeProbe(
            node=node,
            commit=commit,
            host_key_sha256="b" * 64,
            machine_id_sha256="c" * 64,
            gpu_uuid_sha256=("d" * 64,),
            cuda_runtime="12.8",
            memory_total_mib=24_000,
        )

    def stop_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
        self.events.append(f"monitor-stop:{node}")

    def collect_evidence(
        self,
        node: str,
        remote_root: str,
        output: Path,
        service_ids: tuple[str, ...],
    ) -> None:
        assert os.environ.get("TTS_MORE_ORCHESTRATION_TOKEN")
        self.events.append(f"collect:{node}")
        if self.fail_collect:
            raise RuntimeError("collection failed with private path C:\\secret")

    def stop_all_services(self, node: str, ports: tuple[int, ...]) -> None:
        assert os.environ.get("TTS_MORE_ORCHESTRATION_TOKEN")
        assert ports == (9880, 9881, 9882)
        self.events.append(f"services-stop:{node}")


def _patch_orchestration_dependencies(monkeypatch, events: list[str], *, fail_core: bool) -> None:
    monkeypatch.setattr(
        "app.lan_orchestration.secrets.token_hex",
        lambda _size: "one-time-secret-token",
    )
    monkeypatch.setattr(
        "app.lan_orchestration.secrets.token_bytes",
        lambda size: b"s" * size,
    )
    monkeypatch.setattr(
        "app.lan_orchestration.validate_network_identities",
        lambda *_args, **_kwargs: events.append("network"),
    )
    monkeypatch.setattr(
        "app.lan_orchestration.controller_commit",
        lambda *_args, **_kwargs: events.append("commit") or COMMIT,
    )
    monkeypatch.setattr(
        "app.lan_orchestration.controller_id_sha256",
        lambda *_args, **_kwargs: events.append("controller-id") or "e" * 64,
    )

    def render(options: LanRunOptions, app_node: str, **_kwargs: object) -> Path:
        events.append("render")
        path = options.output / "services.external.json"
        path.write_text("[]", encoding="utf-8")
        return path

    monkeypatch.setattr("app.lan_orchestration.render_external_services", render)
    monkeypatch.setattr(
        "app.lan_orchestration.wait_for_services",
        lambda *_args, **_kwargs: events.append("wait"),
    )
    monkeypatch.setattr(
        "app.lan_orchestration.validate_node_probes",
        lambda *_args, **_kwargs: events.append("probe-validation"),
    )

    def preflight(options: LanRunOptions, *_args: object, **_kwargs: object) -> Path:
        events.append("preflight")
        path = options.output / "orchestration-preflight.json"
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr("app.lan_orchestration.write_preflight", preflight)

    def core(*_args: object, **_kwargs: object) -> None:
        events.append("core")
        if fail_core:
            token = os.environ["TTS_MORE_ORCHESTRATION_TOKEN"]
            raise RuntimeError(f"core failure contains secret {token}")

    monkeypatch.setattr("app.lan_orchestration.run_core_cuda_validation", core)

    @contextmanager
    def application(*_args: object, **_kwargs: object):
        events.append("application-start")
        try:
            yield object()
        finally:
            events.append("application-stop")

    monkeypatch.setattr("app.lan_orchestration.control_plane", application)
    monkeypatch.setattr(
        "app.lan_orchestration.run_workstation_e2e",
        lambda *_args, **_kwargs: events.append("playwright"),
    )
    monkeypatch.setattr(
        "app.lan_orchestration.run_fault_recovery",
        lambda *_args, **_kwargs: events.append("fault-recovery") or {"recovery_passed": True},
    )
    monkeypatch.setattr(
        "app.lan_orchestration.write_lan_evidence",
        lambda *_args, **_kwargs: events.append("evidence-manifest"),
    )
    monkeypatch.setattr(
        "app.lan_orchestration.assert_required_evidence",
        lambda *_args, **_kwargs: events.append("evidence-gate"),
    )


def test_orchestrator_has_no_skip_path_and_cleans_owned_processes(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path)
    events: list[str] = []
    _patch_orchestration_dependencies(monkeypatch, events, fail_core=False)
    executor = _FakeExecutor(events)
    manager = _FakeManager(events)
    orchestrator = LanOrchestrator(
        options,
        executor=executor,
        node_manager_factory=lambda *_args, **_kwargs: manager,
        process_runner=lambda *_args, **_kwargs: None,
    )

    assert orchestrator.run() == 0
    assert events == [
        "commit",
        "controller-id",
        "ssh-resolve:gpu-worker",
        "network",
        "ssh-pin:gpu-worker",
        "sync:gpu-worker",
        "deploy:gpu-worker:True",
        "monitor-start:gpu-worker",
        "worker-start:gpu-worker",
        "inspect:gpu-worker",
        "render",
        "wait",
        "probe-validation",
        "preflight",
        "core",
        "application-start",
        "playwright",
        "fault-recovery",
        "application-stop",
        "monitor-stop:gpu-worker",
        "collect:gpu-worker",
        "services-stop:gpu-worker",
        "evidence-manifest",
        "evidence-gate",
    ]
    assert "TTS_MORE_ORCHESTRATION_TOKEN" not in os.environ
    assert json.loads(
        (options.output / "workflow-outcomes.json").read_text(encoding="utf-8")
    ) == {
        "schema_version": 1,
        "core": "success",
        "playwright": "success",
        "cleanup": "success",
    }


def test_failure_writes_bounded_blocker_without_secret_and_still_cleans_up(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path)
    events: list[str] = []
    _patch_orchestration_dependencies(monkeypatch, events, fail_core=True)
    executor = _FakeExecutor(events)
    manager = _FakeManager(events, fail_collect=True)
    orchestrator = LanOrchestrator(
        options,
        executor=executor,
        node_manager_factory=lambda *_args, **_kwargs: manager,
        process_runner=lambda *_args, **_kwargs: None,
    )

    assert orchestrator.run() == 1
    assert events[-3:] == [
        "monitor-stop:gpu-worker",
        "collect:gpu-worker",
        "services-stop:gpu-worker",
    ]
    assert "TTS_MORE_ORCHESTRATION_TOKEN" not in os.environ
    evidence = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in options.output.rglob("*")
        if path.is_file()
    )
    assert "one-time-secret" not in evidence
    assert "C:\\secret" not in evidence
    summary = options.output / "summary.json"
    assert summary.is_file()
    assert summary.stat().st_size < 64 * 1024
    assert json.loads(summary.read_text(encoding="utf-8"))["passed"] is False
    assert json.loads(
        (options.output / "workflow-outcomes.json").read_text(encoding="utf-8")
    ) == {
        "schema_version": 1,
        "core": "failure",
        "playwright": "skipped",
        "cleanup": "failure",
    }


@pytest.mark.parametrize(
    ("failed_stage", "expected"),
    [
        (
            "playwright",
            {"core": "success", "playwright": "failure", "cleanup": "skipped"},
        ),
        (
            "fault-recovery",
            {"core": "success", "playwright": "success", "cleanup": "failure"},
        ),
    ],
)
def test_orchestrator_records_truthful_stage_outcomes(
    tmp_path: Path, monkeypatch, failed_stage: str, expected: dict[str, str]
) -> None:
    options = _options(tmp_path)
    events: list[str] = []
    _patch_orchestration_dependencies(monkeypatch, events, fail_core=False)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"{failed_stage} failed")

    target = {
        "playwright": "run_workstation_e2e",
        "fault-recovery": "run_fault_recovery",
    }[failed_stage]
    monkeypatch.setattr(f"app.lan_orchestration.{target}", fail)
    orchestrator = LanOrchestrator(
        options,
        executor=_FakeExecutor(events),
        node_manager_factory=lambda *_args, **_kwargs: _FakeManager(events),
        process_runner=lambda *_args, **_kwargs: None,
    )

    assert orchestrator.run() == 1
    outcomes = json.loads(
        (options.output / "workflow-outcomes.json").read_text(encoding="utf-8")
    )
    assert outcomes == {"schema_version": 1, **expected}


def test_ambiguous_monitor_start_still_attempts_monitor_stop_and_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path)
    events: list[str] = []
    _patch_orchestration_dependencies(monkeypatch, events, fail_core=False)

    class AmbiguousMonitorManager(_FakeManager):
        def start_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
            self.events.append(f"monitor-started-remotely:{node}")
            raise RuntimeError("SSH disconnected after monitor start")

    manager = AmbiguousMonitorManager(events)
    orchestrator = LanOrchestrator(
        options,
        executor=_FakeExecutor(events),
        node_manager_factory=lambda *_args, **_kwargs: manager,
        process_runner=lambda *_args, **_kwargs: None,
    )

    assert orchestrator.run() == 1
    assert events[-3:] == [
        "monitor-started-remotely:gpu-worker",
        "monitor-stop:gpu-worker",
        "collect:gpu-worker",
    ]
    assert "services-stop:gpu-worker" not in events


def test_ambiguous_worker_start_still_attempts_owned_service_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path)
    events: list[str] = []
    _patch_orchestration_dependencies(monkeypatch, events, fail_core=False)

    class AmbiguousWorkerManager(_FakeManager):
        def start(self, node: str, remote_root: str) -> None:
            self.events.append(f"worker-started-remotely:{node}")
            raise RuntimeError("SSH disconnected after worker start")

    manager = AmbiguousWorkerManager(events)
    orchestrator = LanOrchestrator(
        options,
        executor=_FakeExecutor(events),
        node_manager_factory=lambda *_args, **_kwargs: manager,
        process_runner=lambda *_args, **_kwargs: None,
    )

    assert orchestrator.run() == 1
    assert events[-4:] == [
        "worker-started-remotely:gpu-worker",
        "monitor-stop:gpu-worker",
        "collect:gpu-worker",
        "services-stop:gpu-worker",
    ]


def test_sync_failure_still_attempts_evidence_for_every_policy_worker(
    tmp_path: Path, monkeypatch
) -> None:
    options = _options(tmp_path)
    events: list[str] = []
    _patch_orchestration_dependencies(monkeypatch, events, fail_core=False)

    class FailedSyncManager(_FakeManager):
        def sync_checkout(self, node: str, remote_root: str, commit: str) -> None:
            self.events.append(f"sync-failed:{node}")
            raise RuntimeError("remote sync failed")

    manager = FailedSyncManager(events)
    orchestrator = LanOrchestrator(
        options,
        executor=_FakeExecutor(events),
        node_manager_factory=lambda *_args, **_kwargs: manager,
        process_runner=lambda *_args, **_kwargs: None,
    )

    assert orchestrator.run() == 1
    assert "collect:gpu-worker" in events
    assert "monitor-stop:gpu-worker" not in events
    assert "services-stop:gpu-worker" not in events


def test_launchers_are_thin_argument_forwarders() -> None:
    root = Path(__file__).resolve().parents[2]
    python_launcher = root / "scripts" / "run-lan-validation.py"
    shell_launcher = root / "scripts" / "run-lan-validation.sh"
    powershell_launcher = root / "scripts" / "run-lan-validation.ps1"

    python_text = python_launcher.read_text(encoding="utf-8")
    shell_text = shell_launcher.read_text(encoding="utf-8")
    powershell_text = powershell_launcher.read_text(encoding="utf-8")

    assert "from app.lan_orchestration import main" in python_text
    assert "raise SystemExit(main())" in python_text
    assert 'exec "$PYTHON" "$ROOT/scripts/run-lan-validation.py" "$@"' in shell_text
    assert "run-lan-validation.py\") @args" in powershell_text
    combined = "\n".join((python_text, shell_text, powershell_text)).casefold()
    for forbidden in ("deploy-local-tts", "cleanrepos", "cuda-validationrunner"):
        assert forbidden not in combined
    if os.name != "nt":
        assert python_launcher.stat().st_mode & 0o111
        assert shell_launcher.stat().st_mode & 0o111
