from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.comfyui import reliability_evidence as evidence


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "app.comfyui.reliability_evidence_cli",
            *arguments,
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
        check=False,
    )


def _make_windows_junction(junction: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("real junction behavior is Windows-specific")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _wait_for_path(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"process exited before readiness: {process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            process.communicate()
            pytest.fail("process did not reach readiness gate")
        time.sleep(0.01)


def _assert_process_blocked(process: subprocess.Popen[str]) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0.75)


def test_snapshot_current_reports_pointer_absence_as_legacy_eligible(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()

    completed = _run_cli("snapshot-current", "--output-root", str(output_root))

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "ok": True,
        "snapshot": {
            "legacy_eligible": True,
            "status": "absent",
            "token": "absent",
        },
    }


def _valid_pointer() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "reliability-current-terminal",
        "run_key": "a" * 64,
        "mode": "preflight",
        "outcome": "passed",
        "terminal_size_bytes": 321,
        "terminal_sha256": "b" * 64,
        "previous_pointer_sha256": None,
    }


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def test_snapshot_current_validates_pointer_and_hashes_its_exact_bytes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    pointer = _valid_pointer()
    raw_pointer = _canonical_json(pointer)
    (output_root / "current-terminal.json").write_bytes(raw_pointer)

    completed = _run_cli("snapshot-current", "--output-root", str(output_root))

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "ok": True,
        "snapshot": {
            "legacy_eligible": False,
            "pointer": pointer,
            "status": "valid",
            "token": hashlib.sha256(raw_pointer).hexdigest(),
        },
    }


def test_snapshot_current_rejects_semantically_valid_noncanonical_pointer_bytes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    noncanonical = json.dumps(_valid_pointer(), indent=2).encode("utf-8")
    (output_root / "current-terminal.json").write_bytes(noncanonical)

    completed = _run_cli("snapshot-current", "--output-root", str(output_root))

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "error": {"code": "evidence-store-error"},
        "ok": False,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"terminal_size_bytes": True},
        {"terminal_size_bytes": evidence.MAX_TERMINAL_BYTES + 1},
        {"run_key": "A" * 64},
        {"private_path": "C:/private/model.bin"},
    ],
)
def test_snapshot_current_rejects_invalid_present_pointer_without_legacy_fallback(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    pointer = _valid_pointer() | mutation
    (output_root / "current-terminal.json").write_bytes(_canonical_json(pointer))

    completed = _run_cli("snapshot-current", "--output-root", str(output_root))

    assert completed.returncode != 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "error": {"code": "evidence-store-error"},
        "ok": False,
    }
    assert "legacy" not in (completed.stdout + completed.stderr).lower()


