# macOS LAN CUDA Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a cross-platform validation entrypoint that runs the TTS More application on macOS, deploys and controls Windows CUDA workers over pinned OpenSSH, validates shared-GPU and three-GPU LAN topologies, and produces auditable evidence without weakening the existing Windows release gate.

**Architecture:** Add a topology-policy module, a bounded Windows OpenSSH adapter, and a Python LAN orchestrator with thin POSIX/PowerShell wrappers. Extend the existing CUDA validator with `lan-shared` and `lan-distributed` policies while retaining the current `distributed` contract, then compose remote deployment, core synthesis, Playwright, failure injection and evidence collection in one non-bypassable run.

**Tech Stack:** Python 3.10-3.13, Pydantic v2, FastAPI/httpx, OpenSSH/SCP, Windows PowerShell, pytest, Playwright, pnpm, GitHub Actions.

## Global Constraints

- The macOS controller has no CUDA requirement; all GPU identity and `nvidia-smi` evidence comes from Windows workers.
- Windows workers must use CUDA runtime `12.8` and expose at least `16,000 MiB` total VRAM through `/status`.
- Formal service IDs remain `local-gpt-sovits-main`, `local-indextts`, and `local-cosyvoice`.
- `lan-shared` requires one Windows worker, one shared resource group, `capacity: 1`, one GPU UUID and no overlapping loaded models.
- `lan-distributed` requires three Windows workers, one service per worker, distinct host/IP/machine/GPU identities and at least two overlapping loaded models.
- SSH uses key-only authentication, `IdentitiesOnly yes`, `BatchMode yes`, `StrictHostKeyChecking yes`, and a non-null pinned `UserKnownHostsFile`.
- SSH `Host` aliases must exactly equal topology worker node names.
- Formal automation requires one absolute Windows `--remote-root` without spaces, for example `C:\TTS\TTS_more`, so the same SCP path is unambiguous on every worker.
- Remote services stay `mode: external`, `network_scope: lan`, `managed: false`; no shared filesystem is accepted.
- Reference audio and generated output use `artifact-transfer` with existing 25 MiB upload and 100 MiB output limits.
- Real topology, fixture, SSH config, users, hosts, keys, machine paths, weights and reviewer identities remain ignored or runner-local.
- The CLI requires `--deployment clean|release` and exposes no skip flag for deploy, start, identity, monitoring, failure injection or evidence collection.
- The existing Windows `single-clean`, `single-release`, and `distributed` behavior must remain backward compatible.
- The macOS workflow is manual-only until real shared and three-node certification records are approved.

---

## File Map

- Create `backend/app/lan_topology.py`: validation-specific topology models and shared/distributed policy derivation.
- Create `backend/app/windows_ssh.py`: secure, shell-free OpenSSH/SCP command adapter.
- Create `backend/app/lan_evidence.py`: schema-v2 preflight, identity hashing and required-evidence manifest.
- Modify `backend/app/cuda_validation.py`: LAN modes, generic orchestration preflight and evidence redaction.
- Create `backend/app/lan_nodes.py`: Windows worker inspection, deployment, process control, GPU monitor and evidence retrieval.
- Create `backend/app/lan_orchestration.py`: macOS controller state machine and CLI.
- Create `scripts/run-lan-validation.py`: import-safe Python launcher.
- Create `scripts/run-lan-validation.sh`: POSIX virtualenv wrapper.
- Create `scripts/run-lan-validation.ps1`: Windows-compatible thin wrapper for diagnostics, not the macOS release authority.
- Modify `frontend/e2e/cuda-workstation.spec.ts`: treat `lan-distributed` as overlapping and `lan-shared` as serialized.
- Create `.github/workflows/macos-lan-gpu-validation.yml`: manual self-hosted macOS workflow.
- Create `backend/tests/test_lan_topology.py`, `test_windows_ssh.py`, `test_lan_evidence.py`, `test_lan_nodes.py`, and `test_lan_orchestration.py`.
- Modify `backend/tests/test_cuda_validation.py` and `test_gpu_workflow.py`.
- Update CUDA runbooks, CI architecture, release governance, acceptance record and README only after behavior exists.

---

### Task 1: LAN Topology Policy

**Files:**
- Create: `backend/app/lan_topology.py`
- Create: `backend/tests/test_lan_topology.py`

**Interfaces:**
- Produces: `LanMode`, `LanNode`, `LanTopology`, `LanPolicy`, `load_lan_policy(path, mode)`.
- Consumed by: Tasks 3, 4, 5 and 6.

- [ ] **Step 1: Write failing shared and distributed policy tests**

```python
from pathlib import Path

import pytest

from app.lan_topology import LanMode, load_lan_policy


FORMAL = ["local-gpt-sovits-main", "local-indextts", "local-cosyvoice"]


def write_topology(tmp_path: Path, workers: dict) -> Path:
    import json

    path = tmp_path / "topology.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "test-lan",
                "app_node": "app-controller",
                "nodes": {
                    "app-controller": {
                        "role": "app",
                        "host": "mac-controller.lan",
                        "bind_host": "127.0.0.1",
                        "services": [],
                        "resource_group": "app",
                        "capacity": 1,
                    },
                    **workers,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_shared_policy_requires_one_capacity_one_worker(tmp_path: Path) -> None:
    path = write_topology(
        tmp_path,
        {
            "shared-worker": {
                "role": "worker",
                "host": "tts-shared.lan",
                "bind_host": "0.0.0.0",
                "services": FORMAL,
                "resource_group": "shared-worker:cuda-0",
                "capacity": 1,
            }
        },
    )

    _, policy = load_lan_policy(path, LanMode.SHARED)

    assert policy.workers == ("shared-worker",)
    assert set(policy.service_owners) == set(FORMAL)
    assert policy.expected_gpu_count == 1
    assert policy.require_overlap is False


def test_distributed_policy_rejects_duplicate_hosts(tmp_path: Path) -> None:
    workers = {
        f"worker-{index}": {
            "role": "worker",
            "host": "same-host.lan",
            "bind_host": "0.0.0.0",
            "services": [service_id],
            "resource_group": f"worker-{index}:cuda-0",
            "capacity": 1,
        }
        for index, service_id in enumerate(FORMAL)
    }
    path = write_topology(tmp_path, workers)

    with pytest.raises(ValueError, match="distinct worker hosts"):
        load_lan_policy(path, LanMode.DISTRIBUTED)
```

- [ ] **Step 2: Run the tests and verify import failure**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_topology.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.lan_topology'`.

- [ ] **Step 3: Implement the topology models and policy derivation**

```python
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


FORMAL_SERVICE_IDS = frozenset(
    {"local-gpt-sovits-main", "local-indextts", "local-cosyvoice"}
)


class LanMode(str, Enum):
    SHARED = "lan-shared"
    DISTRIBUTED = "lan-distributed"


class LanNode(BaseModel):
    role: Literal["app", "worker"]
    host: str = Field(min_length=1)
    bind_host: str = Field(min_length=1)
    services: list[str]
    resource_group: str = Field(min_length=1)
    capacity: int = Field(ge=1)


class LanTopology(BaseModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    app_node: str = Field(min_length=1)
    nodes: dict[str, LanNode]


@dataclass(frozen=True)
class LanPolicy:
    mode: LanMode
    app_node: str
    workers: tuple[str, ...]
    service_owners: dict[str, str]
    expected_gpu_count: int
    require_overlap: bool


def _is_loopback(host: str) -> bool:
    normalized = host.casefold().rstrip(".").strip("[]")
    if normalized in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def load_lan_policy(path: Path, mode: LanMode) -> tuple[LanTopology, LanPolicy]:
    topology = LanTopology.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if topology.app_node not in topology.nodes:
        raise ValueError("topology app_node is missing")
    if topology.nodes[topology.app_node].role != "app":
        raise ValueError("topology app_node must have role app")
    if topology.nodes[topology.app_node].services:
        raise ValueError("topology app node cannot own services")

    workers = {name: node for name, node in topology.nodes.items() if node.role == "worker"}
    owners: dict[str, str] = {}
    for name, node in workers.items():
        if _is_loopback(node.host):
            raise ValueError(f"worker {name} must use a non-loopback host")
        for service_id in node.services:
            if service_id in owners:
                raise ValueError(f"service {service_id} has multiple owners")
            owners[service_id] = name
    if set(owners) != FORMAL_SERVICE_IDS:
        raise ValueError("topology must assign every formal service exactly once")

    if mode is LanMode.SHARED:
        if len(workers) != 1:
            raise ValueError("lan-shared requires exactly one worker")
        worker = next(iter(workers.values()))
        if worker.capacity != 1 or set(worker.services) != FORMAL_SERVICE_IDS:
            raise ValueError("lan-shared worker must own all formal services at capacity 1")
        return topology, LanPolicy(mode, topology.app_node, tuple(workers), owners, 1, False)

    if len(workers) != 3 or any(len(node.services) != 1 for node in workers.values()):
        raise ValueError("lan-distributed requires three one-service workers")
    hosts = {node.host.casefold().rstrip(".") for node in workers.values()}
    groups = {node.resource_group for node in workers.values()}
    if len(hosts) != 3:
        raise ValueError("lan-distributed requires distinct worker hosts")
    if len(groups) != 3 or any(node.capacity != 1 for node in workers.values()):
        raise ValueError("lan-distributed requires distinct capacity-one resource groups")
    return topology, LanPolicy(mode, topology.app_node, tuple(workers), owners, 3, True)
```

