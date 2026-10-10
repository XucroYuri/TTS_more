from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service_store_io import ServiceDocument
from app.comfyui.workflow_builder import build_indextts_workflow

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts/start-workstation.ps1"
POWERSHELL = "powershell.exe"
spec = importlib.util.spec_from_file_location("launch_services", ROOT / "scripts/configure-launch-services.py")
assert spec and spec.loader
launch_services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch_services)


def test_readiness_does_not_probe_model_workers(tmp_path, monkeypatch):
    services = tmp_path / "services.json"
    services.write_text("[]", encoding="utf-8")
    monkeypatch.delenv("TTS_MORE_API_TOKEN", raising=False)
    app = create_app(data_root=tmp_path, services_path=services)
    def forbidden():
        raise AssertionError("readiness must not call external workers")
    monkeypatch.setattr(app.state.service_router, "health", forbidden)
    with TestClient(app) as client:
        response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "project_root": str(ROOT.resolve()), "pid": os.getpid(), "comfyui_urls": {}}


@pytest.mark.parametrize("value,expected", [(False, "false"), (True, "true"), (None, "auto"), ("AUTO", "auto")])
def test_index_cuda_kernel_values_match_comfy_node_options(value, expected):
    workflow = build_indextts_workflow({"resource_id": "test", "text": "hello", "use_cuda_kernel": value})
    assert workflow["1"]["inputs"]["use_cuda_kernel"] == expected


def test_port_remap_preserves_remote_custom_and_managed_endpoints():
    urls = ["http://127.0.0.1:8188", "http://localhost:8189", "http://example.com:8188",
            "http://127.0.0.1:9000", "http://127.0.0.1:8188"]
    items = [{"service_id": str(index), "api_contract": "comfyui-tts-audio-suite-v1", "base_url": url}
             for index, url in enumerate(urls)]
    items[4]["portable_locator"] = {"package_id": "owned"}
    items.append({"service_id": "worker", "api_contract": "tts-more-v1", "base_url": urls[0]})
    original = ServiceDocument(1, items)
    result = launch_services.remap(original, 8190, 8189)
    assert [item["base_url"] for item in result.services] == [
        "http://127.0.0.1:8190", "http://127.0.0.1:8190", *urls[2:], urls[0]]
    assert original.services[0]["base_url"] == urls[0]


def ps(code, timeout=35):
    # File-backed output prevents delegated Windows children retaining pipe handles.
    path = ROOT / "data/local/run" / f"launcher-test-{uuid.uuid4().hex}.txt"
    with path.open("w+", encoding="utf-8") as output:
        completed = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                                   f"$env:TTS_MORE_LAUNCH_LANGUAGE='en'; . '{LAUNCHER}'; {code}"], stdout=output, stderr=output, timeout=timeout)
        output.seek(0)
        text = output.read()
    assert completed.returncode == 0, text
    return text


@pytest.mark.skipif(os.name != "nt", reason="Windows startup integration")
def test_port_conflict_falls_back_without_disrupting_listener():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        chosen = int(ps(f"Find-LaunchPort {port}").strip())
        assert chosen != port
        # The unrelated listener remains reachable after selection.
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass


@pytest.mark.skipif(os.name != "nt", reason="Windows startup integration")
def test_owner_check_distinguishes_similarly_named_workspaces():
    result = ps(r"""
        $service=@{Name='frontend';Exe='C:\node.exe';Directory='E:\TTSMore\frontend';Script='E:\TTSMore\frontend\node_modules\vite\bin\vite.js'}
        $own=@{ExecutablePath='C:\node.exe';CommandLine='node E:\TTSMore\frontend\node_modules\.bin\..\vite\bin\vite.js'}
        $other=@{ExecutablePath='C:\node.exe';CommandLine='node E:\TTS_more\frontend\node_modules\vite\bin\vite.js'}
        @((Test-LaunchOwner $own $service),(Test-LaunchOwner $other $service)) | ConvertTo-Json -Compress
    """)
    assert json.loads(result) == [True, False]


@pytest.mark.skipif(os.name != "nt", reason="Windows startup integration")
@pytest.mark.parametrize("bind_race", [False, True])
def test_launch_retries_bind_race_and_checks_frontend_proxy(tmp_path, bind_race):
    frontend = tmp_path / "frontend with spaces"
    frontend.mkdir()
    script = frontend / "vite.js"
    script.write_text('''import argparse, json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
p=argparse.ArgumentParser(); p.add_argument('--host'); p.add_argument('--port',type=int); p.add_argument('--strictPort',action='store_true'); args=p.parse_args()
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
  self.wfile.write(json.dumps({'status':'ok','project_root':'test-root','pid':123}).encode())
 def log_message(self,*args): pass
try: HTTPServer((args.host,args.port),Handler).serve_forever()
except OSError: print('Port is already in use',file=sys.stderr); sys.exit(1)
''', encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    entry_file = tmp_path / "entry.json"
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        race = """
        $originalFind=(Get-Item Function:Find-LaunchPort).ScriptBlock
        $script:selectionCount=0
        function Find-LaunchPort([int]$Preferred,[int[]]$Reserved=@()) {
            $script:selectionCount++
            if ($script:selectionCount -eq 1) { return $Preferred }
            & $originalFind $Preferred $Reserved
        }
        """ if bind_race else ""
        pid = None
        try:
            output = ps(f"""
                {race}
                $logs='{logs}'; $TimeoutSeconds=8
                $service=@{{Name='frontend';Exe='{sys.executable}';Directory='{frontend}';Script='{script}';Port={port}}}
                $entry=Start-LaunchService $service @() @{{project_root='test-root';pid=123}}
                Write-LaunchRecord $entry '{entry_file}'
                Test-LaunchReady $service $entry.port @{{project_root='test-root';pid=999}}
            """)
            entry = json.loads(entry_file.read_text(encoding="utf-8"))
            pid = entry["pid"]
            assert entry["port"] > port
            assert not entry["reused"]
            assert output.strip().endswith("False")  # wrong upstream backend rejected
            if bind_race:
                assert "attempt 2/3" in output
        finally:
            if pid is None and entry_file.exists():
                pid = json.loads(entry_file.read_text(encoding="utf-8"))["pid"]
            if pid:
                os.kill(pid, 15)


@pytest.mark.skipif(os.name != "nt", reason="Windows startup integration")
def test_import_failure_reports_log_without_waiting_for_timeout(tmp_path):
    script = tmp_path / "vite.js"
    script.write_text("import sys; print('missing test dependency',file=sys.stderr); sys.exit(7)", encoding="utf-8")
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    began = time.monotonic()
    output = ps(f"""
        $logs='{tmp_path}'; $TimeoutSeconds=20
        $service=@{{Name='frontend';Exe='{sys.executable}';Directory='{tmp_path}';Script='{script}';Port={port}}}
        try {{ Start-LaunchService $service @() @{{}}; exit 2 }}
        catch {{ Write-Output $_.Exception.Message }}
    """)
    assert "missing test dependency" in output
    assert time.monotonic() - began < 15