def test_snapshot_current_does_not_echo_private_invalid_pointer_values(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    secret = "PRIVATE-TOKEN-DO-NOT-PRINT"
    pointer = _valid_pointer() | {
        "private_path": "C:/private/model.bin",
        "token": secret,
    }
    (output_root / "current-terminal.json").write_bytes(_canonical_json(pointer))

    completed = _run_cli("snapshot-current", "--output-root", str(output_root))

    assert completed.returncode != 0
    assert json.loads(completed.stdout) == {
        "error": {"code": "evidence-store-error"},
        "ok": False,
    }
    combined = completed.stdout + completed.stderr
    assert secret not in combined
    assert str(output_root) not in combined


@pytest.mark.parametrize(
    ("kind", "name", "relative_name"),
    [
        ("supervisor", None, "supervisor.json"),
        ("run-result", None, "run-result.json"),
        ("preflight", None, "preflight.json"),
        ("failure", None, "failure.json"),
        ("summary", None, "reliability-summary.json"),
        ("case", "steady-01", "cases/steady-01.json"),
        ("audio", "steady-01", "audio/steady-01.wav"),
        ("log", "validator", "logs/validator.log"),
    ],
)
def test_write_artifact_uses_only_fixed_derived_run_relative_names(
    tmp_path: Path,
    kind: str,
    name: str | None,
    relative_name: str,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    run_key = "1" * 64
    payload = b'{"status":"passed"}\n'

    commitment = evidence.write_artifact(
        output_root,
        run_key,
        kind,
        payload,
        name=name,
    )

    assert commitment.model_dump(mode="json") == {
        "relative_name": relative_name,
        "size_bytes": 20,
        "sha256": "b215ff190a08ad5dcf98fbe6ee599c5a3a2b5b7034c6b526eeeaa887f67ae648",
    }
    assert (output_root / "runs" / run_key / Path(relative_name)).read_bytes() == payload


def test_write_artifact_replay_is_idempotent_but_different_bytes_never_overwrite(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    run_key = "2" * 64
    original = b'{"status":"passed"}\n'

    first = evidence.write_artifact(output_root, run_key, "preflight", original)
    replay = evidence.write_artifact(output_root, run_key, "preflight", original)
    with pytest.raises(evidence.EvidenceStoreError, match="artifact conflict"):
        evidence.write_artifact(output_root, run_key, "preflight", b"changed")

    assert replay == first
    assert (output_root / "runs" / run_key / "preflight.json").read_bytes() == original


@pytest.mark.parametrize("unsafe_name", ["../private", "nested/private", "PRIVATE-TOKEN"])
def test_write_artifact_rejects_unsafe_names_without_path_escape_or_echo(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"outside-safe")

    with pytest.raises(evidence.EvidenceStoreError) as captured:
        evidence.write_artifact(
            output_root,
            "3" * 64,
            "log",
            b"private",
            name=unsafe_name,
        )

    assert unsafe_name not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)
    assert sentinel.read_bytes() == b"outside-safe"


def _commitment(relative_name: str) -> dict[str, object]:
    return {
        "relative_name": relative_name,
        "size_bytes": 20,
        "sha256": "b215ff190a08ad5dcf98fbe6ee599c5a3a2b5b7034c6b526eeeaa887f67ae648",
    }


def _preflight_passed_terminal(run_key: str = "4" * 64) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "reliability-run-terminal",
        "run_key": run_key,
        "mode": "preflight",
        "outcome": "passed",
        "failure_source": "none",
        "evidence_complete": True,
        "launcher_exit_code": 0,
        "validator_exit_code": 0,
        "cleanup_status": "completed",
        "preflight": _commitment("preflight.json"),
        "failure": None,
        "summary": None,
        "cases": [],
        "artifacts": [
            _commitment("logs/validator.log"),
            _commitment("run-result.json"),
            _commitment("supervisor.json"),
        ],
    }


def test_run_terminal_accepts_complete_strict_preflight_success() -> None:
    payload = _preflight_passed_terminal()

    terminal = evidence.RunTerminal.model_validate(payload, strict=True)

    assert terminal.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "mutation",
    [
        {"launcher_exit_code": True},
        {"launcher_exit_code": 2_147_483_648},
        {"validator_exit_code": -2_147_483_649},
        {"validator_exit_code": None},
        {"launcher_exit_code": 7},
        {"evidence_complete": False},
        {"cleanup_status": "failed"},
        {"failure_source": "validator"},
    ],
)
def test_run_terminal_rejects_invalid_preflight_success_contract(
    mutation: dict[str, object],
) -> None:
    payload = _preflight_passed_terminal() | mutation

    with pytest.raises(ValidationError):
        evidence.RunTerminal.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "failure_source,validator_exit,cleanup_status",
    [
        ("none", None, "completed"),
        ("launcher", 0, "completed"),
        ("validator", None, "completed"),
        ("validator", 0, "completed"),
        ("cleanup", 0, "completed"),
    ],
)
def test_run_terminal_rejects_incoherent_failed_exit_contract(
    failure_source: str,
    validator_exit: int | None,
    cleanup_status: str,
) -> None:
    payload = _preflight_passed_terminal() | {
        "outcome": "failed",
        "failure_source": failure_source,
        "launcher_exit_code": 7,
        "validator_exit_code": validator_exit,
        "cleanup_status": cleanup_status,
        "preflight": None,
        "failure": _commitment("failure.json"),
    }

    with pytest.raises(ValidationError):
        evidence.RunTerminal.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    ("failure_source", "launcher_exit", "validator_exit", "cleanup_status"),
    [
        ("launcher", -1_073_741_819, None, "completed"),
        ("validator", 7, 7, "completed"),
        ("cleanup", 7, 0, "failed"),
    ],
)
def test_run_terminal_accepts_each_complete_failed_terminal_class(
    failure_source: str,
    launcher_exit: int,
    validator_exit: int | None,
    cleanup_status: str,
) -> None:
    payload = _preflight_passed_terminal() | {
        "outcome": "failed",
        "failure_source": failure_source,
        "launcher_exit_code": launcher_exit,
        "validator_exit_code": validator_exit,
        "cleanup_status": cleanup_status,
        "preflight": None,
        "failure": _commitment("failure.json"),
    }

    terminal = evidence.RunTerminal.model_validate(payload, strict=True)

    assert terminal.outcome == "failed"
    assert terminal.failure_source == failure_source