- [ ] **Step 4: Run topology tests**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_topology.py backend/tests/test_deploy_tool.py -q`

Expected: all tests PASS, proving the validation policy agrees with existing deployment topology behavior.

- [ ] **Step 5: Commit**

```bash
git add backend/app/lan_topology.py backend/tests/test_lan_topology.py
git commit -m "feat: add LAN validation topology policies"
```

---

### Task 2: Secure Windows OpenSSH Adapter

**Files:**
- Create: `backend/app/windows_ssh.py`
- Create: `backend/tests/test_windows_ssh.py`

**Interfaces:**
- Produces: `SshResolvedTarget`, `SshCommandResult`, `WindowsSshExecutor`.
- Consumed by: Tasks 4, 5 and 6.

- [ ] **Step 1: Write failing command-construction and policy tests**

```python
import subprocess
from pathlib import Path

import pytest

from app.windows_ssh import WindowsSshExecutor


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append(argv)
        return self.responses.pop(0)


def test_validate_target_requires_pinned_strict_config(tmp_path: Path) -> None:
    config = tmp_path / "ssh_config"
    config.write_text("Host gpt-worker\n  HostName tts-gpt.lan\n", encoding="utf-8")
    runner = FakeRunner(
        [subprocess.CompletedProcess([], 0, "hostname tts-gpt.lan\nuser tester\nbatchmode no\n", "")]
    )

    with pytest.raises(ValueError, match="BatchMode yes"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")


def test_run_powershell_uses_encoded_command_without_shell(tmp_path: Path) -> None:
    config = tmp_path / "ssh_config"
    config.write_text("Host gpt-worker\n", encoding="utf-8")
    resolved = subprocess.CompletedProcess(
        [],
        0,
        "hostname tts-gpt.lan\nuser tester\nbatchmode yes\nidentitiesonly yes\n"
        "stricthostkeychecking true\nuserknownhostsfile ~/.ssh/known_hosts_tts_more\n",
        "",
    )
    runner = FakeRunner([resolved, subprocess.CompletedProcess([], 0, "ok", "")])
    executor = WindowsSshExecutor(config, runner=runner)

    result = executor.run_powershell("gpt-worker", "Get-Date")

    assert result.stdout == "ok"
    assert runner.calls[1][:4] == ["ssh", "-F", str(config), "-o"]
    assert "powershell.exe" in runner.calls[1]
    assert "-EncodedCommand" in runner.calls[1]
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q`

Expected: FAIL with missing `app.windows_ssh`.

- [ ] **Step 3: Implement shell-free SSH, SCP and host-key hashing**

```python
from __future__ import annotations

import base64
import hashlib
import ipaddress
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class SshResolvedTarget:
    alias: str
    hostname: str
    user: str
    known_hosts_file: Path


@dataclass(frozen=True)
class SshCommandResult:
    stdout: str
    stderr: str


class WindowsSshExecutor:
    def __init__(
        self,
        config_path: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.config_path = config_path.resolve()
        self.runner = runner

    def _run(self, argv: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
        result = self.runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"remote command failed with exit code {result.returncode}")
        return result

    def resolve(self, alias: str) -> SshResolvedTarget:
        if not alias or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in alias):
            raise ValueError("SSH alias contains unsupported characters")
        output = self._run(["ssh", "-F", str(self.config_path), "-G", alias], timeout=30).stdout
        settings = dict(line.split(None, 1) for line in output.splitlines() if " " in line)
        if settings.get("batchmode") != "yes":
            raise ValueError("SSH target must set BatchMode yes")
        if settings.get("identitiesonly") != "yes":
            raise ValueError("SSH target must set IdentitiesOnly yes")
        if settings.get("stricthostkeychecking") not in {"yes", "true"}:
            raise ValueError("SSH target must set StrictHostKeyChecking yes")
        known_hosts = settings.get("userknownhostsfile", "").split()[0]
        if not known_hosts or known_hosts == "/dev/null":
            raise ValueError("SSH target must use a pinned UserKnownHostsFile")
        hostname = settings.get("hostname", "")
        try:
            if ipaddress.ip_address(hostname.strip("[]")).is_loopback:
                raise ValueError("SSH target cannot resolve to loopback")
        except ValueError as exc:
            if "loopback" in str(exc):
                raise
        return SshResolvedTarget(alias, hostname, settings.get("user", ""), Path(known_hosts).expanduser())

    def run_powershell(self, alias: str, script: str, *, timeout: int = 1800) -> SshCommandResult:
        self.resolve(alias)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        argv = [
            "ssh", "-F", str(self.config_path), "-o", "BatchMode=yes", alias,
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
        ]
        result = self._run(argv, timeout=timeout)
        return SshCommandResult(result.stdout, result.stderr)

    def copy_to(self, alias: str, source: Path, remote_path: str) -> None:
        self.resolve(alias)
        self._run(["scp", "-F", str(self.config_path), str(source), f"{alias}:{remote_path}"], timeout=600)

    def copy_from(self, alias: str, remote_path: str, destination: Path) -> None:
        self.resolve(alias)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["scp", "-F", str(self.config_path), f"{alias}:{remote_path}", str(destination)], timeout=600)

    def pinned_host_key_sha256(self, alias: str) -> str:
        target = self.resolve(alias)
        result = self._run(
            ["ssh-keygen", "-F", target.hostname, "-f", str(target.known_hosts_file)], timeout=30
        )
        if not result.stdout.strip():
            raise ValueError(f"no pinned host key found for {alias}")
        return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Add traversal, `/dev/null`, `accept-new`, error-redaction and SCP tests**

Add explicit assertions that invalid aliases never reach the runner, `StrictHostKeyChecking accept-new` is rejected, SCP uses the configured file, and exception strings contain neither encoded scripts nor private-key paths.

Run: `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/windows_ssh.py backend/tests/test_windows_ssh.py
git commit -m "feat: add pinned Windows OpenSSH adapter"
```

---

### Task 3: LAN Modes And Orchestration Preflight In The CUDA Runner

**Files:**
- Create: `backend/app/lan_evidence.py`
- Create: `backend/tests/test_lan_evidence.py`
- Modify: `backend/app/cuda_validation.py:33-120, 500-810, 980-1025, 1200-1235`
- Modify: `backend/tests/test_cuda_validation.py`

**Interfaces:**
- Consumes: `LanMode` values from Task 1.
- Produces: `LanNodePreflight`, `LanOrchestrationPreflight`, `write_lan_preflight`, generic `--orchestration-preflight`, LAN-aware `CUDAValidationRunner`.
- Consumed by: Tasks 5 and 6.

- [ ] **Step 1: Add failing tests for shared UUID, distributed UUID and fixture-bound preflight**

```python
def test_lan_shared_accepts_one_gpu_uuid_for_all_services(validation_paths, fake_clients) -> None:
    runner = build_runner(
        validation_paths,
        fake_clients,
        mode="lan-shared",
        status_device_uuids={service_id: "GPU-SHARED" for service_id in FORMAL_SERVICE_IDS.values()},
        orchestration_preflight=True,
    )

    report = runner.run()

    assert report["passed"] is True
    assert report["orchestration_verified"] is True


def test_lan_distributed_rejects_duplicate_gpu_uuid(validation_paths, fake_clients) -> None:
    runner = build_runner(
        validation_paths,
        fake_clients,
        mode="lan-distributed",
        status_device_uuids={service_id: "GPU-DUPLICATE" for service_id in FORMAL_SERVICE_IDS.values()},
        orchestration_preflight=True,
    )

    report = runner.run()

    assert report["passed"] is False
    assert "distinct CUDA device UUID" in str(report["services"])


def test_lan_preflight_rejects_fixture_hash_mismatch(validation_paths, fake_clients) -> None:
    runner = build_runner(validation_paths, fake_clients, mode="lan-shared", orchestration_preflight=True)
    validation_paths.fixture.write_text("{}", encoding="utf-8")

    report = runner.run()

    assert report["passed"] is False
    assert "fixture hash does not match" in str(report["preflight"])


import json
from datetime import datetime, timezone
from pathlib import Path


def test_lan_preflight_writer_uses_schema_two(tmp_path: Path) -> None:
    from app.lan_evidence import LanNodePreflight, LanOrchestrationPreflight, write_lan_preflight

    path = tmp_path / "preflight.json"
    payload = LanOrchestrationPreflight(
        schema_version=2,
        mode="lan-shared",
        topology_sha256="a" * 64,
        fixture_sha256="b" * 64,
        controller_commit="c" * 40,
        controller_id_sha256="d" * 64,
        nodes={
            "shared-worker": LanNodePreflight(
                commit="c" * 40,
                host_key_sha256="e" * 64,
                machine_id_sha256="f" * 64,
            )
        },
        token_sha256="0" * 64,
        created_at=datetime.now(timezone.utc),
    )

    write_lan_preflight(path, payload)

    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
```

