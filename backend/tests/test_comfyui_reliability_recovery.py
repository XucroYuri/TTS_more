from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.comfyui import reliability_evidence as evidence
from app.comfyui import reliability_private_recovery as private_recovery
from app.comfyui import reliability_recovery as recovery
from app.comfyui import reliability_supervision as supervision


BACKEND_ROOT = Path(__file__).resolve().parents[1]
RECOVERY_SCRIPT = BACKEND_ROOT.parent / "scripts" / "recover-windows-comfyui-reliability-run.ps1"


def _canonical(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"Windows junction creation is unavailable: {completed.stderr}")
    else:
        link.symlink_to(target, target_is_directory=True)


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


def _failed_recovery_fixture(
    tmp_path: Path,
    *,
    malformed_supervisor: bool = False,
    malformed_run_result: bool = False,
    wrong_lifecycle_binding: bool = False,
    with_secondary_lifecycle: bool = False,
) -> tuple[Path, str, Path, tuple[dict[str, object], ...], dict[str, int | None]]:
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
            "comfyui": {
                "executable_path": "C:/fixture/python-4102.exe",
                "arguments": ["main.py", "--port", "8188"],
                "working_directory": "C:/fixture/comfyui",
                "port": 8188,
                "temp_root": "C:/fixture/temp",
            },
        },
        "boundary": {"repositories": {}, "private_registry": "C:/private.json", "references": {}},
        "temp_roots": ["C:/fixture/temp"],
    }
    host_payload = _canonical(host)
    (private_leaf / ".h").write_bytes(host_payload)

    snapshot = private_recovery.observe_private_recovery(boundary)
    snapshot_commitment = private_recovery.write_private_recovery_snapshot(
        output_root, run_key, snapshot
    )
    supervisor_model = supervision.SupervisorRecord(
        schema_version=1,
        kind="reliability-supervisor-result",
        run_key=run_key,
        mode="preflight",
        child_start_count=1,
        launcher_exit_code=7,
        validator_exit_code=0,
        cleanup_status="failed",
        outcome="failed",
        failure_source="cleanup",
    )
    inner_model = supervision.InnerRunResult(
        schema_version=1,
        kind="reliability-inner-run-result",
        run_key=run_key,
        mode="preflight",
        outcome="failed",
        failure_source="cleanup",
        validator_exit_code=0,
        cleanup_status="failed",
        reported_by="inner",
    )
    supervisor_payload = b"supervisor\n" if malformed_supervisor else _canonical(supervisor_model.model_dump(mode="json"))
    run_result_payload = b"run-result\n" if malformed_run_result else _canonical(inner_model.model_dump(mode="json"))
    supervisor = _commitment(output_root, run_key, "supervisor", supervisor_payload)
    run_result = _commitment(output_root, run_key, "run-result", run_result_payload)
    _owner_processes, _owner_ports, owner_public_identity = recovery._owner_proof(
        host_payload, run_key
    )
    lifecycle_model = supervision.LauncherLifecycleCommitment(
        schema_version=1,
        kind="reliability-launcher-lifecycle-commitment",
        run_key=run_key,
        source_size_bytes=128,
        source_sha256="9" * 64,
        promotion_ownership_sha256=(
            "8" * 64 if wrong_lifecycle_binding else owner_public_identity
        ),
    )
    lifecycle = _commitment(
        output_root,
        run_key,
        "log",
        _canonical(lifecycle_model.model_dump(mode="json")),
        name="launcher-lifecycle",
    )
    lifecycle_artifacts = [lifecycle]
    if with_secondary_lifecycle:
        secondary = supervision.StreamCommitment(
            schema_version=1,
            kind="reliability-stream-commitment",
            size_bytes=42,
            sha256="7" * 64,
        )
        lifecycle_artifacts.append(
            _commitment(
                output_root,
                run_key,
                "log",
                _canonical(secondary.model_dump(mode="json")),
                name="launcher-lifecycle-secondary",
            )
        )
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
        artifacts=tuple(
            sorted(
                (supervisor, run_result, snapshot_commitment, *lifecycle_artifacts),
                key=lambda item: item.relative_name,
            )
        ),
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
    result = recovery.execute_recovery_delete(
        plan, observed_processes=processes, observed_ports=ports
    )

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


