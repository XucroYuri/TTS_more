from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.comfyui import reliability_evidence as evidence
from app.comfyui import reliability_private_recovery as recovery
from app.comfyui.reliability_private_recovery import (
    PRIVATE_RECOVERY_DIRECTORY,
    PrivateRecoveryError,
    PrivateRecoveryLimits,
    observe_private_recovery,
    prepare_private_recovery,
    private_recovery_root,
    validate_private_recovery,
    write_private_recovery_snapshot,
)


RUN_KEY = "a" * 64


def _identity(path: Path) -> str:
    return evidence.read_directory_identity(path)[1]


def _prepared_boundary(tmp_path: Path) -> tuple[Path, recovery.PrivateRecoveryBoundary, Path]:
    output_root = tmp_path / "output"
    output_root.mkdir()
    boundary = prepare_private_recovery(
        output_root,
        RUN_KEY,
        expected_root_identity=_identity(output_root),
    )
    return output_root, boundary, private_recovery_root(output_root, RUN_KEY)


def _make_windows_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("private recovery junction behavior is Windows-only")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"Windows junction creation is unavailable: {completed.stderr}")


def test_boundary_creates_private_run_directory_with_exact_run_key(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()

    boundary = prepare_private_recovery(
        output_root,
        RUN_KEY,
        expected_root_identity=_identity(output_root),
    )

    expected_private_root = output_root / PRIVATE_RECOVERY_DIRECTORY
    assert boundary.status == "prepared"
    assert boundary.run_key == RUN_KEY
    assert boundary.output_root == str(output_root.absolute())
    assert boundary.private_root == str(expected_private_root.absolute())
    assert private_recovery_root(output_root, RUN_KEY) == expected_private_root / RUN_KEY
    assert (expected_private_root / RUN_KEY).is_dir()
    assert validate_private_recovery(
        output_root,
        RUN_KEY,
        expected_root_identity=boundary.root_identity,
        expected_private_root_identity=boundary.private_root_identity,
    ).status == "validated"


def test_boundary_rejects_existing_private_run_collision(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    private_run = output_root / PRIVATE_RECOVERY_DIRECTORY / RUN_KEY
    private_run.mkdir(parents=True)

    with pytest.raises(PrivateRecoveryError, match="collision"):
        prepare_private_recovery(
            output_root,
            RUN_KEY,
            expected_root_identity=_identity(output_root),
        )


def test_boundary_rejects_output_root_identity_drift(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()

    with pytest.raises(PrivateRecoveryError, match="identity changed"):
        prepare_private_recovery(
            output_root,
            RUN_KEY,
            expected_root_identity="b" * 64,
        )

    assert not (output_root / PRIVATE_RECOVERY_DIRECTORY).exists()


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_junction_boundary_rejects_private_run_link_without_touching_outside_sentinel(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside bytes must remain unchanged")
    before = sentinel.stat()
    private_root = output_root / PRIVATE_RECOVERY_DIRECTORY
    private_root.mkdir()
    _make_windows_junction(private_root / RUN_KEY, outside)

    with pytest.raises(PrivateRecoveryError, match="reparse|unsafe|collision"):
        prepare_private_recovery(
            output_root,
            RUN_KEY,
            expected_root_identity=_identity(output_root),
        )

    after = sentinel.stat()
    assert sentinel.read_bytes() == b"outside bytes must remain unchanged"
    assert after.st_mtime_ns == before.st_mtime_ns


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
@pytest.mark.parametrize("target_name", ["inside", "outside"])
def test_boundary_validation_rejects_private_leaf_junction(
    tmp_path: Path, target_name: str
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    boundary = prepare_private_recovery(
        output_root,
        RUN_KEY,
        expected_root_identity=_identity(output_root),
    )
    private_run = private_recovery_root(output_root, RUN_KEY)
    private_run.rmdir()
    target = (output_root if target_name == "inside" else tmp_path) / target_name
    target.mkdir()
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"private leaf target must not be touched")
    before = sentinel.stat()
    _make_windows_junction(private_run, target)

    with pytest.raises(PrivateRecoveryError, match="reparse|unsafe|identity changed"):
        validate_private_recovery(
            output_root,
            RUN_KEY,
            expected_root_identity=boundary.root_identity,
            expected_private_root_identity=boundary.private_root_identity,
        )

    after = sentinel.stat()
    assert sentinel.read_bytes() == b"private leaf target must not be touched"
    assert after.st_mtime_ns == before.st_mtime_ns


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX openat no-follow semantics")
def test_boundary_portable_prepare_rejects_private_ancestor_replacement_before_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside must not change")
    before = sentinel.stat()
    private_root = output_root / PRIVATE_RECOVERY_DIRECTORY
    parked = output_root / ".private-recovery.parked"
    original_mkdir = os.mkdir
    swapped = False

    def replace_private_root_before_leaf_create(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if path == RUN_KEY and dir_fd is not None and not swapped:
            swapped = True
            private_root.rename(parked)
            private_root.symlink_to(outside, target_is_directory=True)
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(recovery.os, "mkdir", replace_private_root_before_leaf_create)

    with pytest.raises(PrivateRecoveryError):
        recovery._portable_prepare(
            output_root,
            RUN_KEY,
            expected_root_identity=_identity(output_root),
        )

    after = sentinel.stat()
    assert swapped
    assert not (outside / RUN_KEY).exists()
    assert sentinel.read_bytes() == b"outside must not change"
    assert after.st_mtime_ns == before.st_mtime_ns


def test_snapshot_hashes_stable_static_members_and_writes_canonical_public_log(
    tmp_path: Path,
) -> None:
    output_root, boundary, private_run = _prepared_boundary(tmp_path)
    static = {".o": b"owner", ".h": b"history", ".c": b"context"}
    for name, payload in static.items():
        (private_run / name).write_bytes(payload)
    mutable = private_run / ".p"
    mutable.mkdir()
    (mutable / "state.bin").write_bytes(b"state")

    snapshot = observe_private_recovery(boundary)
    commitment = write_private_recovery_snapshot(output_root, RUN_KEY, snapshot)

    assert snapshot.schema_version == 1
    assert snapshot.kind == "reliability-private-recovery-snapshot"
    assert snapshot.namespace_identity_sha256 == boundary.private_root_identity
    assert [(member.role, member.present, member.size_bytes, member.sha256) for member in snapshot.static_members] == [
        (".o", True, 5, hashlib.sha256(b"owner").hexdigest()),
        (".h", True, 7, hashlib.sha256(b"history").hexdigest()),
        (".c", True, 7, hashlib.sha256(b"context").hexdigest()),
    ]
    assert snapshot.mutable_tree.present is True
    assert snapshot.mutable_tree.mutable is True
    assert commitment.relative_name == "logs/private-recovery.log"
    payload = evidence.read_artifact(output_root, RUN_KEY, "log", name="private-recovery")
    assert payload == (
        json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-field",
        "noncanonical",
        "wrong-run-key",
        "wrong-namespace-identity",
        "raw-name",
        "path",
    ],
)
def test_public_snapshot_reader_rejects_noncanonical_or_unbound_private_log(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches a reader that accepts malformed, cross-run, or path-bearing snapshots."""
    output_root, boundary, _ = _prepared_boundary(tmp_path)
    snapshot = observe_private_recovery(boundary)
    write_private_recovery_snapshot(output_root, RUN_KEY, snapshot)
    payload = evidence.read_artifact(output_root, RUN_KEY, "log", name="private-recovery")
    document = json.loads(payload)
    if mutation == "extra-field":
        document["extra"] = "unexpected"
    elif mutation == "wrong-run-key":
        document["run_key"] = "b" * 64
    elif mutation == "wrong-namespace-identity":
        document["namespace_identity_sha256"] = "b" * 64
    elif mutation == "raw-name":
        document["raw_name"] = "private-model.bin"
    elif mutation == "path":
        document["path"] = "C:/private/model.bin"

    candidate = (
        json.dumps(document, indent=2).encode("utf-8")
        if mutation == "noncanonical"
        else (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )

    with pytest.raises(evidence.EvidenceStoreError, match="snapshot"):
        evidence.verify_private_recovery_log(
            candidate,
            expected_run_key=RUN_KEY,
            expected_namespace_identity=boundary.private_root_identity,
        )


def test_snapshot_reports_missing_static_roles_without_hashes(tmp_path: Path) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    (private_run / ".h").write_bytes(b"present history")

    snapshot = observe_private_recovery(boundary)

    assert [(member.role, member.present, member.size_bytes, member.sha256) for member in snapshot.static_members] == [
        (".o", False, None, None),
        (".h", True, 15, hashlib.sha256(b"present history").hexdigest()),
        (".c", False, None, None),
    ]
    assert snapshot.mutable_tree.present is False
    assert snapshot.mutable_tree.entries == ()


def test_snapshot_rechecks_boundary_identities_with_its_observation_handles(
    tmp_path: Path,
) -> None:
    _, boundary, _ = _prepared_boundary(tmp_path)
    stale = boundary.model_copy(update={"root_identity": "b" * 64})

    with pytest.raises(PrivateRecoveryError, match="output root identity changed"):
        observe_private_recovery(stale)


def test_snapshot_fails_closed_when_static_member_changes_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    static = private_run / ".o"
    static.write_bytes(b"before")
    original_read = os.read
    changed = False

    def change_after_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        payload = original_read(descriptor, size)
        if not changed:
            changed = True
            static.write_bytes(b"after-change")
        return payload

    monkeypatch.setattr(recovery.os, "read", change_after_read)

    with pytest.raises(PrivateRecoveryError, match="static|changed|stable"):
        observe_private_recovery(boundary)

    assert changed


def test_snapshot_fails_closed_when_enumerated_static_member_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    static = private_run / ".o"
    static.write_bytes(b"present until observation")
    original_names = recovery._directory_names
    removed = False

    def remove_after_enumeration(handle: int) -> tuple[tuple[str, bool], ...]:
        nonlocal removed
        names = original_names(handle)
        if not removed and any(name == ".o" for name, _ in names):
            removed = True
            static.unlink()
        return names

    monkeypatch.setattr(recovery, "_directory_names", remove_after_enumeration)

    with pytest.raises(PrivateRecoveryError, match="file is unsafe|static|changed"):
        observe_private_recovery(boundary)

    assert removed


def test_snapshot_fails_closed_when_static_member_is_replaced_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    static = private_run / ".h"
    static.write_bytes(b"before")
    replacement = private_run / ".h.replacement"
    original_open = recovery._open_relative_file
    opens = 0
    replaced = False

    def replace_before_identity_reopen(parent: int, name: str) -> int:
        nonlocal opens, replaced
        opens += 1
        if name == ".h" and opens == 2:
            replaced = True
            replacement.write_bytes(b"replacement")
            os.replace(replacement, static)
        return original_open(parent, name)

    monkeypatch.setattr(recovery, "_open_relative_file", replace_before_identity_reopen)

    with pytest.raises(PrivateRecoveryError, match="static|changed|stable"):
        observe_private_recovery(boundary)

    assert replaced


def test_snapshot_fails_closed_for_oversize_static_member(tmp_path: Path) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    (private_run / ".c").write_bytes(b"too large")

    with pytest.raises(PrivateRecoveryError, match="static|size|limit"):
        observe_private_recovery(
            boundary,
            limits=PrivateRecoveryLimits(max_stable_member_bytes=3),
        )


def test_snapshot_hashes_mutable_names_and_orders_by_hashed_name(tmp_path: Path) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    mutable = private_run / ".p"
    mutable.mkdir()
    (mutable / "zeta.txt").write_bytes(b"Z")
    (mutable / "alpha.txt").write_bytes(b"AA")
    (mutable / "nested").mkdir()
    (mutable / "nested" / "value.bin").write_bytes(b"XYZ")

    snapshot = observe_private_recovery(boundary)

    names = [
        hashlib.sha256(name.encode("utf-8")).hexdigest()
        for name in ("zeta.txt", "alpha.txt", "nested", "nested/value.bin")
    ]
    assert [(entry.relative_name_sha256, entry.kind, entry.observed_size_bytes, entry.stable) for entry in snapshot.mutable_tree.entries] == sorted(
        [
            (names[0], "file", 1, True),
            (names[1], "file", 2, True),
            (names[2], "directory", 0, True),
            (names[3], "file", 3, True),
        ]
    )
    assert {
        entry.relative_name_sha256: entry.sha256
        for entry in snapshot.mutable_tree.entries
        if entry.kind == "file"
    } == {
        names[0]: hashlib.sha256(b"Z").hexdigest(),
        names[1]: hashlib.sha256(b"AA").hexdigest(),
        names[3]: hashlib.sha256(b"XYZ").hexdigest(),
    }


def test_snapshot_marks_concurrently_mutated_mutable_file_unstable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    mutable = private_run / ".p"
    mutable.mkdir()
    state = mutable / "state.bin"
    state.write_bytes(b"before")
    original_read = os.read
    changed = False

    def change_after_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        payload = original_read(descriptor, size)
        if not changed:
            changed = True
            state.write_bytes(b"after-change")
        return payload

    monkeypatch.setattr(recovery.os, "read", change_after_read)

    snapshot = observe_private_recovery(boundary)

    assert changed
    assert len(snapshot.mutable_tree.entries) == 1
    assert snapshot.mutable_tree.entries[0].stable is False
    assert snapshot.mutable_tree.entries[0].sha256 is None


@pytest.mark.parametrize(
    ("first", "second"),
    [("Straße.txt", "STRASSE.txt"), ("e\u0301.txt", "é.txt")],
)
def test_snapshot_rejects_casefold_or_normalized_mutable_name_collisions(
    tmp_path: Path, first: str, second: str
) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    mutable = private_run / ".p"
    mutable.mkdir()
    (mutable / first).write_bytes(b"first")
    (mutable / second).write_bytes(b"second")

    with pytest.raises(PrivateRecoveryError, match="names collide"):
        observe_private_recovery(boundary)


def test_snapshot_stops_at_first_mutable_entry_or_byte_limit(tmp_path: Path) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    mutable = private_run / ".p"
    mutable.mkdir()
    (mutable / "one").write_bytes(b"111")
    (mutable / "two").write_bytes(b"222")
    limits = PrivateRecoveryLimits(max_entries=1, max_total_observed_bytes=3)

    snapshot = observe_private_recovery(boundary, limits=limits)

    assert snapshot.overflow is True
    assert snapshot.observation_complete is False
    assert snapshot.mutable_tree.entry_count <= limits.max_entries
    assert snapshot.mutable_tree.observed_total_bytes <= limits.max_total_observed_bytes
    assert len(snapshot.mutable_tree.entries) == 1


def test_snapshot_uses_deterministic_mutable_selection_before_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    mutable = private_run / ".p"
    mutable.mkdir()
    (mutable / "one").write_bytes(b"111")
    (mutable / "two").write_bytes(b"222")
    limits = PrivateRecoveryLimits(max_entries=1, max_total_observed_bytes=3)
    original_names = recovery._directory_names
    reverse = False

    def ordered_names(handle: int) -> tuple[tuple[str, bool], ...]:
        names = original_names(handle)
        return tuple(reversed(names)) if reverse else names

    monkeypatch.setattr(recovery, "_directory_names", ordered_names)
    first = observe_private_recovery(boundary, limits=limits)
    reverse = True
    second = observe_private_recovery(boundary, limits=limits)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_snapshot_canonical_bytes_exclude_private_names_contents_and_paths(tmp_path: Path) -> None:
    output_root, boundary, private_run = _prepared_boundary(tmp_path)
    secret_name = "api-key-HUNTER2.txt"
    secret_content = b"HUNTER2-do-not-publish"
    mutable = private_run / ".p"
    mutable.mkdir()
    (mutable / secret_name).write_bytes(secret_content)
    snapshot = observe_private_recovery(boundary)
    write_private_recovery_snapshot(output_root, RUN_KEY, snapshot)

    payload = evidence.read_artifact(output_root, RUN_KEY, "log", name="private-recovery")

    assert secret_name.encode("utf-8") not in payload
    assert secret_content not in payload
    assert str(private_run).encode("utf-8") not in payload
    assert b"HUNTER2" not in payload


def test_snapshot_writer_rejects_payload_over_its_configured_limit(tmp_path: Path) -> None:
    output_root, boundary, _ = _prepared_boundary(tmp_path)
    snapshot = observe_private_recovery(
        boundary,
        limits=PrivateRecoveryLimits(max_snapshot_bytes=1),
    )

    with pytest.raises(PrivateRecoveryError, match="snapshot exceeds size limit"):
        write_private_recovery_snapshot(output_root, RUN_KEY, snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_stable_member_bytes", 67_108_865),
        ("max_snapshot_bytes", 4_194_305),
    ],
)
def test_snapshot_limits_reject_values_above_fixed_privacy_ceilings(
    field: str, value: int
) -> None:
    with pytest.raises(ValidationError):
        PrivateRecoveryLimits(**{field: value})


def test_snapshot_runtime_rejects_bypassed_over_64mib_static_read_limit(
    tmp_path: Path,
) -> None:
    _, boundary, private_run = _prepared_boundary(tmp_path)
    static = private_run / ".h"
    with static.open("wb") as handle:
        handle.truncate(67_108_865)
    bypassed = PrivateRecoveryLimits.model_construct(max_stable_member_bytes=67_108_865)

    with pytest.raises(PrivateRecoveryError, match="limits are invalid"):
        observe_private_recovery(boundary, limits=bypassed)
