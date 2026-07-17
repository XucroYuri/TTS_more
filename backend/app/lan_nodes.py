from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from app.lan_topology import FORMAL_SERVICE_IDS, LanTopology
from app.windows_ssh import WindowsSshExecutor


_SAFE_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SAFE_RUN_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
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
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_EVIDENCE_FILE_BYTES = 64 * 1024 * 1024
_MONITOR_MAX_SECONDS = 6 * 60 * 60
_MONITOR_MAX_ROWS = 200_000
_MONITOR_MAX_BYTES = 64 * 1024 * 1024
_USE_DIR_FD_EVIDENCE = os.name != "nt"


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
        or run_id[-1] in {" ", "."}
        or run_id.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("validation run ID is invalid")
    return run_id


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("evidence path component is unavailable") from None
        if _is_link_or_reparse(metadata):
            raise ValueError("evidence path contains a symlink component")


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _contained_path(root: Path, candidate: Path) -> Path:
    try:
        canonical = candidate.resolve(strict=False)
        canonical.relative_to(root)
    except (OSError, ValueError):
        raise ValueError("evidence destination containment check failed") from None
    return canonical


def _secure_directory(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            _reject_symlink_components(current)
            try:
                metadata = current.lstat()
            except OSError:
                raise ValueError("evidence directory is unavailable") from None
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("evidence path component is not a directory")
        else:
            try:
                current.mkdir(mode=0o700)
            except OSError:
                raise ValueError("evidence directory could not be created") from None
        _contained_path(root, current)
    return current


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
        self._service_manifests: dict[str, list[PureWindowsPath]] = {}
        self._service_roots: dict[str, PureWindowsPath] = {}
        self._start_generations: dict[str, int] = {}
        self._pending_service_starts: dict[str, PureWindowsPath] = {}

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
        self._start_services(node, remote_root, None)

    def restart_services(
        self,
        node: str,
        remote_root: str,
        service_ids: tuple[str, ...],
    ) -> None:
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        if self._service_roots.get(node) != root:
            raise ValueError("worker node has no prior manager-owned service start")
        if (
            not isinstance(service_ids, tuple)
            or not service_ids
            or len(service_ids) != len(set(service_ids))
            or any(service_id not in FORMAL_SERVICE_IDS for service_id in service_ids)
        ):
            raise ValueError("restart services must be unique formal service IDs")
        self._start_services(node, remote_root, service_ids)

    def _start_services(
        self,
        node: str,
        remote_root: str,
        service_ids: tuple[str, ...] | None,
    ) -> None:
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        topology = root / "data/local/topology.validation.json"
        repo_paths = root / "deployment/app/repo-paths.local.json"
        generation = self._start_generations.get(node, 0)
        manifest_token = hashlib.sha256(
            self.salt
            + b"\0service-manifest\0"
            + node.encode("ascii")
            + b"\0"
            + str(root).casefold().encode("utf-8")
            + b"\0"
            + str(generation).encode("ascii")
        ).hexdigest()[:16]
        pid_manifest = self._pending_service_starts.get(node) or (
            root
            / "data/validation/lan-controller"
            / f"service-processes-{manifest_token}.json"
        )
        start_script = root / "scripts/start-service-workers.ps1"
        remote_python = root / ".venv/Scripts/python.exe"
        selected_services = "*" if service_ids is None else ",".join(service_ids)
        services_parameter = (
            "" if service_ids is None else f" -Services {_powershell_literal(selected_services)}"
        )
        manifest_validator = r"""
import hashlib, json, os, pathlib, stat, sys
MAX_BYTES = 1024 * 1024
def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
if len(sys.argv) != 6:
    raise ValueError("topology-bound manifest validation required")
root = pathlib.Path(sys.argv[1]).resolve()
path = pathlib.Path(sys.argv[2])
metadata = os.lstat(path)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1 or metadata.st_size > MAX_BYTES:
    raise ValueError("invalid manifest file")
with open(path, "rb") as stream:
    raw = stream.read(MAX_BYTES + 1)
if len(raw) < 1 or len(raw) > MAX_BYTES:
    raise ValueError("invalid manifest size")
payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
if not isinstance(payload, dict) or set(payload) != {"schema_version", "processes"}:
    raise ValueError("invalid manifest")
if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
    raise ValueError("invalid schema version")
if not isinstance(payload["processes"], list) or not payload["processes"]:
    raise ValueError("invalid manifest")
modules = {
    "local-gpt-sovits-main": "app.workers.gpt_sovits_worker:app",
    "local-indextts": "app.workers.indextts_worker:app",
    "local-cosyvoice": "app.workers.cosyvoice_worker:app",
}
fields = {"pid", "creation_date", "executable_path", "project_root", "worker_module", "service_id"}
seen = set()
for process in payload["processes"]:
    if not isinstance(process, dict) or set(process) != fields:
        raise ValueError("invalid process")
    service_id = process["service_id"]
    pid = process["pid"]
    if type(pid) is not int or pid < 1:
        raise ValueError("invalid pid")
    if not isinstance(service_id, str) or service_id not in modules or service_id in seen:
        raise ValueError("invalid service identity")
    seen.add(service_id)
    if type(process["worker_module"]) is not str or process["worker_module"] != modules[service_id]:
        raise ValueError("invalid worker identity")
    if type(process["creation_date"]) is not str or not process["creation_date"].strip():
        raise ValueError("invalid creation date")
    if type(process["project_root"]) is not str or pathlib.Path(process["project_root"]).resolve() != root:
        raise ValueError("invalid project root")
    if type(process["executable_path"]) is not str:
        raise ValueError("invalid executable path")
    pathlib.Path(process["executable_path"]).resolve().relative_to(root)
topology_path = pathlib.Path(sys.argv[3])
topology_raw = topology_path.read_bytes()
if len(topology_raw) < 1 or len(topology_raw) > MAX_BYTES:
    raise ValueError("invalid topology size")
topology = json.loads(topology_raw.decode("utf-8"), object_pairs_hook=pairs)
if not isinstance(topology, dict) or set(topology) != {"schema_version", "name", "app_node", "nodes"}:
    raise ValueError("invalid topology")
nodes = topology["nodes"]
if not isinstance(nodes, dict) or sys.argv[4] not in nodes:
    raise ValueError("invalid topology node")
selected = nodes[sys.argv[4]]
if not isinstance(selected, dict) or selected.get("role") != "worker":
    raise ValueError("invalid topology worker")
expected_services = selected.get("services")
if not isinstance(expected_services, list) or any(type(item) is not str for item in expected_services):
    raise ValueError("invalid topology services")
topology_services = set(expected_services)
if not topology_services or len(topology_services) != len(expected_services) or not topology_services.issubset(modules):
    raise ValueError("invalid topology services")
if sys.argv[5] == "*":
    requested_services = expected_services
else:
    requested_services = sys.argv[5].split(",")
if (not requested_services or len(requested_services) != len(set(requested_services)) or
        any(item not in topology_services for item in requested_services)):
    raise ValueError("invalid selected services")
expected = set(requested_services)
if not seen.issubset(expected):
    raise ValueError("manifest service identities do not match topology")
result = {
    "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
    "processes": payload["processes"],
    "complete": seen == expected,
    "expected_service_count": len(expected),
}
print(json.dumps(result, separators=(",", ":")))
""".strip()
        script = f"""
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path {_powershell_literal(str(pid_manifest.parent))} | Out-Null
$python = if (Test-Path -LiteralPath {_powershell_literal(str(remote_python))} -PathType Leaf) {{
  {_powershell_literal(str(remote_python))}
}} else {{ 'python' }}
$manifestValidator = @'
{manifest_validator}
'@
function Test-ExactCommandToken {{
  param([string]$CommandLine, [string]$Token)
  if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($Token)) {{ return $false }}
  $escaped = [regex]::Escape($Token)
  return [regex]::IsMatch($CommandLine, '(?:^|\\s)(?:"' + $escaped + '"|' + $escaped + ')(?=\\s|$)', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
}}
function Test-ExactPortTokens {{
  param([string]$CommandLine, [int]$Port)
  if ([string]::IsNullOrWhiteSpace($CommandLine)) {{ return $false }}
  $escapedPort = [regex]::Escape([string]$Port)
  $pattern = '(?:^|\\s)(?:"--port"|--port)\\s+(?:"' + $escapedPort + '"|' + $escapedPort + ')(?=\\s|$)'
  return [regex]::IsMatch($CommandLine, $pattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)
}}
function Get-ServicePort {{
  param([string]$ServiceId)
  return @{{
    'local-gpt-sovits-main' = 9880
    'local-indextts' = 9881
    'local-cosyvoice' = 9882
  }}[$ServiceId]
}}
function Test-OwnedServiceSnapshot {{
  param($Entry, $Snapshot, [int]$Port)
  if ($null -eq $Snapshot) {{ return $false }}
  $actualExecutable = ''
  try {{ $actualExecutable = [IO.Path]::GetFullPath([string]$Snapshot.ExecutablePath) }} catch {{ return $false }}
  return (
    ([string]$Snapshot.CreationDate).Equals([string]$Entry.creation_date, [StringComparison]::Ordinal) -and
    $actualExecutable.Equals([IO.Path]::GetFullPath([string]$Entry.executable_path), [StringComparison]::OrdinalIgnoreCase) -and
    ([IO.Path]::GetFullPath([string]$Entry.project_root)).Equals({_powershell_literal(str(root))}, [StringComparison]::OrdinalIgnoreCase) -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) ([string]$Entry.worker_module)) -and
    (Test-ExactPortTokens ([string]$Snapshot.CommandLine) $Port)
  )
}}
function Test-OwnedServiceListener {{
  param($Entry, [int]$Port)
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
  $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
  return $owners.Count -eq 1 -and [int]$owners[0] -eq [int]$Entry.pid
}}
function Get-StrictServiceSnapshot {{
  $raw = @(& $python -c $manifestValidator {_powershell_literal(str(root))} {_powershell_literal(str(pid_manifest))} {_powershell_literal(str(topology))} {_powershell_literal(node)} {_powershell_literal(selected_services)}) -join "`n"
  if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($raw)) {{
    throw 'strict service manifest rejected; ownership manifest retained'
  }}
  try {{ return $raw | ConvertFrom-Json }} catch {{
    throw 'strict service manifest rejected; ownership manifest retained'
  }}
}}
function Get-ReconciledServiceProcessSet {{
  param($Snapshot)
  $live = 0
  $missing = 0
  foreach ($entry in @($Snapshot.processes)) {{
    $port = Get-ServicePort ([string]$entry.service_id)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$entry.pid)" -ErrorAction SilentlyContinue
    if ($null -eq $process) {{ $missing++; continue }}
    if (-not (Test-OwnedServiceSnapshot $entry $process $port) -or
        -not (Test-OwnedServiceListener $entry $port)) {{
      throw 'service retry reconciliation found ownership mismatch'
    }}
    $live++
  }}
  return [pscustomobject]@{{ Live = $live; Missing = $missing; Total = @($Snapshot.processes).Count }}
}}
function Invoke-OwnedServiceRollback {{
  param($Snapshot)
  foreach ($entry in @($Snapshot.processes)) {{
    $processId = [int]$entry.pid
    $port = Get-ServicePort ([string]$entry.service_id)
    $first = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $first) {{ continue }}
    if (-not (Test-OwnedServiceSnapshot $entry $first $port) -or
        -not (Test-OwnedServiceListener $entry $port)) {{
      throw 'rollback termination failed; ownership manifest retained'
    }}
    $ownedProcess = Get-Process -Id $processId -ErrorAction Stop
    $null = $ownedProcess.Handle
    $second = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (-not (Test-OwnedServiceSnapshot $entry $second $port) -or
        -not (Test-OwnedServiceListener $entry $port)) {{
      throw 'rollback termination failed; ownership manifest retained'
    }}
    try {{
      $ownedProcess.Kill()
      if (-not $ownedProcess.WaitForExit(10000)) {{
        throw 'rollback process did not exit'
      }}
    }} catch {{ throw 'rollback termination failed; ownership manifest retained' }}
  }}
}}
$validatedSnapshot = $null
if (Test-Path -LiteralPath {_powershell_literal(str(pid_manifest))} -PathType Leaf) {{
  $validatedSnapshot = Get-StrictServiceSnapshot
  $state = Get-ReconciledServiceProcessSet $validatedSnapshot
  if ([bool]$validatedSnapshot.complete -and $state.Live -eq $state.Total -and
      $state.Total -eq [int]$validatedSnapshot.expected_service_count) {{
    Write-Output 'Reconcile-ExactServiceProcessSet: existing process set is owned and live'
    return
  }}
  try {{
    Invoke-OwnedServiceRollback $validatedSnapshot
  }} catch {{
    throw 'rollback termination failed; ownership manifest retained'
  }}
  Remove-Item -LiteralPath {_powershell_literal(str(pid_manifest))} -Force
  $validatedSnapshot = $null
}}
$startFailure = $null
try {{
  & {_powershell_literal(str(start_script))} -Topology {_powershell_literal(str(topology))} -Node {_powershell_literal(node)} -RepoPaths {_powershell_literal(str(repo_paths))} -PidManifest {_powershell_literal(str(pid_manifest))}{services_parameter} -Detach
  if ($LASTEXITCODE -ne 0) {{ throw 'Remote start failed' }}
}} catch {{ $startFailure = $_ }}
if (!(Test-Path -LiteralPath {_powershell_literal(str(pid_manifest))} -PathType Leaf)) {{
  if ($null -ne $startFailure) {{ throw $startFailure }}
  throw 'Remote start did not create the owned PID manifest'
}}
$validatedSnapshot = Get-StrictServiceSnapshot
if ($null -ne $startFailure) {{
  try {{
    Invoke-OwnedServiceRollback $validatedSnapshot
  }} catch {{
    throw 'rollback termination failed; ownership manifest retained'
  }}
  Remove-Item -LiteralPath {_powershell_literal(str(pid_manifest))} -Force
  throw $startFailure
}}
$startedState = Get-ReconciledServiceProcessSet $validatedSnapshot
if (-not [bool]$validatedSnapshot.complete -or $startedState.Live -ne $startedState.Total -or
    $startedState.Total -ne [int]$validatedSnapshot.expected_service_count) {{
  try {{
    Invoke-OwnedServiceRollback $validatedSnapshot
  }} catch {{
    throw 'rollback termination failed; ownership manifest retained'
  }}
  Remove-Item -LiteralPath {_powershell_literal(str(pid_manifest))} -Force
  throw 'Generated validation service process set is incomplete; rollback completed; retry required'
}}
"""
        try:
            self.executor.run_powershell(node, script, timeout=1800)
        except Exception:
            self._service_roots[node] = root
            self._pending_service_starts[node] = pid_manifest
            raise
        self._pending_service_starts.pop(node, None)
        self._service_roots[node] = root
        manifests = self._service_manifests.setdefault(node, [])
        if pid_manifest not in manifests:
            manifests.append(pid_manifest)
        self._start_generations[node] = generation + 1

    def _gpu_monitor_script(self, csv_path: PureWindowsPath) -> str:
        salt = base64.b64encode(self.salt).decode("ascii")
        return f"""
$ErrorActionPreference = 'Stop'
$salt = [Convert]::FromBase64String({_powershell_literal(salt)})
$sha = [Security.Cryptography.SHA256]::Create()
$maxSeconds = {_MONITOR_MAX_SECONDS}
$maxRows = {_MONITOR_MAX_ROWS}
$maxBytes = {_MONITOR_MAX_BYTES}
$rowCount = 0
$byteCount = 0
$deadline = [DateTime]::UtcNow.AddSeconds($maxSeconds)
$clock = [Diagnostics.Stopwatch]::StartNew()
$stream = [IO.FileStream]::new(
  {_powershell_literal(str(csv_path))},
  [IO.FileMode]::CreateNew,
  [IO.FileAccess]::Write,
  [IO.FileShare]::Read
)
$writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
try {{
  :monitor while ($clock.Elapsed.TotalSeconds -lt $maxSeconds -and [DateTime]::UtcNow -lt $deadline) {{
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
      $line = $parts -join ','
      $lineBytes = [Text.Encoding]::UTF8.GetByteCount($line + [Environment]::NewLine)
      if ($rowCount + 1 -gt $maxRows -or $byteCount + $lineBytes -gt $maxBytes) {{
        break monitor
      }}
      $writer.WriteLine($line)
      $writer.Flush()
      $rowCount++
      $byteCount += $lineBytes
    }}
    Start-Sleep -Milliseconds 2000
  }}
}} finally {{
  $writer.Dispose()
  $stream.Dispose()
  $clock.Stop()
  $sha.Dispose()
}}
""".strip()

    @staticmethod
    def _monitor_manifest_validator() -> str:
        return r"""
import hashlib, json, os, pathlib, stat, sys
MAX_BYTES = 1024 * 1024
def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
path = pathlib.Path(sys.argv[1])
metadata = os.lstat(path)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1 or metadata.st_size > MAX_BYTES:
    raise ValueError("invalid manifest file")
with open(path, "rb") as stream:
    raw = stream.read(MAX_BYTES + 1)
if len(raw) < 1 or len(raw) > MAX_BYTES:
    raise ValueError("invalid manifest size")
payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
fields = {"schema_version", "pid", "creation_date", "executable_path", "project_root", "command_sha256"}
if not isinstance(payload, dict) or set(payload) != fields:
    raise ValueError("invalid manifest")
pid = payload["pid"]
if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
    raise ValueError("invalid schema version")
if type(pid) is not int or pid < 1:
    raise ValueError("invalid pid")
for field in ("creation_date", "executable_path", "project_root", "command_sha256"):
    if type(payload[field]) is not str or not payload[field].strip():
        raise ValueError("invalid process identity")
if payload["command_sha256"] != sys.argv[2]:
    raise ValueError("invalid command identity")
if pathlib.Path(payload["project_root"]).resolve() != pathlib.Path(sys.argv[3]).resolve():
    raise ValueError("invalid project root")
result = {"snapshot_sha256": hashlib.sha256(raw).hexdigest(), "process": payload}
print(json.dumps(result, separators=(",", ":")))
""".strip()

    def start_gpu_monitor(self, node: str, remote_root: str, run_id: str) -> None:
        node = _validate_node(node)
        root = _validate_remote_root(remote_root)
        run_id = _validate_run_id(run_id)
        evidence = root / "data/validation/lan-controller" / run_id
        csv_path = evidence / "nvidia-smi.csv"
        stderr_path = evidence / "nvidia-smi.stderr.log"
        manifest_path = evidence / "nvidia-smi.process.json"
        manifest_temp = evidence / "nvidia-smi.process.json.tmp"
        encoded = base64.b64encode(
            self._gpu_monitor_script(csv_path).encode("utf-16-le")
        ).decode("ascii")
        command_sha256 = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        remote_python = root / ".venv/Scripts/python.exe"
        manifest_validator = self._monitor_manifest_validator()
        script = f"""
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path {_powershell_literal(str(evidence))} | Out-Null
$canonicalExecutable = [IO.Path]::GetFullPath(
  [string](Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source

)
$python = if (Test-Path -LiteralPath {_powershell_literal(str(remote_python))} -PathType Leaf) {{
  {_powershell_literal(str(remote_python))}
}} else {{ 'python' }}
$manifestValidator = @'
{manifest_validator}
'@
function Test-ExactCommandToken {{
  param([string]$CommandLine, [string]$Token)
  if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($Token)) {{ return $false }}
  $escaped = [regex]::Escape($Token)
  return [regex]::IsMatch($CommandLine, '(?:^|\\s)(?:"' + $escaped + '"|' + $escaped + ')(?=\\s|$)', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
}}
function Test-MonitorIdentity {{
  param($Snapshot, $Expected)
  if ($null -eq $Snapshot) {{ return $false }}
  $actualExecutable = ''
  try {{ $actualExecutable = [IO.Path]::GetFullPath([string]$Snapshot.ExecutablePath) }} catch {{ return $false }}
  return (
    ([string]$Snapshot.CreationDate).Equals([string]$Expected.creation_date, [StringComparison]::Ordinal) -and
    $actualExecutable.Equals([string]$Expected.executable_path, [StringComparison]::OrdinalIgnoreCase) -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-NoLogo') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-NoProfile') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-NonInteractive') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-EncodedCommand') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) {_powershell_literal(encoded)})
  )
}}
function Publish-MonitorOwnership {{
  param($Ownership)
  if ($null -eq $Ownership) {{
    throw 'monitor rollback termination failed; ownership artifacts retained'
  }}
  if (Test-Path -LiteralPath {_powershell_literal(str(manifest_path))} -PathType Leaf) {{ return }}
  Remove-Item -LiteralPath {_powershell_literal(str(manifest_temp))} -Force -ErrorAction SilentlyContinue
  $json = $Ownership | ConvertTo-Json
  $stream = [IO.FileStream]::new({_powershell_literal(str(manifest_temp))}, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  try {{
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
  }} finally {{ $stream.Dispose() }}
  Move-Item -LiteralPath {_powershell_literal(str(manifest_temp))} -Destination {_powershell_literal(str(manifest_path))}
}}
if (Test-Path -LiteralPath {_powershell_literal(str(manifest_path))} -PathType Leaf) {{
  if (Test-Path -LiteralPath {_powershell_literal(str(manifest_temp))}) {{
    throw 'GPU monitor retry reconciliation found ambiguous artifacts'
  }}
  $validated = @(& $python -c $manifestValidator {_powershell_literal(str(manifest_path))} {_powershell_literal(command_sha256)} {_powershell_literal(str(root))}) -join "`n"
  if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($validated)) {{
    throw 'GPU monitor retry reconciliation failed'
  }}
  $reconciled = ($validated | ConvertFrom-Json).process
  if (-not ([IO.Path]::GetFullPath([string]$reconciled.executable_path)).Equals($canonicalExecutable, [StringComparison]::OrdinalIgnoreCase) -or
      -not ([IO.Path]::GetFullPath([string]$reconciled.project_root)).Equals({_powershell_literal(str(root))}, [StringComparison]::OrdinalIgnoreCase)) {{
    throw 'GPU monitor retry reconciliation failed'
  }}
  $snapshot = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$reconciled.pid)" -ErrorAction SilentlyContinue
  if ($null -eq $snapshot) {{
    foreach ($artifact in @(
      {_powershell_literal(str(manifest_path))},
      {_powershell_literal(str(csv_path))},
      {_powershell_literal(str(stderr_path))}
    )) {{ Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue }}
    Write-Output 'retry reconciliation confirmed exited process'
  }} else {{
    if (-not (Test-MonitorIdentity $snapshot $reconciled)) {{ throw 'GPU monitor retry reconciliation failed' }}
    return
  }}
}}
foreach ($artifact in @(
  {_powershell_literal(str(csv_path))},
  {_powershell_literal(str(stderr_path))},
  {_powershell_literal(str(manifest_temp))}
)) {{
  if (Test-Path -LiteralPath $artifact) {{ throw 'GPU monitor preexisting artifact is not owned' }}
}}
$process = $null
$published = $false
$manifest = $null
try {{
  $process = Start-Process -FilePath $canonicalExecutable -ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',{_powershell_literal(encoded)}) -RedirectStandardError {_powershell_literal(str(stderr_path))} -PassThru
  $null = $process.Handle
  $identity = $null
  for ($attempt = 0; $attempt -lt 20 -and $null -eq $identity; $attempt++) {{
    $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)" -ErrorAction SilentlyContinue
    if ($null -eq $identity) {{ Start-Sleep -Milliseconds 50 }}
  }}
  if ($null -eq $identity) {{ throw 'GPU monitor process identity is unavailable' }}
  $manifest = [ordered]@{{
    schema_version = 1
    pid = [int]$process.Id
    creation_date = [string]$identity.CreationDate
    executable_path = $canonicalExecutable
    project_root = {_powershell_literal(str(root))}
    command_sha256 = {_powershell_literal(command_sha256)}
  }}
  $expected = [pscustomobject]@{{
    creation_date = [string]$identity.CreationDate
    executable_path = $canonicalExecutable
  }}
  if (-not (Test-MonitorIdentity $identity $expected)) {{ throw 'GPU monitor process identity is unavailable' }}
  $json = $manifest | ConvertTo-Json
  $manifestStream = [IO.FileStream]::new({_powershell_literal(str(manifest_temp))}, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  try {{
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
    $manifestStream.Write($bytes, 0, $bytes.Length)
    $manifestStream.Flush($true)
  }} finally {{ $manifestStream.Dispose() }}
  Move-Item -LiteralPath {_powershell_literal(str(manifest_temp))} -Destination {_powershell_literal(str(manifest_path))}
  $published = $true
}} catch {{
  $launchFailure = $_
  $rollbackConfirmed = $null -eq $process
  if ($null -ne $process) {{
    try {{
      $process.Kill()
      $rollbackConfirmed = $process.WaitForExit(10000)
    }} catch {{
      try {{ $rollbackConfirmed = $process.HasExited }} catch {{ $rollbackConfirmed = $false }}
    }}
  }}
  if (-not $rollbackConfirmed) {{
    if ($null -eq $manifest -and $null -ne $process) {{
      $recovery = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)" -ErrorAction SilentlyContinue
      if ($null -ne $recovery) {{
        $recoveryExpected = [pscustomobject]@{{
          creation_date = [string]$recovery.CreationDate
          executable_path = $canonicalExecutable
        }}
        if (Test-MonitorIdentity $recovery $recoveryExpected) {{
          $manifest = [ordered]@{{
            schema_version = 1
            pid = [int]$process.Id
            creation_date = [string]$recovery.CreationDate
            executable_path = $canonicalExecutable
            project_root = {_powershell_literal(str(root))}
            command_sha256 = {_powershell_literal(command_sha256)}
          }}
        }}
      }}
    }}
    Publish-MonitorOwnership $manifest
    throw 'monitor rollback termination failed; ownership artifacts retained'
  }}
  foreach ($artifact in @(
    {_powershell_literal(str(manifest_temp))},
    {_powershell_literal(str(manifest_path))},
    {_powershell_literal(str(csv_path))},
    {_powershell_literal(str(stderr_path))}
  )) {{ Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue }}
  throw $launchFailure
}}
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
        manifest_validator = self._monitor_manifest_validator()
        script = f"""
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath {_powershell_literal(str(manifest_path))} -PathType Leaf)) {{ return }}
$python = if (Test-Path -LiteralPath {_powershell_literal(str(remote_python))} -PathType Leaf) {{
  {_powershell_literal(str(remote_python))}
}} else {{ 'python' }}
$manifestValidator = @'
{manifest_validator}
'@
$validatedManifest = @(& $python -c $manifestValidator {_powershell_literal(str(manifest_path))} {_powershell_literal(command_sha256)} {_powershell_literal(str(root))}) -join "`n"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($validatedManifest)) {{
  throw 'GPU monitor PID manifest is invalid'
}}
try {{ $manifest = ($validatedManifest | ConvertFrom-Json).process }} catch {{
  throw 'GPU monitor PID manifest is invalid'
}}
$fields = @($manifest.PSObject.Properties.Name)
$expectedFields = @('schema_version','pid','creation_date','executable_path','project_root','command_sha256')
if ($fields.Count -ne $expectedFields.Count -or @($fields | Where-Object {{ $_ -notin $expectedFields }}).Count -ne 0) {{
  throw 'GPU monitor PID manifest is invalid'
}}
$monitorPid = $manifest.pid -as [int]
if ($manifest.schema_version -ne 1 -or $null -eq $monitorPid -or $monitorPid -lt 1 -or
    $manifest.command_sha256 -cne {_powershell_literal(command_sha256)} -or
    [string]::IsNullOrWhiteSpace([string]$manifest.creation_date)) {{
  throw 'GPU monitor PID manifest is invalid'
}}
$canonicalExecutable = [IO.Path]::GetFullPath([string](Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source)
if (-not ([IO.Path]::GetFullPath([string]$manifest.executable_path)).Equals($canonicalExecutable, [StringComparison]::OrdinalIgnoreCase) -or
    -not ([IO.Path]::GetFullPath([string]$manifest.project_root)).Equals({_powershell_literal(str(root))}, [StringComparison]::OrdinalIgnoreCase)) {{
  throw 'GPU monitor process identity mismatch'
}}
function Test-ExactCommandToken {{
  param([string]$CommandLine, [string]$Token)
  if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($Token)) {{ return $false }}
  $escaped = [regex]::Escape($Token)
  return [regex]::IsMatch($CommandLine, '(?:^|\\s)(?:"' + $escaped + '"|' + $escaped + ')(?=\\s|$)', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
}}
function Test-MonitorSnapshot {{
  param($Snapshot)
  if ($null -eq $Snapshot) {{ return $false }}
  $executable = ''
  try {{ $executable = [IO.Path]::GetFullPath([string]$Snapshot.ExecutablePath) }} catch {{ return $false }}
  return (
    ([string]$Snapshot.CreationDate).Equals([string]$manifest.creation_date, [StringComparison]::Ordinal) -and
    $executable.Equals($canonicalExecutable, [StringComparison]::OrdinalIgnoreCase) -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-NoLogo') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-NoProfile') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-NonInteractive') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) '-EncodedCommand') -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) {_powershell_literal(encoded)})
  )
}}
$first = Get-CimInstance Win32_Process -Filter "ProcessId = $monitorPid" -ErrorAction SilentlyContinue
if ($null -eq $first) {{
  Remove-Item -LiteralPath {_powershell_literal(str(manifest_path))} -Force
  return
}}
if (-not (Test-MonitorSnapshot $first)) {{ throw 'GPU monitor process identity mismatch' }}
$ownedProcess = Get-Process -Id $monitorPid -ErrorAction Stop
$null = $ownedProcess.Handle
$second = Get-CimInstance Win32_Process -Filter "ProcessId = $monitorPid" -ErrorAction SilentlyContinue
if (-not (Test-MonitorSnapshot $second)) {{ throw 'GPU monitor process identity changed' }}
try {{
  $ownedProcess.Kill()
  if (-not $ownedProcess.WaitForExit(10000)) {{ throw 'GPU monitor did not exit' }}
}} catch {{
  throw 'GPU monitor cleanup blocked after ownership verification'
}}
Remove-Item -LiteralPath {_powershell_literal(str(manifest_path))} -Force
"""
        self.executor.run_powershell(node, script)

    def stop_service(self, node: str, port: int) -> None:
        node = _validate_node(node)
        if isinstance(port, bool) or not isinstance(port, int) or port not in _FORMAL_PORT_MODULES:
            raise ValueError("fault injection port is not a formal worker port")
        root = self._service_roots.get(node)
        manifests: list[PureWindowsPath] = []
        pending = self._pending_service_starts.get(node)
        if pending is not None:
            manifests.append(pending)
        for manifest in reversed(self._service_manifests.get(node, [])):
            if manifest not in manifests:
                manifests.append(manifest)
        if root is None or not manifests:
            raise ValueError("worker node has no manager-owned service manifest")

        failures: list[Exception] = []
        for manifest in manifests:
            try:
                self._stop_service_manifest(node, port, root, manifest)
            except Exception as error:
                failures.append(error)
        if failures:
            error = RuntimeError(
                f"{len(failures)} of {len(manifests)} owned service cleanup attempts failed"
            )
            raise error from failures[0]

    def _stop_service_manifest(
        self,
        node: str,
        port: int,
        root: PureWindowsPath,
        manifest: PureWindowsPath,
    ) -> None:
        module = _FORMAL_PORT_MODULES[port]
        service_id = {
            9880: "local-gpt-sovits-main",
            9881: "local-indextts",
            9882: "local-cosyvoice",
        }[port]
        remote_python = root / ".venv/Scripts/python.exe"
        validator = r"""