@pytest.mark.parametrize(
    "mutation",
    [
        {"preflight": _commitment("failure.json")},
        {
            "artifacts": [
                _commitment("supervisor.json"),
                _commitment("run-result.json"),
            ]
        },
        {
            "artifacts": [
                _commitment("logs/validator.log"),
                _commitment("logs/validator.log"),
            ]
        },
    ],
)
def test_run_terminal_rejects_wrong_role_or_noncanonical_commitment_order(
    mutation: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        evidence.RunTerminal.model_validate(
            _preflight_passed_terminal() | mutation,
            strict=True,
        )


def test_run_terminal_rejects_and_hides_private_extra_values() -> None:
    secret = "PRIVATE-RESOURCE-ID-TOKEN"
    payload = _preflight_passed_terminal() | {
        "resource_id": secret,
        "registry_path": "C:/private/registry.yaml",
        "command": "python private-model.py --token SECRET",
    }

    with pytest.raises(ValidationError) as captured:
        evidence.RunTerminal.model_validate(payload, strict=True)

    assert secret not in str(captured.value)
    assert "C:/private/registry.yaml" not in str(captured.value)


def _write_preflight_passed_run(
    output_root: Path,
    run_key: str = "5" * 64,
) -> evidence.RunTerminal:
    preflight = evidence.write_artifact(
        output_root,
        run_key,
        "preflight",
        b'{"status":"passed"}\n',
    )
    lifecycle = [
        evidence.write_artifact(output_root, run_key, "supervisor", b"supervisor\n"),
        evidence.write_artifact(output_root, run_key, "run-result", b"result\n"),
        evidence.write_artifact(
            output_root,
            run_key,
            "log",
            b"validator\n",
            name="validator",
        ),
    ]
    return evidence.RunTerminal.model_validate(
        _preflight_passed_terminal(run_key)
        | {
            "preflight": preflight.model_dump(mode="json"),
            "artifacts": [
                item.model_dump(mode="json")
                for item in sorted(lifecycle, key=lambda item: item.relative_name)
            ],
        },
        strict=True,
    )


def _write_preflight_failed_run(
    output_root: Path,
    run_key: str = "6" * 64,
) -> evidence.RunTerminal:
    failure = evidence.write_artifact(
        output_root,
        run_key,
        "failure",
        b'{"code":"validator-failed"}\n',
    )
    lifecycle = [
        evidence.write_artifact(output_root, run_key, "supervisor", b"supervisor\n"),
        evidence.write_artifact(output_root, run_key, "run-result", b"result\n"),
    ]
    payload = _preflight_passed_terminal(run_key) | {
        "outcome": "failed",
        "failure_source": "validator",
        "launcher_exit_code": 7,
        "validator_exit_code": 7,
        "preflight": None,
        "failure": failure.model_dump(mode="json"),
        "artifacts": [
            item.model_dump(mode="json")
            for item in sorted(lifecycle, key=lambda item: item.relative_name)
        ],
    }
    return evidence.RunTerminal.model_validate(payload, strict=True)


def _write_matrix_passed_run(
    output_root: Path,
    run_key: str = "7" * 64,
) -> evidence.RunTerminal:
    preflight = evidence.write_artifact(
        output_root,
        run_key,
        "preflight",
        b'{"status":"passed"}\n',
    )
    summary = evidence.write_artifact(
        output_root,
        run_key,
        "summary",
        b'{"status":"passed"}\n',
    )
    case = evidence.write_artifact(
        output_root,
        run_key,
        "case",
        b'{"case":"steady-01"}\n',
        name="steady-01",
    )
    lifecycle = [
        evidence.write_artifact(output_root, run_key, "supervisor", b"supervisor\n"),
        evidence.write_artifact(output_root, run_key, "run-result", b"result\n"),
        evidence.write_artifact(
            output_root,
            run_key,
            "audio",
            b"RIFF-safe-audio",
            name="steady-01",
        ),
        evidence.write_artifact(
            output_root,
            run_key,
            "log",
            b"validator\n",
            name="validator",
        ),
    ]
    payload = _preflight_passed_terminal(run_key) | {
        "mode": "matrix",
        "preflight": preflight.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
        "cases": [case.model_dump(mode="json")],
        "artifacts": [
            item.model_dump(mode="json")
            for item in sorted(lifecycle, key=lambda item: item.relative_name)
        ],
    }
    return evidence.RunTerminal.model_validate(payload, strict=True)


def test_write_terminal_first_writes_canonical_bytes_and_verify_run_rechecks_all(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root)
    expected_bytes = _canonical_json(terminal.model_dump(mode="json"))

    committed = evidence.write_terminal(output_root, terminal)
    replay = evidence.write_terminal(output_root, terminal)
    verified = evidence.verify_run(output_root, terminal.run_key)

    assert committed.model_dump(mode="json") == {
        "size_bytes": len(expected_bytes),
        "sha256": hashlib.sha256(expected_bytes).hexdigest(),
    }
    assert replay == committed
    assert (
        output_root / "runs" / terminal.run_key / "terminal.json"
    ).read_bytes() == expected_bytes
    assert verified.status == "verified"
    assert verified.run_key == terminal.run_key
    assert verified.terminal == terminal
    assert verified.terminal_size_bytes == len(expected_bytes)
    assert verified.terminal_sha256 == hashlib.sha256(expected_bytes).hexdigest()


def test_terminal_freeze_rejects_later_artifact_and_never_overwrites_terminal(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root)
    evidence.write_terminal(output_root, terminal)
    terminal_path = output_root / "runs" / terminal.run_key / "terminal.json"
    original_terminal = terminal_path.read_bytes()

    with pytest.raises(evidence.EvidenceStoreError, match="run is already frozen"):
        evidence.write_artifact(
            output_root,
            terminal.run_key,
            "log",
            b"late",
            name="late",
        )
    terminal_path.write_bytes(b"tampered-terminal")
    with pytest.raises(evidence.EvidenceStoreError, match="terminal conflict"):
        evidence.write_terminal(output_root, terminal)

    assert terminal_path.read_bytes() == b"tampered-terminal"
    assert original_terminal != b"tampered-terminal"


def test_frozen_run_rejects_late_artifact_before_creating_missing_parent(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "a" * 64)
    evidence.write_terminal(output_root, terminal)
    evidence.compare_and_swap_current(
        output_root,
        terminal.run_key,
        expected_token="absent",
    )
    run_root = output_root / "runs" / terminal.run_key
    audio_parent = run_root / "audio"
    assert not audio_parent.exists()
    tracked_paths = sorted(
        [path for path in run_root.rglob("*") if path.is_file()]
        + [output_root / "current-terminal.json"],
        key=lambda path: str(path),
    )
    before = {
        path.relative_to(output_root).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in tracked_paths
    }

    with pytest.raises(evidence.EvidenceStoreError, match="run is already frozen"):
        evidence.write_artifact(
            output_root,
            terminal.run_key,
            "audio",
            b"RIFF-late-audio",
            name="late",
        )

    assert not audio_parent.exists()
    after = {
        path.relative_to(output_root).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in tracked_paths
    }
    assert after == before
    assert evidence.verify_run(output_root, terminal.run_key).status == "verified"
    assert evidence.verify_current(output_root).pointer.run_key == terminal.run_key


@pytest.mark.parametrize(
    "relative_name",
    [
        "preflight.json",
        "reliability-summary.json",
        "cases/steady-01.json",
        "audio/steady-01.wav",
        "logs/validator.log",
    ],
)
def test_verify_run_rejects_committed_matrix_member_replacement(
    tmp_path: Path,
    relative_name: str,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_matrix_passed_run(output_root)
    evidence.write_terminal(output_root, terminal)
    target = output_root / "runs" / terminal.run_key / Path(relative_name)
    original = target.read_bytes()
    target.write_bytes(b"X" * len(original))

    with pytest.raises(evidence.EvidenceStoreError, match="commitment mismatch"):
        evidence.verify_run(output_root, terminal.run_key)


def test_verify_run_rejects_committed_failure_replacement(tmp_path: Path) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_failed_run(output_root)
    evidence.write_terminal(output_root, terminal)
    failure = output_root / "runs" / terminal.run_key / "failure.json"
    failure.write_bytes(b"X" * failure.stat().st_size)

    with pytest.raises(evidence.EvidenceStoreError, match="commitment mismatch"):
        evidence.verify_run(output_root, terminal.run_key)


@pytest.mark.parametrize("drift", ["extra-file", "extra-directory", "missing"])
def test_verify_run_rejects_exact_recursive_membership_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root)
    evidence.write_terminal(output_root, terminal)
    run_root = output_root / "runs" / terminal.run_key
    if drift == "extra-file":
        (run_root / "extra.txt").write_bytes(b"extra")
    elif drift == "extra-directory":
        (run_root / "empty-extra").mkdir()
    else:
        (run_root / "logs" / "validator.log").unlink()

    with pytest.raises(evidence.EvidenceStoreError, match="membership mismatch"):
        evidence.verify_run(output_root, terminal.run_key)


def test_verify_run_rejects_terminal_tampering_and_invalid_schema(tmp_path: Path) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root)
    evidence.write_terminal(output_root, terminal)
    terminal_path = output_root / "runs" / terminal.run_key / "terminal.json"
    payload = json.loads(terminal_path.read_bytes())
    payload["private_model_path"] = "C:/private/model.bin"
    terminal_path.write_bytes(_canonical_json(payload))

    with pytest.raises(evidence.EvidenceStoreError) as captured:
        evidence.verify_run(output_root, terminal.run_key)

    assert "C:/private/model.bin" not in str(captured.value)