Use the existing test factories in `test_cuda_validation.py`; extend them instead of creating a second fake worker stack.

- [ ] **Step 2: Run targeted tests and confirm failures**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_evidence.py backend/tests/test_cuda_validation.py -q`

Expected: FAIL because LAN modes and generic preflight are not accepted.

- [ ] **Step 3: Add LAN mode sets and versioned preflight models**

```python
VALIDATION_MODES = (
    "single-clean", "single-release", "distributed", "lan-shared", "lan-distributed"
)
EXTERNAL_LAN_MODES = frozenset({"distributed", "lan-shared", "lan-distributed"})
DISTINCT_GPU_MODES = frozenset({"distributed", "lan-distributed"})


class LanNodePreflight(BaseModel):
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    host_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LanOrchestrationPreflight(BaseModel):
    schema_version: Literal[2]
    mode: Literal["lan-shared", "lan-distributed"]
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    controller_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    controller_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nodes: dict[str, LanNodePreflight]
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


def write_lan_preflight(path: Path, payload: LanOrchestrationPreflight) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload.model_dump_json(indent=2) + "\n", encoding="utf-8")
```

Place the two schema-v2 models in `lan_evidence.py`. Keep `DistributedOrchestrationPreflight` schema version 1 unchanged in `cuda_validation.py`. Add constructor parameters `orchestration_preflight_path` and `orchestration_token`; map the legacy distributed parameters to them when the new names are absent.

- [ ] **Step 4: Replace mode conditionals with explicit policy sets**

```python
is_external_lan = self.mode in EXTERNAL_LAN_MODES
requires_distinct_gpu = self.mode in DISTINCT_GPU_MODES

if is_external_lan and (
    endpoint.mode != "external" or endpoint.network_scope != "lan" or endpoint.managed
):
    report["preflight"].append(
        {"passed": False, "message": f"{endpoint.service_id} is not an unmanaged external LAN worker"}
    )

if is_external_lan and "artifact-transfer" not in normalized_capabilities:
    report["preflight"].append(
        {"passed": False, "message": f"{endpoint.service_id} lacks artifact-transfer capability"}
    )

if requires_distinct_gpu:
    reject_duplicate_device_uuids(report["services"])
elif self.mode == "lan-shared":
    uuids = {str(item["status"]["device_uuid"]) for item in report["services"] if item.get("status")}
    if len(uuids) != 1:
        for item in report["services"]:
            item["errors"].append("lan-shared workers must report one shared CUDA device UUID")
