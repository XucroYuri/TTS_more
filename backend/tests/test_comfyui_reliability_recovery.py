from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.comfyui import reliability_evidence as evidence
from app.comfyui import reliability_private_recovery as private_recovery
from app.comfyui import reliability_recovery as recovery


BACKEND_ROOT = Path(__file__).resolve().parents[1]
RECOVERY_SCRIPT = BACKEND_ROOT.parent / "scripts" / "recover-windows-comfyui-reliability-run.ps1"


def _canonical(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _process_record(pid: int, parent_pid: int) -> dict[str, object]:
    return {
        "pid": pid,
        "creation_time": "2026-08-03T01:02:03.0000000Z",
        "executable_path": f"C:/fixture/python-{pid}.exe",
        "command_line": f'"C:/fixture/python-{pid}.exe" --fixture',
        "parent_pid": parent_pid,
        "parent_creation_time": "2026-08-03T01:00:00.0000000Z",
    }


def _commitment(output_root: Path, run_key: str, kind: str, payload: bytes, *, name: str | None = None) -> evidence.ArtifactCommitment:
    return evidence.write_artifact(output_root, run_key, kind, payload, name=name)


def _failed_recovery_fixture(tmp_path: Path) -> tuple[Path, str, Path, tuple[dict[str, object], ...], dict[str, int | None]]:
    raw_run_id = "recovery-fixture-run"
    run_key = hashlib.sha256(raw_run_id.encode("utf-8")).hexdigest()
    output_root = tmp_path / "evidence"
    output_root.mkdir(parents=True)
    root_identity = evidence.read_directory_identity(output_root)[1]
    evidence.prepare_new_run_directory(output_root, run_key, root_identity)
    boundary = private_recovery.prepare_private_recovery(
        output_root,
        run_key,
        expected_root_identity=root_identity,
    )
    private_leaf = private_recovery.private_recovery_root(output_root, run_key)
    mutable = private_leaf / ".p"
    (mutable / "nested").mkdir(parents=True)
    (mutable / "nested" / "state.bin").write_bytes(b"private-state")
    (private_leaf / ".o").write_bytes(_canonical({"run_id": raw_run_id}))
    (private_leaf / ".c").write_bytes(_canonical({"version": 2, "run_id": raw_run_id}))
    tts = _process_record(4101, 4000)
    comfy = _process_record(4102, 4000)
    host = {
        "version": 1,
        "run_id": raw_run_id,
        "owned_processes": {"tts-more": tts, "comfyui": comfy},
        "launch_roots": {"tts-more": tts, "comfyui": comfy},
        "launch": {
            "tts-more": {"port": 8000},
            "comfyui": {"port": 8188},
        },
        "boundary": {"repositories": {}, "private_registry": "C:/private.json", "references": {}},
        "temp_roots": ["C:/fixture/temp"],
    }
    (private_leaf / ".h").write_bytes(_canonical(host))

    snapshot = private_recovery.observe_private_recovery(boundary)
    snapshot_commitment = private_recovery.write_private_recovery_snapshot(
        output_root, run_key, snapshot
    )
    supervisor = _commitment(output_root, run_key, "supervisor", b"supervisor\n")
    run_result = _commitment(output_root, run_key, "run-result", b"run-result\n")
    failure = _commitment(output_root, run_key, "failure", b"failure\n")
    terminal = evidence.RunTerminal(
        schema_version=1,
        kind="reliability-run-terminal",
        run_key=run_key,
        mode="preflight",
        outcome="failed",
        failure_source="cleanup",
        evidence_complete=True,
        launcher_exit_code=7,
        validator_exit_code=0,
        cleanup_status="failed",
        preflight=None,
        failure=failure,
        summary=None,
        cases=(),
        artifacts=tuple(sorted((supervisor, run_result, snapshot_commitment), key=lambda item: item.relative_name)),
    )
    evidence.write_terminal(
        output_root,
        terminal,
        expected_private_recovery_namespace_identity=boundary.private_root_identity,
    )
    evidence.compare_and_swap_current(output_root, run_key, expected_token="absent")
    return output_root, run_key, private_leaf, (), {"8000": None, "8188": None}


def _public_bytes(output_root: Path, run_key: str) -> tuple[bytes, bytes, bytes]:
    return (
        (output_root / "current-terminal.json").read_bytes(),
        (output_root / "runs" / run_key / "terminal.json").read_bytes(),
        (output_root / "runs" / run_key / "logs" / "private-recovery.log").read_bytes(),
    )


def test_recovery_removes_only_private_roles_and_leaf(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    public_before = _public_bytes(output_root, run_key)

    plan = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )
    assert isinstance(plan, recovery.RecoveryPlan)
    result = recovery.execute_recovery_delete(plan)

    assert result.status == "removed"
    assert result.deleted_roles == (".p", ".c", ".h", ".o")
    assert not private_leaf.exists()
    assert _public_bytes(output_root, run_key) == public_before


@pytest.mark.parametrize(
    "mutation",
    [
        {"creation_time": "2026-08-03T01:02:04.0000000Z"},
        {"executable_sha256": "1" * 64},
        {"command_line_sha256": "2" * 64},
        {"parent_pid": 4999},
        {"parent_creation_time": "2026-08-03T01:00:01.0000000Z"},
    ],
)
def test_recovery_rejects_pid_reuse_and_identity_or_parent_drift_without_delete(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    output_root, run_key, private_leaf, _processes, ports = _failed_recovery_fixture(tmp_path)
    host = json.loads((private_leaf / ".h").read_text(encoding="utf-8"))
    record = host["owned_processes"]["tts-more"]
    observed = {
        "pid": record["pid"],
        "creation_time": record["creation_time"],
        "executable_sha256": hashlib.sha256(record["executable_path"].encode()).hexdigest(),
        "command_line_sha256": hashlib.sha256(record["command_line"].encode()).hexdigest(),
        "parent_pid": record["parent_pid"],
        "parent_creation_time": record["parent_creation_time"],
    } | mutation
    before = {path.relative_to(private_leaf): path.read_bytes() for path in private_leaf.rglob("*") if path.is_file()}

    decision = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=(observed,), observed_ports=ports
    )

    assert decision == recovery.RecoveryResult(
        status="rejected", run_key=run_key, deleted_roles=(), reason_code="recovery-proof-failed"
    )
    assert {path.relative_to(private_leaf): path.read_bytes() for path in private_leaf.rglob("*") if path.is_file()} == before


def test_recovery_rejects_changed_h_extra_member_and_owned_port_without_delete(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    original = (private_leaf / ".h").read_bytes()
    (private_leaf / ".h").write_bytes(original + b"changed")
    assert isinstance(recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports), recovery.RecoveryResult)
    (private_leaf / ".h").write_bytes(original)
    (private_leaf / "extra").write_bytes(b"unsafe")
    assert isinstance(recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports), recovery.RecoveryResult)
    (private_leaf / "extra").unlink()
    assert isinstance(recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports | {"8188": 999}), recovery.RecoveryResult)
    assert private_leaf.exists()


