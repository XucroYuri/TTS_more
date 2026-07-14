from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID


PHASES = {
    "not_initialized",
    "checking",
    "downloading",
    "installing",
    "validating",
    "starting",
    "ready",
    "stopped",
    "repairable",
    "blocked",
}


def create_operation(root: Path, operation_id: str, component: str, action: str, initiator: str) -> dict[str, object]:
    directory = _operation_dir(root, operation_id)
    directory.mkdir(parents=True, exist_ok=True)
    operation: dict[str, object] = {
        "operation_id": directory.name,
        "component": component,
        "action": action,
        "initiator": initiator,
        "started_at": _timestamp(),
        "status": "not_initialized",
        "exit_code": None,
    }
    _write_json_atomic(directory / "operation.json", operation)
    return operation


def append_event(
    root: Path,
    operation_id: str,
    phase: str,
    message: str,
    *,
    percent: float | None = None,
    error_code: str | None = None,
) -> dict[str, object]:
    if phase not in PHASES:
        raise ValueError(f"unsupported operation phase: {phase}")
    directory = _operation_dir(root, operation_id)
    events_path = directory / "events.jsonl"
    seq = (
        sum(1 for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
        if events_path.exists()
        else 1
    )
    event: dict[str, object] = {
        "seq": seq,
        "timestamp": _timestamp(),
        "phase": phase,
        "message": message,
    }
    if percent is not None:
        event["percent"] = max(0.0, min(100.0, float(percent)))
    if error_code:
        event["error_code"] = error_code
    with events_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return event


def finish_operation(root: Path, operation_id: str, status: str, exit_code: int) -> dict[str, object]:
    if status not in PHASES:
        raise ValueError(f"unsupported operation status: {status}")
    directory = _operation_dir(root, operation_id)
    operation_path = directory / "operation.json"
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    operation["status"] = status
    operation["exit_code"] = int(exit_code)
    operation["finished_at"] = _timestamp()
    _write_json_atomic(operation_path, operation)
    return operation


def read_operation(root: Path, operation_id: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    directory = _operation_dir(root, operation_id)
    operation = json.loads((directory / "operation.json").read_text(encoding="utf-8"))
    events_path = directory / "events.jsonl"
    events = (
        [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if events_path.exists()
        else []
    )
    return operation, events


def _operation_dir(root: Path, operation_id: str) -> Path:
    canonical_id = _canonical_operation_id(operation_id)
    operations_root = Path(root).resolve()
    directory = (operations_root / canonical_id).resolve()
    try:
        directory.relative_to(operations_root)
    except ValueError as error:
        raise ValueError(f"operation directory escapes operations root: {operation_id}") from error
    return directory


def _canonical_operation_id(operation_id: str) -> str:
    try:
        parsed = UUID(operation_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"operation_id must be a valid UUID: {operation_id}") from error
    return str(parsed)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