def test_verify_run_rejects_semantically_valid_noncanonical_terminal_bytes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root)
    evidence.write_terminal(output_root, terminal)
    terminal_path = output_root / "runs" / terminal.run_key / "terminal.json"
    terminal_path.write_bytes(
        json.dumps(terminal.model_dump(mode="json"), indent=2).encode("utf-8")
    )

    with pytest.raises(evidence.EvidenceStoreError, match="terminal is invalid"):
        evidence.verify_run(output_root, terminal.run_key)


def _write_terminal_source(path: Path, terminal: evidence.RunTerminal) -> None:
    path.write_bytes(_canonical_json(terminal.model_dump(mode="json")))


def test_pointer_cas_publishes_verified_run_and_verify_current_binds_exact_terminal(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "8" * 64)
    terminal_commitment = evidence.write_terminal(output_root, terminal)
    before = evidence.snapshot_current(output_root)

    pointer = evidence.compare_and_swap_current(
        output_root,
        terminal.run_key,
        expected_token="absent",
    )
    current = evidence.verify_current(output_root)
    raw_pointer = (output_root / "current-terminal.json").read_bytes()

    assert before == {
        "legacy_eligible": True,
        "status": "absent",
        "token": "absent",
    }
    assert pointer.model_dump(mode="json") == {
        "schema_version": 1,
        "kind": "reliability-current-terminal",
        "run_key": terminal.run_key,
        "mode": "preflight",
        "outcome": "passed",
        "terminal_size_bytes": terminal_commitment.size_bytes,
        "terminal_sha256": terminal_commitment.sha256,
        "previous_pointer_sha256": None,
    }
    assert raw_pointer == _canonical_json(pointer.model_dump(mode="json"))
    assert current.status == "current"
    assert current.token == hashlib.sha256(raw_pointer).hexdigest()
    assert current.pointer == pointer
    assert current.run.terminal == terminal


