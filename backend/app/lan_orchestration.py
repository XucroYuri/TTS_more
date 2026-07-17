from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from app.cuda_validation import CUDAValidationRunner
from app.lan_evidence import (
    LanEvidenceManifest,
    LanNodeEvidence,
    LanNodePreflight,
    LanOrchestrationPreflight,
    assert_required_evidence,
    write_lan_evidence,
    write_lan_preflight,
)
from app.lan_nodes import NodeProbe, WindowsLanNodeManager
from app.lan_topology import LanMode, LanPolicy, LanTopology, load_lan_policy
from app.services import ServiceRegistry
from app.windows_ssh import WindowsSshExecutor


REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT = Path("/usr/bin/git")
_IOREG = Path("/usr/sbin/ioreg")
_SAFE_RUN_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SAFE_WINDOWS_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SAFE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_ORCHESTRATION_TOKEN_ENV = "TTS_MORE_ORCHESTRATION_TOKEN"
_SERVICE_PORTS = {
    "local-gpt-sovits-main": 9880,
    "local-indextts": 9881,
    "local-cosyvoice": 9882,
}
_APP_PORT = 8000
_FRONTEND_PORT = 5173
_MAX_LOCAL_LOG_BYTES = 64 * 1024 * 1024


class DeploymentMode(str, Enum):
    CLEAN = "clean"
    RELEASE = "release"


def _has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    return any(component.is_symlink() for component in (absolute, *absolute.parents))


def _validate_input_file(path: Path, label: str) -> None:
    if not path.is_absolute() or _has_symlink_component(path):
        raise ValueError(f"{label} must be an absolute regular file without symlinks")
    try:
        metadata = path.stat()
    except OSError:
        raise ValueError(f"{label} must be an absolute regular file without symlinks") from None
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_INPUT_BYTES:
        raise ValueError(f"{label} must be a bounded absolute regular file without symlinks")


def _validate_output_path(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError("output must be an absolute new directory")
    if _has_symlink_component(path.parent):
        raise ValueError("output parent must not contain a symlink")
    try:
        parent_metadata = path.parent.stat()
    except OSError:
        raise ValueError("output parent must be an existing absolute directory") from None
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("output parent must be an existing absolute directory")
    name = path.name
    basename = name.split(".", 1)[0].casefold()
    if (
        not _SAFE_RUN_ID.fullmatch(name)
        or basename in _WINDOWS_RESERVED_NAMES
        or name.endswith((".", " "))
    ):
        raise ValueError("output directory name is not a safe run ID")


def _validate_remote_root(remote_root: str) -> None:
    if (
        not isinstance(remote_root, str)
        or remote_root != remote_root.strip()
        or not re.fullmatch(r"[A-Za-z]:[\\/][^:]+", remote_root)
    ):
        raise ValueError("remote root must be an absolute safe Windows path")
    parts = re.split(r"[\\/]", remote_root[3:])
    if not parts:
        raise ValueError("remote root must be an absolute safe Windows path")
    for part in parts:
        basename = part.split(".", 1)[0].casefold()
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or basename in _WINDOWS_RESERVED_NAMES
            or not _SAFE_WINDOWS_SEGMENT.fullmatch(part)
        ):
            raise ValueError("remote root must be an absolute safe Windows path")


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
        if not isinstance(self.mode, LanMode):
            raise ValueError("mode must be an explicit LAN mode")
        if not isinstance(self.deployment, DeploymentMode):
            raise ValueError("deployment must be clean or release")
        if not isinstance(self.require_baseline, bool):
            raise ValueError("require_baseline must be a boolean")
        if self.deployment is DeploymentMode.RELEASE and not self.require_baseline:
            raise ValueError("release deployment requires an approved baseline")
        if self.deployment is DeploymentMode.CLEAN and self.require_baseline:
            raise ValueError(
                "clean certification establishes a baseline and cannot require one"
            )
        if any(
            not isinstance(path, Path)
            for path in (self.topology, self.fixture, self.ssh_config, self.output)
        ):
            raise ValueError("topology, fixture, SSH config, and output must be Path values")
        _validate_input_file(self.topology, "topology")
        _validate_input_file(self.fixture, "fixture")
        _validate_input_file(self.ssh_config, "SSH config")
        _validate_remote_root(self.remote_root)
        _validate_output_path(self.output)


def parse_args(argv: list[str] | None = None) -> LanRunOptions:
    parser = argparse.ArgumentParser(
        description="Run macOS-to-Windows LAN CUDA validation"
    )
    parser.add_argument("--mode", required=True, choices=[item.value for item in LanMode])
    parser.add_argument(
        "--deployment",
        required=True,
        choices=[item.value for item in DeploymentMode],
    )
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
        topology=Path(os.path.abspath(args.topology)),
        fixture=Path(os.path.abspath(args.fixture)),
        ssh_config=Path(os.path.abspath(args.ssh_config)),
        remote_root=args.remote_root,
        output=Path(os.path.abspath(args.output)),
        require_baseline=args.require_baseline,
    )