```

External LAN modes use artifact delivery for all five core cases. Preserve the extra GPT path/artifact comparison only for local single-node modes.

- [ ] **Step 5: Verify generic preflight and redact raw hardware identity**

Implement `_verify_orchestration()` so schema v2 verifies mode, topology hash, fixture hash, controller commit, token hash, 12-hour timestamp, and that the preflight node set exactly matches `load_lan_policy()` for the selected mode. Preserve `_verify_distributed_orchestration()` as a compatibility wrapper for schema v1.

Update `_sanitize_evidence()` to accept an optional per-run `hash_key`. For LAN modes derive it from the current orchestration token and use HMAC-SHA256 so public artifacts cannot correlate hardware across runs:

```python
if lowered in {"device_uuid", "machine_id", "controller_id"}:
    key_bytes = hash_key or b"tts-more-local-evidence"
    digest = hmac.new(key_bytes, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return "hmac-sha256:" + digest
```

Pass `hashlib.sha256(self.orchestration_token.encode("utf-8")).digest()` into report writing only after preflight verification. Never write the key or raw token.

Add argparse aliasing:

```python
parser.add_argument(
    "--orchestration-preflight", "--distributed-preflight",
    dest="orchestration_preflight", type=Path,
)
```

- [ ] **Step 6: Run CUDA validator tests**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_evidence.py backend/tests/test_cuda_validation.py backend/tests/test_real_tts_validation.py -q`

Expected: all tests PASS, including legacy distributed tests.

- [ ] **Step 7: Commit**

```bash
git add backend/app/lan_evidence.py backend/app/cuda_validation.py \
  backend/tests/test_lan_evidence.py backend/tests/test_cuda_validation.py
git commit -m "feat: add LAN policies to CUDA validation"
```

---

### Task 4: Windows Worker Lifecycle Manager

**Files:**
- Create: `backend/app/lan_nodes.py`
- Create: `backend/tests/test_lan_nodes.py`

**Interfaces:**
- Consumes: `LanPolicy`, `LanTopology`, `WindowsSshExecutor`.
- Produces: `NodeProbe`, `WindowsLanNodeManager.inspect`, `.sync_checkout`, `.deploy`, `.start`, `.start_gpu_monitor`, `.stop_gpu_monitor`, `.stop_service`, `.stop_all_services`, `.collect_evidence`.
- Consumed by: Tasks 5 and 6.

- [ ] **Step 1: Write failing worker inspection and deployment tests**

```python
import json
from pathlib import Path

from app.lan_nodes import WindowsLanNodeManager


class FakeExecutor:
    def __init__(self) -> None:
        self.scripts: list[tuple[str, str]] = []
        self.copies: list[tuple[str, Path, str]] = []

    def run_powershell(self, alias: str, script: str, *, timeout: int = 1800):
        from app.windows_ssh import SshCommandResult

        self.scripts.append((alias, script))
        if "ConvertTo-Json" in script:
            return SshCommandResult(
                json.dumps(
                    {
                        "commit": "a" * 40,
                        "dirty": "",
                        "machine_id": "machine-a",
                        "gpu_uuids": ["GPU-a"],
                        "cuda_runtime": "12.8",
                        "memory_total_mib": 24576,
                    }
                ),
                "",
            )
        return SshCommandResult("", "")

    def copy_to(self, alias: str, source: Path, remote_path: str) -> None:
        self.copies.append((alias, source, remote_path))

    def copy_from(self, alias: str, remote_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("evidence", encoding="utf-8")

    def pinned_host_key_sha256(self, alias: str) -> str:
        return "b" * 64


def test_inspect_hashes_identity_and_keeps_raw_machine_id_out_of_probe(tmp_path: Path) -> None:
    manager = WindowsLanNodeManager(FakeExecutor(), salt=b"run-salt")

    probe = manager.inspect("gpt-worker", r"C:\TTS\TTS_more", "a" * 40)

    assert probe.commit == "a" * 40
    assert probe.machine_id_sha256 != "machine-a"
    assert len(probe.machine_id_sha256) == 64


def test_deploy_copies_topology_and_uses_clean_repos(tmp_path: Path) -> None:
    executor = FakeExecutor()
    manager = WindowsLanNodeManager(executor, salt=b"run-salt")
    topology = tmp_path / "topology.json"
    topology.write_text("{}", encoding="utf-8")

    manager.deploy("gpt-worker", r"C:\TTS\TTS_more", topology, clean=True)

    assert executor.copies[0][2].endswith("data/local/topology.validation.json")
    assert "-Profile worker-node" in executor.scripts[-1][1]
    assert "-CleanRepos" in executor.scripts[-1][1]
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_nodes.py -q`

Expected: FAIL with missing `app.lan_nodes`.

- [ ] **Step 3: Implement inspection and identity hashing**

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.windows_ssh import WindowsSshExecutor


@dataclass(frozen=True)
class NodeProbe:
    node: str
    commit: str
    host_key_sha256: str
    machine_id_sha256: str
    gpu_uuid_sha256: tuple[str, ...]
    cuda_runtime: str
    memory_total_mib: int


def _hash_identity(value: str, salt: bytes) -> str:
    return hashlib.sha256(salt + b"\0" + value.encode("utf-8")).hexdigest()


class WindowsLanNodeManager:
    def __init__(self, executor: WindowsSshExecutor, *, salt: bytes) -> None:
        self.executor = executor
        self.salt = salt

    def inspect(self, node: str, remote_root: str, expected_commit: str) -> NodeProbe:
        root = self._quote(remote_root)
        script = f"""
$root = {root}
$commit = (& git -C $root rev-parse HEAD).Trim()
$dirty = (& git -C $root status --porcelain --untracked-files=all) -join "`n"
$machine = (Get-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\Microsoft\\Cryptography').MachineGuid
$gpu = @(& nvidia-smi --query-gpu=uuid --format=csv,noheader)
$header = (& nvidia-smi | Select-Object -First 3) -join ' '
$cudaMatch = [regex]::Match($header, 'CUDA Version:\\s*([0-9.]+)')
$cuda = $cudaMatch.Groups[1].Value
$memory = [int](@(& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits)[0])
[ordered]@{{commit=$commit;dirty=$dirty;machine_id=$machine;gpu_uuids=$gpu;cuda_runtime=$cuda;memory_total_mib=$memory}} | ConvertTo-Json -Compress
"""
        payload = json.loads(self.executor.run_powershell(node, script).stdout)
        if payload["commit"] != expected_commit or payload["dirty"]:
            raise ValueError(f"worker {node} checkout identity mismatch")
        if payload["cuda_runtime"] != "12.8" or int(payload["memory_total_mib"]) < 16000:
            raise ValueError(f"worker {node} does not meet CUDA requirements")
        gpu_values = payload["gpu_uuids"] if isinstance(payload["gpu_uuids"], list) else [payload["gpu_uuids"]]
        return NodeProbe(
            node=node,
            commit=payload["commit"],
            host_key_sha256=self.executor.pinned_host_key_sha256(node),
            machine_id_sha256=_hash_identity(payload["machine_id"], self.salt),
            gpu_uuid_sha256=tuple(_hash_identity(item.strip(), self.salt) for item in gpu_values),
            cuda_runtime=payload["cuda_runtime"],
            memory_total_mib=int(payload["memory_total_mib"]),
        )
```

- [ ] **Step 4: Implement clean sync, topology copy, deploy and start**

Add these methods to `WindowsLanNodeManager`:

```python
@staticmethod
def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

def sync_checkout(self, node: str, remote_root: str, expected_commit: str) -> None:
    root = self._quote(remote_root)
    commit = self._quote(expected_commit)
    script = f"""
$dirty = (& git -C {root} status --porcelain --untracked-files=all) -join "`n"
if ($LASTEXITCODE -ne 0 -or $dirty) {{ throw 'Remote checkout is dirty before sync' }}
& git -C {root} fetch origin {commit}
if ($LASTEXITCODE -ne 0) {{ throw 'Remote fetch failed' }}
& git -C {root} checkout --detach {commit}
if ($LASTEXITCODE -ne 0) {{ throw 'Remote checkout failed' }}
$actual = (& git -C {root} rev-parse HEAD).Trim()
$dirty = (& git -C {root} status --porcelain --untracked-files=all) -join "`n"
if ($actual -ne {commit} -or $dirty) {{ throw 'Remote checkout identity mismatch after sync' }}
"""
    self.executor.run_powershell(node, script, timeout=600)

def deploy(
    self,
    node: str,
    remote_root: str,
    topology: Path,
    *,
    clean: bool,
) -> None:
    remote_topology = str(PureWindowsPath(remote_root) / "data/local/topology.validation.json")
    remote_repo_paths = str(PureWindowsPath(remote_root) / "deployment/app/repo-paths.local.json")
    deploy_script = str(PureWindowsPath(remote_root) / "scripts/deploy-local-tts.ps1")
    expected_hash = hashlib.sha256(topology.read_bytes()).hexdigest()
    self.executor.run_powershell(
        node,
        f"New-Item -ItemType Directory -Force -Path {self._quote(str(PureWindowsPath(remote_topology).parent))} | Out-Null",
    )
    self.executor.copy_to(node, topology, remote_topology.replace("\\", "/"))
    clean_flag = " -CleanRepos" if clean else ""
    script = f"""
if (!(Test-Path -LiteralPath {self._quote(remote_repo_paths)})) {{ throw 'repo-paths.local.json is missing' }}
$hash = (Get-FileHash -LiteralPath {self._quote(remote_topology)} -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hash -ne '{expected_hash}') {{ throw 'Remote topology hash mismatch' }}
& {self._quote(deploy_script)} -Profile worker-node -Device CU128 -Targets default -Topology {self._quote(remote_topology)} -Node {self._quote(node)} -RepoPaths {self._quote(remote_repo_paths)}{clean_flag}
if ($LASTEXITCODE -ne 0) {{ throw 'Remote deployment failed' }}
"""
    self.executor.run_powershell(node, script, timeout=6 * 60 * 60)

def start(self, node: str, remote_root: str) -> None:
    topology = str(PureWindowsPath(remote_root) / "data/local/topology.validation.json")
    repo_paths = str(PureWindowsPath(remote_root) / "deployment/app/repo-paths.local.json")
    script = str(PureWindowsPath(remote_root) / "scripts/start-service-workers.ps1")
    command = (
        f"& {self._quote(script)} -Topology {self._quote(topology)} "
        f"-Node {self._quote(node)} -RepoPaths {self._quote(repo_paths)} -Detach; "
        "if ($LASTEXITCODE -ne 0) { throw 'Remote start failed' }"
    )
    self.executor.run_powershell(node, command, timeout=1800)
```

Import `PureWindowsPath` from `pathlib`. All paths are PowerShell single-quoted with embedded quotes doubled; no local shell interpolation is allowed.

- [ ] **Step 5: Implement monitor, stop and evidence methods**

Use the existing query fields exactly:

```text
timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu
```

Implement the bounded process methods:

```python
def start_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
    evidence = PureWindowsPath(remote_root) / "data/validation/lan-controller" / run_id
    csv_path = evidence / "nvidia-smi.csv"
    pid_path = evidence / "nvidia-smi.pid"
    args = "--query-gpu=timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu --format=csv,noheader,nounits --loop-ms=2000"
    script = f"""
New-Item -ItemType Directory -Force -Path {self._quote(str(evidence))} | Out-Null
$process = Start-Process -FilePath 'nvidia-smi.exe' -ArgumentList {self._quote(args)} -RedirectStandardOutput {self._quote(str(csv_path))} -RedirectStandardError {self._quote(str(evidence / 'nvidia-smi.stderr.log'))} -PassThru
Set-Content -LiteralPath {self._quote(str(pid_path))} -Value $process.Id
"""
    self.executor.run_powershell(node, script)

def stop_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
    pid_path = PureWindowsPath(remote_root) / "data/validation/lan-controller" / run_id / "nvidia-smi.pid"
    script = f"""
if (Test-Path -LiteralPath {self._quote(str(pid_path))}) {{
  $monitorPid = [int](Get-Content -LiteralPath {self._quote(str(pid_path))} -Raw)
  Stop-Process -Id $monitorPid -Force -ErrorAction SilentlyContinue
}}
"""
    self.executor.run_powershell(node, script)

def stop_service(self, node: str, port: int) -> None:
    if port not in {9880, 9881, 9882}:
        raise ValueError("fault injection port is not a formal worker port")
    script = f"""
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction Stop)
if ($listeners.Count -eq 0) {{ throw 'No listener on validation port' }}
$listeners | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {{ Stop-Process -Id $_ -Force }}
"""
    self.executor.run_powershell(node, script)

def stop_all_services(self, node: str, ports: tuple[int, ...]) -> None:
    for port in ports:
        self.stop_service(node, port)

def collect_evidence(
    self,
    node: str,
    remote_root: str,
    output: Path,
    service_ids: tuple[str, ...],
) -> None:
    remote = PureWindowsPath(remote_root) / "data/validation/lan-controller" / output.name
    destination = output / "worker-logs" / node
    self.executor.copy_from(node, str(remote / "nvidia-smi.csv").replace("\\", "/"), destination / "nvidia-smi.csv")
    self.executor.copy_from(node, str(remote / "nvidia-smi.stderr.log").replace("\\", "/"), destination / "nvidia-smi.stderr.log")
    for service_id in service_ids:
        if service_id not in FORMAL_SERVICE_IDS:
            raise ValueError("worker log service is not a formal service ID")
        remote_log = PureWindowsPath(remote_root) / "data/.runtime/logs" / f"{service_id}.log"
        self.executor.copy_from(
            node,
            str(remote_log).replace("\\", "/"),
            destination / f"{service_id}.log",
        )
```

Worker logs are copied only from the deployment tool's fixed `data/.runtime/logs/{formal-service-id}.log` locations; do not accept arbitrary remote paths from the fixture or HTTP responses.

- [ ] **Step 6: Run node lifecycle tests**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_nodes.py backend/tests/test_windows_ssh.py -q`

Expected: all tests PASS; assertions prove no raw machine ID is stored and no command uses `shell=True`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/lan_nodes.py backend/tests/test_lan_nodes.py
git commit -m "feat: manage Windows LAN worker lifecycle"
```

---

### Task 5: Cross-Platform LAN Orchestrator And CLI

**Files:**
- Create: `backend/app/lan_orchestration.py`
- Create: `backend/tests/test_lan_orchestration.py`
- Create: `scripts/run-lan-validation.py`
- Create: `scripts/run-lan-validation.sh`
- Create: `scripts/run-lan-validation.ps1`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: `DeploymentMode`, `LanRunOptions`, `LanOrchestrator.run()`, CLI `main(argv)`.
- Internal helpers with stable signatures: `configure_run_logging(output: Path) -> logging.Logger`, `controller_commit(repo_root: Path) -> str`, `controller_id_sha256(salt: bytes) -> str`, `validate_network_identities(topology, policy) -> None`, `validate_node_probes(policy, controller_hash, probes) -> None`, `render_external_services(options: LanRunOptions, app_node: str) -> Path`, `wait_for_services(services_path: Path, timeout_seconds: int) -> None`, `write_preflight(options, commit, controller_hash, probes, token) -> Path`, and `run_core_cuda_validation(options, services_path, preflight_path, token, output_dir=None) -> None`.
- Consumed by: Tasks 6-8 and the macOS workflow.

- [ ] **Step 1: Write failing CLI and orchestration-order tests**

```python
from pathlib import Path

import pytest

from app.lan_orchestration import DeploymentMode, LanRunOptions, parse_args
from app.lan_topology import LanMode


def test_cli_requires_explicit_deployment_mode(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode", "lan-shared",
                "--topology", str(tmp_path / "topology.json"),
                "--fixture", str(tmp_path / "fixture.json"),
                "--ssh-config", str(tmp_path / "ssh_config"),
                "--remote-root", r"C:\TTS\TTS_more",
                "--output", str(tmp_path / "output"),
            ]
        )


