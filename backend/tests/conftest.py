from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _fake_windows_system_executables_for_cross_platform_controller_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if os.name == "nt":
        return

    from app import portable_control

    system_root = tmp_path / "fake-windows-system"
    system_root.mkdir()
    for name in ("cmd.exe", "explorer.exe"):
        (system_root / name).write_bytes(b"fake executable")

    def resolve(name: str) -> Path:
        executable = system_root / name
        if executable.is_file():
            return executable
        raise portable_control.PortableControlError(
            "PORTABLE_SYSTEM_EXECUTABLE_INVALID",
            f"unsupported fake Windows system executable: {name}",
        )

    monkeypatch.setattr(portable_control, "_windows_system_executable", resolve)
