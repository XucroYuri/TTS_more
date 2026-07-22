from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PORTABLE_TEST_MODULES = {
    "test_prepare_scripts.py",
    "test_integration_sync.py",
    "test_portable_control.py",
    "test_portable_diagnostics.py",
    "test_portable_discovery.py",
    "test_portable_file_io.py",
    "test_portable_first_run_harness.py",
    "test_portable_install.py",
    "test_portable_launcher.py",
    "test_portable_locks.py",
    "test_portable_migration.py",
    "test_portable_operations.py",
    "test_portable_packages.py",
    "test_portable_python_runtime.py",
    "test_portable_services.py",
    "test_portable_start_controller.py",
}

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep retired portable-package coverage out of the default product gate."""
    for item in items:
        if Path(str(item.fspath)).name in LEGACY_PORTABLE_TEST_MODULES:
            item.add_marker(pytest.mark.legacy_portable)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    """Avoid importing retired portable-package tests in the default CI gate."""
    return (
        os.environ.get("TTS_MORE_SKIP_LEGACY_PORTABLE") == "1"
        and collection_path.name in LEGACY_PORTABLE_TEST_MODULES
    )


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
