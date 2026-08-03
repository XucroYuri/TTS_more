from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.comfyui import reliability_evidence as evidence
from app.comfyui.reliability_private_recovery import (
    PRIVATE_RECOVERY_DIRECTORY,
    PrivateRecoveryError,
    prepare_private_recovery,
    private_recovery_root,
    validate_private_recovery,
)


RUN_KEY = "a" * 64


def _identity(path: Path) -> str:
    return evidence.read_directory_identity(path)[1]


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
