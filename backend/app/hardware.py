from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any


def collect_local_hardware_status() -> dict[str, Any]:
    return {
        "host": "local",
        "gpu": _nvidia_smi_status(),
        "system": _system_status(),
    }


def _nvidia_smi_status() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "status": "unavailable", "error": "nvidia-smi not found"}
    query = "name,memory.total,memory.used,utilization.gpu,temperature.gpu"
    try:
        completed = subprocess.run(
            [
                executable,
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        return {"available": False, "status": "degraded", "error": str(exc)}
    if completed.returncode != 0:
        return {"available": False, "status": "degraded", "error": (completed.stderr or completed.stdout).strip()}
    devices = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        devices.append(
            {
                "name": parts[0],
                "memory_total_mb": _int_or_none(parts[1]),
                "memory_used_mb": _int_or_none(parts[2]),
                "utilization_percent": _int_or_none(parts[3]),
                "temperature_c": _int_or_none(parts[4]),
            }
        )
    return {"available": bool(devices), "status": "ok" if devices else "degraded", "devices": devices}


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


def _int_or_none(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None
