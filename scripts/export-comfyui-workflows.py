"""Export runnable API prompts backed by persistent bridge reference assets."""
import json
import sys
from pathlib import Path

import httpx
from local_tts_config import ROOT, integration_folder, load_config, source_path

config, _ = load_config(__doc__)
root = ROOT
sys.path.insert(0, str(root / 'backend'))
from app.comfyui.workflow_builder import build_workflow

folder = integration_folder(config)
comfy_root = source_path(config['comfyui']['source_root'])
comfy_url = config['comfyui']['base_url'].rstrip('/')
destination = source_path(config['comfyui'].get('workflow_directory', comfy_root / 'user/default/workflows/TTSMore'))
destination.mkdir(parents=True, exist_ok=True)
workflows = folder / 'workflows'
workflows.mkdir(exist_ok=True)
index_path = folder / 'workflow-assets.json'
assets = json.loads(index_path.read_text(encoding='utf-8')) if index_path.exists() else {}
exported = []
pending = []
with httpx.Client(timeout=120, trust_env=False) as client:
    response = client.get(config['backend_url'].rstrip('/')+'/api/characters')
    response.raise_for_status()
    characters = {x['id']:x for x in response.json()}
    resources = json.loads((folder/'resources.yaml').read_text(encoding='utf-8'))['resources']
    for rid, resource in resources.items():
        if resource['engine'] == 'index_tts':
            engine = 'indextts'
            project = next(x for x in config['indextts_projects'] if x['id'] == rid)
            audio = source_path(project['reference_audio'])
            reference_text = ''
        else:
            engine = 'gpt-sovits'
            if rid not in characters:
                print('Reference mapping required:',rid,flush=True)
                pending.append(rid)
                continue
            params = characters[rid]['profiles'][0]['config']
            audio = Path(params['reference_audio'])
            reference_text = params['prompt_text']
        entry = assets.get(rid)
        stored = comfy_root / 'input/tts-audio-suite' / entry['filename'] if entry else None
        if stored is None or not stored.is_file():
            with audio.open('rb') as handle:
                response = client.post(comfy_url+'/api/tts-audio-suite/v1/assets/audio',files={'audio':(audio.name,handle,'application/octet-stream')})
            response.raise_for_status()
            entry = response.json()
            assets[rid] = {**entry,'reference_audio':str(audio),'reference_text':reference_text}
            index_path.write_text(json.dumps(assets,ensure_ascii=False,indent=2),encoding='utf-8')
        prompt = build_workflow(engine,{'resource_id':rid,'text':'你好，这是本地语音合成测试。','asset_id':entry['asset_id'],'prompt_text':reference_text})
        content = json.dumps(prompt,ensure_ascii=False,indent=2)
        (workflows/(rid+'.api.json')).write_text(content,encoding='utf-8')
        (destination/(rid+'.api.json')).write_text(content,encoding='utf-8')
        exported.append(rid)
        print(rid,flush=True)
(folder/'workflow-export-evidence.json').write_text(json.dumps({'exported':exported,'reference_mapping_required':pending},indent=2),encoding='utf-8')
print('Exported',len(exported),'runnable API workflows; pending reference mapping:',len(pending))
