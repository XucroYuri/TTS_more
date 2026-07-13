from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from app.lan_topology import FORMAL_SERVICE_IDS, LanTopology
from app.windows_ssh import WindowsSshExecutor


_SAFE_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_WINDOWS_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]*\Z")
_SAFE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_FORMAL_PORT_MODULES = {
    9880: "app.workers.gpt_sovits_worker:app",
    9881: "app.workers.indextts_worker:app",
    9882: "app.workers.cosyvoice_worker:app",
}
_MAX_JSON_BYTES = 1024 * 1024


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


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json(text: str, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(f"{label} JSON is invalid") from None


def _validate_node(node: str) -> str:
    if not isinstance(node, str) or not _SAFE_NODE.fullmatch(node) or node in {".", ".."}:
        raise ValueError("worker node name is invalid")
    return node


def _validate_commit(commit: str) -> str:
    if not isinstance(commit, str) or not _SAFE_COMMIT.fullmatch(commit):
        raise ValueError("expected commit must be a lowercase SHA-1")
    return commit


def _validate_run_id(run_id: str) -> str:
    if (
        not isinstance(run_id, str)
        or not _SAFE_RUN_ID.fullmatch(run_id)
        or run_id in {".", ".."}
    ):
        raise ValueError("validation run ID is invalid")
    return run_id


def _validate_remote_root(remote_root: str) -> PureWindowsPath:
    if not isinstance(remote_root, str) or len(remote_root) > 240:
        raise ValueError("remote root is invalid")
    if remote_root != remote_root.strip() or any(char in remote_root for char in "\r\n\x00"):
        raise ValueError("remote root is invalid")
    if not re.fullmatch(r"[A-Za-z]:[\\/][^:]+", remote_root):
        raise ValueError("remote root must be an absolute drive path")
    path = PureWindowsPath(remote_root)
    if len(path.parts) < 2:
        raise ValueError("remote root must contain a directory")
    for part in path.parts[1:]:
        if (
            part in {"", ".", ".."}
            or not _SAFE_WINDOWS_SEGMENT.fullmatch(part)
            or part[-1] in {" ", "."}
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError("remote root contains an unsafe path segment")
    return path


def _powershell_literal(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ValueError("PowerShell literal contains a control character")
    return "'" + value.replace("'", "''") + "'"


def _validate_topology_file(path: Path, node: str) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("topology must be an absolute regular file")
    try:
        raw = path.read_bytes()
    except OSError:
        raise ValueError("topology file is unreadable") from None
    if not raw or len(raw) > _MAX_JSON_BYTES:
        raise ValueError("topology JSON is invalid")
    try:
        payload = _strict_json(raw.decode("utf-8"), "topology")
    except UnicodeError:
        raise ValueError("topology JSON is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "name",
        "app_node",
        "nodes",
    }:
        raise ValueError("topology JSON is invalid")
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("topology JSON is invalid")
    node_fields = {
        "role",
        "host",
        "bind_host",
        "services",
        "resource_group",
        "capacity",
    }
    if any(not isinstance(value, dict) or set(value) != node_fields for value in nodes.values()):
        raise ValueError("topology JSON is invalid")
    try:
        topology = LanTopology.model_validate(payload)
    except Exception:
        raise ValueError("topology JSON is invalid") from None
    selected = topology.nodes.get(node)
    if selected is None or selected.role != "worker":
        raise ValueError("topology does not contain the requested worker node")
    owners = [
        service_id
        for topology_node in topology.nodes.values()
        if topology_node.role == "worker"
        for service_id in topology_node.services
    ]
    if len(owners) != len(set(owners)) or set(owners) != FORMAL_SERVICE_IDS:
        raise ValueError("topology must assign every formal service exactly once")
    return path


def _validate_probe_payload(payload: Any) -> dict[str, Any]:
    fields = {
        "commit",
        "dirty",
        "machine_id",
        "gpu_uuids",
        "cuda_runtime",
        "memory_total_mib",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("worker probe JSON is invalid")
    if (
        not isinstance(payload["commit"], str)
        or not _SAFE_COMMIT.fullmatch(payload["commit"])
        or not isinstance(payload["dirty"], str)
        or not isinstance(payload["machine_id"], str)
        or not payload["machine_id"].strip()
        or len(payload["machine_id"]) > 256
        or not isinstance(payload["gpu_uuids"], list)
        or not 1 <= len(payload["gpu_uuids"]) <= 16
        or any(
            not isinstance(value, str) or not value.strip() or len(value) > 256
            for value in payload["gpu_uuids"]
        )
        or len(set(payload["gpu_uuids"])) != len(payload["gpu_uuids"])
        or not isinstance(payload["cuda_runtime"], str)
        or isinstance(payload["memory_total_mib"], bool)
        or not isinstance(payload["memory_total_mib"], int)
    ):
        raise ValueError("worker probe JSON is invalid")
    return payload


class WindowsLanNodeManager:
    def __init__(self, executor: WindowsSshExecutor, *, salt: bytes) -> None:
        if not isinstance(salt, bytes) or not salt:
            raise ValueError("identity hash salt must be nonempty bytes")
        self.executor = executor
        self.salt = salt

    def inspect(self, node: str, remote_root: str, expected_commit: str) -> NodeProbe:
        node = _validate_node(node)
        root = str(_validate_remote_root(remote_root))
        expected_commit = _validate_commit(expected_commit)
        script = f"""
$ErrorActionPreference = 'Stop'
$root = {_powershell_literal(root)}
$commit = (& git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {{ throw 'Remote commit inspection failed' }}
$dirty = (& git -C $root status --porcelain --untracked-files=all) -join "`n"
if ($LASTEXITCODE -ne 0) {{ throw 'Remote status inspection failed' }}
$machine = (Get-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\Microsoft\\Cryptography').MachineGuid
$gpu = @(& nvidia-smi.exe --query-gpu=uuid --format=csv,noheader)
if ($LASTEXITCODE -ne 0) {{ throw 'GPU identity inspection failed' }}
$header = (& nvidia-smi.exe | Select-Object -First 3) -join ' '
if ($LASTEXITCODE -ne 0) {{ throw 'CUDA runtime inspection failed' }}
$cudaMatch = [regex]::Match($header, 'CUDA Version:\\s*([0-9.]+)')
$memory = [int](@(& nvidia-smi.exe --query-gpu=memory.total --format=csv,noheader,nounits)[0])
if ($LASTEXITCODE -ne 0) {{ throw 'GPU memory inspection failed' }}
[ordered]@{{
  commit=$commit
  dirty=$dirty
  machine_id=[string]$machine
  gpu_uuids=@($gpu | ForEach-Object {{ ([string]$_).Trim() }})
  cuda_runtime=$cudaMatch.Groups[1].Value
  memory_total_mib=$memory
}} | ConvertTo-Json -Compress
"""
        result = self.executor.run_powershell(node, script)
        payload = _validate_probe_payload(_strict_json(result.stdout, "worker probe"))
        if payload["commit"] != expected_commit or payload["dirty"]:
            raise ValueError(f"worker {node} checkout identity mismatch")
        if payload["cuda_runtime"] != "12.8" or payload["memory_total_mib"] < 16000:
            raise ValueError(f"worker {node} does not meet CUDA requirements")
        host_key = self.executor.pinned_host_key_sha256(node)
        if not isinstance(host_key, str) or not _SAFE_SHA256.fullmatch(host_key):
            raise ValueError("pinned host key identity is invalid")
        return NodeProbe(
            node=node,
            commit=payload["commit"],
            host_key_sha256=host_key,
            machine_id_sha256=_hash_identity(payload["machine_id"], self.salt),
            gpu_uuid_sha256=tuple(
                _hash_identity(value, self.salt) for value in payload["gpu_uuids"]
            ),
            cuda_runtime=payload["cuda_runtime"],
            memory_total_mib=payload["memory_total_mib"],
        )

    def sync_checkout(self, node: str, remote_root: str, expected_commit: str) -> None:
        node = _validate_node(node)
        root = _powershell_literal(str(_validate_remote_root(remote_root)))
        commit = _powershell_literal(_validate_commit(expected_commit))
        script = f"""
$ErrorActionPreference = 'Stop'
$dirty = (& git -C {root} status --porcelain --untracked-files=all) -join "`n"
if ($LASTEXITCODE -ne 0 -or $dirty) {{ throw 'Remote checkout is dirty before sync' }}
& git -C {root} fetch origin {commit}
if ($LASTEXITCODE -ne 0) {{ throw 'Remote fetch failed' }}
& git -C {root} checkout --detach {commit}
if ($LASTEXITCODE -ne 0) {{ throw 'Remote checkout failed' }}
$actual = (& git -C {root} rev-parse HEAD).Trim()
$dirty = (& git -C {root} status --porcelain --untracked-files=all) -join "`n"
if ($LASTEXITCODE -ne 0 -or $actual -ne {commit} -or $dirty) {{
  throw 'Remote checkout identity mismatch after sync'
}}
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
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        topology = _validate_topology_file(topology, node)
        if not isinstance(clean, bool):
            raise ValueError("clean must be a boolean")
        remote_topology = root / "data/local/topology.validation.json"
        remote_repo_paths = root / "deployment/app/repo-paths.local.json"
        remote_repo_lock = root / "repo.lock.json"
        deploy_script = root / "scripts/deploy-local-tts.ps1"
        remote_python = root / ".venv/Scripts/python.exe"
        expected_hash = hashlib.sha256(topology.read_bytes()).hexdigest()
        self.executor.run_powershell(
            node,
            "New-Item -ItemType Directory -Force -Path "
            f"{_powershell_literal(str(remote_topology.parent))} | Out-Null",
        )
        self.executor.copy_to(node, topology, remote_topology.as_posix())
        confirmation_validator = r"""
import json, pathlib, sys
def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
def load(path):
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=pairs)
root = pathlib.Path(sys.argv[1]).resolve()
lock = load(sys.argv[2])
confirmation = load(sys.argv[3])
if not isinstance(lock, dict) or not isinstance(lock.get("repositories"), list):
    raise ValueError("invalid lock")
keys = [item.get("service_id") for item in lock["repositories"] if isinstance(item, dict)]
if any(not isinstance(key, str) or not key for key in keys) or len(keys) != len(set(keys)):
    raise ValueError("invalid lock identities")
if not isinstance(confirmation, dict) or set(confirmation) != {"repositories"}:
    raise ValueError("invalid confirmation")
repositories = confirmation["repositories"]
if not isinstance(repositories, dict) or set(repositories) != set(keys):
    raise ValueError("incomplete confirmation")
for value in repositories.values():
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid confirmed path")
    candidate = pathlib.Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate.resolve(strict=False).relative_to(root)
""".strip()
        clean_flag = " -CleanRepos" if clean else ""
        script = f"""
$ErrorActionPreference = 'Stop'
$repoPaths = {_powershell_literal(str(remote_repo_paths))}
$repoLock = {_powershell_literal(str(remote_repo_lock))}
if (!(Test-Path -LiteralPath $repoPaths -PathType Leaf)) {{
  throw 'repo-paths.local.json is not a complete confirmation file'
}}
if (!(Test-Path -LiteralPath $repoLock -PathType Leaf)) {{ throw 'repo.lock.json is missing' }}
$python = if (Test-Path -LiteralPath {_powershell_literal(str(remote_python))} -PathType Leaf) {{
  {_powershell_literal(str(remote_python))}
}} else {{ 'python' }}
$confirmationValidator = @'
{confirmation_validator}
'@
& $python -c $confirmationValidator {_powershell_literal(str(root))} $repoLock $repoPaths
if ($LASTEXITCODE -ne 0) {{
  throw 'repo-paths.local.json is not a complete confirmation file'
}}
$hash = (Get-FileHash -LiteralPath {_powershell_literal(str(remote_topology))} -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hash -ne {_powershell_literal(expected_hash)}) {{ throw 'Remote topology hash mismatch' }}
& {_powershell_literal(str(deploy_script))} -Profile 'worker-node' -Device 'CU128' -Targets 'default' -Topology {_powershell_literal(str(remote_topology))} -Node {_powershell_literal(node)} -RepoPaths $repoPaths{clean_flag}
if ($LASTEXITCODE -ne 0) {{ throw 'Remote deployment failed' }}
"""
        self.executor.run_powershell(node, script, timeout=6 * 60 * 60)

    def start(self, node: str, remote_root: str) -> None:
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        topology = root / "data/local/topology.validation.json"
        repo_paths = root / "deployment/app/repo-paths.local.json"
        pid_manifest = root / "data/validation/lan-controller/service-processes.json"
        start_script = root / "scripts/start-service-workers.ps1"
        remote_python = root / ".venv/Scripts/python.exe"
        manifest_validator = r"""
import json, pathlib, sys
def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
root = pathlib.Path(sys.argv[1]).resolve()
with open(sys.argv[2], "r", encoding="utf-8") as stream:
    payload = json.load(stream, object_pairs_hook=pairs)
if not isinstance(payload, dict) or set(payload) != {"schema_version", "processes"}:
    raise ValueError("invalid manifest")
if payload["schema_version"] != 1 or not isinstance(payload["processes"], list):
    raise ValueError("invalid manifest")
modules = {
    "local-gpt-sovits-main": "app.workers.gpt_sovits_worker:app",
    "local-indextts": "app.workers.indextts_worker:app",
    "local-cosyvoice": "app.workers.cosyvoice_worker:app",
}
fields = {"pid", "creation_date", "executable_path", "project_root", "worker_module", "service_id"}
for process in payload["processes"]:
    if not isinstance(process, dict) or set(process) != fields:
        raise ValueError("invalid process")
    service_id = process["service_id"]
    pid = process["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ValueError("invalid pid")
    if service_id not in modules or process["worker_module"] != modules[service_id]:
        raise ValueError("invalid worker identity")
    if not isinstance(process["creation_date"], str) or not process["creation_date"].strip():
        raise ValueError("invalid creation date")
    if pathlib.Path(process["project_root"]).resolve() != root:
        raise ValueError("invalid project root")
    pathlib.Path(process["executable_path"]).resolve().relative_to(root)
""".strip()
        script = f"""
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path {_powershell_literal(str(pid_manifest.parent))} | Out-Null
if (Test-Path -LiteralPath {_powershell_literal(str(pid_manifest))} -PathType Leaf) {{
  $python = if (Test-Path -LiteralPath {_powershell_literal(str(remote_python))} -PathType Leaf) {{
    {_powershell_literal(str(remote_python))}
  }} else {{ 'python' }}
  $manifestValidator = @'
{manifest_validator}
'@
  & $python -c $manifestValidator {_powershell_literal(str(root))} {_powershell_literal(str(pid_manifest))}
  if ($LASTEXITCODE -ne 0) {{ throw 'Existing validation service PID manifest is invalid' }}
}}
& {_powershell_literal(str(start_script))} -Topology {_powershell_literal(str(topology))} -Node {_powershell_literal(node)} -RepoPaths {_powershell_literal(str(repo_paths))} -PidManifest {_powershell_literal(str(pid_manifest))} -Detach
if ($LASTEXITCODE -ne 0) {{ throw 'Remote start failed' }}
if (!(Test-Path -LiteralPath {_powershell_literal(str(pid_manifest))} -PathType Leaf)) {{
  throw 'Remote start did not create the owned PID manifest'
}}
"""
        self.executor.run_powershell(node, script, timeout=1800)

    def _gpu_monitor_script(self, csv_path: PureWindowsPath) -> str:
        salt = base64.b64encode(self.salt).decode("ascii")
        return f"""
$ErrorActionPreference = 'Stop'
$salt = [Convert]::FromBase64String({_powershell_literal(salt)})
$sha = [Security.Cryptography.SHA256]::Create()
try {{
  while ($true) {{
    $rows = @(& nvidia-smi.exe --query-gpu=timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu --format=csv,noheader,nounits)
    if ($LASTEXITCODE -ne 0) {{ throw 'nvidia-smi query failed' }}
    foreach ($row in $rows) {{
      $parts = @($row -split ',\\s*', 7)
      if ($parts.Count -ne 7) {{ throw 'nvidia-smi row is malformed' }}
      $identity = [Text.Encoding]::UTF8.GetBytes([string]$parts[2])
      $digestInput = [byte[]]::new($salt.Length + 1 + $identity.Length)
      [Array]::Copy($salt, 0, $digestInput, 0, $salt.Length)
      [Array]::Copy($identity, 0, $digestInput, $salt.Length + 1, $identity.Length)
      $gpu_uuid_sha256 = ([BitConverter]::ToString($sha.ComputeHash($digestInput))).Replace('-', '').ToLowerInvariant()
      $parts[2] = $gpu_uuid_sha256
      Add-Content -LiteralPath {_powershell_literal(str(csv_path))} -Value ($parts -join ',') -Encoding UTF8
    }}
    Start-Sleep -Milliseconds 2000
  }}
}} finally {{
  $sha.Dispose()
}}
""".strip()

    def start_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        run_id = _validate_run_id(run_id)
        evidence = root / "data/validation/lan-controller" / run_id
        csv_path = evidence / "nvidia-smi.csv"
        stderr_path = evidence / "nvidia-smi.stderr.log"
        manifest_path = evidence / "nvidia-smi.process.json"
        encoded = base64.b64encode(
            self._gpu_monitor_script(csv_path).encode("utf-16-le")
        ).decode("ascii")
        command_sha256 = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        script = f"""
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path {_powershell_literal(str(evidence))} | Out-Null
if (Test-Path -LiteralPath {_powershell_literal(str(manifest_path))}) {{
  throw 'GPU monitor PID manifest already exists'
}}
if (Test-Path -LiteralPath {_powershell_literal(str(csv_path))}) {{
  throw 'GPU monitor evidence already exists'
}}
$process = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',{_powershell_literal(encoded)}) -RedirectStandardError {_powershell_literal(str(stderr_path))} -PassThru
$identity = $null
for ($attempt = 0; $attempt -lt 20 -and $null -eq $identity; $attempt++) {{
  $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)" -ErrorAction SilentlyContinue
  if ($null -eq $identity) {{ Start-Sleep -Milliseconds 50 }}
}}
if ($null -eq $identity -or [IO.Path]::GetFileName($identity.ExecutablePath) -ine 'powershell.exe') {{
  Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
  throw 'GPU monitor process identity is unavailable'
}}
$manifest = [ordered]@{{
  schema_version = 1
  pid = [int]$process.Id
  creation_date = [string]$identity.CreationDate
  executable_name = 'powershell.exe'
  command_sha256 = {_powershell_literal(command_sha256)}
}}
$manifest | ConvertTo-Json | Set-Content -LiteralPath {_powershell_literal(str(manifest_path))} -Encoding UTF8
"""
        self.executor.run_powershell(node, script)

    def stop_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        run_id = _validate_run_id(run_id)
        evidence = root / "data/validation/lan-controller" / run_id
        manifest_path = evidence / "nvidia-smi.process.json"
        encoded = base64.b64encode(
            self._gpu_monitor_script(evidence / "nvidia-smi.csv").encode("utf-16-le")
        ).decode("ascii")
        command_sha256 = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        remote_python = root / ".venv/Scripts/python.exe"
        manifest_validator = r"""
import json, sys
def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
with open(sys.argv[1], "r", encoding="utf-8-sig") as stream:
    payload = json.load(stream, object_pairs_hook=pairs)
fields = {"schema_version", "pid", "creation_date", "executable_name", "command_sha256"}
if not isinstance(payload, dict) or set(payload) != fields:
    raise ValueError("invalid manifest")
pid = payload["pid"]
if payload["schema_version"] != 1 or isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
    raise ValueError("invalid manifest")
if payload["executable_name"] != "powershell.exe" or payload["command_sha256"] != sys.argv[2]:
    raise ValueError("invalid process identity")
if not isinstance(payload["creation_date"], str) or not payload["creation_date"].strip():
    raise ValueError("invalid creation date")
print(json.dumps(payload, separators=(",", ":")))
""".strip()
        script = f"""
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath {_powershell_literal(str(manifest_path))} -PathType Leaf)) {{ return }}
$python = if (Test-Path -LiteralPath {_powershell_literal(str(remote_python))} -PathType Leaf) {{
  {_powershell_literal(str(remote_python))}
}} else {{ 'python' }}
$manifestValidator = @'
{manifest_validator}
'@
$validatedManifest = @(& $python -c $manifestValidator {_powershell_literal(str(manifest_path))} {_powershell_literal(command_sha256)}) -join "`n"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($validatedManifest)) {{
  throw 'GPU monitor PID manifest is invalid'
}}
try {{ $manifest = $validatedManifest | ConvertFrom-Json }} catch {{
  throw 'GPU monitor PID manifest is invalid'
}}
$fields = @($manifest.PSObject.Properties.Name)
$expectedFields = @('schema_version','pid','creation_date','executable_name','command_sha256')
if ($fields.Count -ne $expectedFields.Count -or @($fields | Where-Object {{ $_ -notin $expectedFields }}).Count -ne 0) {{
  throw 'GPU monitor PID manifest is invalid'
}}
$monitorPid = $manifest.pid -as [int]
if ($manifest.schema_version -ne 1 -or $null -eq $monitorPid -or $monitorPid -lt 1 -or
    $manifest.executable_name -cne 'powershell.exe' -or
    $manifest.command_sha256 -cne {_powershell_literal(command_sha256)} -or
    [string]::IsNullOrWhiteSpace([string]$manifest.creation_date)) {{
  throw 'GPU monitor PID manifest is invalid'
}}
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $monitorPid" -ErrorAction SilentlyContinue
if ($null -ne $process) {{
  if ([string]$process.CreationDate -cne [string]$manifest.creation_date -or
      [IO.Path]::GetFileName([string]$process.ExecutablePath) -ine 'powershell.exe' -or
      [string]$process.CommandLine -notlike '*{encoded}*') {{
    throw 'GPU monitor process identity mismatch'
  }}
  Stop-Process -Id $monitorPid -Force -ErrorAction Stop
}}
Remove-Item -LiteralPath {_powershell_literal(str(manifest_path))} -Force
"""
        self.executor.run_powershell(node, script)

    def stop_service(self, node: str, port: int) -> None:
        node = _validate_node(node)
        if isinstance(port, bool) or not isinstance(port, int) or port not in _FORMAL_PORT_MODULES:
            raise ValueError("fault injection port is not a formal worker port")
        module = _FORMAL_PORT_MODULES[port]
        port_pattern = rf"(?:^|\s)--port(?:\s+|=){port}(?:\s|$)"
        script = f"""