import hashlib, json, os, pathlib, stat, sys
MAX_BYTES = 1024 * 1024
def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
path = pathlib.Path(sys.argv[1])
try:
    metadata = os.lstat(path)
except FileNotFoundError:
    print(json.dumps({"state": "absent"}, separators=(",", ":")))
    raise SystemExit(0)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1 or metadata.st_size > MAX_BYTES:
    raise ValueError("invalid manifest file")
with open(path, "rb") as stream:
    raw = stream.read(MAX_BYTES + 1)
if len(raw) < 1 or len(raw) > MAX_BYTES:
    raise ValueError("invalid manifest size")
payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
if not isinstance(payload, dict) or set(payload) != {"schema_version", "processes"}:
    raise ValueError("invalid manifest")
if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
    raise ValueError("invalid schema version")
if not isinstance(payload["processes"], list):
    raise ValueError("invalid processes")
fields = {"pid", "creation_date", "executable_path", "project_root", "worker_module", "service_id"}
modules = {
    "local-gpt-sovits-main": "app.workers.gpt_sovits_worker:app",
    "local-indextts": "app.workers.indextts_worker:app",
    "local-cosyvoice": "app.workers.cosyvoice_worker:app",
}
matches = []
seen = set()
for process in payload["processes"]:
    if not isinstance(process, dict) or set(process) != fields:
        raise ValueError("invalid process")
    pid = process["pid"]
    if type(pid) is not int or pid < 1:
        raise ValueError("invalid pid")
    for field in ("creation_date", "executable_path", "project_root", "worker_module", "service_id"):
        if type(process[field]) is not str or not process[field].strip():
            raise ValueError("invalid process identity")
    service_id = process["service_id"]
    if service_id not in modules or service_id in seen or process["worker_module"] != modules[service_id]:
        raise ValueError("invalid worker identity")
    seen.add(service_id)
    if pathlib.Path(process["project_root"]).resolve() != pathlib.Path(sys.argv[2]).resolve():
        raise ValueError("invalid project root")
    pathlib.Path(process["executable_path"]).resolve().relative_to(pathlib.Path(sys.argv[2]).resolve())
    if service_id == sys.argv[3]:
        matches.append(process)