def test_execute_revalidates_fresh_owner_and_both_owned_ports(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )
    assert isinstance(plan, recovery.RecoveryPlan)
    host = json.loads((private_leaf / ".h").read_text(encoding="utf-8"))
    owner = host["owned_processes"]["tts-more"]
    live_owner = {
        "pid": owner["pid"],
        "creation_time": owner["creation_time"],
        "executable_sha256": hashlib.sha256(owner["executable_path"].encode()).hexdigest(),
        "command_line_sha256": hashlib.sha256(owner["command_line"].encode()).hexdigest(),
        "parent_pid": owner["parent_pid"],
        "parent_creation_time": owner["parent_creation_time"],
    }

    owner_result = recovery.execute_recovery_delete(
        plan,
        observed_processes=(live_owner,),
        observed_ports=ports,
    )
    port_result = recovery.execute_recovery_delete(
        plan,
        observed_processes=processes,
        observed_ports={"8000": 9999, "8188": None},
    )

    assert owner_result.status == "rejected"
    assert port_result.status == "rejected"
    assert (private_leaf / ".h").exists()


@pytest.mark.parametrize("malformed", ["supervisor", "run-result", "lifecycle-binding"])
def test_recovery_rejects_malformed_public_ownership_artifacts_before_delete(
    tmp_path: Path, malformed: str
) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(
        tmp_path,
        malformed_supervisor=malformed == "supervisor",
        malformed_run_result=malformed == "run-result",
        wrong_lifecycle_binding=malformed == "lifecycle-binding",
    )

    decision = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )

    assert isinstance(decision, recovery.RecoveryResult)
    assert decision.deleted_roles == ()
    assert (private_leaf / ".h").exists()


def test_recovery_binds_optional_secondary_lifecycle_stream_without_treating_it_as_owner(
    tmp_path: Path,
) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(
        tmp_path, with_secondary_lifecycle=True
    )

    decision = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )

    assert isinstance(decision, recovery.RecoveryPlan)
    assert (private_leaf / ".h").exists()


def test_supervision_lifecycle_commitment_exports_owner_digest_for_recovery() -> None:
    run_key = "a" * 64
    processes: list[dict[str, object]] = []
    rows: list[str] = []
    for role, pid in (("tts-more", 4101), ("comfyui", 4102)):
        record = recovery._record(_process_record(pid, 4000))
        for kind in ("launch-root", "listener"):
            identity = str(record["public_identity_sha256"]).upper()
            process = {
                "role": role,
                "kind": kind,
                "pid": pid,
                "creation_time_utc": record["public_creation_time"],
                "parent_pid": 4000,
                "parent_creation_time_utc": record["public_parent_creation_time"],
                "identity_sha256": identity,
            }
            processes.append(process)
            rows.append(
                f"{role}|{kind}|{pid}|{record['public_creation_time']}|4000|{identity}"
            )
    ownership = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    raw = _canonical(
        {
            "schema_version": 1,
            "kind": "launcher-failure-lifecycle",
            "status": "failed",
            "run_id_sha256": run_key.upper(),
            "primary": {},
            "validation": {},
            "case": {},
            "timestamps": {},
            "processes": processes,
            "promotion_ownership_sha256": ownership.upper(),
            "cleanup": {},
        }
    )

    commitment = supervision._launcher_lifecycle_commitment(raw, run_key=run_key)

    assert commitment.promotion_ownership_sha256 == ownership
    assert commitment.run_key == run_key


