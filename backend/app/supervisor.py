from __future__ import annotations

import json
import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import TTSServiceEndpoint


class ServiceSupervisor:
    def __init__(self, project_root: Path, runtime_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.runtime_root = runtime_root
        self.records_dir = runtime_root / "services"
        self.logs_dir = runtime_root / "logs"

    def status(self, endpoint: TTSServiceEndpoint) -> dict[str, Any]:
        record = self._read_record(endpoint.service_id)
        return {
            "service_id": endpoint.service_id,
            "manageable": self._manageable(endpoint),
            "record": record,
            "running": self._is_pid_running(record.get("pid")) if record else False,
        }

    def start(self, endpoint: TTSServiceEndpoint) -> dict[str, Any]:
        if endpoint.mode != "local" or not endpoint.managed:
            return {"status": "not manageable", "reason": f"{endpoint.service_id} is {endpoint.mode}"}
        if not endpoint.start_command:
            return {"status": "not manageable", "reason": f"{endpoint.service_id} has no start_command"}

        existing = self._read_record(endpoint.service_id)
        if existing and self._is_pid_running(existing.get("pid")):
            return {"status": "already running", **existing}

        command = self._resolve_command(endpoint.start_command)
        cwd = self._resolve_cwd(endpoint)
        log_path = self.logs_dir / f"{endpoint.service_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_path.open("ab")
        popen_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": {**os.environ, **self._resolve_env(endpoint.env)},
            "close_fds": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        process = subprocess.Popen(command, **popen_kwargs)
        log_file.close()
        record = {
            "service_id": endpoint.service_id,
            "pid": process.pid,
            "command": command,
            "cwd": str(cwd),
            "log_path": str(log_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._record_path(endpoint.service_id).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "started", **record}

    def stop(self, service_id: str) -> dict[str, Any]:
        record = self._read_record(service_id)
        if not record:
            return {"status": "not running", "service_id": service_id}
        pid = record.get("pid")
        if pid:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
            else:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
        self._record_path(service_id).unlink(missing_ok=True)
        return {"status": "stopped", "service_id": service_id, "pid": pid}

    def logs(self, service_id: str, lines: int = 120) -> dict[str, Any]:
        record = self._read_record(service_id)
        if not record:
            return {"status": "missing", "service_id": service_id, "lines": []}
        log_path = Path(record.get("log_path", ""))
        if not log_path.exists():
            return {"status": "missing log", "service_id": service_id, "lines": []}
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"status": "ok", "service_id": service_id, "log_path": str(log_path), "lines": content[-max(1, lines):]}

    def _manageable(self, endpoint: TTSServiceEndpoint) -> bool:
        return endpoint.mode == "local" and endpoint.managed and bool(endpoint.start_command)

    def _record_path(self, service_id: str) -> Path:
        safe = service_id.replace("/", "_").replace("\\", "_")
        return self.records_dir / f"{safe}.json"

    def _read_record(self, service_id: str) -> dict[str, Any] | None:
        path = self._record_path(service_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _resolve_command(self, command: list[str]) -> list[str]:
        if not command:
            return command
        executable = Path(command[0])
        if not executable.is_absolute() and ("\\" in command[0] or "/" in command[0]):
            candidate = (self.project_root / executable).resolve()
            if self._inside_project(candidate):
                return [str(candidate), *command[1:]]
        return command

    def _resolve_cwd(self, endpoint: TTSServiceEndpoint) -> Path:
        raw = endpoint.start_cwd or ("." if endpoint.service_id in {"local-indextts"} else endpoint.repo_path) or "."
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        resolved = candidate.resolve(strict=False)
        if not self._inside_project(resolved):
            raise ValueError(f"start_cwd escapes project root: {raw}")
        return resolved

    def _resolve_env(self, env: dict[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for key, value in env.items():
            if key.upper() == "PATH":
                resolved[key] = self._resolve_path_env(value)
                continue
            if key.endswith("_PATH") or key.endswith("_DIR") or key.endswith("_PYTHON"):
                path = Path(value)
                if not path.is_absolute() and ("\\" in value or "/" in value):
                    candidate = (self.project_root / path).resolve(strict=False)
                    if self._inside_project(candidate):
                        resolved[key] = str(candidate)
                        continue
            resolved[key] = value
        return resolved

    def _resolve_path_env(self, value: str) -> str:
        parts: list[str] = []
        for item in value.replace("%PATH%", "{PATH}").split(os.pathsep):
            if item == "{PATH}":
                parts.append(os.environ.get("PATH", ""))
                continue
            path = Path(item)
            if item and not path.is_absolute() and ("\\" in item or "/" in item):
                candidate = (self.project_root / path).resolve(strict=False)
                if self._inside_project(candidate):
                    parts.append(str(candidate))
                    continue
            parts.append(item)
        return os.pathsep.join(part for part in parts if part)

    def _inside_project(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self.project_root)
            return True
        except ValueError:
            return False

    def _is_pid_running(self, pid: Any) -> bool:
        if not pid:
            return False
        try:
            if os.name == "nt":
                result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}"], capture_output=True, text=True, check=False)
                return str(pid) in result.stdout
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False