def test_stale_pointer_cas_loses_without_removing_either_run(tmp_path: Path) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    first = _write_preflight_passed_run(output_root, "9" * 64)
    second = _write_preflight_passed_run(output_root, "a" * 64)
    evidence.write_terminal(output_root, first)
    evidence.write_terminal(output_root, second)

    evidence.compare_and_swap_current(output_root, first.run_key, expected_token="absent")
    with pytest.raises(evidence.EvidenceStoreError, match="compare-and-swap conflict"):
        evidence.compare_and_swap_current(
            output_root,
            second.run_key,
            expected_token="absent",
        )

    assert evidence.verify_current(output_root).pointer.run_key == first.run_key
    assert evidence.verify_run(output_root, first.run_key).status == "verified"
    assert evidence.verify_run(output_root, second.run_key).status == "verified"


def test_successive_pointer_cas_commits_exact_previous_pointer_hash(tmp_path: Path) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    first = _write_preflight_passed_run(output_root, "5" * 64)
    second = _write_preflight_passed_run(output_root, "6" * 64)
    evidence.write_terminal(output_root, first)
    evidence.write_terminal(output_root, second)
    evidence.compare_and_swap_current(output_root, first.run_key, expected_token="absent")
    first_snapshot = evidence.snapshot_current(output_root)

    pointer = evidence.compare_and_swap_current(
        output_root,
        second.run_key,
        expected_token=first_snapshot["token"],
    )

    assert pointer.previous_pointer_sha256 == first_snapshot["token"]
    assert evidence.verify_current(output_root).pointer.run_key == second.run_key


def test_independent_crashes_before_terminal_and_before_pointer_preserve_old_current(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    old = _write_preflight_passed_run(output_root, "b" * 64)
    evidence.write_terminal(output_root, old)
    evidence.compare_and_swap_current(output_root, old.run_key, expected_token="absent")
    old_pointer = (output_root / "current-terminal.json").read_bytes()

    before_terminal_key = "c" * 64
    crash_before_terminal = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; from pathlib import Path; "
                "from app.comfyui.reliability_evidence import write_artifact; "
                "write_artifact(Path(sys.argv[1]),sys.argv[2],'log',b'crash\\n',name='crash'); "
                "os._exit(91)"
            ),
            str(output_root),
            before_terminal_key,
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert crash_before_terminal.returncode == 91
    assert not (output_root / "runs" / before_terminal_key / "terminal.json").exists()
    assert (output_root / "current-terminal.json").read_bytes() == old_pointer

    before_pointer = _write_preflight_passed_run(output_root, "d" * 64)
    source = tmp_path / "terminal-source.json"
    _write_terminal_source(source, before_pointer)
    crash_before_pointer = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; from pathlib import Path; "
                "from app.comfyui.reliability_evidence import RunTerminal,write_terminal; "
                "terminal=RunTerminal.model_validate_json(Path(sys.argv[2]).read_bytes(),strict=True); "
                "write_terminal(Path(sys.argv[1]),terminal); os._exit(92)"
            ),
            str(output_root),
            str(source),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert crash_before_pointer.returncode == 92
    assert evidence.verify_run(output_root, before_pointer.run_key).status == "verified"
    assert (output_root / "current-terminal.json").read_bytes() == old_pointer


