from __future__ import annotations

import json
import base64
import re
from pathlib import Path

import pytest

from app.lan_nodes import NodeProbe, WindowsLanNodeManager
from app.windows_ssh import SshCommandResult


COMMIT = "a" * 40
FORMAL_SERVICES = (
    "local-gpt-sovits-main",
    "local-indextts",
    "local-cosyvoice",
)


def _topology_payload(node: str = "gpt-worker") -> dict[str, object]:
    return {
        "schema_version": 1,
        "name": "validation",
        "app_node": "app-controller",
        "nodes": {
            "app-controller": {
                "role": "app",
                "host": "mac.example.test",
                "bind_host": "127.0.0.1",
                "services": [],
                "resource_group": "app",
                "capacity": 1,
            },
            node: {
                "role": "worker",
                "host": "worker.example.test",
                "bind_host": "0.0.0.0",
                "services": list(FORMAL_SERVICES),
                "resource_group": "gpu-0",
                "capacity": 1,
            },
        },
    }


def _write_topology(path: Path, node: str = "gpt-worker") -> Path:
    path.write_text(json.dumps(_topology_payload(node)), encoding="utf-8")
    return path


class FakeExecutor:
    def __init__(self, inspect_payload: object | None = None) -> None:
        self.scripts: list[tuple[str, str, int]] = []
        self.copies_to: list[tuple[str, Path, str]] = []
        self.copies_from: list[tuple[str, str, Path]] = []
        self.inspect_payload = inspect_payload or {
            "commit": COMMIT,
            "dirty": "",
            "machine_id": "machine-a",
            "gpu_uuids": ["GPU-a"],
            "cuda_runtime": "12.8",
            "memory_total_mib": 24576,
        }

    def run_powershell(
        self, alias: str, script: str, *, timeout: int = 1800
    ) -> SshCommandResult:
        self.scripts.append((alias, script, timeout))
        if "ConvertTo-Json -Compress" in script:
            return SshCommandResult(json.dumps(self.inspect_payload), "")
        return SshCommandResult("", "")

    def copy_to(self, alias: str, source: Path, remote_path: str) -> None:
        self.copies_to.append((alias, source, remote_path))

    def copy_from(self, alias: str, remote_path: str, destination: Path) -> None:
        self.copies_from.append((alias, remote_path, destination))

    def pinned_host_key_sha256(self, alias: str) -> str:
        return "b" * 64


@pytest.fixture
def manager() -> WindowsLanNodeManager:
    return WindowsLanNodeManager(FakeExecutor(), salt=b"run-salt")


def test_inspect_hashes_machine_and_gpu_identity_without_storing_raw_values() -> None:
    probe = WindowsLanNodeManager(FakeExecutor(), salt=b"run-salt").inspect(
        "gpt-worker", r"C:\TTS\TTS_more", COMMIT
    )

    assert isinstance(probe, NodeProbe)
    assert probe.commit == COMMIT
    assert probe.machine_id_sha256 != "machine-a"
    assert probe.gpu_uuid_sha256 != ("GPU-a",)
    assert len(probe.machine_id_sha256) == 64
    assert all(len(value) == 64 for value in probe.gpu_uuid_sha256)
    assert "machine-a" not in repr(probe)
    assert "GPU-a" not in repr(probe)


@pytest.mark.parametrize(
    "payload",
    [
        {"commit": COMMIT},
        {
            "commit": COMMIT,
            "dirty": "",
            "machine_id": "machine-a",
            "gpu_uuids": ["GPU-a"],
            "cuda_runtime": "12.8",
            "memory_total_mib": 24576,
            "unexpected": True,
        },
        {
            "commit": COMMIT,
            "dirty": "",
            "machine_id": "machine-a",
            "gpu_uuids": [],
            "cuda_runtime": "12.8",
            "memory_total_mib": 24576,
        },
        {
            "commit": COMMIT,
            "dirty": "",
            "machine_id": "machine-a",
            "gpu_uuids": ["GPU-a"],
            "cuda_runtime": "12.8",
            "memory_total_mib": True,
        },
    ],
)
def test_inspect_rejects_nonconforming_json(payload: object) -> None:
    with pytest.raises(ValueError, match="probe JSON is invalid"):
        WindowsLanNodeManager(FakeExecutor(payload), salt=b"salt").inspect(
            "gpt-worker", r"C:\TTS\TTS_more", COMMIT
        )


