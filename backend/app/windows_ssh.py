from __future__ import annotations

import base64
import glob
import hashlib
import ipaddress
import itertools
import os
import re
import shlex
import socket
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_SAFE_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SAFE_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SAFE_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LEGACY_NUMERIC_IP = re.compile(r"(?:0[x][0-9a-f]+|[0-9]+)(?:\.(?:0[x][0-9a-f]+|[0-9]+))*", re.I)
_SAFE_REMOTE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]*\Z")
_MAX_CONFIG_FILE_BYTES = 1024 * 1024
_MAX_CONFIG_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_CONFIG_FILES = 64


@dataclass
class _ConfigBudget:
    files: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class SshResolvedTarget:
    alias: str
    hostname: str
    address: str
    user: str
    port: int
    known_hosts_file: Path
    identity_files: tuple[Path, ...]
    certificate_files: tuple[Path, ...]


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
        resolver: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.config_path = Path(os.path.abspath(config_path.expanduser()))
        self.runner = runner
        self.resolver = resolver or self._resolve_addresses

    @staticmethod
    def _resolve_addresses(hostname: str) -> list[str]:
        try:
            records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except OSError:
            raise ValueError("SSH target DNS resolution failed") from None
        return [record[4][0] for record in records]

    def _run(
        self,
        argv: list[str],
        *,
        operation: str,
        alias: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{operation} for {alias} timed out") from None
        except OSError:
            raise RuntimeError(f"{operation} for {alias} could not start") from None
        if result.returncode != 0:
            raise RuntimeError(f"{operation} for {alias} failed with exit code {result.returncode}")
        return result

    @staticmethod
    def _directive(line: str) -> tuple[str, list[str]] | None:
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            raise ValueError("SSH configuration is malformed") from None
        if not tokens:
            return None
        if "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            arguments = [value, *tokens[1:]]
        elif len(tokens) > 1 and tokens[1] == "=":
            key = tokens[0]
            arguments = tokens[2:]
        else:
            key = tokens[0]
            arguments = tokens[1:]
        return key.casefold(), arguments

    def _validate_config_text(self, content: str, *, assembled: bool) -> None:
        if any(
            (ord(character) < 32 and character != "\n") or ord(character) == 127
            for character in content
        ):
            raise ValueError("SSH configuration contains an unsupported control character")
        for line in content.split("\n"):
            directive = self._directive(line)
            if directive is None:
                continue
            key, _ = directive
            if key == "match":
                raise ValueError(
                    "SSH configuration must not use Match directives (including Match exec)"
                )
            if assembled and key == "include":
                raise ValueError("SSH configuration snapshot contains an unexpanded Include")

    def _expand_config_file(
        self, path: Path, stack: tuple[Path, ...], budget: _ConfigBudget
    ) -> str:
        path = path.expanduser()
        path = Path(os.path.abspath(path))
        if path in stack or len(stack) >= 32:
            raise ValueError("SSH Include graph is recursive or too deep")
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise ValueError("SSH configuration must not use symlinks")
        try:
            metadata = path.lstat()
        except OSError:
            raise ValueError("SSH Include file is unavailable") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("SSH configuration must be a regular file")
        if metadata.st_size > _MAX_CONFIG_FILE_BYTES:
            raise ValueError("SSH configuration exceeds the per-file size limit")
        budget.files += 1
        if budget.files > _MAX_CONFIG_FILES:
            raise ValueError("SSH configuration exceeds the file count limit")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as config_file:
                opened_metadata = os.fstat(config_file.fileno())
                if (
                    not stat.S_ISREG(opened_metadata.st_mode)
                    or opened_metadata.st_dev != metadata.st_dev
                    or opened_metadata.st_ino != metadata.st_ino
                ):
                    raise ValueError("SSH configuration must be a regular file")
                raw_content = config_file.read(_MAX_CONFIG_FILE_BYTES + 1)
        except OSError:
            raise ValueError("SSH configuration is unreadable") from None
        if len(raw_content) > _MAX_CONFIG_FILE_BYTES:
            raise ValueError("SSH configuration exceeds the per-file size limit")
        budget.total_bytes += len(raw_content)
        if budget.total_bytes > _MAX_CONFIG_TOTAL_BYTES:
            raise ValueError("SSH configuration exceeds the total byte limit")
        try:
            content = raw_content.decode("utf-8")
        except UnicodeError:
            raise ValueError("SSH configuration is unreadable") from None

        self._validate_config_text(content, assembled=False)

        expanded: list[str] = []
        for line in content.splitlines(keepends=True):
            directive = self._directive(line)
            if directive is None:
                expanded.append(line)
                continue
            key, arguments = directive
            if key != "include":
                expanded.append(line)
                continue
            if not arguments:
                raise ValueError("SSH Include directive is malformed")
            for pattern_text in arguments:
                if "%" in pattern_text or "${" in pattern_text:
                    raise ValueError("SSH Include pattern uses unsupported expansion")
                pattern = Path(pattern_text).expanduser()
                if not pattern.is_absolute():
                    pattern = Path.home() / ".ssh" / pattern
                remaining_files = _MAX_CONFIG_FILES - budget.files
                included_names = list(
                    itertools.islice(
                        glob.iglob(str(pattern)), remaining_files + 1
                    )
                )
                if len(included_names) > remaining_files:
                    raise ValueError("SSH configuration exceeds the file count limit")
                for included_name in sorted(included_names):
                    expanded.append(
                        self._expand_config_file(
                            Path(included_name), (*stack, path), budget
                        )
                    )
        fragment = "".join(expanded)
        if fragment and not fragment.endswith("\n"):
            fragment += "\n"
        return fragment

    def _resolved_config_output(self, alias: str) -> str:
        snapshot = self._expand_config_file(self.config_path, (), _ConfigBudget())
        if len(snapshot.encode("utf-8")) > _MAX_CONFIG_TOTAL_BYTES:
            raise ValueError("SSH configuration snapshot exceeds the total byte limit")
        self._validate_config_text(snapshot, assembled=True)
        with tempfile.TemporaryDirectory(prefix="tts-more-ssh-") as directory:
            snapshot_path = Path(directory) / "ssh_config"
            snapshot_path.write_bytes(snapshot.encode("utf-8"))
            snapshot_path.chmod(0o600)
            return self._run(
                ["ssh", "-F", str(snapshot_path), "-G", alias],
                operation="SSH resolution",
                alias=alias,
                timeout=30,
            ).stdout

    @staticmethod
    def _validate_alias(alias: str) -> None:
        if not alias or alias in {".", ".."} or not _SAFE_ALIAS.fullmatch(alias):
            raise ValueError("SSH alias contains unsupported characters")

    @staticmethod
    def _parse_settings(output: str) -> dict[str, list[str]]:
        settings: dict[str, list[str]] = {}
        for line in output.splitlines():
            key, separator, value = line.partition(" ")
            if line and not separator:
                raise ValueError("SSH configuration output is malformed")
            if separator and key and value:
                settings.setdefault(key.casefold(), []).append(value.strip())
        return settings

    @staticmethod
    def _setting(
        settings: dict[str, list[str]], key: str, *, default: str = ""
    ) -> str:
        values = settings.get(key, [])
        if not values:
            return default
        if len(values) != 1:
            raise ValueError(f"SSH configuration has duplicate {key}")
        return values[0]

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
            labels = normalized.split(".")
            if len(normalized) > 253 or any(
                not _SAFE_HOST_LABEL.fullmatch(label) for label in labels
            ):
                raise ValueError("SSH target must use a valid DNS hostname") from None
            return normalized
        if address.is_loopback or address.is_unspecified:
            raise ValueError("SSH target must use a non-loopback, specified hostname")
        return address.compressed

    def _validated_address(self, hostname: str) -> str:
        try:
            literal = ipaddress.ip_address(hostname.strip("[]"))
        except ValueError:
            answers = self.resolver(hostname)
        else:
            answers = [literal.compressed]
        if not answers:
            raise ValueError("SSH target DNS resolution returned no addresses")

        validated: list[str] = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(answer)
            except ValueError:
                raise ValueError("SSH target DNS returned an invalid address") from None
            if address.is_loopback or address.is_unspecified or address.is_multicast:
                raise ValueError("SSH target DNS returned a prohibited address")
            validated.append(address.compressed)
        return validated[0]

    @staticmethod
    def _validate_known_hosts(value: str) -> Path:
        paths = value.split()
        if len(paths) != 1:
            raise ValueError("SSH target must use a pinned UserKnownHostsFile")
        path = Path(paths[0]).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.name or any(component.is_symlink() for component in (path, *path.parents)):
            raise ValueError("SSH target must use a pinned UserKnownHostsFile")
        try:
            canonical = path.resolve(strict=True)
            mode = canonical.stat().st_mode
        except OSError:
            raise ValueError("SSH target must use a pinned UserKnownHostsFile") from None
        if not stat.S_ISREG(mode):
            raise ValueError("SSH target must use a pinned UserKnownHostsFile")
        return canonical

    @staticmethod
    def _validate_auth_files(
        values: list[str], setting: str, *, require_exists: bool
    ) -> tuple[Path, ...]:
        validated: list[Path] = []
        for value in values:
            if value.casefold() == "none":
                continue
            if (
                not value
                or "%" in value
                or "${" in value
                or any(character.isspace() or character in "\"'\\" for character in value)
                or any(character in value for character in "\r\n\x00")
            ):
                raise ValueError(f"SSH target has an invalid {setting}")
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.name or any(
                component.is_symlink() for component in (path, *path.parents)
            ):
                raise ValueError(f"SSH target has an invalid {setting}")
            canonical = path.resolve(strict=False)
            try:
                exists = canonical.exists()
                if require_exists and not exists:
                    raise ValueError(f"SSH target has an invalid {setting}")
                if exists and not stat.S_ISREG(canonical.stat().st_mode):
                    raise ValueError(f"SSH target has an invalid {setting}")
            except OSError:
                raise ValueError(f"SSH target has an invalid {setting}") from None
            validated.append(canonical)
        return tuple(validated)

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
        settings = self._parse_settings(self._resolved_config_output(alias))
        if self._setting(settings, "batchmode") != "yes":
            raise ValueError("SSH target must set BatchMode yes")
        if self._setting(settings, "identitiesonly") != "yes":
            raise ValueError("SSH target must set IdentitiesOnly yes")
        if self._setting(settings, "stricthostkeychecking") not in {"yes", "true"}:
            raise ValueError("SSH target must set StrictHostKeyChecking yes")
        if self._setting(settings, "proxycommand", default="none").casefold() != "none":
            raise ValueError("SSH target must not set a proxy route")
        if self._setting(settings, "proxyjump", default="none").casefold() != "none":
            raise ValueError("SSH target must not set a proxy route")
        if self._setting(settings, "permitlocalcommand", default="no").casefold() != "no":
            raise ValueError("SSH target must disable local command execution")
        if self._setting(settings, "localcommand", default="none").casefold() != "none":
            raise ValueError("SSH target must not set a local command")
        if self._setting(settings, "controlpath", default="none").casefold() != "none":
            raise ValueError("SSH target must disable connection sharing")
        if self._setting(settings, "controlmaster", default="no").casefold() not in {
            "no",
            "false",
        }:
            raise ValueError("SSH target must disable connection sharing")
        if self._setting(settings, "controlpersist", default="no").casefold() not in {
            "no",
            "false",
        }:
            raise ValueError("SSH target must disable connection sharing")
        if self._setting(settings, "knownhostscommand", default="none").casefold() != "none":
            raise ValueError("SSH target must not use an alternative host-key source")
        if self._setting(settings, "verifyhostkeydns", default="no").casefold() not in {
            "no",
            "false",
        }:
            raise ValueError("SSH target must not use an alternative host-key source")

        hostname = self._validate_hostname(self._setting(settings, "hostname"))
        user = self._setting(settings, "user").strip()
        if not _SAFE_USER.fullmatch(user):
            raise ValueError("SSH target must set a valid user")
        port_text = self._setting(settings, "port", default="22")
        if not port_text.isascii() or not port_text.isdecimal():
            raise ValueError("SSH target must set a valid port")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("SSH target must set a valid port")
        known_hosts_file = self._validate_known_hosts(
            self._setting(settings, "userknownhostsfile")
        )
        identity_files = self._validate_auth_files(
            settings.get("identityfile", []), "IdentityFile", require_exists=False
        )
        certificate_files = self._validate_auth_files(
            settings.get("certificatefile", []), "CertificateFile", require_exists=True
        )
        address = self._validated_address(hostname)
        return SshResolvedTarget(
            alias,
            hostname,
            address,
            user,
            port,
            known_hosts_file,
            identity_files,
            certificate_files,
        )

    @staticmethod
    def _connection_arguments(target: SshResolvedTarget) -> list[str]:
        options = [
            f"HostName={target.address}",
            f"HostKeyAlias={target.hostname}",
            f"User={target.user}",
            f"Port={target.port}",
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "StrictHostKeyChecking=yes",
            f"UserKnownHostsFile={target.known_hosts_file}",
            "GlobalKnownHostsFile=none",
            "ProxyCommand=none",
            "ProxyJump=none",
            "PermitLocalCommand=no",
            "LocalCommand=none",
            "ControlPath=none",
            "ControlMaster=no",
            "ControlPersist=no",
            "KnownHostsCommand=none",
            "VerifyHostKeyDNS=no",
            *(f"CertificateFile={path}" for path in target.certificate_files),
        ]
        arguments = [argument for option in options for argument in ("-o", option)]
        arguments.extend(
            argument
            for path in target.identity_files
            for argument in ("-i", str(path))
        )
        return arguments

    def run_powershell(
        self, alias: str, script: str, *, timeout: int = 1800
    ) -> SshCommandResult:
        target = self.resolve(alias)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = self._run(
            [
                "ssh",
                "-F",
                os.devnull,
                *self._connection_arguments(target),
                target.alias,
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
        local_source = source.expanduser().resolve()
        target = self.resolve(alias)
        self._run(
            [
                "scp",
                "-s",
                "-F",
                os.devnull,
                *self._connection_arguments(target),
                str(local_source),
                f"{target.alias}:{safe_remote_path}",
            ],
            operation="SCP upload",
            alias=alias,
            timeout=600,
        )

    def copy_from(self, alias: str, remote_path: str, destination: Path) -> None:
        self._validate_alias(alias)
        safe_remote_path = self._validate_remote_path(remote_path)
        local_destination = destination.expanduser().resolve()
        target = self.resolve(alias)
        local_destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "scp",
                "-s",
                "-F",
                os.devnull,
                *self._connection_arguments(target),
                f"{target.alias}:{safe_remote_path}",
                str(local_destination),
            ],
            operation="SCP download",
            alias=alias,
            timeout=600,
        )

    def pinned_host_key_sha256(self, alias: str) -> str:
        target = self.resolve(alias)
        if target.port != 22:
            lookup_host = f"[{target.hostname}]:{target.port}"
        else:
            try:
                address = ipaddress.ip_address(target.hostname)
            except ValueError:
                lookup_host = target.hostname
            else:
                lookup_host = (
                    f"[{address.compressed}]" if address.version == 6 else address.compressed
                )
        result = self._run(
            ["ssh-keygen", "-F", lookup_host, "-f", str(target.known_hosts_file)],
            operation="SSH host key lookup",
            alias=alias,
            timeout=30,
        )
        if not result.stdout.strip():
            raise ValueError("no pinned host key found")
        return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