def test_recovery_rejects_live_descendant_of_recorded_owner(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, _processes, ports = _failed_recovery_fixture(tmp_path)
    host = json.loads((private_leaf / ".h").read_text(encoding="utf-8"))
    parent = host["owned_processes"]["tts-more"]
    descendant = {
        "pid": 5101,
        "creation_time": "2026-08-03T01:03:00.0000000Z",
        "executable_sha256": "3" * 64,
        "command_line_sha256": "4" * 64,
        "parent_pid": parent["pid"],
        "parent_creation_time": parent["creation_time"],
    }

    decision = recovery.validate_recovery_owner(
        output_root,
        run_key,
        observed_processes=(descendant,),
        observed_ports=ports,
    )

    assert isinstance(decision, recovery.RecoveryResult)
    assert decision.deleted_roles == ()
    assert private_leaf.exists()


def test_recovery_execute_revalidates_concurrent_replacement_and_is_not_replayable(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports)
    assert isinstance(plan, recovery.RecoveryPlan)
    (private_leaf / ".h").write_bytes(b"replacement")
    rejected = recovery.execute_recovery_delete(plan)
    assert rejected.status == "rejected"
    assert rejected.deleted_roles == ()
    assert private_leaf.exists()

    # A fresh fixture proves that a successful plan cannot be replayed after removal.
    output_root2, run_key2, _leaf2, processes2, ports2 = _failed_recovery_fixture(tmp_path / "second")
    plan2 = recovery.validate_recovery_owner(output_root2, run_key2, observed_processes=processes2, observed_ports=ports2)
    assert isinstance(plan2, recovery.RecoveryPlan)
    assert recovery.execute_recovery_delete(plan2).status == "removed"
    assert recovery.execute_recovery_delete(plan2) == recovery.RecoveryResult(
        status="rejected", run_key=run_key2, deleted_roles=(), reason_code="recovery-proof-failed"
    )


def test_recovery_partial_os_failure_is_truthful_and_never_touches_public_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    public_before = _public_bytes(output_root, run_key)
    plan = recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports)
    assert isinstance(plan, recovery.RecoveryPlan)
    original = recovery._delete_validated_role

    def fail_after_mutable(*args: object, **kwargs: object) -> None:
        role = args[2]
        if role == ".c":
            raise OSError("injected post-commit failure")
        original(*args, **kwargs)

    monkeypatch.setattr(recovery, "_delete_validated_role", fail_after_mutable)
    result = recovery.execute_recovery_delete(plan)

    assert result.status == "rejected"
    assert result.deleted_roles == (".p",)
    assert result.reason_code == "recovery-delete-partial"
    assert not (private_leaf / ".p").exists()
    assert (private_leaf / ".h").exists()
    assert _public_bytes(output_root, run_key) == public_before