@pytest.mark.parametrize("stdout", ["not-json", '{"commit":"a","commit":"b"}'])
def test_inspect_rejects_malformed_or_duplicate_key_json(stdout: str) -> None:
    executor = FakeExecutor()

    def run(_alias: str, _script: str, *, timeout: int = 1800) -> SshCommandResult:
        return SshCommandResult(stdout, "")

    executor.run_powershell = run  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="probe JSON is invalid"):
        WindowsLanNodeManager(executor, salt=b"salt").inspect(
            "gpt-worker", r"C:\TTS\TTS_more", COMMIT
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node", "worker'; Stop-Process 1; #"),
        ("node", ".."),
        ("remote_root", r"C:\TTS\..\Windows"),
        ("remote_root", "C:\\TTS\nInjected"),
        ("remote_root", r"\\server\share\TTS"),
        ("expected_commit", "a" * 39),
        ("expected_commit", "A" * 40),
    ],
)
def test_inspect_rejects_unsafe_identifiers_and_paths(
    manager: WindowsLanNodeManager, field: str, value: str
) -> None:
    arguments = {
        "node": "gpt-worker",
        "remote_root": r"C:\TTS\TTS_more",
        "expected_commit": COMMIT,
    }
    arguments[field] = value
    with pytest.raises(ValueError):
        manager.inspect(**arguments)


def test_sync_checkout_uses_validated_literal_values(manager: WindowsLanNodeManager) -> None:
    manager.sync_checkout("gpt-worker", r"C:\TTS\TTS_more", COMMIT)

    alias, script, timeout = manager.executor.scripts[-1]
    assert alias == "gpt-worker"
    assert timeout == 600
    assert "status --porcelain --untracked-files=all" in script
    assert f"fetch origin '{COMMIT}'" in script
    assert f"checkout --detach '{COMMIT}'" in script


def test_deploy_validates_topology_and_complete_repo_confirmation(tmp_path: Path) -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"run-salt")
    topology = _write_topology(tmp_path / "topology.json")

    manager.deploy("gpt-worker", r"C:\TTS\TTS_more", topology, clean=True)

    assert executor.copies_to == [
        (
            "gpt-worker",
            topology,
            "C:/TTS/TTS_more/data/local/topology.validation.json",
        )
    ]
    deploy_script = executor.scripts[-1][1]
    assert "repo-paths.local.json is not a complete confirmation file" in deploy_script
    assert "repo.lock.json" in deploy_script
    assert "-Profile 'worker-node'" in deploy_script
    assert "-Device 'CU128'" in deploy_script
    assert "-Targets 'default'" in deploy_script
    assert "-CleanRepos" in deploy_script
    assert executor.scripts[-1][2] == 6 * 60 * 60


def test_deploy_rejects_invalid_or_wrong_node_topology(tmp_path: Path) -> None:
    manager = WindowsLanNodeManager(FakeExecutor(), salt=b"salt")
    wrong_node = _write_topology(tmp_path / "wrong.json", node="other-worker")
    with pytest.raises(ValueError, match="worker node"):
        manager.deploy("gpt-worker", r"C:\TTS\TTS_more", wrong_node, clean=False)

    duplicate_keys = tmp_path / "duplicate.json"
    duplicate_keys.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="topology JSON is invalid"):
        manager.deploy("gpt-worker", r"C:\TTS\TTS_more", duplicate_keys, clean=False)


def test_start_uses_real_worker_cli_and_controlled_pid_manifest() -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")

    manager.start("gpt-worker", r"C:\TTS\TTS_more")

    script = executor.scripts[-1][1]
    assert "start-service-workers.ps1" in script
    assert "-Topology 'C:\\TTS\\TTS_more\\data\\local\\topology.validation.json'" in script
    assert "-Node 'gpt-worker'" in script
    assert "-RepoPaths 'C:\\TTS\\TTS_more\\deployment\\app\\repo-paths.local.json'" in script
    assert "-PidManifest 'C:\\TTS\\TTS_more\\data\\validation\\lan-controller\\service-processes.json'" in script
    assert "-Detach" in script
    assert "already exists" not in script
    assert "Existing validation service PID manifest is invalid" in script


