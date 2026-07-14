from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Mapping


SUPPORTED_COMPONENTS = {"gpt-sovits", "indextts", "cosyvoice"}


def build_worker_process(
    package_root: Path, environ: Mapping[str, str] | None = None
) -> tuple[list[str], Path, dict[str, str]]:
    root = package_root.expanduser().resolve(strict=True)
    manifest = json.loads((root / "package" / "tts-more-package.json").read_text(encoding="utf-8-sig"))
    config = json.loads((root / "tts_more" / "component.json").read_text(encoding="utf-8-sig"))
    component = str(manifest.get("component") or "")
    if component not in SUPPORTED_COMPONENTS or config.get("component") != component:
        raise ValueError("portable package component metadata is invalid")
    if manifest.get("api_contract") != "tts-more-v1":
        raise ValueError("portable package API contract is not tts-more-v1")

    runtime_python = root / "runtime" / "live" / "python.exe"
    if not runtime_python.is_file():
        raise FileNotFoundError("portable package runtime is missing; run Initialize.cmd first")
    module = str(config.get("module") or "")
    if not module.startswith("tts_more_worker.") or not module.endswith(":app"):
        raise ValueError("portable worker module is invalid")
    source_env = dict(os.environ if environ is None else environ)
    port = int(source_env.get("TTS_MORE_PORT") or config.get("port") or 0)
    if not 1 <= port <= 65535:
        raise ValueError("portable worker port is invalid")
    trusted_lan = source_env.get("TTS_MORE_TRUSTED_LAN") == "1"
    host = "0.0.0.0" if trusted_lan else "127.0.0.1"

    worker_env = {**source_env}
    worker_env["TTS_MORE_PACKAGE_ROOT"] = str(root)
    worker_env["TTS_MORE_ARTIFACT_ROOT"] = str(root / "data" / "local" / "artifacts")
    if trusted_lan:
        worker_env.pop("TTS_MORE_WORKER_ALLOW_PATH_DELIVERY", None)
    else:
        worker_env["TTS_MORE_WORKER_ALLOW_PATH_DELIVERY"] = "1"
    if component == "gpt-sovits":
        worker_env["TTS_MORE_GPTSOVITS_REPO"] = str(root)
    elif component == "indextts":
        worker_env["TTS_MORE_INDEXTTS_REPO"] = str(root)
        worker_env["TTS_MORE_INDEXTTS_PYTHON"] = str(runtime_python)
    else:
        worker_env["TTS_MORE_COSYVOICE_REPO"] = str(root)
        worker_env["TTS_MORE_COSYVOICE_MODEL_DIR"] = str(root / "pretrained_models" / "CosyVoice-300M")

    command = [
        str(runtime_python),
        "-m",
        "uvicorn",
        module,
        "--app-dir",
        str(root / "tts_more"),
        "--host",
        host,
        "--port",
        str(port),
    ]
    return command, root, worker_env


def port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.4)
        return client.connect_ex((host, port)) == 0


def windows_port_owner(port: int) -> str:
    if os.name != "nt":
        return "owner lookup is only available on Windows"
    script = (
        f"$items=@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
        "ForEach-Object {$p=Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{pid=$_.OwningProcess;name=if($p){$p.ProcessName}else{'unknown'};"
        "path=if($p){$p.Path}else{'unknown'}}}); ConvertTo-Json -InputObject $items -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.stdout.strip() or "owner unavailable"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a validated sibling TTS More portable worker in the foreground")
    parser.add_argument("--package-root", required=True, type=Path)
    args = parser.parse_args(argv)
    command, cwd, environment = build_worker_process(args.package_root)
    port = int(command[-1])
    if port_is_listening(port):
        print(f"worker port {port} is already in use; owner: {windows_port_owner(port)}", file=sys.stderr)
        return 3
    process = subprocess.Popen(command, cwd=cwd, env=environment)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            return process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
