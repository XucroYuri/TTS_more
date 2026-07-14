from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


BUILD_MARKER = ".portable-build.json"
ProcessInspector = Callable[[int], dict[str, object] | None]
Terminator = Callable[[int], None]
PortInspector = Callable[[int], bool]


def run(command: list[str], **kwargs: Any) -> None:
    subprocess.run(command, check=True, **kwargs)


def extract_archive(archive: Path, destination: Path) -> None:
    powershell = shutil.which("powershell") or "powershell"
    command = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force",
        str(archive),
        str(destination),
    ]
    run(command)


def prepare_runtime(package_root: Path) -> Path:
    """Restore the package-local runtime when this package moves directories."""
    root = package_root.resolve(strict=True)
    manifest_path = root / "package" / "tts-more-package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    build_id = str(manifest["build_id"])
    archive = _relative_path(root, str(manifest["runtime"]))
    if not archive.is_file():
        raise FileNotFoundError(f"portable runtime archive is missing: {archive}")
    live = root / "runtime" / "live"
    marker = live / BUILD_MARKER
    if _marker_matches(marker, build_id):
        return live
    if (live / "python.exe").is_file():
        _run_conda_unpack(live)
        marker.write_text(json.dumps({"build_id": build_id}, sort_keys=True), encoding="utf-8")
        return live
    if live.exists():
        shutil.rmtree(live)
    live.parent.mkdir(parents=True, exist_ok=True)
    extract_archive(archive, live)
    _run_conda_unpack(live)
    marker.write_text(json.dumps({"build_id": build_id}, sort_keys=True), encoding="utf-8")
    return live