$ErrorActionPreference = 'Stop'
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction Stop)
if ($listeners.Count -eq 0) {{ throw 'No listener on validation port' }}
$processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($processId in $processIds) {{
  $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
  if ([string]$process.CommandLine -notlike '*{module}*' -or
      -not [regex]::IsMatch([string]$process.CommandLine, {_powershell_literal(port_pattern)})) {{
    throw 'Worker process identity mismatch'
  }}
}}
foreach ($processId in $processIds) {{ Stop-Process -Id $processId -Force -ErrorAction Stop }}
"""
        self.executor.run_powershell(node, script)

    def stop_all_services(self, node: str, ports: tuple[int, ...]) -> None:
        node = _validate_node(node)
        if (
            not isinstance(ports, tuple)
            or not ports
            or len(ports) != len(set(ports))
            or any(
                isinstance(port, bool)
                or not isinstance(port, int)
                or port not in _FORMAL_PORT_MODULES
                for port in ports
            )
        ):
            raise ValueError("service ports must be unique formal worker ports")
        for port in ports:
            self.stop_service(node, port)

    def collect_evidence(
        self,
        node: str,
        remote_root: str,
        output: Path,
        service_ids: tuple[str, ...],
    ) -> None:
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        output = Path(output)
        if not output.is_absolute() or output.is_symlink() or not output.is_dir():
            raise ValueError("evidence output must be an absolute directory")
        run_id = _validate_run_id(output.name)
        if (
            not isinstance(service_ids, tuple)
            or not service_ids
            or len(service_ids) != len(set(service_ids))
            or any(service_id not in FORMAL_SERVICE_IDS for service_id in service_ids)
        ):
            raise ValueError("worker log service is not a unique formal service ID")
        remote = root / "data/validation/lan-controller" / run_id
        destination = output / "worker-logs" / node
        sources = [
            (remote / "nvidia-smi.csv", destination / "nvidia-smi.csv"),
            (remote / "nvidia-smi.stderr.log", destination / "nvidia-smi.stderr.log"),
            *(
                (
                    root / "data/.runtime/logs" / f"{service_id}.log",
                    destination / f"{service_id}.log",
                )
                for service_id in service_ids
            ),
        ]
        for remote_path, local_path in sources:
            self.executor.copy_from(node, remote_path.as_posix(), local_path)
