from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


BUILD_MARKER = ".portable-build.json"


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


def stop_worker(package_root: Path) -> int:
    """Stop only the process whose recorded executable belongs to this package."""
    root = package_root.resolve(strict=True)
    record = root / "data" / "local" / "run" / "worker.pid.json"
    if not record.is_file():
        return 0
    payload = json.loads(record.read_text(encoding="utf-8"))
    executable = Path(str(payload.get("executable_path") or "")).resolve(strict=False)
    _ensure_within(root, executable)
    pid = int(payload["pid"])
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
    record.unlink(missing_ok=True)
    return 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and stop a TTS More portable worker package")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare-runtime", "stop-worker"):
        subcommand = subcommands.add_parser(command)
        subcommand.add_argument("--package-root", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare-runtime":
        print(prepare_runtime(args.package_root))
        return 0
    if args.command == "stop-worker":
        return stop_worker(args.package_root)
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
