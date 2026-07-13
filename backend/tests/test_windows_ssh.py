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


def resolved_settings(
    *,
    hostname: str = "tts-gpt.lan",
    user: str = "tester",
    known_hosts: str = "~/.ssh/known_hosts_tts_more",
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

    result = WindowsSshExecutor(config, runner=runner).run_powershell(
        "gpt-worker", "Get-Date"
    )

    assert result.stdout == "ok"
    assert runner.calls[1][:4] == ["ssh", "-F", str(config), "-o"]
    assert "powershell.exe" in runner.calls[1]
    assert "-EncodedCommand" in runner.calls[1]
    assert all(kwargs["shell"] is False for kwargs in runner.kwargs)


@pytest.mark.parametrize("alias", ["", " worker", "worker\nnext", "-oProxyCommand=x"])
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
    ],
)
def test_resolve_rejects_empty_or_unsafe_target_values(
    tmp_path: Path, hostname: str, user: str
) -> None:
    runner = FakeRunner([completed(resolved_settings(hostname=hostname, user=user))])

    with pytest.raises(ValueError):
        WindowsSshExecutor(write_config(tmp_path), runner=runner).resolve("gpt-worker")


@pytest.mark.parametrize("known_hosts", ["", "/", "/dev/null", " /dev/null "])
def test_resolve_rejects_unpinned_known_hosts(tmp_path: Path, known_hosts: str) -> None:
    runner = FakeRunner([completed(resolved_settings(known_hosts=known_hosts))])

    with pytest.raises(ValueError, match="UserKnownHostsFile"):
        WindowsSshExecutor(write_config(tmp_path), runner=runner).resolve("gpt-worker")


def test_resolve_rejects_accept_new_host_key_policy(tmp_path: Path) -> None:
    runner = FakeRunner(
        [completed(resolved_settings().replace("stricthostkeychecking true", "stricthostkeychecking accept-new"))]
    )

    with pytest.raises(ValueError, match="StrictHostKeyChecking yes"):
        WindowsSshExecutor(write_config(tmp_path), runner=runner).resolve("gpt-worker")


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

    WindowsSshExecutor(config, runner=runner).copy_to(
        "gpt-worker", source, r"C:\TTS More\artifacts\model.pt"
    )

    assert runner.calls[1] == [
        "scp",
        "-F",
        str(config),
        str(source),
        r"gpt-worker:C:\TTS More\artifacts\model.pt",
    ]
    assert runner.kwargs[1]["shell"] is False


def test_pinned_host_key_uses_bracketed_ipv6_lookup(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    known_hosts = tmp_path / "known_hosts"
    runner = FakeRunner(
        [
            completed(resolved_settings(hostname="2001:db8::10", known_hosts=str(known_hosts))),
            completed("2001:db8::10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI"),
        ]
    )

    digest = WindowsSshExecutor(config, runner=runner).pinned_host_key_sha256("gpt-worker")

    assert len(digest) == 64
    assert runner.calls[1] == ["ssh-keygen", "-F", "[2001:db8::10]", "-f", str(known_hosts)]


def test_resolve_caches_a_validated_target(tmp_path: Path) -> None:
    runner = FakeRunner([completed(resolved_settings())])
    executor = WindowsSshExecutor(write_config(tmp_path), runner=runner)

    assert executor.resolve("gpt-worker") == executor.resolve("gpt-worker")

    assert len(runner.calls) == 1


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
        WindowsSshExecutor(config, runner=runner).run_powershell("gpt-worker", "Get-Date")

    message = str(raised.value)
    assert message == "SSH execution for gpt-worker failed with exit code 23"
    assert encoded_script not in message
    assert "/private/key" not in message