def test_release_requires_baseline(tmp_path: Path) -> None:
    topology = tmp_path / "topology.json"
    fixture = tmp_path / "fixture.json"
    ssh_config = tmp_path / "ssh_config"
    for path in (topology, fixture, ssh_config):
        path.write_text("{}", encoding="utf-8")
    options = LanRunOptions(
        mode=LanMode.DISTRIBUTED,
        deployment=DeploymentMode.RELEASE,
        topology=topology,
        fixture=fixture,
        ssh_config=ssh_config,
        remote_root=r"C:\TTS\TTS_more",
        output=Path("output"),
        require_baseline=False,
    )

    with pytest.raises(ValueError, match="release deployment requires an approved baseline"):
        options.validate()
```

Add a fake node manager and process runner test asserting this order:

```text
validate controller -> sync nodes -> copy topology -> deploy -> start monitor -> start workers
-> render app services -> write one-time preflight -> run core CUDA
-> collect evidence -> clear token
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_orchestration.py -q`

Expected: FAIL with missing `app.lan_orchestration`.

- [ ] **Step 3: Implement options, controller identity and preflight**

```python
class DeploymentMode(str, Enum):
    CLEAN = "clean"
    RELEASE = "release"


@dataclass(frozen=True)
class LanRunOptions:
    mode: LanMode
    deployment: DeploymentMode
    topology: Path
    fixture: Path
    ssh_config: Path
    remote_root: str
    output: Path
    require_baseline: bool

    def validate(self) -> None:
        for path in (self.topology, self.fixture, self.ssh_config):
            if not path.is_file():
                raise ValueError(f"required input does not exist: {path.name}")
        if self.deployment is DeploymentMode.RELEASE and not self.require_baseline:
            raise ValueError("release deployment requires an approved baseline")
        if self.deployment is DeploymentMode.CLEAN and self.require_baseline:
            raise ValueError("clean certification establishes a baseline and cannot require one")
        if not re.fullmatch(r"[A-Za-z]:\\[A-Za-z0-9._\\-]+", self.remote_root):
            raise ValueError("remote root must be an absolute Windows path without spaces")


def parse_args(argv: list[str] | None = None) -> LanRunOptions:
    parser = argparse.ArgumentParser(description="Run macOS-to-Windows LAN CUDA validation")
    parser.add_argument("--mode", required=True, choices=[item.value for item in LanMode])
    parser.add_argument("--deployment", required=True, choices=[item.value for item in DeploymentMode])
    parser.add_argument("--topology", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--ssh-config", required=True, type=Path)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-baseline", action="store_true")
    args = parser.parse_args(argv)
    return LanRunOptions(
        mode=LanMode(args.mode),
        deployment=DeploymentMode(args.deployment),
        topology=args.topology.resolve(),
        fixture=args.fixture.resolve(),
        ssh_config=args.ssh_config.resolve(),
        remote_root=args.remote_root,
        output=args.output.resolve(),
        require_baseline=args.require_baseline,
    )
```

Resolve and hash controller identity with these exact helpers:

```python
def _checked(argv: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"local command failed with exit code {result.returncode}")
    return result.stdout.strip()


def controller_commit(repo_root: Path) -> str:
    commit = _checked(["git", "rev-parse", "HEAD"], cwd=repo_root)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("controller commit is invalid")
    dirty = _checked(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo_root)
    if dirty:
        raise ValueError("controller checkout must be clean")
    return commit


def controller_id_sha256(salt: bytes) -> str:
    if sys.platform != "darwin":
        raise ValueError("LAN release controller must run on macOS")
    output = _checked(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
    if match is None:
        raise ValueError("macOS platform UUID is unavailable")
    return hashlib.sha256(salt + b"\0" + match.group(1).encode("utf-8")).hexdigest()


def validate_network_identities(topology: LanTopology, policy: LanPolicy) -> None:
    owners: dict[str, str] = {}
    for node_name in (policy.app_node, *policy.workers):
        host = topology.nodes[node_name].host
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, None)
            if not ipaddress.ip_address(item[4][0]).is_loopback
        }
        if not addresses:
            raise ValueError(f"topology node {node_name} has no non-loopback address")
        for address in addresses:
            previous = owners.get(address)
            if previous is not None:
                raise ValueError(f"topology nodes {previous} and {node_name} resolve to the same address")
            owners[address] = node_name


def validate_node_probes(
    policy: LanPolicy,
    controller_hash: str,
    probes: list[NodeProbe],
) -> None:
    if {probe.node for probe in probes} != set(policy.workers):
        raise ValueError("worker probe set does not match topology")
    machine_hashes = {controller_hash, *(probe.machine_id_sha256 for probe in probes)}
    if len(machine_hashes) != len(probes) + 1:
        raise ValueError("controller and worker machine identities must be distinct")
    host_keys = {probe.host_key_sha256 for probe in probes}
    if len(host_keys) != len(probes):
        raise ValueError("worker SSH host keys must be distinct")
    if any(not probe.gpu_uuid_sha256 for probe in probes):
        raise ValueError("every worker must expose at least one physical GPU")
    if policy.mode is LanMode.DISTRIBUTED:
        all_gpu_hashes = [value for probe in probes for value in probe.gpu_uuid_sha256]
        if len(set(all_gpu_hashes)) != len(all_gpu_hashes):
            raise ValueError("distributed workers must not expose the same physical GPU UUID")