@pytest.mark.parametrize(
    "pointer_mutation",
    ({"run_key": "f" * 64}, {"outcome": "passed"}),
)
def test_recovery_rejects_noncurrent_or_nonfailed_run_and_preserves_private_sentinel(
    tmp_path: Path, pointer_mutation: dict[str, object]
) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    pointer = json.loads((output_root / "current-terminal.json").read_text(encoding="utf-8"))
    pointer.update(pointer_mutation)
    (output_root / "current-terminal.json").write_bytes(_canonical(pointer))
    sentinel = (private_leaf / ".p" / "nested" / "state.bin").read_bytes()

    decision = recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports)

    assert isinstance(decision, recovery.RecoveryResult)
    assert (private_leaf / ".p" / "nested" / "state.bin").read_bytes() == sentinel


def test_recovery_cli_plan_and_execute_expose_only_an_opaque_token(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    observations = json.dumps({"processes": list(processes), "ports": ports})
    planned = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.comfyui.reliability_recovery_cli",
            "plan",
            "--output-root",
            str(output_root),
            "--run-key",
            run_key,
        ],
        cwd=BACKEND_ROOT,
        input=observations,
        capture_output=True,
        text=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stderr
    plan_document = json.loads(planned.stdout)
    assert set(plan_document) == {"ok", "plan_token"}
    assert str(private_leaf) not in planned.stdout

    executed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.comfyui.reliability_recovery_cli",
            "execute",
            "--output-root",
            str(output_root),
            "--run-key",
            run_key,
            "--plan-token",
            plan_document["plan_token"],
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr + executed.stdout
    assert json.loads(executed.stdout)["result"]["status"] == "removed"
    assert not private_leaf.exists()


def test_recovery_cli_rejects_tampered_token_without_deleting_private_bytes(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    planned = subprocess.run(
        [sys.executable, "-m", "app.comfyui.reliability_recovery_cli", "plan", "--output-root", str(output_root), "--run-key", run_key],
        cwd=BACKEND_ROOT,
        input=json.dumps({"processes": list(processes), "ports": ports}),
        capture_output=True,
        text=True,
        check=False,
    )
    token = json.loads(planned.stdout)["plan_token"]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    before = (private_leaf / ".h").read_bytes()

    executed = subprocess.run(
        [sys.executable, "-m", "app.comfyui.reliability_recovery_cli", "execute", "--output-root", str(output_root), "--run-key", run_key, "--plan-token", tampered],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert executed.returncode != 0
    assert (private_leaf / ".h").read_bytes() == before


def test_recovery_execute_rejects_namespace_or_leaf_identity_drift_without_delete(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports)
    assert isinstance(plan, recovery.RecoveryPlan)
    wrong_namespace = plan.model_copy(update={"namespace_identity_sha256": "e" * 64})
    assert recovery.execute_recovery_delete(wrong_namespace).deleted_roles == ()
    parked = private_leaf.with_name(private_leaf.name + ".parked")
    private_leaf.rename(parked)
    private_leaf.mkdir()

    result = recovery.execute_recovery_delete(plan)

    assert result.status == "rejected"
    assert result.deleted_roles == ()
    assert (parked / ".h").exists()
    assert private_leaf.is_dir()


def test_recovery_powershell_entry_accepts_no_private_path_parameter() -> None:
    source = RECOVERY_SCRIPT.read_text(encoding="utf-8")
    parameter_block = source[source.index("param(") : source.index(")\n\n$ErrorActionPreference")]
    assert "$OutputRoot" in parameter_block
    assert "$RunKey" in parameter_block
    assert "Private" not in parameter_block
    assert ".private-recovery" not in source


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_recovery_rejects_private_mutable_junction_and_preserves_outside_sentinel(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside-safe")
    before = sentinel.stat().st_mtime_ns
    for path in sorted((private_leaf / ".p").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (private_leaf / ".p").rmdir()
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(private_leaf / ".p"), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    decision = recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports)

    assert isinstance(decision, recovery.RecoveryResult)
    assert sentinel.read_bytes() == b"outside-safe"
    assert sentinel.stat().st_mtime_ns == before
