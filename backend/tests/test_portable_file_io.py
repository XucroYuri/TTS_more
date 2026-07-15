from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app import portable_file_io
from app.portable_file_io import PortableFileError, safe_read_bytes


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("directory junction verification is Windows-only")
    environment = os.environ.copy()
    environment["B2_IO_JUNCTION_PATH"] = str(link)
    environment["B2_IO_JUNCTION_TARGET"] = str(target)
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "New-Item -ItemType Junction -Path $env:B2_IO_JUNCTION_PATH -Target $env:B2_IO_JUNCTION_TARGET | Out-Null",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        pytest.fail(f"Windows junction command failed: {exc}")
    if completed.returncode != 0:
        pytest.fail(f"Windows junction creation failed: {completed.stderr}")


def _hardlink(link: Path, target: Path) -> None:
    try:
        os.link(target, link)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"hardlink creation is unavailable: {exc}")
        pytest.fail(f"Windows hardlink creation failed: {exc}")


def test_safe_read_is_handle_first_bounded_and_stable(tmp_path: Path) -> None:
    root = tmp_path / "package"
    path = root / "data" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"status":"ready"}')

    assert safe_read_bytes(root, path, max_bytes=1024, label="state") == b'{"status":"ready"}'

    path.write_bytes(b"x" * 1025)
    with pytest.raises(PortableFileError, match="too large"):
        safe_read_bytes(root, path, max_bytes=1024, label="state")


def test_safe_read_rejects_hardlinks_before_returning_content(tmp_path: Path) -> None:
    root = tmp_path / "package"
    path = root / "data" / "events.jsonl"
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b'{"message":"outside"}\n')
    _hardlink(path, outside)

    with pytest.raises(PortableFileError, match="hard link"):
        safe_read_bytes(root, path, max_bytes=1024, label="events")


def test_safe_read_retries_one_atomic_file_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "package"
    path = root / "data" / "operation.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"status":"checking"}')
    replacement = path.with_name("replacement.json")
    replacement.write_bytes(b'{"status":"ready"}')
    original_open = portable_file_io._open_binary
    swapped = False

    def replace_before_open(current: Path):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.replace(replacement, current)
        return original_open(current)

    monkeypatch.setattr(portable_file_io, "_open_binary", replace_before_open)

    assert safe_read_bytes(root, path, max_bytes=1024, label="operation", retries=2) == b'{"status":"ready"}'


def test_safe_read_never_returns_content_after_parent_is_swapped_to_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    operation = root / "data" / "operations" / "op"
    path = operation / "operation.json"
    operation.mkdir(parents=True)
    path.write_bytes(b'{"status":"inside"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "operation.json").write_bytes(b'{"status":"benign-outside"}')
    moved = root / "data" / "operations" / "op-old"
    original_open = portable_file_io._open_binary
    swapped = False

    def swap_parent_before_open(current: Path):
        nonlocal swapped
        if not swapped:
            swapped = True
            operation.rename(moved)
            _junction(operation, outside)
        return original_open(current)

    monkeypatch.setattr(portable_file_io, "_open_binary", swap_parent_before_open)

    with pytest.raises(PortableFileError, match="reparse|changed|escape"):
        safe_read_bytes(root, path, max_bytes=1024, label="operation", retries=1)
