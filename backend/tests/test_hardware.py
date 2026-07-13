from __future__ import annotations

import subprocess

from app import hardware


def test_nvidia_smi_probe_uses_noninteractive_subprocess_kwargs(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="GPU, 16380, 100, 2, 40\n", stderr="")

    monkeypatch.setattr(hardware.shutil, "which", lambda _name: "nvidia-smi.exe")
    monkeypatch.setattr(hardware, "noninteractive_subprocess_kwargs", lambda: {"creationflags": 0x08000000})
    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    status = hardware.collect_local_hardware_status()["gpu"]

    assert captured["creationflags"] == 0x08000000
    assert status["status"] == "ok"
    assert status["devices"][0]["memory_total_mb"] == 16380


def test_nvidia_smi_probe_still_degrades_on_launch_failure(monkeypatch) -> None:
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: "nvidia-smi.exe")
    monkeypatch.setattr(hardware, "noninteractive_subprocess_kwargs", lambda: {})
    monkeypatch.setattr(hardware.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("failed")))

    status = hardware.collect_local_hardware_status()["gpu"]

    assert status == {"available": False, "status": "degraded", "error": "failed"}
