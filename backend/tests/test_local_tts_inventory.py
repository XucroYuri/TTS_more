"""Check operator-owned inventories without loading models or touching sources."""
import importlib.util
import json
import subprocess
import sys
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_inventory_keeps_complete_pairs_and_reports_both_orphan_weight_types(tmp_path):
    source = tmp_path / "native"
    for folder in ("GPT_weights_v2", "SoVITS_weights_v2", "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large", "GPT_SoVITS/pretrained_models/chinese-hubert-base"):
        (source / folder).mkdir(parents=True)
    for name in ("hero-e10.ckpt", "hero-e20.ckpt", "gpt-only-e5.ckpt"):
        (source / "GPT_weights_v2" / name).touch()
    for name in ("hero_e12_s120.pth", "sovits-only_e5_s50.pth"):
        (source / "SoVITS_weights_v2" / name).touch()
    logs = source / "logs" / "renamed-session"
    logs.mkdir(parents=True)
    (logs / "2-name2text.txt").write_text("annotated.wav\tspeaker\tzh\tReference text", encoding="utf-8")
    (logs / "notes.txt").write_text("This is not a training annotation", encoding="utf-8")
    output = tmp_path / "integration"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"version": 1, "integration_dir": str(output), "gpt_sovits_projects": [{"id": "native-one", "source_root": str(source), "python_executable": sys.executable, "logs_mapping": {"hero": str(logs)}}]}), encoding="utf-8")
    before = sorted(str(path.relative_to(source)) for path in source.rglob("*"))

    result = subprocess.run([sys.executable, str(ROOT / "scripts/inventory-local-tts.py"), "--config", str(config)], capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == {"registered": 1, "training_names": 3, "unpaired": 2}
    resources = json.loads((output / "resources.yaml").read_text(encoding="utf-8"))["resources"]
    resource = next(iter(resources.values()))
    assert Path(resource["gpt_weight"]).name == "hero-e20.ckpt"
    assert Path(resource["sovits_weight"]).name == "hero_e12_s120.pth"
    assert all(Path(value).is_absolute() for key, value in resource.items() if key not in {"engine", "version"})
    inventory = json.loads((output / "model-inventory.json").read_text(encoding="utf-8"))
    hero = next(item for item in inventory if item["name"] == "hero")
    assert hero["annotation_files"] == [str(logs / "2-name2text.txt")]
    assert sorted(str(path.relative_to(source)) for path in source.rglob("*")) == before


def test_relative_operator_paths_use_repository_root(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("local_tts_config_for_test", ROOT / "scripts/local_tts_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path / "workstation")
    assert module.source_path("../native/models") == tmp_path / "native" / "models"
    assert module.integration_folder({}) == tmp_path / "workstation" / "data/local/comfyui"


def test_gpt_reference_defaults_skip_short_long_unannotated_and_missing_audio(tmp_path):
    import numpy as np
    import soundfile
    spec = importlib.util.spec_from_file_location("local_tts_reference_for_test", ROOT / "scripts/local_tts_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    samples = [{'path':str(tmp_path/'missing.wav'),'text':'Missing audio'}]
    for name,seconds,text in [('short',2,'Short'),('long',11,'Long'),('unannotated',4,''),('valid',4,'Valid annotation')]:
        path = tmp_path/(name+'.wav')
        soundfile.write(path,np.ones(seconds*8000)*.1,8000)
        samples.append({'path':str(path),'text':text})
    assert module.gpt_reference_sample(samples) == samples[-1]
    assert module.gpt_reference_sample(samples[:-1]) is None


def test_workflow_export_replaces_changed_cached_audio_and_reuses_current_asset(tmp_path):
    import numpy as np
    import soundfile
    audio = tmp_path/'selected.wav'
    soundfile.write(audio,np.ones(4*8000)*.1,8000)
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    integration = tmp_path/'integration'
    integration.mkdir()
    comfy = tmp_path/'comfy'
    asset_root = comfy/'input/tts-audio-suite'
    asset_root.mkdir(parents=True)
    (asset_root/'old.wav').write_bytes(b'old reference')
    (integration/'resources.yaml').write_text(json.dumps({'resources':{'hero':{'engine':'gpt_sovits'}}}),encoding='utf-8')
    (integration/'workflow-assets.json').write_text(json.dumps({'hero':{'asset_id':'old','filename':'old.wav','sha256':'0'*64}}),encoding='utf-8')
    uploads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path=='/api/characters'
            self.send_json([{'id':'hero','profiles':[{'config':{'reference_audio':str(audio),'prompt_text':'Reference annotation'}}]}])

        def do_POST(self):
            assert self.path=='/api/tts-audio-suite/v1/assets/audio'
            uploads.append(self.rfile.read(int(self.headers['Content-Length'])))
            (asset_root/'replacement.wav').write_bytes(audio.read_bytes())
            self.send_json({'asset_id':'replacement','filename':'replacement.wav','sha256':digest})

    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'version':1,'integration_dir':str(integration),'backend_url':url,'comfyui':{'source_root':str(comfy),'base_url':url}}),encoding='utf-8')
    try:
        for _ in range(2):
            subprocess.run([sys.executable,str(ROOT/'scripts/export-comfyui-workflows.py'),'--config',str(config)],capture_output=True,text=True,check=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert len(uploads)==1
    workflow = json.loads((integration/'workflows/hero.api.json').read_text(encoding='utf-8'))
    assert workflow['2']['inputs']=={'asset_id':'replacement','reference_text':'Reference annotation'}
    assert (comfy/'user/default/workflows/TTSMore/hero.api.json').read_bytes()==(integration/'workflows/hero.api.json').read_bytes()
    assert (asset_root/'old.wav').exists()
