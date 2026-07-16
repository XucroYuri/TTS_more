from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from app.subprocess_safety import noninteractive_subprocess_kwargs


_CIM_QUERY = (
    "Get-CimInstance Win32_VideoController -ErrorAction Stop | "
    "Select-Object Name,DriverVersion,AdapterRAM,AdapterCompatibility,PNPDeviceID | "
    "ConvertTo-Json -Compress"
)


def collect_local_hardware_status() -> dict[str, Any]:
    return {
        "host": "local",
        "gpu": _gpu_status(),
        "system": _system_status(),
    }


def _gpu_status() -> dict[str, Any]:
    cached = _read_controller_cache(_video_controllers_cache_path())
    if cached is not None:
        return _controller_status(cached, source="portable-cache")
    if sys.platform != "win32":
        return {
            "available": False,
            "status": "unavailable",
            "source": "platform",
            "error": "Windows CIM GPU discovery is unavailable on this platform",
            "devices": [],
        }
    return _windows_cim_status()


def _video_controllers_cache_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "cache" / "portable" / "video-controllers.json"


def _read_controller_cache(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _controller_items(payload)


def _fixed_windows_powershell_path() -> Path:
    system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _windows_cim_status() -> dict[str, Any]:
    powershell = _fixed_windows_powershell_path()
    if not powershell.is_file():
        return {
            "available": False,
            "status": "unavailable",
            "source": "windows-cim",
            "error": "Windows PowerShell is unavailable at the fixed system path",
            "devices": [],
        }
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _CIM_QUERY,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **noninteractive_subprocess_kwargs(),
        )
    except Exception as exc:
        return {
            "available": False,
            "status": "degraded",
            "source": "windows-cim",
            "error": str(exc),
            "devices": [],
        }
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout).strip() or "Windows CIM GPU discovery failed"
        return {
            "available": False,
            "status": "degraded",
            "source": "windows-cim",
            "error": error,
            "devices": [],
        }
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "status": "degraded",
            "source": "windows-cim",
            "error": f"Windows CIM returned invalid JSON: {exc}",
            "devices": [],
        }
    return _controller_status(_controller_items(payload), source="windows-cim")


def _controller_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _controller_status(controllers: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    for controller in controllers:
        name = str(controller.get("name") or controller.get("Name") or "").strip()
        compatibility = str(
            controller.get("adapter_compatibility") or controller.get("AdapterCompatibility") or ""
        ).strip()
        if "nvidia" not in f"{name} {compatibility}".casefold():
            continue
        driver_version = str(
            controller.get("driver_version") or controller.get("DriverVersion") or ""
        ).strip() or None
        adapter_ram = controller.get("adapter_ram", controller.get("AdapterRAM"))
        devices.append(
            {
                "name": name or "NVIDIA GPU",
                "driver_version": driver_version,
                "memory_total_mb": _bytes_to_mb(adapter_ram),
            }
        )
    if not devices:
        return {
            "available": False,
            "status": "unavailable",
            "source": source,
            "error": "No NVIDIA video controller was reported",
            "devices": [],
        }
    return {"available": True, "status": "ok", "source": source, "devices": devices}


def _bytes_to_mb(value: Any) -> int | None:
    try:
        bytes_value = int(value)
    except (TypeError, ValueError):
        return None
    return bytes_value // (1024 * 1024) if bytes_value > 0 else None


def _system_status() -> dict[str, Any]:
    try:
        load = os.getloadavg() if hasattr(os, "getloadavg") else None
    except OSError:
        load = None
    return {
        "pid": os.getpid(),
        "load_average": load,
        "status": "ok",
    }