def test_cross_process_freeze_holds_run_lock_until_terminal_publication(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "7" * 64)
    source = tmp_path / "terminal-source.json"
    _write_terminal_source(source, terminal)
    freeze_ready = tmp_path / "freeze-ready"
    freeze_release = tmp_path / "freeze-release"
    writer_started = tmp_path / "writer-started"
    freezer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; from pathlib import Path; "
                "from app.comfyui import reliability_evidence as e; "
                "root,source,ready,release=map(Path,sys.argv[1:5]); "
                "original=e._first_write_terminal; "
                "exec('def gated(path,payload):\\n ready.write_bytes(b\"ready\")\\n "
                "while not release.exists(): time.sleep(0.01)\\n "
                "return original(path,payload)',globals()); "
                "e._first_write_terminal=gated; "
                "e.write_terminal(root,e.load_terminal(source))"
            ),
            str(output_root),
            str(source),
            str(freeze_ready),
            str(freeze_release),
        ],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    writer: subprocess.Popen[str] | None = None
    try:
        _wait_for_path(freeze_ready, freezer)
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "from app.comfyui.reliability_evidence import "
                    "EvidenceStoreError,write_artifact; "
                    "Path(sys.argv[3]).write_bytes(b'started'); "
                    "\ntry: write_artifact(Path(sys.argv[1]),sys.argv[2],"
                    "'log',b'late\\n',name='late')"
                    "\nexcept EvidenceStoreError: raise SystemExit(1)"
                ),
                str(output_root),
                terminal.run_key,
                str(writer_started),
            ],
            cwd=BACKEND_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        _wait_for_path(writer_started, writer)
        _assert_process_blocked(writer)
        freeze_release.write_bytes(b"release")
        freeze_stdout, freeze_stderr = freezer.communicate(timeout=10)
        writer_stdout, writer_stderr = writer.communicate(timeout=10)

        assert freezer.returncode == 0, freeze_stdout + freeze_stderr
        assert writer.returncode == 1, writer_stdout + writer_stderr
        assert evidence.verify_run(output_root, terminal.run_key).status == "verified"
        assert not (
            output_root / "runs" / terminal.run_key / "logs" / "late.log"
        ).exists()
    finally:
        freeze_release.touch(exist_ok=True)
        for process in (freezer, writer):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


def test_cross_process_artifact_write_holds_run_lock_across_first_write(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "8" * 64)
    source = tmp_path / "terminal-source.json"
    _write_terminal_source(source, terminal)
    writer_ready = tmp_path / "writer-ready"
    writer_release = tmp_path / "writer-release"
    freeze_started = tmp_path / "freeze-started"
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; from pathlib import Path; "
                "from app.comfyui import reliability_evidence as e; "
                "root,ready,release=map(Path,(sys.argv[1],sys.argv[3],sys.argv[4])); "
                "key=sys.argv[2]; original=e._first_write_bytes; "
                "exec('def gated(path,payload):\\n ready.write_bytes(b\"ready\")\\n "
                "while not release.exists(): time.sleep(0.01)\\n "
                "return original(path,payload)',globals()); "
                "e._first_write_bytes=gated; "
                "e.write_artifact(root,key,'log',b'late\\n',name='late')"
            ),
            str(output_root),
            terminal.run_key,
            str(writer_ready),
            str(writer_release),
        ],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    freezer: subprocess.Popen[str] | None = None
    try:
        _wait_for_path(writer_ready, writer)
        freezer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "from app.comfyui.reliability_evidence import "
                    "EvidenceStoreError,load_terminal,write_terminal; "
                    "Path(sys.argv[3]).write_bytes(b'started'); "
                    "\ntry: write_terminal(Path(sys.argv[1]),load_terminal(Path(sys.argv[2])))"
                    "\nexcept EvidenceStoreError: raise SystemExit(1)"
                ),
                str(output_root),
                str(source),
                str(freeze_started),
            ],
            cwd=BACKEND_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        _wait_for_path(freeze_started, freezer)
        _assert_process_blocked(freezer)
        writer_release.write_bytes(b"release")
        writer_stdout, writer_stderr = writer.communicate(timeout=10)
        freeze_stdout, freeze_stderr = freezer.communicate(timeout=10)

        assert writer.returncode == 0, writer_stdout + writer_stderr
        assert freezer.returncode == 1, freeze_stdout + freeze_stderr
        assert not (
            output_root / "runs" / terminal.run_key / "terminal.json"
        ).exists()
        assert (
            output_root / "runs" / terminal.run_key / "logs" / "late.log"
        ).read_bytes() == b"late\n"
    finally:
        writer_release.touch(exist_ok=True)
        for process in (writer, freezer):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