def test_recovery_execute_revalidates_concurrent_replacement_and_is_not_replayable(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports)
    assert isinstance(plan, recovery.RecoveryPlan)
    (private_leaf / ".h").write_bytes(b"replacement")
    rejected = recovery.execute_recovery_delete(
        plan, observed_processes=processes, observed_ports=ports
    )
    assert rejected.status == "rejected"
    assert rejected.deleted_roles == ()
    assert private_leaf.exists()

    # A fresh fixture proves that a successful plan cannot be replayed after removal.
    output_root2, run_key2, _leaf2, processes2, ports2 = _failed_recovery_fixture(tmp_path / "second")
    plan2 = recovery.validate_recovery_owner(output_root2, run_key2, observed_processes=processes2, observed_ports=ports2)
    assert isinstance(plan2, recovery.RecoveryPlan)
    assert recovery.execute_recovery_delete(
        plan2, observed_processes=processes2, observed_ports=ports2
    ).status == "removed"
    assert recovery.execute_recovery_delete(
        plan2, observed_processes=processes2, observed_ports=ports2
    ) == recovery.RecoveryResult(
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
    result = recovery.execute_recovery_delete(
        plan, observed_processes=processes, observed_ports=ports
    )

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
    assert len(plan_document["plan_token"]) == 64
    int(plan_document["plan_token"], 16)

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
        input=observations,
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
        input=json.dumps({"processes": list(processes), "ports": ports}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert executed.returncode != 0
    assert (private_leaf / ".h").read_bytes() == before
    assert recovery.decode_plan_token(token).run_key == run_key


def test_recovery_cli_token_is_one_shot_and_execute_requires_fresh_observations(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    safe = json.dumps({"processes": list(processes), "ports": ports})
    planned = subprocess.run(
        [sys.executable, "-m", "app.comfyui.reliability_recovery_cli", "plan", "--output-root", str(output_root), "--run-key", run_key],
        cwd=BACKEND_ROOT,
        input=safe,
        capture_output=True,
        text=True,
        check=False,
    )
    token = json.loads(planned.stdout)["plan_token"]
    occupied = json.dumps({"processes": [], "ports": {"8000": 9999, "8188": None}})
    arguments = [
        sys.executable,
        "-m",
        "app.comfyui.reliability_recovery_cli",
        "execute",
        "--output-root",
        str(output_root),
        "--run-key",
        run_key,
        "--plan-token",
        token,
    ]

    first = subprocess.run(arguments, cwd=BACKEND_ROOT, input=occupied, capture_output=True, text=True, check=False)
    replay = subprocess.run(arguments, cwd=BACKEND_ROOT, input=safe, capture_output=True, text=True, check=False)

    assert first.returncode != 0
    assert replay.returncode != 0
    assert (private_leaf / ".h").exists()


def test_capability_store_rejects_inherited_or_world_accessible_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / recovery.CAPABILITY_DIRECTORY
    store.mkdir()
    if os.name != "nt":
        store.chmod(0o777)
    monkeypatch.setattr(recovery.tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(ValueError, match="capability store"):
        recovery._capability_directory()


def test_capability_store_rejects_owner_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recovery.tempfile, "gettempdir", lambda: str(tmp_path))
    store = recovery._capability_directory()
    if os.name == "nt":
        expected = recovery._windows_private_sddl(directory=True)
        foreign_owner = "O:S-1-5-18" + expected[expected.index("D:") :]
        monkeypatch.setattr(
            recovery, "_windows_security_descriptor_sddl", lambda _path: foreign_owner
        )
    else:
        monkeypatch.setattr(recovery.os, "geteuid", lambda: store.stat().st_uid + 1)

    with pytest.raises(ValueError, match="capability store"):
        recovery._validate_capability_directory_security(store)


def test_capability_store_rejects_acl_drift_after_secure_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recovery.tempfile, "gettempdir", lambda: str(tmp_path))
    store = recovery._capability_directory()
    if os.name == "nt":
        changed = subprocess.run(
            ["icacls.exe", str(store), "/grant", "*S-1-1-0:(RX)"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert changed.returncode == 0, changed.stderr + changed.stdout
    else:
        store.chmod(0o750)

    with pytest.raises(ValueError, match="capability store"):
        recovery._capability_directory()


def test_capability_store_rejects_reparse_without_touching_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside-must-survive")
    base = tmp_path / "base"
    base.mkdir()
    link = base / recovery.CAPABILITY_DIRECTORY
    _make_directory_link(link, outside)
    monkeypatch.setattr(recovery.tempfile, "gettempdir", lambda: str(base))
    try:
        with pytest.raises(ValueError, match="capability store"):
            recovery._capability_directory()
        assert sentinel.read_bytes() == b"outside-must-survive"
    finally:
        if link.exists():
            link.rmdir()


def test_capability_file_never_contains_output_or_private_paths(tmp_path: Path) -> None:
    output_root, run_key, _private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )
    assert isinstance(plan, recovery.RecoveryPlan)
    token = recovery.encode_plan_token(plan)
    target = recovery._capability_directory() / (
        hashlib.sha256(token.encode("ascii")).hexdigest() + ".cap"
    )
    try:
        payload = target.read_bytes()
        for sensitive_path in (str(output_root), str(plan.private_root)):
            assert sensitive_path.encode("utf-8") not in payload
            escaped = json.dumps(sensitive_path, ensure_ascii=False)[1:-1].encode("utf-8")
            assert escaped not in payload
        assert b'"plan"' not in payload
        assert not payload.startswith(b"{")
    finally:
        for suffix in (".cap", ".claim"):
            path = target.with_suffix(suffix)
            if path.exists():
                path.unlink()


def test_attacker_cannot_forge_plaintext_capability_for_chosen_token(tmp_path: Path) -> None:
    output_root, run_key, _private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )
    assert isinstance(plan, recovery.RecoveryPlan)
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = int(recovery.time.time())
    forged = {
        "schema_version": 1,
        "kind": "reliability-recovery-capability",
        "token_sha256": token_hash,
        "issued_at": now,
        "expires_at": now + recovery.CAPABILITY_LIFETIME_SECONDS,
        "plan": plan.model_dump(mode="json"),
    }
    target = recovery._capability_directory() / f"{token_hash}.cap"
    forged_payload = _canonical(forged)
    descriptor = recovery._create_private_capability_file(
        target,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        assert os.write(descriptor, forged_payload) == len(forged_payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    recovery._validate_capability_file(target)
    try:
        with pytest.raises(ValueError, match="plan token"):
            recovery.decode_plan_token(token)
        assert (output_root / ".private-recovery" / run_key / ".h").exists()
    finally:
        for path in (target, target.with_suffix(".claim")):
            if path.exists():
                path.unlink()


def test_copying_capability_ciphertext_to_another_token_cannot_authorize_it(
    tmp_path: Path,
) -> None:
    output_root, run_key, _private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )
    assert isinstance(plan, recovery.RecoveryPlan)
    first_token = recovery.encode_plan_token(plan)
    second_token = recovery.encode_plan_token(plan)
    directory = recovery._capability_directory()
    first = directory / f"{hashlib.sha256(first_token.encode('ascii')).hexdigest()}.cap"
    second = directory / f"{hashlib.sha256(second_token.encode('ascii')).hexdigest()}.cap"
    try:
        first_payload = first.read_bytes()
        assert str(output_root).encode("utf-8") not in first_payload
        escaped_root = json.dumps(str(output_root), ensure_ascii=False)[1:-1].encode("utf-8")
        assert escaped_root not in first_payload
        assert b'"plan"' not in first_payload
        shutil.copyfile(first, second)
        with pytest.raises(ValueError, match="plan token"):
            recovery.decode_plan_token(second_token)
        assert recovery.decode_plan_token(first_token).run_key == run_key
    finally:
        for target in (first, second):
            for path in (target, target.with_suffix(".claim")):
                if path.exists():
                    path.unlink()


def test_capability_file_acl_drift_is_rejected_before_decryption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability_base = tmp_path / "cap-base"
    capability_base.mkdir()
    monkeypatch.setattr(
        recovery.tempfile, "gettempdir", lambda: str(capability_base)
    )
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(
        tmp_path / "fixture"
    )
    plan = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )
    assert isinstance(plan, recovery.RecoveryPlan)
    token = recovery.encode_plan_token(plan)
    target = recovery._capability_directory() / (
        hashlib.sha256(token.encode("ascii")).hexdigest() + ".cap"
    )
    if os.name == "nt":
        changed = subprocess.run(
            ["icacls.exe", str(target), "/grant", "*S-1-1-0:(R)"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert changed.returncode == 0, changed.stderr + changed.stdout
    else:
        target.chmod(0o640)
    try:
        with pytest.raises(ValueError, match="plan token"):
            recovery.decode_plan_token(token)
        assert (private_leaf / ".h").exists()
    finally:
        if target.exists():
            target.unlink()


def test_expired_capability_is_rejected_without_deleting_private_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability_base = tmp_path / "cap-base"
    capability_base.mkdir()
    monkeypatch.setattr(
        recovery.tempfile, "gettempdir", lambda: str(capability_base)
    )
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(
        tmp_path / "fixture"
    )
    plan = recovery.validate_recovery_owner(
        output_root, run_key, observed_processes=processes, observed_ports=ports
    )
    assert isinstance(plan, recovery.RecoveryPlan)
    issued_at = int(recovery.time.time())
    token = recovery.encode_plan_token(plan)
    target = recovery._capability_directory() / (
        hashlib.sha256(token.encode("ascii")).hexdigest() + ".cap"
    )
    monkeypatch.setattr(
        recovery.time,
        "time",
        lambda: issued_at + recovery.CAPABILITY_LIFETIME_SECONDS + 1,
    )
    try:
        with pytest.raises(ValueError, match="plan token"):
            recovery.decode_plan_token(token)
        assert (private_leaf / ".h").exists()
    finally:
        if target.exists():
            target.unlink()


def test_recovery_execute_rejects_namespace_or_leaf_identity_drift_without_delete(tmp_path: Path) -> None:
    output_root, run_key, private_leaf, processes, ports = _failed_recovery_fixture(tmp_path)
    plan = recovery.validate_recovery_owner(output_root, run_key, observed_processes=processes, observed_ports=ports)
    assert isinstance(plan, recovery.RecoveryPlan)
    wrong_namespace = plan.model_copy(update={"namespace_identity_sha256": "e" * 64})
    assert recovery.execute_recovery_delete(
        wrong_namespace, observed_processes=processes, observed_ports=ports
    ).deleted_roles == ()
    parked = private_leaf.with_name(private_leaf.name + ".parked")
    private_leaf.rename(parked)
    private_leaf.mkdir()

    result = recovery.execute_recovery_delete(
        plan, observed_processes=processes, observed_ports=ports
    )

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
    assert "Get-NetTCPConnection" in source
    assert "-ErrorAction Stop" in source
    assert "SilentlyContinue" not in source
    assert source.count("Get-RecoveryObservation") >= 3


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