if len(matches) > 1 or (matches and matches[0]["worker_module"] != sys.argv[4]):
    raise ValueError("owned service identity is ambiguous")
result = {
    "state": "present",
    "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
    "process": matches[0] if matches else None,
}
print(json.dumps(result, separators=(",", ":")))
""".strip()
        script = f"""
$ErrorActionPreference = 'Stop'
$python = if (Test-Path -LiteralPath {_powershell_literal(str(remote_python))} -PathType Leaf) {{
  {_powershell_literal(str(remote_python))}
}} else {{ 'python' }}
$manifestValidator = @'
{validator}
'@
$validated = @(& $python -c $manifestValidator {_powershell_literal(str(manifest))} {_powershell_literal(str(root))} {_powershell_literal(service_id)} {_powershell_literal(module)}) -join "`n"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($validated)) {{
  throw 'Worker process identity mismatch'
}}
try {{ $snapshot = $validated | ConvertFrom-Json }} catch {{
  throw 'Worker process identity mismatch'
}}
if ([string]$snapshot.state -eq 'absent') {{ return }}
if ([string]$snapshot.state -ne 'present') {{ throw 'Worker process identity mismatch' }}
$entry = $snapshot.process
if ($null -eq $entry) {{ return }}
function Test-ExactCommandToken {{
  param([string]$CommandLine, [string]$Token)
  if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($Token)) {{ return $false }}
  $escaped = [regex]::Escape($Token)
  return [regex]::IsMatch($CommandLine, '(?:^|\\s)(?:"' + $escaped + '"|' + $escaped + ')(?=\\s|$)', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
}}
function Test-ExactPortTokens {{
  param([string]$CommandLine, [int]$Port)
  if ([string]::IsNullOrWhiteSpace($CommandLine)) {{ return $false }}
  $escapedPort = [regex]::Escape([string]$Port)
  $pattern = '(?:^|\\s)(?:"--port"|--port)\\s+(?:"' + $escapedPort + '"|' + $escapedPort + ')(?=\\s|$)'
  return [regex]::IsMatch($CommandLine, $pattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)
}}
function Test-ServiceSnapshot {{
  param($Snapshot)
  if ($null -eq $Snapshot) {{ return $false }}
  $actualExecutable = ''
  try {{ $actualExecutable = [IO.Path]::GetFullPath([string]$Snapshot.ExecutablePath) }} catch {{ return $false }}
  return (
    ([string]$Snapshot.CreationDate).Equals([string]$entry.creation_date, [StringComparison]::Ordinal) -and
    $actualExecutable.Equals([IO.Path]::GetFullPath([string]$entry.executable_path), [StringComparison]::OrdinalIgnoreCase) -and
    ([IO.Path]::GetFullPath([string]$entry.project_root)).Equals({_powershell_literal(str(root))}, [StringComparison]::OrdinalIgnoreCase) -and
    (Test-ExactCommandToken ([string]$Snapshot.CommandLine) {_powershell_literal(module)}) -and
    (Test-ExactPortTokens ([string]$Snapshot.CommandLine) {port})
  )
}}
function Test-ServiceListenerOwnership {{
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction Stop)
  if ($listeners.Count -gt 0) {{
    $processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    return $processIds.Count -eq 1 -and [int]$processIds[0] -eq [int]$entry.pid
  }}
  return $true
}}
$processId = [int]$entry.pid
$first = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
if ($null -eq $first) {{ return }}
if (-not (Test-ServiceSnapshot $first) -or -not (Test-ServiceListenerOwnership)) {{
  throw 'Worker process identity mismatch'
}}
$ownedProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
if ($null -eq $ownedProcess) {{ return }}
try {{
  $null = $ownedProcess.Handle
}} catch {{
  $afterHandleFailure = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
  if ($null -eq $afterHandleFailure) {{ return }}
  throw 'Worker process handle binding failed'
}}
$second = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
if ($null -eq $second) {{ return }}
if (-not (Test-ServiceSnapshot $second) -or -not (Test-ServiceListenerOwnership)) {{
  throw 'Worker process identity changed'
}}
try {{
  $ownedProcess.Kill()
  if (-not $ownedProcess.WaitForExit(10000)) {{ throw 'Worker process did not exit' }}
}} catch {{
  $remaining = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
  if ($null -eq $remaining) {{ return }}
  throw 'Worker process termination failed after ownership verification'
}}
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
        failures: list[Exception] = []
        for port in ports:
            try:
                self.stop_service(node, port)
            except Exception as error:
                failures.append(error)
        if failures:
            error = RuntimeError(
                f"{len(failures)} of {len(ports)} owned service cleanup attempts failed"
            )
            raise error from failures[0]

    def _prepare_remote_evidence_snapshot(
        self,
        node: str,
        root: PureWindowsPath,
        source: PureWindowsPath,
    ) -> tuple[PureWindowsPath, int, str, int]:
        token = hashlib.sha256(
            self.salt
            + b"\0evidence-snapshot\0"
            + node.encode("ascii")
            + b"\0"
            + str(source).casefold().encode("utf-8")
        ).hexdigest()[:24]
        snapshot = (
            root
            / "data/validation/lan-controller"
            / f".evidence-snapshot-{token}.tmp"
        )
        script = f"""
# TTS_MORE_EVIDENCE_SNAPSHOT
$ErrorActionPreference = 'Stop'
$root = {_powershell_literal(str(root))}
$source = {_powershell_literal(str(source))}
$snapshot = {_powershell_literal(str(snapshot))}
$maxBytes = {_MAX_EVIDENCE_FILE_BYTES}
function Assert-ContainedNoReparsePath {{
  param([string]$Candidate)
  $canonicalRoot = [IO.Path]::GetFullPath($root).TrimEnd([char[]]@('\\','/'))
  $canonicalCandidate = [IO.Path]::GetFullPath($Candidate)
  $prefix = $canonicalRoot + [IO.Path]::DirectorySeparatorChar
  if (-not $canonicalCandidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {{
    throw 'Remote evidence path escaped the project root'
  }}
  $pathRoot = [IO.Path]::GetPathRoot($canonicalCandidate)
  $current = $pathRoot
  foreach ($segment in $canonicalCandidate.Substring($pathRoot.Length) -split '[\\/]') {{
    $current = Join-Path $current $segment
    if (Test-Path -LiteralPath $current) {{
      $item = Get-Item -LiteralPath $current -Force
      if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
        throw 'Remote evidence path contains a ReparsePoint'
      }}
    }}
  }}
}}
Assert-ContainedNoReparsePath $source
Assert-ContainedNoReparsePath $snapshot
if (Test-Path -LiteralPath $snapshot) {{ throw 'Remote evidence snapshot already exists' }}
$sourceItem = Get-Item -LiteralPath $source -Force -ErrorAction Stop
$sourceMtimeNs = ([int64]$sourceItem.LastWriteTimeUtc.Ticks - 621355968000000000) * 100
if ($sourceItem.PSIsContainer -or
    ($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
    [int64]$sourceItem.Length -lt 0 -or [int64]$sourceItem.Length -gt $maxBytes) {{
  throw 'Remote evidence source is not a bounded regular file'
}}
New-Item -ItemType Directory -Force -Path {_powershell_literal(str(snapshot.parent))} | Out-Null
Assert-ContainedNoReparsePath $source
Assert-ContainedNoReparsePath $snapshot
if ($null -eq ('LanEvidenceNative' -as [type])) {{
  Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class LanEvidenceNative {{
  [StructLayout(LayoutKind.Sequential)]
  public struct FILE_ATTRIBUTE_TAG_INFO {{
    public UInt32 FileAttributes;
    public UInt32 ReparseTag;
  }}
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  private static extern UInt32 GetFinalPathNameByHandle(
    SafeFileHandle handle, StringBuilder path, UInt32 length, UInt32 flags);
  [DllImport("kernel32.dll", SetLastError = true)]
  private static extern bool GetFileInformationByHandleEx(
    SafeFileHandle handle, int FileAttributeTagInfo,
    out FILE_ATTRIBUTE_TAG_INFO info, UInt32 size);
  public static string FinalPath(SafeFileHandle handle) {{
    StringBuilder path = new StringBuilder(32768);
    UInt32 length = GetFinalPathNameByHandle(handle, path, (UInt32)path.Capacity, 0);
    if (length == 0 || length >= path.Capacity) throw new Win32Exception();
    return path.ToString();
  }}
  public static UInt32 Attributes(SafeFileHandle handle) {{
    FILE_ATTRIBUTE_TAG_INFO info;
    if (!GetFileInformationByHandleEx(
      handle, 9, out info, (UInt32)Marshal.SizeOf(typeof(FILE_ATTRIBUTE_TAG_INFO)))) {{
      throw new Win32Exception();
    }}
    return info.FileAttributes;
  }}
}}
'@
}}
$input = $null
$output = $null
try {{
  $input = [IO.FileStream]::new($source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
  $openedPath = [LanEvidenceNative]::FinalPath($input.SafeFileHandle)
  if ($openedPath.StartsWith('\\\\?\\')) {{ $openedPath = $openedPath.Substring(4) }}
  $openedPath = [IO.Path]::GetFullPath($openedPath)
  if (-not $openedPath.Equals([IO.Path]::GetFullPath($source), [StringComparison]::OrdinalIgnoreCase) -or
      (([LanEvidenceNative]::Attributes($input.SafeFileHandle) -band [uint32][IO.FileAttributes]::ReparsePoint) -ne 0)) {{
    throw 'Remote evidence opened handle identity changed'
  }}
  if ($input.Length -gt $maxBytes) {{ throw 'Remote evidence source exceeds the byte limit' }}
  $output = [IO.FileStream]::new($snapshot, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  $buffer = [byte[]]::new(65536)
  [int64]$total = 0
  while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {{
    $total += $read
    if ($total -gt $maxBytes) {{ throw 'Remote evidence source exceeds the byte limit' }}
    $output.Write($buffer, 0, $read)
  }}
  $output.Flush($true)
}} catch {{
  if ($null -ne $output) {{ $output.Dispose(); $output = $null }}
  Remove-Item -LiteralPath $snapshot -Force -ErrorAction SilentlyContinue
  throw
}} finally {{
  if ($null -ne $output) {{ $output.Dispose() }}
  if ($null -ne $input) {{ $input.Dispose() }}
}}
$snapshotItem = Get-Item -LiteralPath $snapshot -Force
if ($snapshotItem.PSIsContainer -or
    ($snapshotItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
    [int64]$snapshotItem.Length -gt $maxBytes) {{
  Remove-Item -LiteralPath $snapshot -Force -ErrorAction SilentlyContinue
  throw 'Remote evidence snapshot is invalid'
}}
[ordered]@{{
  snapshot_path = $snapshot.Replace('\\','/')
  size = [int64]$snapshotItem.Length
  sha256 = (Get-FileHash -LiteralPath $snapshot -Algorithm SHA256).Hash.ToLowerInvariant()
  source_mtime_ns = [int64]$sourceMtimeNs
}} | ConvertTo-Json -Compress
"""
        result = self.executor.run_powershell(node, script, timeout=600)
        payload = _strict_json(result.stdout, "remote evidence snapshot")
        if not isinstance(payload, dict) or set(payload) != {
            "snapshot_path",
            "size",
            "sha256",
            "source_mtime_ns",
        }:
            raise ValueError("remote evidence snapshot JSON is invalid")
        if (
            type(payload["snapshot_path"]) is not str
            or payload["snapshot_path"] != snapshot.as_posix()
            or type(payload["size"]) is not int
            or not 0 <= payload["size"] <= _MAX_EVIDENCE_FILE_BYTES
            or type(payload["sha256"]) is not str
            or not _SAFE_SHA256.fullmatch(payload["sha256"])
            or type(payload["source_mtime_ns"]) is not int
            or not 0 < payload["source_mtime_ns"] <= 9_223_372_036_854_775_807
        ):
            raise ValueError("remote evidence snapshot JSON is invalid")
        return (
            snapshot,
            payload["size"],
            payload["sha256"],
            payload["source_mtime_ns"],
        )

    def _copy_evidence_atomically(
        self,
        node: str,
        remote_snapshot: PureWindowsPath,
        destination: Path,
        evidence_root: Path,
        expected_size: int,
        expected_digest: str,
        source_mtime_ns: int,
    ) -> None:
        if not _USE_DIR_FD_EVIDENCE:
            self._copy_evidence_atomically_portable(
                node,
                remote_snapshot,
                destination,
                evidence_root,
                expected_size,
                expected_digest,
                source_mtime_ns,
            )
            return
        _reject_symlink_components(destination.parent)
        _contained_path(evidence_root, destination.parent)
        if destination.exists() or destination.is_symlink():
            try:
                existing = destination.lstat()
            except OSError:
                raise ValueError("evidence destination is unavailable") from None
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise ValueError("evidence destination is not a regular file")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        root_descriptor = -1
        staging_descriptor = -1
        destination_descriptor = -1
        staging_name = f".lan-evidence-{secrets.token_hex(16)}"
        temporary_name = "payload.tmp"
        try:
            root_descriptor = os.open(evidence_root, directory_flags)
            root_metadata = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise ValueError("evidence root identity is invalid")
            os.mkdir(staging_name, mode=0o700, dir_fd=root_descriptor)
            staging_descriptor = os.open(
                staging_name, directory_flags, dir_fd=root_descriptor
            )
            staging_metadata = os.fstat(staging_descriptor)
            if (
                not stat.S_ISDIR(staging_metadata.st_mode)
                or stat.S_IMODE(staging_metadata.st_mode) != 0o700
                or (
                    hasattr(os, "getuid")
                    and staging_metadata.st_uid != os.getuid()
                )
            ):
                raise ValueError("private evidence staging identity is invalid")
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            create_flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_descriptor = os.open(
                temporary_name,
                create_flags,
                0o600,
                dir_fd=staging_descriptor,
            )
            initial_temp = os.fstat(temporary_descriptor)
            os.close(temporary_descriptor)
            staging_path = evidence_root / staging_name / temporary_name
            self.executor.copy_from(node, remote_snapshot.as_posix(), staging_path)
            read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(
                    temporary_name, read_flags, dir_fd=staging_descriptor
                )
                with os.fdopen(descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or metadata.st_dev != initial_temp.st_dev
                        or metadata.st_ino != initial_temp.st_ino
                    ):
                        raise ValueError(
                            "private evidence staging temporary was replaced"
                        )
                    raw = stream.read(_MAX_EVIDENCE_FILE_BYTES + 1)
            except OSError:
                raise ValueError(
                    "private evidence staging temporary was replaced"
                ) from None
            if len(raw) != expected_size or len(raw) > _MAX_EVIDENCE_FILE_BYTES:
                raise ValueError("local evidence size does not match remote snapshot")
            if hashlib.sha256(raw).hexdigest() != expected_digest:
                raise ValueError("local evidence digest does not match remote snapshot")
            os.utime(
                temporary_name,
                ns=(source_mtime_ns, source_mtime_ns),
                dir_fd=staging_descriptor,
                follow_symlinks=False,
            )
            relative_parent = destination.parent.relative_to(evidence_root)
            destination_descriptor = os.dup(root_descriptor)
            for part in relative_parent.parts:
                next_descriptor = os.open(
                    part,
                    directory_flags,
                    dir_fd=destination_descriptor,
                )
                os.close(destination_descriptor)
                destination_descriptor = next_descriptor
            opened_directory = os.fstat(destination_descriptor)
            if not stat.S_ISDIR(opened_directory.st_mode):
                raise ValueError("evidence destination parent changed")
            current_temp = os.stat(
                temporary_name,
                dir_fd=staging_descriptor,
                follow_symlinks=False,
            )
            if (
                current_temp.st_dev != initial_temp.st_dev
                or current_temp.st_ino != initial_temp.st_ino
                or not stat.S_ISREG(current_temp.st_mode)
            ):
                raise ValueError("private evidence staging temporary was replaced")
            try:
                final_metadata = os.stat(
                    destination.name,
                    dir_fd=destination_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(final_metadata.st_mode):
                    raise ValueError("evidence destination changed before publication")
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=staging_descriptor,
                dst_dir_fd=destination_descriptor,
            )
            current_root = os.stat(evidence_root, follow_symlinks=False)
            if (
                current_root.st_dev != root_metadata.st_dev
                or current_root.st_ino != root_metadata.st_ino
                or not stat.S_ISDIR(current_root.st_mode)
            ):
                raise ValueError("evidence root changed during publication")
            _contained_path(evidence_root, destination)
        except OSError:
            raise ValueError("atomic evidence publication failed") from None
        finally:
            if destination_descriptor >= 0:
                os.close(destination_descriptor)
            if staging_descriptor >= 0:
                try:
                    os.unlink(
                        temporary_name,
                        dir_fd=staging_descriptor,
                    )
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                os.close(staging_descriptor)
            if root_descriptor >= 0:
                try:
                    os.rmdir(staging_name, dir_fd=root_descriptor)
                except FileNotFoundError:
                    pass
                except OSError:
                    raise ValueError(
                        "private evidence staging cleanup failed"
                    ) from None
                finally:
                    os.close(root_descriptor)

    def _copy_evidence_atomically_portable(
        self,
        node: str,
        remote_snapshot: PureWindowsPath,
        destination: Path,
        evidence_root: Path,
        expected_size: int,
        expected_digest: str,
        source_mtime_ns: int,
    ) -> None:
        _reject_symlink_components(evidence_root)
        _reject_symlink_components(destination.parent)
        _contained_path(evidence_root, destination.parent)
        try:
            root_identity = evidence_root.lstat()
            parent_identity = destination.parent.lstat()
        except OSError:
            raise ValueError("evidence destination is unavailable") from None
        if (
            _is_link_or_reparse(root_identity)
            or not stat.S_ISDIR(root_identity.st_mode)
            or _is_link_or_reparse(parent_identity)
            or not stat.S_ISDIR(parent_identity.st_mode)
        ):
            raise ValueError("evidence destination identity is invalid")

        staging = evidence_root / f".lan-evidence-{secrets.token_hex(16)}"
        temporary = staging / "payload.tmp"
        try:
            staging.mkdir(mode=0o700)
            staging_identity = staging.lstat()
            if _is_link_or_reparse(staging_identity) or not stat.S_ISDIR(
                staging_identity.st_mode
            ):
                raise ValueError("private evidence staging identity is invalid")
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, create_flags, 0o600)
            initial_temp = os.fstat(descriptor)
            os.close(descriptor)

            self.executor.copy_from(node, remote_snapshot.as_posix(), temporary)
            try:
                with temporary.open("rb") as stream:
                    current_temp = os.fstat(stream.fileno())
                    if (
                        _is_link_or_reparse(current_temp)
                        or not stat.S_ISREG(current_temp.st_mode)
                        or current_temp.st_nlink != 1
                        or not _same_identity(current_temp, initial_temp)
                    ):
                        raise ValueError(
                            "private evidence staging temporary was replaced"
                        )
                    raw = stream.read(_MAX_EVIDENCE_FILE_BYTES + 1)
            except OSError:
                raise ValueError(
                    "private evidence staging temporary was replaced"
                ) from None
            if len(raw) != expected_size or len(raw) > _MAX_EVIDENCE_FILE_BYTES:
                raise ValueError("local evidence size does not match remote snapshot")
            if hashlib.sha256(raw).hexdigest() != expected_digest:
                raise ValueError("local evidence digest does not match remote snapshot")
            os.utime(
                temporary,
                ns=(source_mtime_ns, source_mtime_ns),
                follow_symlinks=False,
            )

            _reject_symlink_components(evidence_root)
            _reject_symlink_components(destination.parent)
            current_root = evidence_root.lstat()
            current_parent = destination.parent.lstat()
            current_temp = temporary.lstat()
            if (
                not _same_identity(current_root, root_identity)
                or not _same_identity(current_parent, parent_identity)
                or not _same_identity(current_temp, initial_temp)
                or _is_link_or_reparse(current_temp)
                or not stat.S_ISREG(current_temp.st_mode)
            ):
                raise ValueError("evidence path identity changed before publication")
            if destination.exists() or destination.is_symlink():
                existing = destination.lstat()
                if _is_link_or_reparse(existing) or not stat.S_ISREG(existing.st_mode):
                    raise ValueError("evidence destination is not a regular file")
            os.replace(temporary, destination)
            published = destination.lstat()
            if (
                not _same_identity(published, initial_temp)
                or _is_link_or_reparse(published)
                or not stat.S_ISREG(published.st_mode)
            ):
                raise ValueError("atomic evidence publication identity changed")
            _contained_path(evidence_root, destination)
        except OSError:
            raise ValueError("atomic evidence publication failed") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                staging.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                raise ValueError("private evidence staging cleanup failed") from None

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
        if not output.is_absolute():
            raise ValueError("evidence output must be an absolute directory")
        _reject_symlink_components(output)
        try:
            output_metadata = output.lstat()
            canonical_output = output.resolve(strict=True)
        except OSError:
            raise ValueError("evidence output must be an absolute directory") from None
        if not stat.S_ISDIR(output_metadata.st_mode):
            raise ValueError("evidence output must be an absolute directory")
        _reject_symlink_components(canonical_output)
        run_id = _validate_run_id(output.name)
        if (
            not isinstance(service_ids, tuple)
            or not service_ids
            or len(service_ids) != len(set(service_ids))
            or any(service_id not in FORMAL_SERVICE_IDS for service_id in service_ids)
        ):
            raise ValueError("worker log service is not a unique formal service ID")
        remote = root / "data/validation/lan-controller" / run_id
        destination = _secure_directory(
            canonical_output, Path("worker-logs") / node
        )
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
            remote_snapshot: PureWindowsPath | None = None
            try:
                remote_snapshot, size, digest, source_mtime_ns = (
                    self._prepare_remote_evidence_snapshot(node, root, remote_path)
                )
                self._copy_evidence_atomically(
                    node,
                    remote_snapshot,
                    local_path,
                    canonical_output,
                    size,
                    digest,
                    source_mtime_ns,
                )
            finally:
                if remote_snapshot is not None:
                    self.executor.run_powershell(
                        node,
                        "Remove-Item -LiteralPath "
                        f"{_powershell_literal(str(remote_snapshot))} "
                        "-Force -ErrorAction SilentlyContinue",
                    )
