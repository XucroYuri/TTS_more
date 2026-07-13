from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
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


def _embedded_python(script: str, variable: str = "manifestValidator") -> str:
    match = re.search(rf"\${variable} = @'\n(.*?)\n'@", script, re.DOTALL)
    assert match is not None
    return match.group(1)


class FakeExecutor:
    def __init__(
        self,
        inspect_payload: object | None = None,
        *,
        evidence_bytes: bytes = b"evidence",
        evidence_digest: str | None = None,
    ) -> None:
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
        self.evidence_bytes = evidence_bytes
        self.evidence_digest = evidence_digest

    def run_powershell(
        self, alias: str, script: str, *, timeout: int = 1800
    ) -> SshCommandResult:
        self.scripts.append((alias, script, timeout))
        if "TTS_MORE_EVIDENCE_SNAPSHOT" in script:
            snapshot = re.search(r"\$snapshot = '([^']+)'", script)
            assert snapshot is not None
            digest = self.evidence_digest or hashlib.sha256(self.evidence_bytes).hexdigest()
            return SshCommandResult(
                json.dumps(
                    {
                        "snapshot_path": snapshot.group(1).replace("\\", "/"),
                        "size": len(self.evidence_bytes),
                        "sha256": digest,
                    }
                ),
                "",
            )
        if "ConvertTo-Json -Compress" in script:
            return SshCommandResult(json.dumps(self.inspect_payload), "")
        return SshCommandResult("", "")

    def copy_to(self, alias: str, source: Path, remote_path: str) -> None:
        self.copies_to.append((alias, source, remote_path))

    def copy_from(self, alias: str, remote_path: str, destination: Path) -> None:
        self.copies_from.append((alias, remote_path, destination))
        destination.write_bytes(self.evidence_bytes)

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
    assert re.search(
        r"-PidManifest 'C:\\TTS\\TTS_more\\data\\validation\\lan-controller\\service-processes-[0-9a-f]{16}\.json'",
        script,
    )
    assert "-Detach" in script
    assert "fresh manifest" in script.casefold()
    assert "Generated validation service PID manifest is invalid" in script


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
    assert ".Kill()" in stop_script
    assert "Stop-Process" not in stop_script


@pytest.mark.parametrize(
    "run_id",
    [
        "../escape",
        "run/id",
        ".",
        "run';exit #",
        " run",
        "con",
        "AUX.txt",
        "com1",
        "Lpt9.log",
        "run.",
        "run ",
        "RUN-20260713",
    ],
)
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
    manager.start("gpt-worker", r"C:\TTS\TTS_more")
    executor.scripts.clear()

    manager.stop_service("gpt-worker", 9880)

    script = executor.scripts[-1][1]
    assert "app.workers.gpt_sovits_worker:app" in script
    assert script.count("Get-CimInstance Win32_Process") >= 2
    assert "OwningProcess" in script
    assert "process identity mismatch" in script
    assert "Test-ExactCommandToken" in script
    assert "Test-ExactPortTokens" in script
    assert "executable_path" in script
    assert "project_root" in script
    assert "creation_date" in script
    assert ".Handle" in script
    assert ".Kill()" in script
    assert "Stop-Process" not in script
    second_snapshot = script.rindex("Get-CimInstance Win32_Process")
    assert second_snapshot < script.index(".Kill()", second_snapshot)


def test_stop_service_fails_closed_without_a_manager_owned_start() -> None:
    manager = WindowsLanNodeManager(FakeExecutor(), salt=b"salt")

    with pytest.raises(ValueError, match="owned service manifest"):
        manager.stop_service("gpt-worker", 9880)


def test_monitor_is_bounded_failure_atomic_and_retry_reconciled() -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")

    manager.start_gpu_monitor("gpt-worker", r"C:\TTS\TTS_more", "run-20260713")

    launch = executor.scripts[-1][1]
    encoded = re.search(r"'-EncodedCommand','([A-Za-z0-9+/=]+)'", launch)
    assert encoded is not None
    monitor = base64.b64decode(encoded.group(1)).decode("utf-16-le")
    assert "Stopwatch" in monitor
    assert "maxRows" in monitor
    assert "maxBytes" in monitor
    assert "deadline" in monitor
    assert "FileMode]::CreateNew" in monitor
    assert "while ($true)" not in monitor
    assert "nvidia-smi.stderr.log" in launch
    assert "nvidia-smi.process.json.tmp" in launch
    assert "reconcile" in launch.casefold()
    assert "Get-Command powershell.exe" in launch
    assert "$process.Kill()" in launch
    assert "Remove-Item" in launch
    assert "Move-Item" in launch


