from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_SAFE_ALIAS = re.compile(r"[A-Za-z0-9._-]+\Z")
_LEGACY_NUMERIC_IP = re.compile(r"(?:0[x][0-9a-f]+|[0-9]+)(?:\.(?:0[x][0-9a-f]+|[0-9]+))*", re.I)
_SAFE_REMOTE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]*\Z")


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
        self._resolved_targets: dict[str, SshResolvedTarget] = {}

    def _run(
        self,
        argv: list[str],
        *,
        operation: str,
        alias: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{operation} for {alias} failed with exit code {result.returncode}")
        return result

    @staticmethod
    def _validate_alias(alias: str) -> None:
        if not alias or not _SAFE_ALIAS.fullmatch(alias):
            raise ValueError("SSH alias contains unsupported characters")

    @staticmethod
    def _parse_settings(output: str) -> dict[str, str]:
        settings: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition(" ")
            if separator and key and value:
                settings[key.casefold()] = value.strip()
        return settings

    @staticmethod
    def _validate_hostname(hostname: str) -> str:
        normalized = hostname.strip().casefold().rstrip(".")
        if not normalized or normalized in {"localhost", "ip6-localhost"}:
            raise ValueError("SSH target must use a non-loopback, specified hostname")
        candidate = normalized.strip("[]")
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            if _LEGACY_NUMERIC_IP.fullmatch(candidate):
                raise ValueError("SSH target must use a canonical numeric IP address") from None
            return normalized
        if address.is_loopback or address.is_unspecified:
            raise ValueError("SSH target must use a non-loopback, specified hostname")
        return address.compressed

    @staticmethod
    def _validate_known_hosts(value: str) -> Path:
        paths = value.split()
        if len(paths) != 1:
            raise ValueError("SSH target must use a pinned UserKnownHostsFile")
        path = Path(paths[0]).expanduser()
        if not path.name or path == Path("/dev/null"):
            raise ValueError("SSH target must use a pinned UserKnownHostsFile")
        return path

    @staticmethod
    def _validate_remote_path(remote_path: str) -> str:
        if not remote_path or remote_path != remote_path.strip():
            raise ValueError("remote path is unsafe")
        if any(character in remote_path for character in "\r\n\x00"):
            raise ValueError("remote path is unsafe")

        if re.fullmatch(r"[A-Za-z]:[\\/].*", remote_path):
            parts = re.split(r"[\\/]", remote_path[3:])
        elif ":" not in remote_path and not remote_path.startswith(("/", "\\", "-")):
            parts = re.split(r"[\\/]", remote_path)
        else:
            raise ValueError("remote path is unsafe")

        if not parts or any(part in {"", ".", ".."} or not _SAFE_REMOTE_SEGMENT.fullmatch(part) for part in parts):
            raise ValueError("remote path is unsafe")
        return remote_path

    def resolve(self, alias: str) -> SshResolvedTarget:
        self._validate_alias(alias)
        cached = self._resolved_targets.get(alias)
        if cached is not None:
            return cached

        result = self._run(
            ["ssh", "-F", str(self.config_path), "-G", alias],
            operation="SSH resolution",
            alias=alias,
            timeout=30,
        )
        settings = self._parse_settings(result.stdout)
        if settings.get("batchmode") != "yes":
            raise ValueError("SSH target must set BatchMode yes")
        if settings.get("identitiesonly") != "yes":
            raise ValueError("SSH target must set IdentitiesOnly yes")
        if settings.get("stricthostkeychecking") not in {"yes", "true"}:
            raise ValueError("SSH target must set StrictHostKeyChecking yes")

        hostname = self._validate_hostname(settings.get("hostname", ""))
        user = settings.get("user", "").strip()
        if not user:
            raise ValueError("SSH target must set a nonempty user")
        known_hosts_file = self._validate_known_hosts(settings.get("userknownhostsfile", ""))
        target = SshResolvedTarget(alias, hostname, user, known_hosts_file)
        self._resolved_targets[alias] = target
        return target

    def run_powershell(
        self, alias: str, script: str, *, timeout: int = 1800
    ) -> SshCommandResult:
        self.resolve(alias)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = self._run(
            [
                "ssh",
                "-F",
                str(self.config_path),
                "-o",
                "BatchMode=yes",
                alias,
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            operation="SSH execution",
            alias=alias,
            timeout=timeout,
        )
        return SshCommandResult(result.stdout, result.stderr)

    def copy_to(self, alias: str, source: Path, remote_path: str) -> None:
        self._validate_alias(alias)
        safe_remote_path = self._validate_remote_path(remote_path)
        self.resolve(alias)
        self._run(
            ["scp", "-F", str(self.config_path), str(source), f"{alias}:{safe_remote_path}"],
            operation="SCP upload",
            alias=alias,
            timeout=600,
        )

    def copy_from(self, alias: str, remote_path: str, destination: Path) -> None:
        self._validate_alias(alias)
        safe_remote_path = self._validate_remote_path(remote_path)
        self.resolve(alias)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["scp", "-F", str(self.config_path), f"{alias}:{safe_remote_path}", str(destination)],
            operation="SCP download",
            alias=alias,
            timeout=600,
        )

    def pinned_host_key_sha256(self, alias: str) -> str:
        target = self.resolve(alias)
        try:
            address = ipaddress.ip_address(target.hostname)
        except ValueError:
            lookup_host = target.hostname
        else:
            lookup_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        result = self._run(
            ["ssh-keygen", "-F", lookup_host, "-f", str(target.known_hosts_file)],
            operation="SSH host key lookup",
            alias=alias,
            timeout=30,
        )
        if not result.stdout.strip():
            raise ValueError("no pinned host key found")
        return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
