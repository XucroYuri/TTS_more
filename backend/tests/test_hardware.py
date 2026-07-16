from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import hardware


def _poison_nvidia_smi(command, **_kwargs):
    executable = Path(str(command[0])).name.casefold()
    if executable in {"nvidia-smi", "nvidia-smi.exe"}:
        raise AssertionError("ordinary hardware status must not launch nvidia-smi")
    raise AssertionError(f"unexpected subprocess: {command!r}")


def test_hardware_status_prefers_portable_controller_cache_without_spawning(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "data" / "cache" / "portable" / "video-controllers.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            [
                {"name": "NVIDIA GeForce RTX 4060 Ti", "driver_version": "32.0.15.9186"},
                {"name": "Oray Virtual Display", "driver_version": "1.0"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hardware, "_video_controllers_cache_path", lambda: cache, raising=False)
    monkeypatch.setattr(hardware.subprocess, "run", _poison_nvidia_smi)

    status = hardware.collect_local_hardware_status()["gpu"]

    assert status == {
        "available": True,
        "status": "ok",
        "source": "portable-cache",
        "devices": [
            {
                "name": "NVIDIA GeForce RTX 4060 Ti",
                "driver_version": "32.0.15.9186",
                "memory_total_mb": None,
            }
        ],
    }


@pytest.mark.parametrize(
    ("payload", "expected_names"),
    [
        (
            {
                "Name": "NVIDIA RTX 5090",
                "DriverVersion": "32.0.15.7652",
                "AdapterRAM": 16 * 1024 * 1024,
                "AdapterCompatibility": "NVIDIA",
            },
            ["NVIDIA RTX 5090"],
        ),
        (
            [
                {
                    "Name": "NVIDIA RTX 4090",
                    "DriverVersion": "32.0.15.7652",
                    "AdapterRAM": 24 * 1024 * 1024,
                    "AdapterCompatibility": "NVIDIA",
                },
                {
                    "Name": "Oray Virtual Display",
                    "DriverVersion": "1.0",
                    "AdapterRAM": 0,
                    "AdapterCompatibility": "Oray",
                },
            ],
            ["NVIDIA RTX 4090"],
        ),
    ],
)
def test_windows_cim_probe_accepts_single_object_or_array_and_never_launches_nvidia_smi(
    payload: object,
    expected_names: list[str],
    tmp_path: Path,
    monkeypatch,
) -> None:
    powershell = tmp_path / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.touch()
    spawned: list[list[str]] = []

    def fake_run(command, **kwargs):
        executable = Path(str(command[0])).name.casefold()
        assert executable not in {"nvidia-smi", "nvidia-smi.exe"}
        assert Path(command[0]) == powershell
        assert "Get-CimInstance Win32_VideoController" in command[-1]
        assert kwargs["timeout"] == 5
        spawned.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="win32"), raising=False)
    monkeypatch.setattr(hardware, "_video_controllers_cache_path", lambda: tmp_path / "missing.json", raising=False)
    monkeypatch.setattr(hardware, "_fixed_windows_powershell_path", lambda: powershell, raising=False)
    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    status = hardware.collect_local_hardware_status()["gpu"]

    assert status["available"] is True
    assert status["status"] == "ok"
    assert status["source"] == "windows-cim"
    assert [device["name"] for device in status["devices"]] == expected_names
    assert len(spawned) == 1


def test_windows_cim_launch_failure_degrades_without_falling_back_to_nvidia_smi(tmp_path: Path, monkeypatch) -> None:
    powershell = tmp_path / "powershell.exe"
    powershell.touch()
    calls: list[list[str]] = []

    def fail_cim(command, **_kwargs):
        calls.append(command)
        assert Path(command[0]) == powershell
        raise OSError("PowerShell failed")

    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="win32"), raising=False)
    monkeypatch.setattr(hardware, "_video_controllers_cache_path", lambda: tmp_path / "missing.json", raising=False)
    monkeypatch.setattr(hardware, "_fixed_windows_powershell_path", lambda: powershell, raising=False)
    monkeypatch.setattr(hardware.subprocess, "run", fail_cim)

    status = hardware.collect_local_hardware_status()["gpu"]

    assert calls and len(calls) == 1
    assert status == {
        "available": False,
        "status": "degraded",
        "source": "windows-cim",
        "error": "PowerShell failed",
        "devices": [],
    }


def test_non_windows_hardware_status_is_unavailable_without_spawning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="linux"), raising=False)
    monkeypatch.setattr(hardware, "_video_controllers_cache_path", lambda: tmp_path / "missing.json", raising=False)
    monkeypatch.setattr(hardware.subprocess, "run", _poison_nvidia_smi)

    status = hardware.collect_local_hardware_status()["gpu"]

    assert status == {
        "available": False,
        "status": "unavailable",
        "source": "platform",
        "error": "Windows CIM GPU discovery is unavailable on this platform",
        "devices": [],
    }


def test_ordinary_gpu_status_sources_do_not_reference_nvidia_smi() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    ordinary_sources = (
        repo_root / "backend" / "app" / "hardware.py",
        repo_root / "backend" / "app" / "workers" / "runtime.py",
        repo_root / "scripts" / "prepare-models.ps1",
    )

    for source in ordinary_sources:
        assert "nvidia-smi" not in source.read_text(encoding="utf-8").casefold(), source

    assert "nvidia-smi" in (repo_root / "backend" / "app" / "cuda_validation.py").read_text(encoding="utf-8").casefold()
    assert "nvidia-smi" in (repo_root / "scripts" / "start-cuda-gpu-monitor.ps1").read_text(encoding="utf-8").casefold()