def test_gpu_monitor_manifest_records_process_identity_and_stop_verifies_it() -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")

    manager.start_gpu_monitor("gpt-worker", r"C:\TTS\TTS_more", "run-20260713")
    start_script = executor.scripts[-1][1]
    assert "nvidia-smi.process.json" in start_script
    assert "CreationDate" in start_script
    encoded = re.search(r"'-EncodedCommand','([A-Za-z0-9+/=]+)'", start_script)
    assert encoded is not None
    monitor_script = base64.b64decode(encoded.group(1)).decode("utf-16-le")
    assert "timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu" in monitor_script
    assert "SHA256" in monitor_script
    assert "gpu_uuid_sha256" in monitor_script
    assert "nvidia-smi.exe" in monitor_script

    manager.stop_gpu_monitor("gpt-worker", r"C:\TTS\TTS_more", "run-20260713")
    stop_script = executor.scripts[-1][1]
    assert "ConvertFrom-Json" in stop_script
    assert "object_pairs_hook" in stop_script
    assert "schema_version" in stop_script
    assert "CreationDate" in stop_script
    assert "powershell.exe" in stop_script
    assert "command_sha256" in stop_script
    assert "Stop-Process" in stop_script


@pytest.mark.parametrize("run_id", ["../escape", "run/id", ".", "run';exit #", " run"])
def test_gpu_monitor_rejects_unsafe_run_id(
    manager: WindowsLanNodeManager, run_id: str
) -> None:
    with pytest.raises(ValueError, match="run ID"):
        manager.start_gpu_monitor("gpt-worker", r"C:\TTS\TTS_more", run_id)


@pytest.mark.parametrize("port", [0, 9883, True, "9880"])
def test_stop_service_rejects_nonformal_port(
    manager: WindowsLanNodeManager, port: object
) -> None:
    with pytest.raises(ValueError, match="formal worker port"):
        manager.stop_service("gpt-worker", port)  # type: ignore[arg-type]


def test_stop_service_verifies_listener_process_identity_before_stopping() -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")

    manager.stop_service("gpt-worker", 9880)

    script = executor.scripts[-1][1]
    assert "app.workers.gpt_sovits_worker:app" in script
    assert "Win32_Process" in script
    assert "OwningProcess" in script
    assert "process identity mismatch" in script
    assert "Stop-Process" in script


def test_collect_evidence_uses_only_controlled_remote_paths(tmp_path: Path) -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")
    output = tmp_path / "run-20260713"
    output.mkdir()

    manager.collect_evidence(
        "gpt-worker",
        r"C:\TTS\TTS_more",
        output,
        ("local-gpt-sovits-main", "local-indextts"),
    )

    assert [remote for _, remote, _ in executor.copies_from] == [
        "C:/TTS/TTS_more/data/validation/lan-controller/run-20260713/nvidia-smi.csv",
        "C:/TTS/TTS_more/data/validation/lan-controller/run-20260713/nvidia-smi.stderr.log",
        "C:/TTS/TTS_more/data/.runtime/logs/local-gpt-sovits-main.log",
        "C:/TTS/TTS_more/data/.runtime/logs/local-indextts.log",
    ]
    assert all(destination.is_relative_to(output) for _, _, destination in executor.copies_from)


def test_collect_evidence_rejects_service_and_unsafe_output(tmp_path: Path) -> None:
    manager = WindowsLanNodeManager(FakeExecutor(), salt=b"salt")
    output = tmp_path / "run"
    output.mkdir()
    with pytest.raises(ValueError, match="formal service ID"):
        manager.collect_evidence(
            "gpt-worker", r"C:\TTS\TTS_more", output, ("../../fixture.log",)
        )

    relative_output = Path("run")
    with pytest.raises(ValueError, match="absolute directory"):
        manager.collect_evidence(
            "gpt-worker", r"C:\TTS\TTS_more", relative_output, FORMAL_SERVICES
        )