```

The raw platform UUID is never returned or written.

- [ ] **Step 4: Implement the orchestration state machine**

Implement the helpers referenced by the state machine:

```python
def configure_run_logging(output: Path) -> logging.Logger:
    logger = logging.getLogger("tts_more.lan_validation")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(output / "controller.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def render_external_services(options: LanRunOptions, app_node: str) -> Path:
    services_path = REPO_ROOT / "data" / "local" / "services.json"
    argv = [
        sys.executable, str(REPO_ROOT / "scripts" / "tts_more_deploy.py"),
        "render-services", "--profile", "app-only", "--platform", "posix",
        "--topology", str(options.topology), "--node", app_node,
        "--output", str(services_path),
    ]
    _checked(argv, cwd=REPO_ROOT)
    return services_path


def wait_for_services(services_path: Path, timeout_seconds: int) -> None:
    endpoints = ServiceRegistry.load(services_path).services
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ready = []
        for endpoint in endpoints:
            try:
                health_url = endpoint.health_url or endpoint.base_url.rstrip("/") + "/health"
                response = httpx.get(health_url, timeout=10.0)
                ready.append(response.is_success and bool(response.json().get("ready")))
            except (httpx.HTTPError, ValueError):
                ready.append(False)
        if ready and all(ready):
            return
        time.sleep(5)
    raise TimeoutError("LAN workers did not become ready")


def write_preflight(
    options: LanRunOptions,
    commit: str,
    controller_hash: str,
    probes: list[NodeProbe],
    token: str,
) -> Path:
    payload = LanOrchestrationPreflight(
        schema_version=2,
        mode=options.mode.value,
        topology_sha256=hashlib.sha256(options.topology.read_bytes()).hexdigest(),
        fixture_sha256=hashlib.sha256(options.fixture.read_bytes()).hexdigest(),
        controller_commit=commit,
        controller_id_sha256=controller_hash,
        nodes={
            probe.node: LanNodePreflight(
                commit=probe.commit,
                host_key_sha256=probe.host_key_sha256,
                machine_id_sha256=probe.machine_id_sha256,
            )
            for probe in probes
        },
        token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        created_at=datetime.now(timezone.utc),
    )
    path = options.output / "orchestration-preflight.json"
    write_lan_preflight(path, payload)
    return path


def run_core_cuda_validation(
    options: LanRunOptions,
    services_path: Path,
    preflight_path: Path,
    token: str,
    output_dir: Path | None = None,
) -> None:
    runner = CUDAValidationRunner(
        mode=options.mode.value,
        services_path=services_path,
        fixture_path=options.fixture,
        output_dir=output_dir or options.output,
        topology_path=options.topology,
        expected_commit=controller_commit(REPO_ROOT),
        require_baseline=options.require_baseline,
        orchestration_preflight_path=preflight_path,
        orchestration_token=token,
    )
    report = runner.run()
    if not report["passed"]:
        raise RuntimeError("LAN CUDA core validation failed")
```

Then compose them without Playwright or fault injection yet; Task 6 adds those gates after the core runner:

```python
class LanOrchestrator:
    def __init__(self, options: LanRunOptions, *, executor, node_manager_factory, process_runner) -> None:
        self.options = options
        self.executor = executor
        self.node_manager_factory = node_manager_factory
        self.process_runner = process_runner

    def run(self) -> int:
        self.options.validate()
        topology, policy = load_lan_policy(self.options.topology, self.options.mode)
        validate_network_identities(topology, policy)
        commit = controller_commit(REPO_ROOT)
        token = secrets.token_hex(32)
        salt = secrets.token_bytes(32)
        controller_hash = controller_id_sha256(salt)
        manager = self.node_manager_factory(self.executor, salt=salt)
        self.options.output.mkdir(parents=True, exist_ok=False)
        logger = configure_run_logging(self.options.output)
        probes = []
        evidence_collected = False
        try:
            os.environ["TTS_MORE_LAN_ORCHESTRATION_TOKEN"] = token
            for node in policy.workers:
                logger.info("prepare worker node=%s", node)
                manager.sync_checkout(node, self.options.remote_root, commit)
                manager.deploy(
                    node,
                    self.options.remote_root,
                    self.options.topology,
                    clean=self.options.deployment is DeploymentMode.CLEAN,
                )
                manager.start_gpu_monitor(node, self.options.remote_root, self.options.output.name)
                manager.start(node, self.options.remote_root)
                probes.append(manager.inspect(node, self.options.remote_root, commit))
            services_path = render_external_services(self.options, topology.app_node)
            wait_for_services(services_path, timeout_seconds=600)
            validate_node_probes(policy, controller_hash, probes)
            preflight = write_preflight(self.options, commit, controller_hash, probes, token)
            run_core_cuda_validation(self.options, services_path, preflight, token)
            logger.info("core CUDA validation passed mode=%s", self.options.mode.value)
            return 0
        finally:
            os.environ.pop("TTS_MORE_LAN_ORCHESTRATION_TOKEN", None)
            if not evidence_collected:
                for node in policy.workers:
                    manager.stop_gpu_monitor(node, self.options.remote_root, self.options.output.name)
                    owned_services = tuple(
                        service_id for service_id, owner in policy.service_owners.items() if owner == node
                    )
                    manager.collect_evidence(
                        node, self.options.remote_root, self.options.output, owned_services
                    )
```

Every helper in this block must receive injected runners in tests. Do not call `shell=True` or interpolate command strings into `/bin/sh`.

- [ ] **Step 5: Add launchers**

Finish `backend/app/lan_orchestration.py` with:

```python
def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    executor = WindowsSshExecutor(options.ssh_config)
    orchestrator = LanOrchestrator(
        options,
        executor=executor,
        node_manager_factory=WindowsLanNodeManager,
        process_runner=subprocess.run,
    )
    return orchestrator.run()
```

Create `scripts/run-lan-validation.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.lan_orchestration import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
```

`scripts/run-lan-validation.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "Missing $PYTHON" >&2; exit 1; }
exec "$PYTHON" "$ROOT/scripts/run-lan-validation.py" "$@"
```

`scripts/run-lan-validation.ps1` contains no deployment logic:

```powershell
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python)) { throw "Missing $Python" }
& $Python (Join-Path $Root "scripts\run-lan-validation.py") @args
exit $LASTEXITCODE
```

Mark both POSIX launchers executable:

```bash
chmod +x scripts/run-lan-validation.py scripts/run-lan-validation.sh
```

- [ ] **Step 6: Run orchestrator and CLI tests**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_orchestration.py backend/tests/test_lan_nodes.py backend/tests/test_cuda_validation.py -q`

Expected: all tests PASS. Add a test that attempts `--skip-deploy` and confirms argparse rejects the unknown option.

- [ ] **Step 7: Commit**

```bash
git add backend/app/lan_orchestration.py backend/tests/test_lan_orchestration.py \
  scripts/run-lan-validation.py scripts/run-lan-validation.sh scripts/run-lan-validation.ps1
git commit -m "feat: add cross-platform LAN CUDA orchestrator"
```

---

### Task 6: Application Loop, Failure Injection And Evidence Completion

**Files:**
- Modify: `backend/app/lan_orchestration.py`
- Modify: `backend/app/lan_nodes.py`
- Modify: `backend/app/lan_evidence.py`
- Modify: `backend/tests/test_lan_orchestration.py`
- Modify: `backend/tests/test_lan_nodes.py`
- Modify: `backend/tests/test_lan_evidence.py`

**Interfaces:**
- Extends: `LanOrchestrator.run`, `WindowsLanNodeManager` stop/restart methods.
- Produces helpers: `control_plane(services_path, output)`, `run_workstation_e2e(options)`, `wait_service_state(service_id, ready, timeout_seconds)`, and `run_fault_recovery(options, policy, manager, services_path, preflight_path, token)`.
- Produces: `fault-recovery.json`, `distributed-evidence.json`, worker logs, remote GPU CSV, Playwright JUnit and recovery CUDA report.

- [ ] **Step 1: Write failing shared and distributed fault tests**

```python
def test_shared_fault_stops_one_service_then_all_and_recovers(orchestrator_fixture) -> None:
    result = orchestrator_fixture.run(mode="lan-shared")

    assert result == 0
    report = orchestrator_fixture.read_json("fault-recovery.json")
    assert report["degraded_within_seconds"] <= 15
    assert report["other_services_ready"] is True
    assert report["all_services_degraded"] is True
    assert report["application_survived"] is True
    assert report["recovery_passed"] is True


def test_distributed_fault_keeps_two_workers_ready(orchestrator_fixture) -> None:
    result = orchestrator_fixture.run(mode="lan-distributed")

    assert result == 0
    report = orchestrator_fixture.read_json("fault-recovery.json")
    assert report["fault_node"] == "gpt-worker"
    assert report["other_services_ready"] is True
    assert report["recovery_passed"] is True
```

- [ ] **Step 2: Run tests and verify missing evidence failures**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_orchestration.py -k fault -q`

Expected: FAIL because fault and evidence reports are not complete.

- [ ] **Step 3: Start the loopback control plane and Playwright**

Start uvicorn as an argument list, not a shell command:

```python
backend_argv = [
    str(repo_root / ".venv" / "bin" / "python"),
    "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
    "--host", "127.0.0.1", "--port", "8000",
]
backend_env = {
    **os.environ,
    "TTS_MORE_SERVICE_MODE": "real",
    "TTS_MORE_SERVICES_PATH": str(services_path),
}
```

Wait for `/api/health`, then run:

```python
playwright_env = {
    **os.environ,
    "TTS_MORE_RUN_CUDA_E2E": "1",
    "TTS_MORE_CUDA_VALIDATION_MODE": options.mode.value,
    "TTS_MORE_CUDA_FIXTURE": str(options.fixture),
    "TTS_MORE_E2E_BASE_URL": "http://127.0.0.1:5173",
    "TTS_MORE_API_TARGET": "http://127.0.0.1:8000",
}
argv = ["pnpm", "--dir", "frontend", "cuda:e2e"]
```

Redirect backend stdout/stderr and Playwright output into the run directory. Always terminate the control plane in `finally`.

Implement the process boundary as a context manager and copy the JUnit file into the run directory:

```python
@contextmanager
def control_plane(services_path: Path, output: Path, *, popen_factory=subprocess.Popen):
    logs = output / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TTS_MORE_SERVICE_MODE": "real", "TTS_MORE_SERVICES_PATH": str(services_path)}
    with (logs / "app-backend.stdout.log").open("w", encoding="utf-8") as stdout, (
        logs / "app-backend.stderr.log"
    ).open("w", encoding="utf-8") as stderr:
        process = popen_factory(backend_argv, cwd=REPO_ROOT, env=env, stdout=stdout, stderr=stderr)
        try:
            wait_http_ready("http://127.0.0.1:8000/api/health", timeout_seconds=120)
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)