def test_cli_commit_and_verify_commands_emit_only_structured_json(tmp_path: Path) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "e" * 64)
    terminal_source = tmp_path / "terminal.json"
    _write_terminal_source(terminal_source, terminal)

    committed = _run_cli(
        "commit-run",
        "--output-root",
        str(output_root),
        "--run-key",
        terminal.run_key,
        "--terminal-json",
        str(terminal_source),
        "--expected-token",
        "absent",
    )
    verified_run = _run_cli(
        "verify-run",
        "--output-root",
        str(output_root),
        "--run-key",
        terminal.run_key,
    )
    verified_current = _run_cli("verify-current", "--output-root", str(output_root))

    assert committed.returncode == verified_run.returncode == verified_current.returncode == 0
    assert committed.stderr == verified_run.stderr == verified_current.stderr == ""
    committed_payload = json.loads(committed.stdout)
    assert committed_payload["ok"] is True
    assert committed_payload["pointer"]["run_key"] == terminal.run_key
    assert len(committed_payload["token"]) == 64
    assert json.loads(verified_run.stdout)["verification"]["status"] == "verified"
    assert json.loads(verified_current.stdout)["verification"]["status"] == "current"


def test_two_real_cli_writers_from_one_token_have_one_winner_and_keep_both_runs(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminals = [
        _write_preflight_passed_run(output_root, "f" * 64),
        _write_preflight_passed_run(output_root, "0" * 64),
    ]
    lock_held = tmp_path / "pointer-lock-held"
    lock_release = tmp_path / "pointer-lock-release"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; from pathlib import Path; "
                "from app.comfyui import reliability_evidence as e; "
                "root,held,release=map(Path,sys.argv[1:4]); "
                "exec('with e._current_pointer_lock(root):\\n "
                "held.write_bytes(b\"held\")\\n "
                "while not release.exists(): time.sleep(0.01)',globals())"
            ),
            str(output_root),
            str(lock_held),
            str(lock_release),
        ],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    processes: list[subprocess.Popen[str]] = []
    try:
        _wait_for_path(lock_held, holder)
        for index, terminal in enumerate(terminals):
            source = tmp_path / f"terminal-{index}.json"
            _write_terminal_source(source, terminal)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "app.comfyui.reliability_evidence_cli",
                        "commit-run",
                        "--output-root",
                        str(output_root),
                        "--run-key",
                        terminal.run_key,
                        "--terminal-json",
                        str(source),
                        "--expected-token",
                        "absent",
                    ],
                    cwd=BACKEND_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
            )
        for process, terminal in zip(processes, terminals, strict=True):
            _wait_for_path(
                output_root / "runs" / terminal.run_key / "terminal.json",
                process,
            )
        for process in processes:
            _assert_process_blocked(process)
        lock_release.write_bytes(b"release")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, holder_stdout + holder_stderr
        results = [process.communicate(timeout=30) for process in processes]
    finally:
        lock_release.touch(exist_ok=True)
        for process in (holder, *processes):
            if process.poll() is None:
                process.kill()
                process.communicate()

    codes = [process.returncode for process in processes]

    assert sorted(codes) == [0, 1]
    assert all(stderr == "" for _, stderr in results)
    winner_index = codes.index(0)
    loser_index = codes.index(1)
    assert json.loads(results[winner_index][0])["pointer"]["run_key"] == terminals[
        winner_index
    ].run_key
    assert json.loads(results[loser_index][0]) == {
        "error": {"code": "evidence-store-error"},
        "ok": False,
    }
    assert evidence.verify_current(output_root).pointer.run_key == terminals[
        winner_index
    ].run_key
    for terminal in terminals:
        assert evidence.verify_run(output_root, terminal.run_key).status == "verified"
    lock_path = output_root / ".current-terminal.lock"
    assert lock_path.is_file()
    assert not lock_path.is_symlink()


def test_verify_current_rejects_pointer_or_terminal_tampering(tmp_path: Path) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "1" * 64)
    evidence.write_terminal(output_root, terminal)
    evidence.compare_and_swap_current(output_root, terminal.run_key, expected_token="absent")
    pointer_path = output_root / "current-terminal.json"
    pointer = json.loads(pointer_path.read_bytes())
    pointer["terminal_sha256"] = "0" * 64
    pointer_path.write_bytes(_canonical_json(pointer))

    with pytest.raises(evidence.EvidenceStoreError, match="pointer terminal mismatch"):
        evidence.verify_current(output_root)


