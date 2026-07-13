import os
import subprocess
from pathlib import Path

import pytest

import app.windows_ssh as windows_ssh_module
from app.windows_ssh import WindowsSshExecutor


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        return self.responses.pop(0)


class FakeResolver:
    def __init__(self, responses: list[list[str]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, hostname: str) -> list[str]:
        self.calls.append(hostname)
        return self.responses.pop(0)


def resolved_settings(
    *,
    hostname: str = "tts-gpt.lan",
    user: str = "tester",
    known_hosts: str = str(Path(__file__).resolve()),
) -> str:
    return (
        f"hostname {hostname}\nuser {user}\nbatchmode yes\nidentitiesonly yes\n"
        "stricthostkeychecking true\n"
        f"userknownhostsfile {known_hosts}\n"
    )


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def write_config(tmp_path: Path) -> Path:
    config = tmp_path / "ssh_config"
    config.write_text("Host gpt-worker\n", encoding="utf-8")
    return config


def test_validate_target_requires_pinned_strict_config(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    runner = FakeRunner([completed("hostname tts-gpt.lan\nuser tester\nbatchmode no\n")])

    with pytest.raises(ValueError, match="BatchMode yes"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")


def test_run_powershell_uses_encoded_command_without_shell(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    runner = FakeRunner([completed(resolved_settings()), completed("ok")])

    result = WindowsSshExecutor(
        config, runner=runner, resolver=FakeResolver([["192.0.2.10"]])
    ).run_powershell("gpt-worker", "Get-Date")

    assert result.stdout == "ok"
    assert runner.calls[1][:4] == ["ssh", "-F", os.devnull, "-o"]
    assert "powershell.exe" in runner.calls[1]
    assert "-EncodedCommand" in runner.calls[1]
    assert all(kwargs["shell"] is False for kwargs in runner.kwargs)


@pytest.mark.parametrize(
    "alias", ["", " worker", "worker\nnext", "-oProxyCommand=x", "-v", "-F", "-o", ".", ".."]
)
def test_resolve_rejects_unsafe_alias_without_running_ssh(tmp_path: Path, alias: str) -> None:
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="alias"):
        WindowsSshExecutor(write_config(tmp_path), runner=runner).resolve(alias)

    assert runner.calls == []


@pytest.mark.parametrize(
    ("hostname", "user"),
    [
        ("", "tester"),
        ("tts-gpt.lan", ""),
        ("localhost", "tester"),
        ("127.0.0.1", "tester"),
        ("127.1", "tester"),
        ("2130706433", "tester"),
        ("0.0.0.0", "tester"),
        ("::1", "tester"),
        ("[::]", "tester"),
        ("bad host", "tester"),
        ("-host.example", "tester"),
        ("host..example", "tester"),
        ("host/example", "tester"),
        ("host%zone", "tester"),
    ],
)
def test_resolve_rejects_empty_or_unsafe_target_values(
    tmp_path: Path, hostname: str, user: str
) -> None:
    runner = FakeRunner([completed(resolved_settings(hostname=hostname, user=user))])

    with pytest.raises(ValueError):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


@pytest.mark.parametrize("known_hosts", ["", "/", "/dev/null", " /dev/null "])
def test_resolve_rejects_unpinned_known_hosts(tmp_path: Path, known_hosts: str) -> None:
    runner = FakeRunner([completed(resolved_settings(known_hosts=known_hosts))])

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


def test_resolve_rejects_accept_new_host_key_policy(tmp_path: Path) -> None:
    runner = FakeRunner(
        [completed(resolved_settings().replace("stricthostkeychecking true", "stricthostkeychecking accept-new"))]
    )

    with pytest.raises(ValueError, match="StrictHostKeyChecking yes"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


@pytest.mark.parametrize("remote_path", ["-option", "C:\\work\nnext", "C:\\work:ambiguous", "..\\secret", "C:\\work\\..\\secret"])
def test_copy_rejects_ambiguous_or_unsafe_remote_path(tmp_path: Path, remote_path: str) -> None:
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="remote path"):
        WindowsSshExecutor(write_config(tmp_path), runner=runner).copy_to(
            "gpt-worker", tmp_path / "source.txt", remote_path
        )

    assert runner.calls == []


def test_copy_uses_configured_scp_argv_and_safe_windows_path(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    runner = FakeRunner([completed(resolved_settings()), completed()])

    WindowsSshExecutor(
        config, runner=runner, resolver=FakeResolver([["192.0.2.10"]])
    ).copy_to("gpt-worker", source, r"C:\TTS More\artifacts\model.pt")

    assert runner.calls[1][:4] == ["scp", "-s", "-F", os.devnull]
    assert "HostName=192.0.2.10" in runner.calls[1]
    assert "GlobalKnownHostsFile=none" in runner.calls[1]
    assert runner.calls[1][-2:] == [
        str(source.resolve()),
        r"gpt-worker:C:\TTS More\artifacts\model.pt",
    ]
    assert runner.kwargs[1]["shell"] is False


def test_pinned_host_key_uses_bracketed_ipv6_lookup(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("[2001:db8::10] ssh-ed25519 AAAA\n", encoding="utf-8")
    runner = FakeRunner(
        [
            completed(resolved_settings(hostname="2001:db8::10", known_hosts=str(known_hosts))),
            completed("2001:db8::10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI"),
        ]
    )

    digest = WindowsSshExecutor(config, runner=runner).pinned_host_key_sha256("gpt-worker")

    assert len(digest) == 64
    assert runner.calls[1] == ["ssh-keygen", "-F", "[2001:db8::10]", "-f", str(known_hosts)]


def test_resolve_does_not_cache_a_validated_target(tmp_path: Path) -> None:
    runner = FakeRunner([completed(resolved_settings()), completed(resolved_settings())])
    resolver = FakeResolver([["192.0.2.10"], ["192.0.2.10"]])
    executor = WindowsSshExecutor(write_config(tmp_path), runner=runner, resolver=resolver)

    assert executor.resolve("gpt-worker") == executor.resolve("gpt-worker")

    assert len(runner.calls) == 2
    assert resolver.calls == ["tts-gpt.lan", "tts-gpt.lan"]


def test_command_failure_redacts_command_and_sensitive_output(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    encoded_script = "RwBlAHQALQBEAGEAdABlAA=="
    runner = FakeRunner(
        [
            completed(resolved_settings()),
            completed(returncode=23, stderr=f"IdentityFile /private/key {encoded_script}"),
        ]
    )

    with pytest.raises(RuntimeError) as raised:
        WindowsSshExecutor(
            config, runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).run_powershell("gpt-worker", "Get-Date")

    message = str(raised.value)
    assert message == "SSH execution for gpt-worker failed with exit code 23"
    assert encoded_script not in message
    assert "/private/key" not in message


def test_execution_binds_validated_target_and_policy_in_argv(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("tts-gpt.lan ssh-ed25519 AAAA\n", encoding="utf-8")
    runner = FakeRunner(
        [completed(resolved_settings(known_hosts=str(known_hosts))), completed("ok")]
    )
    resolver = FakeResolver([["192.0.2.10"]])

    WindowsSshExecutor(config, runner=runner, resolver=resolver).run_powershell(
        "gpt-worker", "Get-Date"
    )

    argv = runner.calls[1]
    assert "HostName=192.0.2.10" in argv
    assert "HostKeyAlias=tts-gpt.lan" in argv
    assert "User=tester" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert f"UserKnownHostsFile={known_hosts}" in argv
    assert "GlobalKnownHostsFile=none" in argv
    assert "gpt-worker" in argv
    assert str(config) not in argv


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["::1"],
        ["192.0.2.10", "::"],
        ["192.0.2.10", "127.0.0.1"],
    ],
)
def test_resolve_rejects_any_prohibited_dns_answer(
    tmp_path: Path, answers: list[str]
) -> None:
    runner = FakeRunner([completed(resolved_settings())])

    with pytest.raises(ValueError, match="DNS"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([answers])
        ).resolve("gpt-worker")


def test_resolve_accepts_mixed_valid_ipv4_and_ipv6_answers(tmp_path: Path) -> None:
    runner = FakeRunner([completed(resolved_settings())])
    resolver = FakeResolver([["2001:db8::10", "192.0.2.10"]])

    target = WindowsSshExecutor(
        write_config(tmp_path), runner=runner, resolver=resolver
    ).resolve("gpt-worker")

    assert target.address == "2001:db8::10"
    assert resolver.calls == ["tts-gpt.lan"]


def test_each_execution_resolves_again_and_rejects_dns_rebinding(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    runner = FakeRunner(
        [
            completed(resolved_settings()),
            completed("first"),
            completed(resolved_settings()),
        ]
    )
    resolver = FakeResolver([["192.0.2.10"], ["127.0.0.1"]])
    executor = WindowsSshExecutor(config, runner=runner, resolver=resolver)

    assert executor.run_powershell("gpt-worker", "Get-Date").stdout == "first"
    with pytest.raises(ValueError, match="DNS"):
        executor.run_powershell("gpt-worker", "Get-Date")

    assert "HostName=192.0.2.10" in runner.calls[1]
    assert len(runner.calls) == 3


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("proxycommand", "sh -c touch /tmp/pwned"),
        ("proxyjump", "jump-host"),
        ("permitlocalcommand", "yes"),
        ("localcommand", "touch /tmp/pwned"),
    ],
)
def test_resolve_rejects_local_command_and_proxy_routes(
    tmp_path: Path, setting: str, value: str
) -> None:
    runner = FakeRunner([completed(resolved_settings() + f"{setting} {value}\n")])

    with pytest.raises(ValueError, match="proxy|local command"):
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")


def test_execution_forces_proxy_and_local_command_options_off(tmp_path: Path) -> None:
    runner = FakeRunner([completed(resolved_settings()), completed()])

    WindowsSshExecutor(
        write_config(tmp_path),
        runner=runner,
        resolver=FakeResolver([["192.0.2.10"]]),
    ).run_powershell("gpt-worker", "Get-Date")

    argv = runner.calls[1]
    assert "ProxyCommand=none" in argv
    assert "ProxyJump=none" in argv
    assert "PermitLocalCommand=no" in argv
    assert "LocalCommand=none" in argv


@pytest.mark.parametrize("user", ["-root", "user name", "user\tname", "user\nname", "user@example"])
def test_resolve_rejects_malformed_user(tmp_path: Path, user: str) -> None:
    runner = FakeRunner([completed(resolved_settings(user=user))])

    with pytest.raises(ValueError, match="valid user|malformed"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


def test_resolve_rejects_missing_known_hosts_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing_known_hosts"
    runner = FakeRunner([completed(resolved_settings(known_hosts=str(missing)))])

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


def test_resolve_rejects_known_hosts_directory(tmp_path: Path) -> None:
    runner = FakeRunner([completed(resolved_settings(known_hosts=str(tmp_path)))])

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


def test_resolve_rejects_known_hosts_symlink(tmp_path: Path) -> None:
    regular = tmp_path / "known_hosts"
    regular.write_text("host key\n", encoding="utf-8")
    symlink = tmp_path / "known_hosts_link"
    symlink.symlink_to(regular)
    runner = FakeRunner([completed(resolved_settings(known_hosts=str(symlink)))])

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


def test_resolve_rejects_known_hosts_below_symlink_directory(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    (real_directory / "known_hosts").write_text("host key\n", encoding="utf-8")
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    runner = FakeRunner(
        [completed(resolved_settings(known_hosts=str(linked_directory / "known_hosts")))]
    )

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


def test_resolve_rejects_lexically_disguised_null_known_hosts(tmp_path: Path) -> None:
    runner = FakeRunner([completed(resolved_settings(known_hosts="/dev/../dev/null"))])

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(
            write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
        ).resolve("gpt-worker")


def test_resolve_returns_canonical_known_hosts_path(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host key\n", encoding="utf-8")
    (tmp_path / "child").mkdir()
    lexical_path = tmp_path / "child" / ".." / "known_hosts"
    runner = FakeRunner([completed(resolved_settings(known_hosts=str(lexical_path)))])

    target = WindowsSshExecutor(
        write_config(tmp_path), runner=runner, resolver=FakeResolver([["192.0.2.10"]])
    ).resolve("gpt-worker")

    assert target.known_hosts_file == known_hosts.resolve()


@pytest.mark.parametrize(
    "raised",
    [
        subprocess.TimeoutExpired(["ssh", "-EncodedCommand", "SECRET"], 17),
        OSError("cannot launch /private/key SECRET"),
    ],
)
def test_process_exceptions_are_redacted(
    tmp_path: Path, raised: BaseException
) -> None:
    class RaisingRunner:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, argv: list[str], **kwargs):
            self.calls += 1
            if self.calls == 1:
                return completed(resolved_settings())
            raise raised

    with pytest.raises(RuntimeError) as caught:
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=RaisingRunner(),
            resolver=FakeResolver([["192.0.2.10"]]),
        ).run_powershell(
            "gpt-worker", "SECRET"
        )

    message = str(caught.value)
    assert message in {
        "SSH execution for gpt-worker timed out",
        "SSH execution for gpt-worker could not start",
    }
    assert "SECRET" not in message
    assert "/private/key" not in message


def test_scp_forces_sftp_and_absolute_local_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path)
    source = tmp_path / "-payload.txt"
    source.write_text("payload", encoding="utf-8")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host key\n", encoding="utf-8")
    runner = FakeRunner(
        [completed(resolved_settings(known_hosts=str(known_hosts))), completed()]
    )
    monkeypatch.chdir(tmp_path)

    WindowsSshExecutor(
        config, runner=runner, resolver=FakeResolver([["192.0.2.10"]])
    ).copy_to(
        "gpt-worker", Path("-payload.txt"), r"C:\work\payload.txt"
    )

    argv = runner.calls[1]
    assert "-s" in argv
    assert str(source.resolve()) in argv
    assert "-payload.txt" not in argv


def test_scp_download_uses_absolute_local_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host key\n", encoding="utf-8")
    runner = FakeRunner(
        [completed(resolved_settings(known_hosts=str(known_hosts))), completed()]
    )
    monkeypatch.chdir(tmp_path)

    WindowsSshExecutor(
        config, runner=runner, resolver=FakeResolver([["192.0.2.10"]])
    ).copy_from(
        "gpt-worker", r"C:\work\payload.txt", Path("-downloads/result.txt")
    )

    assert str((tmp_path / "-downloads/result.txt").resolve()) in runner.calls[1]


def test_scp_binds_ipv6_address_while_using_safe_alias_operand(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    source = tmp_path / "payload.txt"
    source.write_text("payload", encoding="utf-8")
    runner = FakeRunner(
        [completed(resolved_settings(hostname="2001:db8::10")), completed()]
    )

    WindowsSshExecutor(config, runner=runner).copy_to(
        "gpt-worker", source, r"C:\work\payload.txt"
    )

    assert "HostName=2001:db8::10" in runner.calls[1]
    assert runner.calls[1][-1] == r"gpt-worker:C:\work\payload.txt"


@pytest.mark.parametrize(
    "match_line",
    [
        'Match exec "touch /tmp/pwned"',
        'Match host gpt-worker exec "touch /tmp/pwned"',
        'Match !exec "touch /tmp/pwned"',
        'Match exec="touch /tmp/pwned"',
    ],
)
def test_match_exec_in_root_config_is_rejected_before_runner(
    tmp_path: Path, match_line: str
) -> None:
    config = tmp_path / "ssh_config"
    config.write_text(f"Host gpt-worker\n{match_line}\n", encoding="utf-8")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="Match exec"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


def test_match_exec_in_recursive_include_is_rejected_before_runner(tmp_path: Path) -> None:
    nested = tmp_path / "nested.conf"
    nested.write_text('Match exec "touch /tmp/pwned"\n', encoding="utf-8")
    included = tmp_path / "included.conf"
    included.write_text(f'Include "{nested}"\n', encoding="utf-8")
    config = tmp_path / "ssh_config"
    config.write_text(f'Include "{included}"\nHost gpt-worker\n', encoding="utf-8")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="Match exec"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


def test_relative_include_uses_user_ssh_directory_like_openssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    ssh_directory = home / ".ssh"
    ssh_directory.mkdir(parents=True)
    (ssh_directory / "included.conf").write_text(
        "Host gpt-worker\n  User tester\n", encoding="utf-8"
    )
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    (config_directory / "included.conf").write_text(
        'Match exec "touch /tmp/pwned"\n', encoding="utf-8"
    )
    config = config_directory / "ssh_config"
    config.write_text("Include included.conf\nHost gpt-worker\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    class SnapshotRunner(FakeRunner):
        def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
            snapshot = Path(argv[2]).read_text(encoding="utf-8")
            assert "User tester" in snapshot
            assert "Match exec" not in snapshot
            return super().__call__(argv, **kwargs)

    runner = SnapshotRunner([completed(resolved_settings())])

    WindowsSshExecutor(
        config, runner=runner, resolver=FakeResolver([["192.0.2.10"]])
    ).resolve("gpt-worker")

    assert len(runner.calls) == 1


def test_resolution_and_execution_never_reuse_mutable_original_config(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    class MutatingRunner(FakeRunner):
        def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
            if not self.calls:
                snapshot = Path(argv[2])
                assert snapshot != config
                assert snapshot.read_text(encoding="utf-8") == config.read_text(encoding="utf-8")
                config.write_text(
                    'Match exec "touch /tmp/pwned"\nHost gpt-worker\n'
                    "  StrictHostKeyChecking no\n",
                    encoding="utf-8",
                )
            return super().__call__(argv, **kwargs)

    runner = MutatingRunner([completed(resolved_settings()), completed("ok")])

    result = WindowsSshExecutor(
        config, runner=runner, resolver=FakeResolver([["192.0.2.10"]])
    ).run_powershell("gpt-worker", "Get-Date")

    assert result.stdout == "ok"
    assert runner.calls[1][1:3] == ["-F", os.devnull]
    assert str(config) not in runner.calls[1]


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("controlpath", "/tmp/unsafe-control"),
        ("controlmaster", "auto"),
        ("controlmaster", "yes"),
        ("controlpersist", "yes"),
        ("controlpersist", "60"),
        ("controlpersist", "0"),
    ],
)
def test_resolve_rejects_connection_multiplexing(
    tmp_path: Path, setting: str, value: str
) -> None:
    runner = FakeRunner([completed(resolved_settings() + f"{setting} {value}\n")])

    with pytest.raises(ValueError, match="connection sharing"):
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("knownhostscommand", "touch /tmp/pwned"),
        ("verifyhostkeydns", "yes"),
        ("verifyhostkeydns", "ask"),
    ],
)
def test_resolve_rejects_alternative_host_key_sources(
    tmp_path: Path, setting: str, value: str
) -> None:
    runner = FakeRunner([completed(resolved_settings() + f"{setting} {value}\n")])

    with pytest.raises(ValueError, match="host-key source"):
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")


def test_execution_forces_sharing_and_alternative_host_key_sources_off(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([completed(resolved_settings()), completed()])

    WindowsSshExecutor(
        write_config(tmp_path),
        runner=runner,
        resolver=FakeResolver([["192.0.2.10"]]),
    ).run_powershell("gpt-worker", "Get-Date")

    argv = runner.calls[1]
    assert "ControlPath=none" in argv
    assert "ControlMaster=no" in argv
    assert "ControlPersist=no" in argv
    assert "KnownHostsCommand=none" in argv
    assert "VerifyHostKeyDNS=no" in argv


def test_alias_authentication_semantics_are_bound_across_all_operations(
    tmp_path: Path,
) -> None:
    config = write_config(tmp_path)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("[tts-gpt.lan]:2222 ssh-ed25519 AAAA\n", encoding="utf-8")
    identity_one = tmp_path / "identity_one"
    identity_one.write_text("PRIVATE KEY\n", encoding="utf-8")
    identity_two = tmp_path / "identity_two"
    identity_two.write_text("PRIVATE KEY\n", encoding="utf-8")
    certificate = tmp_path / "identity-cert.pub"
    certificate.write_text("ssh-ed25519-cert-v01@openssh.com AAAA\n", encoding="utf-8")
    settings = resolved_settings(known_hosts=str(known_hosts)) + (
        f"port 2222\nidentityfile {identity_one}\nidentityfile {identity_two}\n"
        f"certificatefile {certificate}\n"
    )
    runner = FakeRunner(
        [
            completed(settings),
            completed("ok"),
            completed(settings),
            completed(),
            completed(settings),
            completed(),
            completed(settings),
            completed("[tts-gpt.lan]:2222 ssh-ed25519 AAAA"),
        ]
    )
    resolver = FakeResolver([["192.0.2.10"] for _ in range(4)])
    executor = WindowsSshExecutor(config, runner=runner, resolver=resolver)
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")

    executor.run_powershell("gpt-worker", "Get-Date")
    executor.copy_to("gpt-worker", source, r"C:\work\source.txt")
    executor.copy_from("gpt-worker", r"C:\work\result.txt", tmp_path / "result.txt")
    executor.pinned_host_key_sha256("gpt-worker")

    for call_index in (1, 3, 5):
        argv = runner.calls[call_index]
        assert argv[1:3] == ["-F", os.devnull] or argv[1:4] == ["-s", "-F", os.devnull]
        assert "Port=2222" in argv
        assert ["-i", str(identity_one)] == argv[
            argv.index(str(identity_one)) - 1 : argv.index(str(identity_one)) + 1
        ]
        assert ["-i", str(identity_two)] == argv[
            argv.index(str(identity_two)) - 1 : argv.index(str(identity_two)) + 1
        ]
        assert f"CertificateFile={certificate}" in argv
        assert any(
            argument == "gpt-worker" or argument.startswith("gpt-worker:")
            for argument in argv
        )
    assert runner.calls[3][-1] == r"gpt-worker:C:\work\source.txt"
    assert runner.calls[5][-2] == r"gpt-worker:C:\work\result.txt"
    assert runner.calls[7] == [
        "ssh-keygen",
        "-F",
        "[tts-gpt.lan]:2222",
        "-f",
        str(known_hosts),
    ]


@pytest.mark.parametrize("port", ["0", "65536", "-1", "22x"])
def test_resolve_rejects_invalid_port(tmp_path: Path, port: str) -> None:
    runner = FakeRunner([completed(resolved_settings() + f"port {port}\n")])

    with pytest.raises(ValueError, match="valid port"):
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")


@pytest.mark.parametrize(
    "auth_setting",
    [
        "identityfile %d/.ssh/worker_key",
        "certificatefile %d/.ssh/worker-cert.pub",
        "certificatefile /definitely/missing/worker-cert.pub",
    ],
)
def test_resolve_rejects_unresolved_or_missing_auth_files(
    tmp_path: Path, auth_setting: str
) -> None:
    runner = FakeRunner([completed(resolved_settings() + auth_setting + "\n")])

    with pytest.raises(ValueError, match="IdentityFile|CertificateFile"):
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")


def test_bare_carriage_return_is_rejected_before_runner(tmp_path: Path) -> None:
    config = tmp_path / "ssh_config"
    config.write_bytes(b'Host gpt-worker\nMatch\r exec "touch /tmp/pwned"\n')
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="control character"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


def test_include_fragments_cannot_synthesize_match_across_eof(tmp_path: Path) -> None:
    first = tmp_path / "first.conf"
    first.write_text("Match", encoding="utf-8")
    second = tmp_path / "second.conf"
    second.write_text(' exec "touch /tmp/pwned"\n', encoding="utf-8")
    config = tmp_path / "ssh_config"
    config.write_text(f'Include "{first}" "{second}"\n', encoding="utf-8")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="Match"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


@pytest.mark.parametrize(
    "match_line",
    [
        "Match all",
        "Match command powershell.exe",
        "Match sessiontype exec",
        "Match sessiontype subsystem",
    ],
)
def test_all_match_directives_are_rejected_before_runner(
    tmp_path: Path, match_line: str
) -> None:
    config = tmp_path / "ssh_config"
    config.write_text(f"Host gpt-worker\n{match_line}\n", encoding="utf-8")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="Match"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


def test_fifo_include_is_rejected_before_open_or_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "unsafe.conf"
    os.mkfifo(fifo)
    config = tmp_path / "ssh_config"
    config.write_text(f'Include "{fifo}"\nHost gpt-worker\n', encoding="utf-8")
    runner = FakeRunner([])
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path == fifo:
            raise AssertionError("FIFO was opened before type validation")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with pytest.raises(ValueError, match="regular file"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


def test_config_single_file_size_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_ssh_module, "_MAX_CONFIG_FILE_BYTES", 32, raising=False)
    config = tmp_path / "ssh_config"
    config.write_text("Host gpt-worker\n" + "#" * 64 + "\n", encoding="utf-8")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="size limit"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


def test_config_total_byte_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_ssh_module, "_MAX_CONFIG_FILE_BYTES", 256, raising=False)
    monkeypatch.setattr(windows_ssh_module, "_MAX_CONFIG_TOTAL_BYTES", 100, raising=False)
    included = tmp_path / "included.conf"
    included.write_text("#" * 80 + "\n", encoding="utf-8")
    config = tmp_path / "ssh_config"
    config.write_text(f'Include "{included}"\nHost gpt-worker\n', encoding="utf-8")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="total byte limit"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


def test_config_file_count_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_ssh_module, "_MAX_CONFIG_FILES", 2, raising=False)
    first = tmp_path / "first.conf"
    first.write_text("# first\n", encoding="utf-8")
    second = tmp_path / "second.conf"
    second.write_text("# second\n", encoding="utf-8")
    config = tmp_path / "ssh_config"
    config.write_text(f'Include "{first}" "{second}"\n', encoding="utf-8")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="file count limit"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []


@pytest.mark.parametrize("setting", ["identityfile", "certificatefile"])
@pytest.mark.parametrize(
    "filename",
    ["worker key", "worker\tkey", 'worker"key', "worker\\key"],
)
def test_auth_paths_with_unsafe_config_characters_are_rejected(
    tmp_path: Path, setting: str, filename: str
) -> None:
    auth_file = tmp_path / filename
    auth_file.write_text("key material\n", encoding="utf-8")
    runner = FakeRunner([completed(resolved_settings() + f"{setting} {auth_file}\n")])

    with pytest.raises(ValueError, match="IdentityFile|CertificateFile"):
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")


def test_generated_auth_arguments_parse_with_real_openssh(tmp_path: Path) -> None:
    identity = tmp_path / "worker_key"
    identity.write_text("PRIVATE KEY\n", encoding="utf-8")
    certificate = tmp_path / "worker-cert.pub"
    certificate.write_text("CERTIFICATE\n", encoding="utf-8")
    settings = resolved_settings() + (
        f"identityfile {identity}\ncertificatefile {certificate}\n"
    )
    runner = FakeRunner([completed(settings), completed()])

    WindowsSshExecutor(
        write_config(tmp_path),
        runner=runner,
        resolver=FakeResolver([["192.0.2.10"]]),
    ).run_powershell("gpt-worker", "Get-Date")

    generated = runner.calls[1]
    alias_index = generated.index("gpt-worker")
    parse_argv = [*generated[:alias_index], "-G", "gpt-worker"]
    parsed = subprocess.run(
        parse_argv,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    assert parsed.returncode == 0, parsed.stderr
    assert f"identityfile {identity}" in parsed.stdout
    assert f"certificatefile {certificate}" in parsed.stdout


def test_known_hosts_rejects_percent_token_after_first_openssh_expansion(
    tmp_path: Path,
) -> None:
    literal_directory = tmp_path / "%h"
    literal_directory.mkdir()
    literal_known_hosts = literal_directory / "known_hosts"
    literal_known_hosts.write_text("tts-gpt.lan ssh-ed25519 AAAA\n", encoding="utf-8")
    config = tmp_path / "ssh_config"
    config.write_text(
        "Host gpt-worker\n"
        f"  UserKnownHostsFile {tmp_path}/%%h/known_hosts\n",
        encoding="utf-8",
    )
    runner = FakeRunner(
        [completed(resolved_settings(known_hosts=str(literal_known_hosts)))]
    )

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(
            config,
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")

    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "parent_name", ["parent%h", "parent${HOME}", "parent space", 'parent"quote', "parent\\slash"]
)
@pytest.mark.parametrize(
    ("setting", "error"),
    [
        ("userknownhostsfile", "UserKnownHostsFile"),
        ("identityfile", "IdentityFile"),
        ("certificatefile", "CertificateFile"),
    ],
)
def test_canonical_parent_cannot_introduce_unsafe_path_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_name: str,
    setting: str,
    error: str,
) -> None:
    unsafe_parent = tmp_path / parent_name
    unsafe_parent.mkdir()
    filename = "known_hosts" if setting == "userknownhostsfile" else "worker_key"
    (unsafe_parent / filename).write_text("key material\n", encoding="utf-8")
    monkeypatch.chdir(unsafe_parent)
    output = (
        resolved_settings(known_hosts=filename)
        if setting == "userknownhostsfile"
        else resolved_settings() + f"{setting} {filename}\n"
    )
    runner = FakeRunner([completed(output)])

    with pytest.raises(ValueError, match=error):
        WindowsSshExecutor(
            write_config(tmp_path),
            runner=runner,
            resolver=FakeResolver([["192.0.2.10"]]),
        ).resolve("gpt-worker")


def test_safe_known_hosts_literal_matches_ssh_and_keygen_argv(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("tts-gpt.lan ssh-ed25519 AAAA\n", encoding="utf-8")
    settings = resolved_settings(known_hosts=str(known_hosts))
    runner = FakeRunner(
        [
            completed(settings),
            completed(),
            completed(settings),
            completed("tts-gpt.lan ssh-ed25519 AAAA"),
        ]
    )
    executor = WindowsSshExecutor(
        write_config(tmp_path),
        runner=runner,
        resolver=FakeResolver([["192.0.2.10"], ["192.0.2.10"]]),
    )

    executor.run_powershell("gpt-worker", "Get-Date")
    executor.pinned_host_key_sha256("gpt-worker")

    canonical = str(known_hosts.resolve())
    assert f"UserKnownHostsFile={canonical}" in runner.calls[1]
    assert runner.calls[3][-1] == canonical


def test_tab_indentation_is_accepted(tmp_path: Path) -> None:
    config = tmp_path / "ssh_config"
    config.write_bytes(b"Host gpt-worker\n\tUser tester\n")
    runner = FakeRunner([completed(resolved_settings())])

    target = WindowsSshExecutor(
        config,
        runner=runner,
        resolver=FakeResolver([["192.0.2.10"]]),
    ).resolve("gpt-worker")

    assert target.user == "tester"


def test_crlf_is_normalized_to_lf_before_snapshot(tmp_path: Path) -> None:
    config = tmp_path / "ssh_config"
    config.write_bytes(b"Host gpt-worker\r\n\tUser tester\r\n")

    class LfSnapshotRunner(FakeRunner):
        def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
            snapshot = Path(argv[2]).read_bytes()
            assert b"\r" not in snapshot
            assert snapshot == b"Host gpt-worker\n\tUser tester\n"
            return super().__call__(argv, **kwargs)

    runner = LfSnapshotRunner([completed(resolved_settings())])

    WindowsSshExecutor(
        config,
        runner=runner,
        resolver=FakeResolver([["192.0.2.10"]]),
    ).resolve("gpt-worker")

    assert len(runner.calls) == 1


@pytest.mark.parametrize("control", [b"\x00", b"\x01", b"\x0b", b"\x7f"])
def test_non_whitespace_config_controls_remain_rejected(
    tmp_path: Path, control: bytes
) -> None:
    config = tmp_path / "ssh_config"
    config.write_bytes(b"Host gpt-worker\n" + control + b"User tester\n")
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="control character"):
        WindowsSshExecutor(config, runner=runner).resolve("gpt-worker")

    assert runner.calls == []