def test_monitor_stop_uses_exact_identity_and_second_snapshot_immediately_before_kill() -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")

    manager.stop_gpu_monitor("gpt-worker", r"C:\TTS\TTS_more", "run-20260713")

    script = executor.scripts[-1][1]
    assert script.count("Get-CimInstance Win32_Process") >= 2
    assert "Test-ExactCommandToken" in script
    assert "executable_path" in script
    assert "project_root" in script
    assert "Get-Command powershell.exe" in script
    assert ".Handle" in script
    assert ".Kill()" in script
    assert "Stop-Process" not in script
    second_snapshot = script.rindex("Get-CimInstance Win32_Process")
    assert second_snapshot < script.index(".Kill()", second_snapshot)


def test_manifest_validators_use_bounded_single_snapshots_and_exact_numeric_types() -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")
    manager.start("gpt-worker", r"C:\TTS\TTS_more")
    start_script = executor.scripts[-1][1]
    assert "type(payload[\"schema_version\"]) is not int" in start_script
    assert "read(MAX_BYTES + 1)" in start_script
    assert "sha256" in start_script
    assert "fresh manifest" in start_script.casefold()

    manager.stop_gpu_monitor("gpt-worker", r"C:\TTS\TTS_more", "run-20260713")
    monitor_stop = executor.scripts[-1][1]
    assert "type(payload[\"schema_version\"]) is not int" in monitor_stop
    assert "type(pid) is not int" in monitor_stop
    assert "read(MAX_BYTES + 1)" in monitor_stop
    assert "snapshot_sha256" in monitor_stop