def run_workstation_e2e(options: LanRunOptions, *, runner=subprocess.run) -> None:
    env = {
        **os.environ,
        "TTS_MORE_RUN_CUDA_E2E": "1",
        "TTS_MORE_CUDA_VALIDATION_MODE": options.mode.value,
        "TTS_MORE_CUDA_FIXTURE": str(options.fixture),
        "TTS_MORE_E2E_BASE_URL": "http://127.0.0.1:5173",
        "TTS_MORE_API_TARGET": "http://127.0.0.1:8000",
    }
    result = runner(
        ["pnpm", "--dir", "frontend", "cuda:e2e"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    (options.output / "playwright.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError("Playwright LAN closed loop failed")
    source = REPO_ROOT / "frontend" / "test-results" / "playwright-junit.xml"
    if not source.is_file():
        raise RuntimeError("Playwright JUnit output is missing")
    shutil.copy2(source, options.output / "playwright-junit.xml")
```

- [ ] **Step 4: Implement mandatory failure injection**

For `lan-shared`:

1. Stop the GPT service listener only, poll `/api/services/status` for degradation within 15 seconds, assert Index/Cosy remain ready, restart shared worker and rerun GPT core case.
2. Stop all three listener ports, assert application health survives and all services degrade, restart shared worker and rerun all five core cases into `recovery/`.

For `lan-distributed`, choose `TTS_MORE_VALIDATION_FAULT_NODE` when set or the first sorted worker, stop its assigned listener, assert two other services remain ready, restart only that worker and rerun all five core cases.

Write all measured times and booleans to `fault-recovery.json`; any false or missing field makes the process exit non-zero.

Implement the local API probes first:

```python
def _api_headers() -> dict[str, str]:
    token = os.environ.get("TTS_MORE_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def wait_http_ready(url: str, *, timeout_seconds: int) -> None:
    started = time.monotonic()
    while time.monotonic() - started <= timeout_seconds:
        try:
            if httpx.get(url, headers=_api_headers(), timeout=5.0).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError(f"HTTP endpoint did not become ready: {urlsplit(url).path}")


def application_ready() -> bool:
    try:
        response = httpx.get(
            "http://127.0.0.1:8000/api/health", headers=_api_headers(), timeout=5.0
        )
        return response.is_success
    except httpx.HTTPError:
        return False


def current_service_ready(service_id: str) -> bool:
    try:
        response = httpx.get(
            "http://127.0.0.1:8000/api/services/status",
            headers=_api_headers(),
            timeout=10.0,
        )
        response.raise_for_status()
        services = response.json()["services"]
        return any(item.get("service_id") == service_id and item.get("ready") for item in services)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return False


def wait_service_state(service_id: str, *, ready: bool, timeout_seconds: int) -> float:
    started = time.monotonic()
    while time.monotonic() - started <= timeout_seconds:
        if current_service_ready(service_id) is ready:
            return time.monotonic() - started
        time.sleep(1)
    raise TimeoutError(f"service {service_id} did not reach ready={ready}")
```

Then use this orchestration shape:

```python
def run_fault_recovery(
    options: LanRunOptions,
    policy: LanPolicy,
    manager: WindowsLanNodeManager,
    services_path: Path,
    preflight_path: Path,
    token: str,
) -> dict[str, object]:
    registry = ServiceRegistry.load(services_path)
    endpoints = {item.service_id: item for item in registry.services}
    service_ports = {
        service_id: urlsplit(endpoint.base_url).port
        for service_id, endpoint in endpoints.items()
    }
    fault_node = os.environ.get("TTS_MORE_VALIDATION_FAULT_NODE") or sorted(policy.workers)[0]
    if fault_node not in policy.workers:
        raise ValueError("TTS_MORE_VALIDATION_FAULT_NODE is not a topology worker")
    fault_services = sorted(
        service_id for service_id, owner in policy.service_owners.items() if owner == fault_node
    )
    first_service = fault_services[0]
    manager.stop_service(fault_node, int(service_ports[first_service]))
    degraded = wait_service_state(first_service, ready=False, timeout_seconds=15)
    other_ready = all(
        current_service_ready(service_id)
        for service_id in policy.service_owners
        if service_id != first_service
    )
    application_survived = application_ready()
    if not application_survived:
        raise RuntimeError("application failed during worker fault")
    manager.start(fault_node, options.remote_root)
    wait_for_services(services_path, timeout_seconds=600)

    all_service_outage = None
    if policy.mode is LanMode.SHARED:
        manager.stop_all_services(
            fault_node,
            tuple(int(service_ports[item]) for item in fault_services),
        )
        all_service_outage = all(
            wait_service_state(item, ready=False, timeout_seconds=15) <= 15
            for item in fault_services
        )
        application_survived = application_survived and application_ready()
        if not application_survived:
            raise RuntimeError("application failed during shared worker outage")
        manager.start(fault_node, options.remote_root)
        wait_for_services(services_path, timeout_seconds=600)

    run_core_cuda_validation(
        options,
        services_path,
        preflight_path,
        token,
        output_dir=options.output / "recovery",
    )
    report = {
        "fault_node": fault_node,
        "service_id": first_service,
        "degraded_within_seconds": degraded,
        "other_services_ready": other_ready,
        "all_services_degraded": all_service_outage,
        "application_survived": application_survived,
        "recovery_passed": True,
    }
    if degraded > 15 or not other_ready or report["application_survived"] is not True:
        raise RuntimeError("LAN fault recovery gate failed")
    (options.output / "fault-recovery.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
```

`current_service_ready()` and `application_ready()` use an `httpx.Client` pointed at `127.0.0.1:8000`, include `TTS_MORE_API_TOKEN` when configured, and return `False` on HTTP/JSON errors. `wait_service_state()` measures with `time.monotonic()` and never sleeps longer than one second between probes.

- [ ] **Step 5: Complete evidence collection and sanitization**

Write `distributed-evidence.json` with mode, deployment mode, commit map, topology/fixture hashes, hashed identities, service-to-node ownership and collected relative evidence paths. Do not include raw hostnames, IPs, usernames, absolute paths or UUIDs.

Add the evidence schema and writer to `lan_evidence.py`:

```python
class LanNodeEvidence(BaseModel):
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    host_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gpu_uuid_sha256: list[str]
    gpu_log: str


class LanEvidenceManifest(BaseModel):
    schema_version: Literal[1]
    mode: Literal["lan-shared", "lan-distributed"]
    deployment: Literal["clean", "release"]
    controller_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_owners: dict[str, str]
    nodes: dict[str, LanNodeEvidence]
    fault_recovery: Literal["fault-recovery.json"] = "fault-recovery.json"


def write_lan_evidence(path: Path, payload: LanEvidenceManifest) -> None:
    path.write_text(payload.model_dump_json(indent=2) + "\n", encoding="utf-8")


def assert_required_evidence(output: Path, service_owners: dict[str, str]) -> None:
    required = {
        "summary.json", "junit.xml", "controller.log", "orchestration-preflight.json",
        "fault-recovery.json", "distributed-evidence.json", "human-listening-review.md",
        "playwright-junit.xml",
    }
    missing = sorted(name for name in required if not (output / name).is_file())
    wav_files = list((output / "wav").glob("*.wav")) if (output / "wav").is_dir() else []
    if len(wav_files) < 5:
        missing.append("wav/five-core-samples")
    summary_path = output / "summary.json"
    if summary_path.is_file() and json.loads(summary_path.read_text(encoding="utf-8")).get("passed") is not True:
        missing.append("summary.json:passed")
    for node in sorted(set(service_owners.values())):
        gpu_log = output / "worker-logs" / node / "nvidia-smi.csv"
        if not gpu_log.is_file() or gpu_log.stat().st_size == 0:
            missing.append(str(gpu_log.relative_to(output)))
    for service_id, node in service_owners.items():
        worker_log = output / "worker-logs" / node / f"{service_id}.log"
        if not worker_log.is_file() or worker_log.stat().st_size == 0:
            missing.append(str(worker_log.relative_to(output)))
    if missing:
        raise RuntimeError("LAN validation evidence is incomplete: " + ", ".join(missing))
```

This requires one worker GPU CSV per policy worker and one concrete worker log per formal service.

Extend `LanOrchestrator.run()` immediately after the initial core validation:

```python
with control_plane(services_path, self.options.output):
    run_workstation_e2e(self.options, runner=self.process_runner)
    fault_report = run_fault_recovery(
        self.options, policy, manager, services_path, preflight, token
    )
for node in policy.workers:
    manager.stop_gpu_monitor(node, self.options.remote_root, self.options.output.name)
    owned_services = tuple(
        service_id for service_id, owner in policy.service_owners.items() if owner == node
    )
    manager.collect_evidence(node, self.options.remote_root, self.options.output, owned_services)
evidence_collected = True
evidence = LanEvidenceManifest(
    schema_version=1,
    mode=self.options.mode.value,
    deployment=self.options.deployment.value,
    controller_commit=commit,
    topology_sha256=hashlib.sha256(self.options.topology.read_bytes()).hexdigest(),
    fixture_sha256=hashlib.sha256(self.options.fixture.read_bytes()).hexdigest(),
    service_owners=policy.service_owners,
    nodes={
        probe.node: LanNodeEvidence(
            commit=probe.commit,
            host_key_sha256=probe.host_key_sha256,
            machine_id_sha256=probe.machine_id_sha256,
            gpu_uuid_sha256=list(probe.gpu_uuid_sha256),
            gpu_log=f"worker-logs/{probe.node}/nvidia-smi.csv",
        )
        for probe in probes
    },
)
write_lan_evidence(self.options.output / "distributed-evidence.json", evidence)
assert_required_evidence(self.options.output, policy.service_owners)
```

- [ ] **Step 6: Run fault and evidence tests**

Run: `.venv/bin/python -m pytest backend/tests/test_lan_orchestration.py backend/tests/test_lan_nodes.py -q`

Expected: all tests PASS, including cleanup after injected subprocess and SSH failures.

- [ ] **Step 7: Commit**

```bash
git add backend/app/lan_orchestration.py backend/app/lan_nodes.py backend/app/lan_evidence.py \
  backend/tests/test_lan_orchestration.py backend/tests/test_lan_nodes.py backend/tests/test_lan_evidence.py
git commit -m "feat: close LAN validation recovery and evidence loop"
```

---

### Task 7: Playwright Policy And Manual macOS GPU Workflow

**Files:**
- Modify: `frontend/e2e/cuda-workstation.spec.ts:95-115`
- Create: `.github/workflows/macos-lan-gpu-validation.yml`
- Modify: `backend/tests/test_gpu_workflow.py`

**Interfaces:**
- Consumes: Task 5 CLI.
- Produces: manual self-hosted workflow and correct UI overlap policy.

- [ ] **Step 1: Write failing static workflow and Playwright policy tests**

```python
def test_playwright_distinguishes_both_lan_modes() -> None:
    spec = _read(PLAYWRIGHT_SPEC)
    assert '"distributed", "lan-distributed"' in spec
    assert '"single-clean", "single-release", "lan-shared"' in spec


def test_macos_lan_workflow_is_manual_only() -> None:
    workflow = _read(ROOT / ".github" / "workflows" / "macos-lan-gpu-validation.yml")
    assert "workflow_dispatch:" in workflow
    assert "release:" not in workflow
    assert "[self-hosted, macOS, tts-more-lan-controller]" in workflow
    assert "scripts/run-lan-validation.sh" in workflow
    assert "--deployment" in workflow
```

- [ ] **Step 2: Run tests and verify failures**

Run: `.venv/bin/python -m pytest backend/tests/test_gpu_workflow.py -q`

Expected: FAIL because the workflow does not exist and Playwright treats only `distributed` as overlapping.

- [ ] **Step 3: Update Playwright mode policy**

```typescript
const validationMode = process.env.TTS_MORE_CUDA_VALIDATION_MODE ?? "";
const distributedModes = new Set(["distributed", "lan-distributed"]);

if (distributedModes.has(validationMode)) {
  expect(maxSimultaneouslyLoaded).toBeGreaterThanOrEqual(2);
} else {
  expect(validationMode).toBe("lan-shared");
  expect(maxSimultaneouslyLoaded).toBeLessThanOrEqual(1);
}
```

Preserve `single-clean` and `single-release` support by adding them to a `serializedModes` set alongside `lan-shared`; reject unknown values with an explicit assertion.

- [ ] **Step 4: Add the manual workflow**

Create `macos-lan-gpu-validation.yml` with inputs:

```yaml
mode: {type: choice, options: [lan-shared, lan-distributed], required: true}
deployment: {type: choice, options: [clean, release], required: true}
topology: {type: string, required: true}
fixture: {type: string, required: true}
ssh_config: {type: string, required: true}
remote_root: {type: string, required: true}
require_baseline: {type: boolean, default: false, required: true}
```

Use `runs-on: [self-hosted, macOS, tts-more-lan-controller]`, Python 3.11 and pnpm 10. Install `backend[dev]`, `faster-whisper`, frontend dependencies and Chromium, then invoke the shell wrapper with every required argument. Upload only the sanitized output directory with 30-day retention and `if: always()`.

Do not add `release` triggers or modify `stable-release-gate` in this task.

- [ ] **Step 5: Run frontend and workflow tests**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_gpu_workflow.py -q
pnpm --dir frontend test
pnpm --dir frontend build
```

Expected: Python tests PASS, 104 or more frontend tests PASS, and Vite build exits 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/e2e/cuda-workstation.spec.ts .github/workflows/macos-lan-gpu-validation.yml \
  backend/tests/test_gpu_workflow.py
git commit -m "ci: add manual macOS LAN CUDA validation"
```

---

### Task 8: Documentation, Full Regression And Hardware Handoff

**Files:**
- Modify: `docs/cuda-e2e-macos-lan.md`
- Modify: `docs/cuda-e2e-validation.md`
- Modify: `docs/cuda-e2e-acceptance-record.md`
- Modify: `docs/ci-architecture.md`
- Modify: `docs/release-governance.md`
- Modify: `docs/TODO.md`
- Modify: `README.md`

**Interfaces:**
- Documents: implemented CLI, evidence schema, current gate status and hardware certification commands.
- Does not promote the workflow to a release trigger.

- [ ] **Step 1: Update current/implemented wording**

Replace the stage-two “planned interface” block in `cuda-e2e-macos-lan.md` with the actual command:

```bash
./scripts/run-lan-validation.sh \
  --mode lan-shared \
  --deployment clean \
  --topology deployment/app/topology.macos-shared.local.json \
  --fixture data/validation/cuda-fixture.shared.local.json \
  --ssh-config ~/.ssh/config.tts-more \
  --remote-root 'C:\TTS\TTS_more' \
  --output data/validation/runs/macos-lan-shared-20260710-120000
```

Document the corresponding `lan-distributed --deployment release --require-baseline` command. Keep the status “manual workflow, not stable release authority” until real hardware records exist.

- [ ] **Step 2: Extend the acceptance record**

Add fields for controller OS/commit/hash, deployment mode, SSH config hash, node commit map, hashed machine/GPU identities, remote GPU CSV, shared/distributed policy result, Playwright overlap count and failure-injection result. Explicitly prohibit raw identifiers.

- [ ] **Step 3: Update CI and release governance**

State that the macOS workflow is `workflow_dispatch` only. Add a promotion checklist requiring one approved shared run, one approved distributed run, two reviewers, comparison with Windows four-node results, and a separate PR before adding release triggers.

- [ ] **Step 4: Run full local verification**

Run:

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m compileall -q backend scripts
pnpm --dir frontend test
pnpm --dir frontend build
pnpm --dir frontend cuda:e2e
git diff --check
```

Expected on non-CUDA macOS: backend and frontend suites PASS; build exits 0; CUDA Playwright reports exactly one skipped test unless `TTS_MORE_RUN_CUDA_E2E=1` is intentionally set against real workers; diff check is clean.

- [ ] **Step 5: Perform real shared-GPU certification**

On the configured macOS controller and one Windows CUDA host, run `lan-shared --deployment clean` without `--require-baseline`. Expected artifacts: core JSON/JUnit, five model samples, 30 Playwright items, no overlap, one GPU CSV, single-service and all-service recovery, ASR thresholds and two completed listening reviews.

After the automated listener-stop test, perform one supervised host-level outage by disconnecting the shared Windows host from the validation LAN. Confirm the macOS application remains healthy while all three services degrade, restore the LAN connection manually, and retain timestamps plus application/worker logs. This supervised check does not run inside CI and must not expose the host address in public evidence.

Record the approved warm p95 in the ignored shared fixture, then rerun `lan-shared --deployment release --require-baseline`.

- [ ] **Step 6: Perform real three-GPU certification**

Run `lan-distributed --deployment clean` without `--require-baseline` against three distinct Windows CUDA hosts. Expected artifacts: three unique hashed machine/GPU identities, at least two overlapping loaded services, three GPU CSVs, single-node 15-second degradation, unaffected services ready, recovery core rerun and two completed listening reviews.

After approval, write the distributed warm p95 baseline into the ignored fixture and rerun with `--deployment release --require-baseline`.

- [ ] **Step 7: Commit documentation after executable behavior is verified**

```bash
git add README.md docs/cuda-e2e-macos-lan.md docs/cuda-e2e-validation.md \
  docs/cuda-e2e-acceptance-record.md docs/ci-architecture.md docs/release-governance.md docs/TODO.md
git commit -m "docs: operationalize macOS LAN CUDA validation"
```

- [ ] **Step 8: Open a separate promotion change only after hardware approval**

The promotion change may add release integration only when both approved run URLs and listening records are available. It must not be folded into the implementation PR.

---

## Completion Criteria

- All eight tasks have independent passing test evidence and focused commits.
- Existing Windows CUDA tests remain green and schema-v1 distributed preflight remains accepted.
- No raw host, IP, username, absolute path, SSH key, platform UUID, MachineGuid or GPU UUID appears in committed fixtures or uploaded public evidence.
- `lan-shared` proves serialization and recovery on one remote GPU host.
- `lan-distributed` proves three-node identity, overlap and fault isolation.
- The manual macOS workflow uploads complete sanitized evidence.
- Stable release governance remains unchanged until the separate promotion review.