def _trusted_executable(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("required controller tool is unavailable")
    try:
        metadata = path.stat()
    except OSError:
        raise RuntimeError("required controller tool is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise RuntimeError("required controller tool is unavailable")
    return str(path)


def _checked(
    argv: list[str],
    *,
    cwd: Path | None = None,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: int = 60,
) -> str:
    try:
        result = process_runner(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("local controller command could not complete") from None
    if result.returncode != 0:
        raise RuntimeError(f"local controller command failed with exit code {result.returncode}")
    if len(result.stdout.encode("utf-8")) > 1024 * 1024:
        raise RuntimeError("local controller command returned excessive output")
    return result.stdout.strip()


def controller_commit(
    repo_root: Path,
    *,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    repo_root = Path(os.path.abspath(repo_root))
    if not repo_root.is_absolute() or _has_symlink_component(repo_root):
        raise ValueError("controller repository root is invalid")
    try:
        if not stat.S_ISDIR(repo_root.stat().st_mode):
            raise ValueError("controller repository root is invalid")
    except OSError:
        raise ValueError("controller repository root is invalid") from None
    git = _trusted_executable(_GIT)
    top_level = Path(
        _checked(
            [git, "rev-parse", "--show-toplevel"],
            cwd=repo_root,
            process_runner=process_runner,
        )
    )
    if Path(os.path.abspath(top_level)) != repo_root:
        raise ValueError("controller path must be the complete repository root")
    commit = _checked(
        [git, "rev-parse", "HEAD"], cwd=repo_root, process_runner=process_runner
    )
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("controller commit is invalid")
    dirty = _checked(
        [git, "status", "--porcelain", "--untracked-files=all"],
        cwd=repo_root,
        process_runner=process_runner,
    )
    if dirty:
        raise ValueError("controller checkout must be clean")
    return commit


def controller_id_sha256(
    salt: bytes,
    *,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    if not isinstance(salt, bytes) or len(salt) != 32:
        raise ValueError("controller identity salt must contain 32 bytes")
    if sys.platform != "darwin":
        raise ValueError("LAN release controller must run on macOS")
    output = _checked(
        [_trusted_executable(_IOREG), "-rd1", "-c", "IOPlatformExpertDevice"],
        process_runner=process_runner,
    )
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"\r\n]+)"', output)
    if match is None:
        raise ValueError("macOS platform UUID is unavailable")
    return hashlib.sha256(salt + b"\0" + match.group(1).encode("utf-8")).hexdigest()


def validate_network_identities(
    topology: LanTopology,
    policy: LanPolicy,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ssh_targets: Mapping[str, Any] | None = None,
) -> None:
    if ssh_targets is not None and set(ssh_targets) != set(policy.workers):
        raise ValueError("SSH target set does not match topology workers")
    owners: dict[str, str] = {}
    addresses_by_node: dict[str, set[str]] = {}
    for node_name in (policy.app_node, *policy.workers):
        host = topology.nodes[node_name].host
        try:
            records = resolver(host, None, type=socket.SOCK_STREAM)
        except OSError:
            raise ValueError(f"topology node {node_name} DNS resolution failed") from None
        addresses: set[str] = set()
        for record in records:
            try:
                address = ipaddress.ip_address(record[4][0])
            except (IndexError, TypeError, ValueError):
                raise ValueError(
                    f"topology node {node_name} DNS returned an invalid address"
                ) from None
            if (
                address.is_loopback
                or address.is_unspecified
                or address.is_multicast
                or address.is_link_local
            ):
                raise ValueError(f"topology node {node_name} DNS returned a prohibited address")
            if getattr(address, "ipv4_mapped", None) is not None:
                address = address.ipv4_mapped
            addresses.add(address.compressed)
        if not addresses:
            raise ValueError(f"topology node {node_name} has no usable address")
        for address in addresses:
            previous = owners.get(address)
            if previous is not None:
                raise ValueError(
                    f"topology nodes {previous} and {node_name} resolve to the same address"
                )
            owners[address] = node_name
        addresses_by_node[node_name] = addresses
    if ssh_targets is not None:
        for node, target in ssh_targets.items():
            try:
                address = ipaddress.ip_address(target.address)
            except (AttributeError, TypeError, ValueError):
                raise ValueError("SSH target returned an invalid resolved address") from None
            if getattr(address, "ipv4_mapped", None) is not None:
                address = address.ipv4_mapped
            if address.compressed not in addresses_by_node[node]:
                raise ValueError(f"SSH target {node} does not match topology DNS identity")


def validate_node_probes(
    policy: LanPolicy,
    controller_hash: str,
    probes: list[NodeProbe],
) -> None:
    if not _SAFE_SHA256.fullmatch(controller_hash):
        raise ValueError("controller machine identity hash is invalid")
    if len(probes) != len(policy.workers) or {probe.node for probe in probes} != set(
        policy.workers
    ):
        raise ValueError("worker probe set does not match topology")
    for probe in probes:
        hashes = (
            probe.host_key_sha256,
            probe.machine_id_sha256,
            *probe.gpu_uuid_sha256,
        )
        if (
            not re.fullmatch(r"[0-9a-f]{40}", probe.commit)
            or any(not _SAFE_SHA256.fullmatch(value) for value in hashes)
        ):
            raise ValueError("worker probe identity hash is invalid")
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


def configure_run_logging(output: Path) -> logging.Logger:
    logger = logging.getLogger("tts_more.lan_validation")
    for existing in logger.handlers:
        existing.close()
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(output / "controller.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _close_run_logging(logger: logging.Logger | None) -> None:
    if logger is None:
        return
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    logger.handlers.clear()


def _trusted_repo_script(path: Path) -> str:
    expected_parent = REPO_ROOT / "scripts"
    if (
        path.parent != expected_parent
        or _has_symlink_component(path)
        or not path.is_file()
    ):
        raise RuntimeError("trusted deployment tooling is unavailable")
    return str(path)


def render_external_services(
    options: LanRunOptions,
    app_node: str,
    *,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    if not isinstance(app_node, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", app_node
    ):
        raise ValueError("app node name is unsafe")
    services_path = options.output / "services.external.json"
    if services_path.exists() or services_path.is_symlink():
        raise ValueError("run-local services registry already exists")
    python = _trusted_executable(Path(sys.executable).resolve(strict=True))
    deploy_tool = _trusted_repo_script(REPO_ROOT / "scripts" / "tts_more_deploy.py")
    argv = [
        python,
        deploy_tool,
        "render-services",
        "--profile",
        "app-only",
        "--platform",
        "posix",
        "--topology",
        str(options.topology),
        "--node",
        app_node,
        "--output",
        str(services_path),
    ]
    _checked(
        argv,
        cwd=REPO_ROOT,
        process_runner=process_runner,
        timeout_seconds=120,
    )
    _validate_input_file(services_path, "services registry")
    return services_path


def wait_for_services(
    services_path: Path,
    timeout_seconds: int,
    *,
    http_get: Callable[..., Any] = httpx.get,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise ValueError("service readiness timeout must be an integer")
    if not 1 <= timeout_seconds <= 3600:
        raise ValueError("service readiness timeout is outside the bounded range")
    try:
        _validate_input_file(services_path, "services registry")
        endpoints = ServiceRegistry.load(services_path).services
    except (OSError, ValueError, TypeError):
        raise ValueError("services registry must be a valid nonempty regular file") from None
    if not endpoints:
        raise ValueError("services registry must be a valid nonempty regular file")
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        ready: list[bool] = []
        for endpoint in endpoints:
            health_url = endpoint.health_url or endpoint.base_url.rstrip("/") + "/health"
            try:
                response = http_get(health_url, timeout=10.0)
                payload = response.json()
                ready.append(
                    bool(response.is_success)
                    and isinstance(payload, dict)
                    and payload.get("ready") is True
                )
            except (httpx.HTTPError, OSError, TypeError, ValueError):
                ready.append(False)
        if ready and all(ready):
            return
        sleeper(min(5.0, max(0.0, deadline - clock())))
    raise TimeoutError("LAN workers did not become ready within the bounded timeout")


def write_preflight(
    options: LanRunOptions,
    commit: str,
    controller_hash: str,
    probes: list[NodeProbe],
    token: str,
) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("controller commit is invalid")
    if not _SAFE_SHA256.fullmatch(controller_hash):
        raise ValueError("controller identity hash is invalid")
    if (
        not isinstance(probes, list)
        or not probes
        or any(not isinstance(probe, NodeProbe) for probe in probes)
        or len({probe.node for probe in probes}) != len(probes)
    ):
        raise ValueError("worker preflight probe set must be nonempty and unique")
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 256
        or any(character.isspace() or ord(character) < 0x21 for character in token)
    ):
        raise ValueError("orchestration token is invalid")
    payload = LanOrchestrationPreflight(
        schema_version=2,
        mode=options.mode.value,
        topology_sha256=hashlib.sha256(options.topology.read_bytes()).hexdigest(),
        fixture_sha256=hashlib.sha256(options.fixture.read_bytes()).hexdigest(),
        controller_commit=commit,
        controller_id_sha256=hashlib.sha256(
            controller_hash.encode("utf-8")
        ).hexdigest(),
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
    if path.exists() or path.is_symlink():
        raise ValueError("orchestration preflight already exists")
    write_lan_preflight(path, payload)
    return path


def run_core_cuda_validation(
    options: LanRunOptions,
    services_path: Path,
    preflight_path: Path,
    token: str,
    output_dir: Path | None = None,
    *,
    expected_commit: str | None = None,
    controller_identity: str | None = None,
    runner_factory: Callable[..., Any] = CUDAValidationRunner,
) -> None:
    if controller_identity is not None and not _SAFE_SHA256.fullmatch(
        controller_identity
    ):
        raise ValueError("controller identity is invalid")
    runner = runner_factory(
        mode=options.mode.value,
        services_path=services_path,
        fixture_path=options.fixture,
        output_dir=output_dir or options.output,
        topology_path=options.topology,
        expected_commit=expected_commit or controller_commit(REPO_ROOT),
        require_baseline=options.require_baseline,
        orchestration_preflight_path=preflight_path,
        orchestration_token=token,
        controller_identity_provider=(
            (lambda: controller_identity) if controller_identity is not None else None
        ),
    )
    report = runner.run()
    if not isinstance(report, dict) or report.get("passed") is not True:
        raise RuntimeError("LAN CUDA core validation failed")


def _assert_fixed_loopback_port_available(port: int) -> None:
    if port not in {_APP_PORT, _FRONTEND_PORT}:
        raise ValueError("fixed loopback port is not approved")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        raise RuntimeError("fixed loopback port is already in use") from None
    finally:
        probe.close()


def _controlled_logs_directory(output: Path) -> Path:
    if not output.is_absolute() or not output.is_dir() or _has_symlink_component(output):
        raise ValueError("run output must be an absolute nonsymlinked directory")
    logs = output / "logs"
    if logs.exists() or logs.is_symlink():
        if logs.is_symlink() or not logs.is_dir():
            raise ValueError("run logs path is unsafe")
    else:
        logs.mkdir(mode=0o700)
    return logs


def _open_private_log(path: Path) -> Any:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise ValueError("controlled run log destination is unavailable") from None
    return os.fdopen(descriptor, "w", encoding="utf-8")


@contextmanager
def control_plane(
    services_path: Path,
    output: Path,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
):
    _validate_input_file(services_path, "services registry")
    _assert_fixed_loopback_port_available(_APP_PORT)
    logs = _controlled_logs_directory(output)
    backend_argv = [
        _trusted_executable(Path(sys.executable).resolve(strict=True)),
        "-m",
        "uvicorn",
        "app.main:app",
        "--app-dir",
        "backend",
        "--host",
        "127.0.0.1",
        "--port",
        str(_APP_PORT),
    ]
    backend_env = {
        **os.environ,
        "TTS_MORE_SERVICE_MODE": "real",
        "TTS_MORE_SERVICES_PATH": str(services_path),
        "TTS_MORE_INSTANCE_ID": secrets.token_hex(32),
    }
    with _open_private_log(logs / "app-backend.stdout.log") as stdout, _open_private_log(
        logs / "app-backend.stderr.log"
    ) as stderr:
        process = popen_factory(
            backend_argv,
            cwd=REPO_ROOT,
            env=backend_env,
            stdout=stdout,
            stderr=stderr,
            shell=False,
        )
        try:
            wait_http_ready(
                f"http://127.0.0.1:{_APP_PORT}/api/health",
                timeout_seconds=120,
                expected_instance_id=backend_env["TTS_MORE_INSTANCE_ID"],
            )
            if process.poll() is not None:
                raise RuntimeError("application backend child exited before ownership confirmation")
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)


def _api_headers() -> dict[str, str]:
    token = os.environ.get("TTS_MORE_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def wait_http_ready(
    url: str,
    *,
    timeout_seconds: int,
    expected_instance_id: str | None = None,
    http_get: Callable[..., Any] = httpx.get,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    if not 1 <= timeout_seconds <= 3600:
        raise ValueError("HTTP readiness timeout is outside the bounded range")
    started = clock()
    while clock() - started <= timeout_seconds:
        try:
            response = http_get(url, headers=_api_headers(), timeout=5.0)
            if response.is_success:
                if expected_instance_id is None:
                    return
                payload = response.json()
                instance_id = (
                    payload.get("instance_id") if isinstance(payload, dict) else None
                )
                if isinstance(instance_id, str) and secrets.compare_digest(
                    instance_id, expected_instance_id
                ):
                    return
        except (httpx.HTTPError, OSError, TypeError, ValueError):
            pass
        sleeper(min(1.0, max(0.0, timeout_seconds - (clock() - started))))
    raise TimeoutError(f"HTTP endpoint did not become ready: {urlsplit(url).path}")


def application_ready(*, http_get: Callable[..., Any] = httpx.get) -> bool:
    try:
        return bool(
            http_get(
                f"http://127.0.0.1:{_APP_PORT}/api/health",
                headers=_api_headers(),
                timeout=5.0,
            ).is_success
        )
    except (httpx.HTTPError, OSError):
        return False


def current_service_ready(
    service_id: str, *, http_get: Callable[..., Any] = httpx.get
) -> bool:
    if service_id not in _SERVICE_PORTS:
        raise ValueError("service readiness requires a formal service ID")
    try:
        response = http_get(
            f"http://127.0.0.1:{_APP_PORT}/api/services/status",
            headers=_api_headers(),
            timeout=10.0,
        )
        response.raise_for_status()
        services = response.json()["services"]
        return isinstance(services, list) and any(
            isinstance(item, dict)
            and item.get("service_id") == service_id
            and item.get("ready") is True
            for item in services
        )
    except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError):
        return False


def wait_service_state(
    service_id: str,
    *,
    ready: bool,
    timeout_seconds: int,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> float:
    if not isinstance(ready, bool) or not 1 <= timeout_seconds <= 3600:
        raise ValueError("service state wait parameters are invalid")
    started = clock()
    while clock() - started <= timeout_seconds:
        if current_service_ready(service_id) is ready:
            return clock() - started
        sleeper(min(1.0, max(0.0, timeout_seconds - (clock() - started))))
    raise TimeoutError(f"service {service_id} did not reach requested readiness")


def _wait_all_services_degraded(
    service_ids: tuple[str, ...],
    *,
    started: float,
    timeout_seconds: int = 15,
) -> float:
    deadline = started + timeout_seconds
    while time.monotonic() <= deadline:
        if all(not current_service_ready(service_id) for service_id in service_ids):
            return time.monotonic() - started
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    raise TimeoutError("all selected services did not degrade within the bounded timeout")


def run_workstation_e2e(
    options: LanRunOptions,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    _assert_fixed_loopback_port_available(_FRONTEND_PORT)
    logs = _controlled_logs_directory(options.output)
    playwright_root = options.output / "playwright"
    playwright_root.mkdir(mode=0o700)
    artifacts = playwright_root / "artifacts"
    artifacts.mkdir(mode=0o700)
    junit_path = options.output / "playwright-junit.xml"
    if junit_path.exists() or junit_path.is_symlink():
        raise ValueError("Playwright JUnit destination already exists")
    started_at = time.time()
    env = {
        **os.environ,
        "TTS_MORE_RUN_CUDA_E2E": "1",
        "TTS_MORE_CUDA_VALIDATION_MODE": options.mode.value,
        "TTS_MORE_CUDA_FIXTURE": str(options.fixture),
        "TTS_MORE_CUDA_E2E_PROJECT_ID": f"cuda-e2e-{options.output.name}",
        "TTS_MORE_E2E_BASE_URL": f"http://127.0.0.1:{_FRONTEND_PORT}",
        "TTS_MORE_API_TARGET": f"http://127.0.0.1:{_APP_PORT}",
        "PLAYWRIGHT_JUNIT_OUTPUT_FILE": str(junit_path),
    }
    argv = [
        "pnpm",
        "--dir",
        "frontend",
        "cuda:e2e",
        "--",
        "--output",
        str(artifacts),
    ]
    try:
        with _open_private_log(logs / "playwright.stdout.log") as stdout, _open_private_log(
            logs / "playwright.stderr.log"
        ) as stderr:
            result = runner(
                argv,
                cwd=REPO_ROOT,
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=4 * 60 * 60,
                check=False,
                shell=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Playwright LAN closed loop could not complete") from None
    if result.returncode != 0:
        raise RuntimeError("Playwright LAN closed loop failed")
    try:
        metadata = junit_path.lstat()
    except OSError:
        raise RuntimeError("Playwright JUnit output is missing") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 < metadata.st_size <= _MAX_LOCAL_LOG_BYTES
        or metadata.st_mtime < started_at
    ):
        raise RuntimeError("Playwright JUnit output is invalid")


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_fault_recovery(
    options: LanRunOptions,
    policy: LanPolicy,
    manager: WindowsLanNodeManager,
    services_path: Path,
    preflight_path: Path,
    token: str,
) -> dict[str, object]:
    registry = ServiceRegistry.load(services_path)
    endpoint_ports = {
        endpoint.service_id: urlsplit(endpoint.base_url).port
        for endpoint in registry.services
        if endpoint.service_id in _SERVICE_PORTS
    }
    if endpoint_ports != _SERVICE_PORTS:
        raise ValueError("services registry does not bind exact formal worker ports")
    fault_node = os.environ.get("TTS_MORE_VALIDATION_FAULT_NODE") or sorted(
        policy.workers
    )[0]
    if fault_node not in policy.workers:
        raise ValueError("configured fault node is not a topology worker")
    fault_services = tuple(
        service_id
        for service_id in _SERVICE_PORTS
        if policy.service_owners.get(service_id) == fault_node
    )
    first_service = (
        "local-gpt-sovits-main"
        if policy.mode is LanMode.SHARED
        else fault_services[0]
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "mode": policy.mode.value,
        "fault_node": fault_node,
        "service_id": first_service,
        "degraded_within_seconds": None,
        "restart_seconds": None,
        "other_services_ready": False,
        "all_services_degraded": None,
        "all_services_degraded_within_seconds": None,
        "all_services_restart_seconds": None,
        "application_survived": False,
        "retry_passed": False,
        "retry_seconds": None,
        "recovery_passed": False,
        "recovery_seconds": None,
    }
    stopped_services: tuple[str, ...] = ()
    fault_path = options.output / "fault-recovery.json"
    primary_error: BaseException | None = None
    try:
        stopped_services = (first_service,)
        manager.stop_service(fault_node, _SERVICE_PORTS[first_service])
        report["degraded_within_seconds"] = wait_service_state(
            first_service, ready=False, timeout_seconds=15
        )
        report["other_services_ready"] = all(
            current_service_ready(service_id)
            for service_id in policy.service_owners
            if service_id != first_service
        )
        report["application_survived"] = application_ready()
        if (
            report["other_services_ready"] is not True
            or report["application_survived"] is not True
        ):
            raise RuntimeError("LAN application fault isolation gate failed")
        restart_started = time.monotonic()
        manager.restart_services(
            fault_node, options.remote_root, stopped_services
        )
        stopped_services = ()
        wait_for_services(services_path, timeout_seconds=600)
        report["restart_seconds"] = time.monotonic() - restart_started

        if policy.mode is LanMode.SHARED:
            retry_started = time.monotonic()
            run_core_cuda_validation(
                options,
                services_path,
                preflight_path,
                token,
                output_dir=options.output / "retry",
            )
            report["retry_passed"] = True
            report["retry_seconds"] = time.monotonic() - retry_started
            all_outage_started = time.monotonic()
            confirmed_stops: list[str] = []
            for service_id in fault_services:
                manager.stop_service(fault_node, _SERVICE_PORTS[service_id])
                confirmed_stops.append(service_id)
                stopped_services = tuple(confirmed_stops)
            report["all_services_degraded_within_seconds"] = (
                _wait_all_services_degraded(
                    fault_services,
                    started=all_outage_started,
                )
            )
            report["all_services_degraded"] = True
            report["application_survived"] = bool(
                report["application_survived"] and application_ready()
            )
            if (
                report["all_services_degraded"] is not True
                or report["application_survived"] is not True
            ):
                raise RuntimeError("LAN shared outage gate failed")
            all_restart_started = time.monotonic()
            manager.restart_services(
                fault_node, options.remote_root, stopped_services
            )
            stopped_services = ()
            wait_for_services(services_path, timeout_seconds=600)
            report["all_services_restart_seconds"] = (
                time.monotonic() - all_restart_started
            )

        recovery_started = time.monotonic()
        run_core_cuda_validation(
            options,
            services_path,
            preflight_path,
            token,
            output_dir=options.output / "recovery",
        )
        if policy.mode is LanMode.DISTRIBUTED:
            report["retry_passed"] = True
        report["recovery_passed"] = True
        report["recovery_seconds"] = time.monotonic() - recovery_started
        if policy.mode is LanMode.DISTRIBUTED:
            report["retry_seconds"] = report["recovery_seconds"]
        _atomic_write_json(fault_path, report)
        if not (
            isinstance(report["degraded_within_seconds"], (int, float))
            and not isinstance(report["degraded_within_seconds"], bool)
            and report["degraded_within_seconds"] <= 15
        ):
            raise RuntimeError("LAN fault degradation gate failed")
        return report
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if stopped_services:
            try:
                manager.restart_services(
                    fault_node, options.remote_root, stopped_services
                )
                wait_for_services(services_path, timeout_seconds=600)
            except BaseException:
                if primary_error is None:
                    raise
        if primary_error is not None and not fault_path.exists():
            try:
                _atomic_write_json(fault_path, report)
            except BaseException:
                pass


def _ensure_output_directory(output: Path) -> None:
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    output.chmod(0o700)


def _service_ids_for_node(policy: LanPolicy, node: str) -> tuple[str, ...]:
    return tuple(
        service_id
        for service_id, owner in policy.service_owners.items()
        if owner == node
    )


def _service_ports_for_node(policy: LanPolicy, node: str) -> tuple[int, ...]:
    return tuple(_SERVICE_PORTS[item] for item in _service_ids_for_node(policy, node))


def _write_blocker_evidence(
    options: LanRunOptions,
    *,
    services_path: Path,
    preflight_path: Path,
    commit: str | None,
    error: BaseException,
    cleanup_error_count: int,
    core_completed: bool,
) -> None:
    runner = CUDAValidationRunner(
        mode=options.mode.value,
        services_path=services_path,
        fixture_path=options.fixture,
        output_dir=options.output,
        topology_path=options.topology,
        expected_commit=commit,
        require_baseline=options.require_baseline,
        orchestration_preflight_path=preflight_path,
    )
    message = (
        f"orchestration failed ({type(error).__name__}); "
        f"owned cleanup blockers={min(cleanup_error_count, 99)}"
    )
    preserve = core_completed and (options.output / "summary.json").is_file()
    try:
        runner.write_blocker_report(
            stage="evidence-collection" if preserve else "lan-orchestration",
            message=message[:512],
            preserve_existing=preserve,
        )
    except Exception:
        if preserve:
            runner.write_blocker_report(
                stage="lan-orchestration",
                message=message[:512],
                preserve_existing=False,
            )
        else:
            raise


class LanOrchestrator:
    def __init__(
        self,
        options: LanRunOptions,
        *,
        executor: Any,
        node_manager_factory: Callable[..., Any],
        process_runner: Callable[..., subprocess.CompletedProcess[str]],
    ) -> None:
        self.options = options
        self.executor = executor
        self.node_manager_factory = node_manager_factory
        self.process_runner = process_runner

    def run(self) -> int:
        self.options.validate()
        logger: logging.Logger | None = None
        topology: LanTopology | None = None
        policy: LanPolicy | None = None
        commit: str | None = None
        controller_hash: str | None = None
        manager: Any = None
        services_path = self.options.output / "services.external.json"
        preflight_path = self.options.output / "orchestration-preflight.json"
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        monitor_nodes: list[str] = []
        service_nodes: list[str] = []
        evidence_nodes: list[str] = []
        probes: list[NodeProbe] = []
        core_completed = False
        stage_outcomes = {
            "schema_version": 1,
            "core": "skipped",
            "playwright": "skipped",
            "cleanup": "skipped",
        }
        output_created = False
        token: str | None = None
        run_started_at: datetime | None = None

        try:
            _ensure_output_directory(self.options.output)
            output_created = True
            run_started_at = datetime.now(timezone.utc)
            logger = configure_run_logging(self.options.output)
            topology, policy = load_lan_policy(self.options.topology, self.options.mode)
            commit = controller_commit(
                REPO_ROOT,
                process_runner=self.process_runner,
            )
            salt = secrets.token_bytes(32)
            controller_hash = controller_id_sha256(
                salt,
                process_runner=self.process_runner,
            )
            ssh_targets: dict[str, Any] = {}
            for node in policy.workers:
                ssh_targets[node] = self.executor.resolve(node)
            validate_network_identities(
                topology,
                policy,
                ssh_targets=ssh_targets,
            )
            pinned_executor = self.executor.with_pinned_targets(ssh_targets)

            token = secrets.token_hex(32)
            os.environ[_ORCHESTRATION_TOKEN_ENV] = token
            manager = self.node_manager_factory(pinned_executor, salt=salt)
            evidence_nodes.extend(policy.workers)

            for node in policy.workers:
                logger.info("sync worker node=%s", node)
                manager.sync_checkout(node, self.options.remote_root, commit)
            for node in policy.workers:
                logger.info("deploy worker node=%s", node)
                manager.deploy(
                    node,
                    self.options.remote_root,
                    self.options.topology,
                    clean=self.options.deployment is DeploymentMode.CLEAN,
                )
            for node in policy.workers:
                logger.info("start GPU monitor node=%s", node)
                monitor_nodes.append(node)
                manager.start_gpu_monitor(
                    node,
                    self.options.remote_root,
                    self.options.output.name,
                )
            for node in policy.workers:
                logger.info("start worker services node=%s", node)
                service_nodes.append(node)
                manager.start(node, self.options.remote_root)
            probes = [
                manager.inspect(node, self.options.remote_root, commit)
                for node in policy.workers
            ]
            services_path = render_external_services(
                self.options,
                topology.app_node,
                process_runner=self.process_runner,
            )
            wait_for_services(services_path, timeout_seconds=600)
            validate_node_probes(policy, controller_hash, probes)
            preflight_path = write_preflight(
                self.options,
                commit,
                controller_hash,
                probes,
                token,
            )
            stage_outcomes["core"] = "failure"
            run_core_cuda_validation(
                self.options,
                services_path,
                preflight_path,
                token,
                expected_commit=commit,
                controller_identity=controller_hash,
            )
            core_completed = True
            stage_outcomes["core"] = "success"
            with control_plane(services_path, self.options.output):
                stage_outcomes["playwright"] = "failure"
                run_workstation_e2e(
                    self.options,
                    runner=self.process_runner,
                )
                stage_outcomes["playwright"] = "success"
                stage_outcomes["cleanup"] = "failure"
                run_fault_recovery(
                    self.options,
                    policy,
                    manager,
                    services_path,
                    preflight_path,
                    token,
                )
        except BaseException as error:
            primary_error = error
            if logger is not None:
                logger.error("stage failed error_type=%s", type(error).__name__)
        finally:
            if manager is not None and policy is not None:
                for node in reversed(monitor_nodes):
                    try:
                        manager.stop_gpu_monitor(
                            node,
                            self.options.remote_root,
                            self.options.output.name,
                        )
                    except BaseException as error:
                        cleanup_errors.append(error)
                for node in evidence_nodes:
                    try:
                        manager.collect_evidence(
                            node,
                            self.options.remote_root,
                            self.options.output,
                            _service_ids_for_node(policy, node),
                        )
                    except BaseException as error:
                        cleanup_errors.append(error)
                for node in reversed(service_nodes):
                    try:
                        manager.stop_all_services(
                            node,
                            _service_ports_for_node(policy, node),
                        )
                    except BaseException as error:
                        cleanup_errors.append(error)
            os.environ.pop(_ORCHESTRATION_TOKEN_ENV, None)

        if primary_error is None and cleanup_errors:
            primary_error = RuntimeError("owned LAN cleanup failed")
        if (
            primary_error is None
            and policy is not None
            and commit is not None
            and probes
            and run_started_at is not None
        ):
            try:
                manifest = LanEvidenceManifest(
                    schema_version=1,
                    mode=self.options.mode.value,
                    deployment=self.options.deployment.value,
                    controller_commit=commit,
                    topology_sha256=hashlib.sha256(
                        self.options.topology.read_bytes()
                    ).hexdigest(),
                    fixture_sha256=hashlib.sha256(
                        self.options.fixture.read_bytes()
                    ).hexdigest(),
                    service_owners=policy.service_owners,
                    nodes={
                        probe.node: LanNodeEvidence(
                            commit=probe.commit,
                            host_key_sha256=probe.host_key_sha256,
                            machine_id_sha256=probe.machine_id_sha256,
                            gpu_uuid_sha256=list(probe.gpu_uuid_sha256),
                            gpu_log=f"worker-logs/{probe.node}/nvidia-smi.csv",
                            service_logs={
                                service_id: (
                                    f"worker-logs/{probe.node}/{service_id}.log"
                                )
                                for service_id in _service_ids_for_node(
                                    policy, probe.node
                                )
                            },
                        )
                        for probe in probes
                    },
                    human_review_status="pending",
                )
                write_lan_evidence(
                    self.options.output / "distributed-evidence.json", manifest
                )
                assert_required_evidence(
                    self.options.output,
                    policy.service_owners,
                    started_at=run_started_at,
                    expected_manifest=manifest,
                )
                stage_outcomes["cleanup"] = "success"
            except BaseException as error:
                primary_error = error
                if logger is not None:
                    logger.error(
                        "evidence gate failed error_type=%s", type(error).__name__
                    )
        try:
            _atomic_write_json(
                self.options.output / "workflow-outcomes.json", stage_outcomes
            )
        except BaseException as error:
            if primary_error is None:
                primary_error = error
            if logger is not None:
                logger.error(
                    "workflow outcomes failed error_type=%s", type(error).__name__
                )
        if primary_error is not None:
            if not output_created:
                _ensure_output_directory(self.options.output)
                output_created = True
                logger = configure_run_logging(self.options.output)
                logger.error("stage failed error_type=%s", type(primary_error).__name__)
            try:
                _write_blocker_evidence(
                    self.options,
                    services_path=services_path,
                    preflight_path=preflight_path,
                    commit=commit,
                    error=primary_error,
                    cleanup_error_count=len(cleanup_errors),
                    core_completed=core_completed,
                )
            except BaseException as blocker_error:
                if logger is not None:
                    logger.error(
                        "blocker evidence failed error_type=%s",
                        type(blocker_error).__name__,
                    )
            finally:
                _close_run_logging(logger)
            return 1

        if logger is not None:
            logger.info("LAN CUDA validation passed mode=%s", self.options.mode.value)
        _close_run_logging(logger)
        return 0


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    executor = WindowsSshExecutor(options.ssh_config)
    return LanOrchestrator(
        options,
        executor=executor,
        node_manager_factory=WindowsLanNodeManager,
        process_runner=subprocess.run,
    ).run()