def test_service_manifest_validator_rejects_bool_float_duplicate_and_extra(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")
    manager.start("gpt-worker", r"C:\TTS\TTS_more")
    validator = _embedded_python(executor.scripts[-1][1])
    manifest = tmp_path / "manifest.json"
    root = tmp_path / "root"
    root.mkdir()
    process = {
        "pid": 123,
        "creation_date": "20260713010101.000000+480",
        "executable_path": str(root / ".venv" / "Scripts" / "python.exe"),
        "project_root": str(root),
        "worker_module": "app.workers.gpt_sovits_worker:app",
        "service_id": "local-gpt-sovits-main",
    }

    invalid_payloads = [
        {"schema_version": True, "processes": [process]},
        {"schema_version": 1.0, "processes": [process]},
        {"schema_version": 1, "processes": [{**process, "pid": True}]},
        {"schema_version": 1, "processes": [{**process, "pid": 123.0}]},
        {"schema_version": 1, "processes": [{**process, "extra": "value"}]},
    ]
    for payload in invalid_payloads:
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-c", validator, str(root), str(manifest)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0

    manifest.write_text(
        '{"schema_version":1,"schema_version":1,"processes":[]}',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-c", validator, str(root), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0


def test_monitor_manifest_validator_rejects_exact_type_and_structure_variants(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")
    manager.stop_gpu_monitor("gpt-worker", r"C:\TTS\TTS_more", "run-20260713")
    validator = _embedded_python(executor.scripts[-1][1])
    manifest = tmp_path / "monitor.json"
    root = tmp_path / "root"
    root.mkdir()
    command_digest = "a" * 64
    valid = {
        "schema_version": 1,
        "pid": 456,
        "creation_date": "20260713010101.000000+480",
        "executable_path": str(root / "powershell.exe"),
        "project_root": str(root),
        "command_sha256": command_digest,
    }
    invalid_payloads = [
        {**valid, "schema_version": True},
        {**valid, "schema_version": 1.0},
        {**valid, "pid": True},
        {**valid, "pid": 456.0},
        {**valid, "extra": "value"},
    ]
    for payload in invalid_payloads:
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                validator,
                str(manifest),
                command_digest,
                str(root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0

    manifest.write_text(
        '{"schema_version":1,"pid":456,"pid":457}', encoding="utf-8"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            validator,
            str(manifest),
            command_digest,
            str(root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0


def test_service_stop_consumes_validated_snapshot_not_replaced_manifest(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"salt")
    manager.start("gpt-worker", r"C:\TTS\TTS_more")
    manager.stop_service("gpt-worker", 9880)
    validator = _embedded_python(executor.scripts[-1][1])
    manifest = tmp_path / "service.json"
    root = tmp_path / "root"
    root.mkdir()
    original = {
        "schema_version": 1,
        "processes": [
            {
                "pid": 123,
                "creation_date": "original-creation",
                "executable_path": str(root / "python.exe"),
                "project_root": str(root),
                "worker_module": "app.workers.gpt_sovits_worker:app",
                "service_id": "local-gpt-sovits-main",
            }
        ],
    }
    raw = json.dumps(original).encode("utf-8")
    manifest.write_bytes(raw)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            validator,
            str(manifest),
            str(root),
            "local-gpt-sovits-main",
            "app.workers.gpt_sovits_worker:app",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    snapshot = json.loads(completed.stdout)

    manifest.write_text(json.dumps({**original, "processes": []}), encoding="utf-8")
    assert snapshot["process"]["pid"] == 123
    assert snapshot["snapshot_sha256"] == hashlib.sha256(raw).hexdigest()


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

    source_scripts = [script for _, script, _ in executor.scripts if "TTS_MORE_EVIDENCE_SNAPSHOT" in script]
    assert len(source_scripts) == 4
    assert "run-20260713\\nvidia-smi.csv" in source_scripts[0]
    assert "run-20260713\\nvidia-smi.stderr.log" in source_scripts[1]
    assert "data\\.runtime\\logs\\local-gpt-sovits-main.log" in source_scripts[2]
    assert "data\\.runtime\\logs\\local-indextts.log" in source_scripts[3]
    assert all("ReparsePoint" in script for script in source_scripts)
    assert all("FileMode]::CreateNew" in script for script in source_scripts)
    assert all("maxBytes" in script for script in source_scripts)
    assert all(".evidence-snapshot-" in remote for _, remote, _ in executor.copies_from)
    assert all(destination.name.endswith(".tmp") for _, _, destination in executor.copies_from)
    destination = output / "worker-logs" / "gpt-worker"
    assert (destination / "nvidia-smi.csv").read_bytes() == b"evidence"
    assert (destination / "local-gpt-sovits-main.log").read_bytes() == b"evidence"
    assert not list(destination.glob("*.tmp"))


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


@pytest.mark.parametrize("link_name", ["worker-logs", "gpt-worker"])
def test_collect_evidence_rejects_symlinked_destination_component(
    tmp_path: Path, link_name: str
) -> None:
    output = tmp_path / "run-20260713"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    worker_logs = output / "worker-logs"
    if link_name == "worker-logs":
        worker_logs.symlink_to(outside, target_is_directory=True)
    else:
        worker_logs.mkdir()
        (worker_logs / "gpt-worker").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|containment"):
        WindowsLanNodeManager(FakeExecutor(), salt=b"salt").collect_evidence(
            "gpt-worker",
            r"C:\TTS\TTS_more",
            output,
            ("local-gpt-sovits-main",),
        )
    assert not list(outside.iterdir())


def test_collect_evidence_rejects_digest_mismatch_without_publishing(tmp_path: Path) -> None:
    output = tmp_path / "run-20260713"
    output.mkdir()
    executor = FakeExecutor(evidence_digest="0" * 64)

    with pytest.raises(ValueError, match="digest"):
        WindowsLanNodeManager(executor, salt=b"salt").collect_evidence(
            "gpt-worker",
            r"C:\TTS\TTS_more",
            output,
            ("local-gpt-sovits-main",),
        )

    destination = output / "worker-logs" / "gpt-worker"
    assert not (destination / "nvidia-smi.csv").exists()
    assert not list(destination.glob("*.tmp"))