def write_process_record(
    record_path: Path,
    *,
    pid: int,
    parent_pid: int,
    child_pids: Iterable[int],
    process_created_at: str,
    executable_path: Path,
    command: Iterable[str],
    port: int,
    package_root: Path,
    build_id: str,
) -> None:
    """Persist enough immutable identity to reject stale or foreign PIDs."""
    root = package_root.resolve(strict=False)
    executable = executable_path.resolve(strict=False)
    _ensure_within(root, executable)
    command_digest = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()
    payload = {
        "schema_version": 2,
        "pid": int(pid),
        "parent_pid": int(parent_pid),
        "child_pids": [int(child) for child in child_pids],
        "process_created_at": process_created_at,
        "recorded_at": datetime.now(UTC).isoformat(),
        "executable_path": str(executable),
        "command_sha256": command_digest,
        "port": int(port),
        "package_root": str(root),
        "build_id": str(build_id),
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = record_path.with_suffix(record_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, record_path)


def listener_is_owned(
    record_path: Path,
    *,
    package_root: Path,
    port: int,
    build_id: str,
    executable_path: Path,
    command: Iterable[str],
    listener_pids: set[int],
    inspector: ProcessInspector | None = None,
) -> bool:
    """Return true only when record, listener, process, command, and build identities agree."""
    try:
        root = package_root.resolve(strict=True)
        executable = executable_path.resolve(strict=True)
        _ensure_within(root, executable)
        if executable != (root / "runtime" / "live" / "python.exe").resolve(strict=False):
            return False
        payload = json.loads(record_path.read_text(encoding="utf-8-sig"))
        if int(payload.get("schema_version") or 0) != 2:
            return False
        if Path(str(payload.get("package_root") or "")).resolve(strict=False) != root:
            return False
        if Path(str(payload.get("executable_path") or "")).resolve(strict=False) != executable:
            return False
        pid = int(payload.get("pid") or 0)
        expected_command = list(command)
        command_digest = hashlib.sha256("\0".join(expected_command).encode("utf-8")).hexdigest()
        if (
            pid not in listener_pids
            or int(payload.get("port") or 0) != int(port)
            or str(payload.get("build_id") or "") != str(build_id)
            or str(payload.get("command_sha256") or "") != command_digest
        ):
            return False
        process = (inspector or _inspect_process)(pid)
        if process is None or int(process.get("pid") or 0) != pid:
            return False
        if str(process.get("created_at") or "") != str(payload.get("process_created_at") or ""):
            return False
        if Path(str(process.get("executable_path") or "")).resolve(strict=False) != executable:
            return False
        actual_command = process.get("command_args")
        if actual_command is not None and list(actual_command) != expected_command:
            return False
        return actual_command is not None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def stop_worker(
    package_root: Path,
    *,
    inspector: ProcessInspector | None = None,
    terminator: Terminator | None = None,
    port_is_listening: PortInspector | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = 15,
) -> int:
    """Stop only an owned process and retain evidence until its port is released."""
    root = package_root.resolve(strict=True)
    record = root / "data" / "local" / "run" / "worker.pid.json"
    if not record.is_file():
        return 0
    payload = json.loads(record.read_text(encoding="utf-8-sig"))
    if int(payload.get("schema_version") or 0) != 2:
        return _stop_legacy_worker(root, record, payload)
    recorded_root = Path(str(payload.get("package_root") or "")).resolve(strict=False)
    if recorded_root != root:
        raise ValueError("PID record belongs to a different package root")
    executable = Path(str(payload.get("executable_path") or "")).resolve(strict=False)
    _ensure_within(root, executable)
    pid = int(payload["pid"])
    port = int(payload["port"])
    inspect = inspector or _inspect_process
    terminate = terminator or _terminate_process_tree
    is_listening = port_is_listening or _port_is_listening
    process = inspect(pid)
    listening = is_listening(port)
    if process is not None:
        actual_executable = Path(str(process.get("executable_path") or "")).resolve(strict=False)
        created_at = str(process.get("created_at") or "")
        if actual_executable != executable or created_at != str(payload.get("process_created_at") or ""):
            raise RuntimeError("recorded PID identity does not match the running process")
        terminate(pid)
    elif not listening:
        record.unlink(missing_ok=True)
        return 0

    deadline = time.monotonic() + max(0, timeout_seconds)
    while time.monotonic() <= deadline:
        process = inspect(pid)
        listening = is_listening(port)
        if process is None and not listening:
            record.unlink(missing_ok=True)
            return 0
        if timeout_seconds <= 0:
            break
        sleep(min(0.2, timeout_seconds))
    return 2


def _stop_legacy_worker(root: Path, record: Path, payload: dict[str, object]) -> int:
    """Compatibility path for schema-v1 packages; all newly built packages use v2."""
    executable = Path(str(payload.get("executable_path") or "")).resolve(strict=False)
    _ensure_within(root, executable)
    pid = int(payload["pid"])
    if os.name != "nt":
        raise RuntimeError("portable process termination is supported only on Windows")
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
    record.unlink(missing_ok=True)
    return 0


def _inspect_process(pid: int) -> dict[str, object] | None:
    if os.name != "nt":
        return None
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$p=Get-Process -Id $args[0] -ErrorAction SilentlyContinue; "
        "$c=Get-CimInstance Win32_Process -Filter ('ProcessId='+$args[0]) -ErrorAction SilentlyContinue; "
        "if($p){@{pid=$p.Id;created_at=$p.StartTime.ToUniversalTime().ToString('o');"
        "executable_path=$p.Path;command_line=if($c){$c.CommandLine}else{''}}|ConvertTo-Json -Compress}",
        str(pid),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    output = completed.stdout.strip()
    if not output:
        return None
    payload = json.loads(output)
    command_line = str(payload.pop("command_line", "") or "")
    payload["command_args"] = _split_windows_command_line(command_line)[1:] if command_line else None
    return payload


def _split_windows_command_line(command_line: str) -> list[str]:
    if os.name != "nt":
        return []
    import ctypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(command_line, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(argv)


def _terminate_process_tree(pid: int) -> None:
    if os.name != "nt":
        raise RuntimeError("portable process termination is supported only on Windows")
    completed = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, text=True
    )
    if completed.returncode not in (0, 128):
        raise RuntimeError(f"failed to terminate owned process {pid}: {completed.stderr.strip()}")


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _run_conda_unpack(live: Path) -> None:
    candidates = (live / "Scripts" / "conda-unpack.exe", live / "conda-unpack.exe")
    for executable in candidates:
        if executable.is_file():
            run([str(executable)], cwd=live)
            return


def _marker_matches(marker: Path, build_id: str) -> bool:
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("build_id") == build_id
    except (OSError, json.JSONDecodeError):
        return False


def _relative_path(root: Path, value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute() or ":" in value or ".." in candidate.parts:
        raise ValueError("portable manifest path must be relative")
    path = (root / candidate).resolve(strict=False)
    _ensure_within(root, path)
    return path


def _ensure_within(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside portable package: {path}") from exc


def _command_arguments(values: Iterable[str]) -> list[str]:
    arguments = list(values)
    return arguments[1:] if arguments and arguments[0] == "--" else arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and stop a TTS More portable worker package")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare-runtime", "stop-worker"):
        subcommand = subcommands.add_parser(command)
        subcommand.add_argument("--package-root", required=True, type=Path)
    record = subcommands.add_parser("write-process-record")
    record.add_argument("--package-root", required=True, type=Path)
    record.add_argument("--record-path", required=True, type=Path)
    record.add_argument("--pid", required=True, type=int)
    record.add_argument("--parent-pid", required=True, type=int)
    record.add_argument("--process-created-at", required=True)
    record.add_argument("--executable", required=True, type=Path)
    record.add_argument("--port", required=True, type=int)
    record.add_argument("--build-id", required=True)
    record.add_argument("command_args", nargs=argparse.REMAINDER)
    verify = subcommands.add_parser("verify-owned-listener")
    verify.add_argument("--package-root", required=True, type=Path)
    verify.add_argument("--record-path", required=True, type=Path)
    verify.add_argument("--port", required=True, type=int)
    verify.add_argument("--build-id", required=True)
    verify.add_argument("--executable", required=True, type=Path)
    verify.add_argument("--listener-pid", action="append", required=True, type=int)
    verify.add_argument("command_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "prepare-runtime":
        print(prepare_runtime(args.package_root))
        return 0
    if args.command == "stop-worker":
        return stop_worker(args.package_root)
    if args.command == "write-process-record":
        write_process_record(
            args.record_path,
            pid=args.pid,
            parent_pid=args.parent_pid,
            child_pids=[],
            process_created_at=args.process_created_at,
            executable_path=args.executable,
            command=_command_arguments(args.command_args),
            port=args.port,
            package_root=args.package_root,
            build_id=args.build_id,
        )
        return 0
    if args.command == "verify-owned-listener":
        return 0 if listener_is_owned(
            args.record_path,
            package_root=args.package_root,
            port=args.port,
            build_id=args.build_id,
            executable_path=args.executable,
            command=_command_arguments(args.command_args),
            listener_pids=set(args.listener_pid),
        ) else 3
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
