import subprocess
from pathlib import Path

import pytest

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
    assert runner.calls[1][:4] == ["ssh", "-F", str(config), "-o"]
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

    assert runner.calls[1][:4] == ["scp", "-s", "-F", str(config)]
    assert "HostName=192.0.2.10" in runner.calls[1]
    assert "GlobalKnownHostsFile=none" in runner.calls[1]
    assert runner.calls[1][-2:] == [
        str(source.resolve()),
        r"192.0.2.10:C:\TTS More\artifacts\model.pt",
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
    assert "gpt-worker" not in argv


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


def test_scp_brackets_validated_ipv6_remote_operand(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    source = tmp_path / "payload.txt"
    source.write_text("payload", encoding="utf-8")
    runner = FakeRunner(
        [completed(resolved_settings(hostname="2001:db8::10")), completed()]
    )

    WindowsSshExecutor(config, runner=runner).copy_to(
        "gpt-worker", source, r"C:\work\payload.txt"
    )

    assert runner.calls[1][-1] == r"[2001:db8::10]:C:\work\payload.txt"