def test_cli_invalid_terminal_does_not_echo_private_values_or_paths(tmp_path: Path) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "2" * 64)
    secret = "PRIVATE-MODEL-RESOURCE-TOKEN"
    payload = terminal.model_dump(mode="json") | {
        "resource_id": secret,
        "registry_path": "C:/private/registry.yaml",
    }
    source = tmp_path / "private-terminal-source.json"
    source.write_bytes(_canonical_json(payload))

    completed = _run_cli(
        "commit-run",
        "--output-root",
        str(output_root),
        "--run-key",
        terminal.run_key,
        "--terminal-json",
        str(source),
        "--expected-token",
        "absent",
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout) == {
        "error": {"code": "evidence-store-error"},
        "ok": False,
    }
    combined = completed.stdout + completed.stderr
    assert secret not in combined
    assert str(source) not in combined
    assert str(output_root) not in combined


@pytest.mark.parametrize(
    "arguments",
    [
        ("PRIVATE-SUBCOMMAND-SECRET",),
        (
            "snapshot-current",
            "--output-root",
            ".",
            "--PRIVATE-OPTION-SECRET",
        ),
    ],
)
def test_cli_argument_errors_are_structured_and_never_echo_argv_secrets(
    arguments: tuple[str, ...],
) -> None:
    completed = _run_cli(*arguments)

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "error": {"code": "evidence-store-error"},
        "ok": False,
    }
    combined = completed.stdout + completed.stderr
    assert "PRIVATE-" not in combined


def test_write_rejects_real_run_junction_without_touching_outside_sentinel(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    runs = output_root / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside-must-survive", encoding="utf-8")
    run_key = "3" * 64
    junction = runs / run_key
    _make_windows_junction(junction, outside)

    try:
        with pytest.raises(evidence.EvidenceStoreError, match="directory is unsafe"):
            evidence.write_artifact(
                output_root,
                run_key,
                "log",
                b"must-not-escape\n",
                name="escape",
            )
        assert sentinel.read_text(encoding="utf-8") == "outside-must-survive"
        assert not (outside / "logs" / "escape.log").exists()
    finally:
        junction.rmdir()


def test_write_rejects_controlled_parent_swap_to_real_junction_before_open(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "9" * 64)
    run_root = output_root / "runs" / terminal.run_key
    logs = run_root / "logs"
    original_logs = run_root / "logs-original"
    outside = tmp_path / "outside-swap"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside-must-survive", encoding="utf-8")
    ready = tmp_path / "open-ready"
    release = tmp_path / "open-release"
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; from pathlib import Path; "
                "from app.comfyui import reliability_evidence as e; "
                "root,ready,release=map(Path,(sys.argv[1],sys.argv[3],sys.argv[4])); "
                "key=sys.argv[2]; original=e._first_write_bytes; "
                "exec('def gated(path,payload):\\n ready.write_bytes(b\"ready\")\\n "
                "while not release.exists(): time.sleep(0.01)\\n "
                "return original(path,payload)',globals()); "
                "e._first_write_bytes=gated; "
                "\ntry: e.write_artifact(root,key,'log',b'escape\\n',name='escape')"
                "\nexcept e.EvidenceStoreError: raise SystemExit(1)"
            ),
            str(output_root),
            terminal.run_key,
            str(ready),
            str(release),
        ],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        _wait_for_path(ready, writer)
        logs.rename(original_logs)
        _make_windows_junction(logs, outside)
        release.write_bytes(b"release")
        stdout, stderr = writer.communicate(timeout=10)

        assert writer.returncode == 1, stdout + stderr
        assert sentinel.read_text(encoding="utf-8") == "outside-must-survive"
        assert sorted(path.name for path in outside.iterdir()) == ["sentinel.txt"]
    finally:
        release.touch(exist_ok=True)
        if writer.poll() is None:
            writer.kill()
            writer.communicate()
        escaped = outside / "escape.log"
        if escaped.exists():
            escaped.unlink()
        if logs.exists():
            logs.rmdir()
        if original_logs.exists():
            original_logs.rename(logs)


def test_verify_rejects_real_member_junction_without_reading_outside_sentinel(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    terminal = _write_preflight_passed_run(output_root, "4" * 64)
    evidence.write_terminal(output_root, terminal)
    run_root = output_root / "runs" / terminal.run_key
    committed_log = run_root / "logs" / "validator.log"
    committed_log.unlink()
    committed_log.parent.rmdir()
    outside = tmp_path / "outside-member"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside-must-survive", encoding="utf-8")
    junction = run_root / "logs"
    _make_windows_junction(junction, outside)

    try:
        with pytest.raises(evidence.EvidenceStoreError, match="reparse member"):
            evidence.verify_run(output_root, terminal.run_key)
        assert sentinel.read_text(encoding="utf-8") == "outside-must-survive"
    finally:
        junction.rmdir()
