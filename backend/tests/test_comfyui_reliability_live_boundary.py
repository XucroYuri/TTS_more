from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.comfyui import reliability_validation


def test_live_private_recovery_marker_binds_exact_launcher_temp_roots(
    tmp_path: Path,
) -> None:
    run_id = "a" * 32
    private_root = tmp_path / "private-recovery" / ("b" * 64)
    temp_root = private_root / ".p"
    runner_root = temp_root / "runner"
    comfy_root = temp_root / "comfyui" / "temp"
    runner_root.mkdir(parents=True)
    comfy_root.mkdir(parents=True)
    (private_root / ".o").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "temp_root": str(temp_root),
                "runner_temp_root": str(runner_root),
                "comfy_temp_root": str(comfy_root),
            }
        ),
        encoding="utf-8",
    )
    manifest = SimpleNamespace(
        run_id=run_id,
        temp_roots=(runner_root, comfy_root),
    )

    roots = reliability_validation._verified_validation_runner_roots(
        manifest,
        validation_root=private_root,
    )

    assert roots == (runner_root.resolve(),)


def test_supervised_launcher_cleanup_tracks_validator_control_state_path() -> None:
    launcher = (
        Path(__file__).parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    ).read_text(encoding="utf-8")

    # reliability_validation derives its private control document from the
    # manifest path, so cleanup must remove that exact sibling rather than a
    # different legacy `.c` name.
    assert "$controlStatePath = Join-Path $privateRecoveryRootPath '.c'" in launcher
    assert "'--control-state', $controlStatePath" in launcher
    assert "-ControlStatePath $controlStatePath" in launcher


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior is Windows-only")
def test_live_private_recovery_rejects_outside_runner_junction_and_preserves_sentinel(
    tmp_path: Path,
) -> None:
    run_id = "c" * 32
    private_root = tmp_path / "private-recovery" / ("d" * 64)
    temp_root = private_root / ".p"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")
    (temp_root / "comfyui" / "temp").mkdir(parents=True)
    junction = temp_root / "runner"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")
    try:
        (private_root / ".o").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "temp_root": str(temp_root),
                    "runner_temp_root": str(junction),
                    "comfy_temp_root": str(temp_root / "comfyui" / "temp"),
                }
            ),
            encoding="utf-8",
        )
        manifest = SimpleNamespace(
            run_id=run_id,
            temp_roots=(junction, temp_root / "comfyui" / "temp"),
        )

        with pytest.raises(ValueError, match="validation temp"):
            reliability_validation._verified_validation_runner_roots(
                manifest,
                validation_root=private_root,
            )
        assert sentinel.read_text(encoding="utf-8") == "outside"
    finally:
        subprocess.run(
            ["cmd", "/c", "rmdir", str(junction)],
            capture_output=True,
            text=True,
            check=False,
        )
