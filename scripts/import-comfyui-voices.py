"""Add local registered voices to TTSMore while preserving existing characters."""
import json
from pathlib import Path
import httpx
from local_tts_config import ROOT, gpt_reference_sample, integration_folder, load_config, source_path

config, _ = load_config(__doc__)
root = ROOT
folder = integration_folder(config)
inventory = json.loads((folder / 'model-inventory.json').read_text(encoding='utf-8'))
resources = json.loads((folder / 'resources.yaml').read_text(encoding='utf-8'))['resources']
with httpx.Client(base_url=config['backend_url'], timeout=30, trust_env=False) as client:
    response = client.get('/api/characters')
    response.raise_for_status()
    characters = response.json()
    revision = response.headers.get('etag')
    backup = folder / 'characters.before-import.json'
    if not backup.exists():
        backup.write_text(json.dumps(characters, ensure_ascii=False, indent=2), encoding='utf-8')
    imported = []
    for item in inventory:
        if not item.get('paths_exist') or not item['annotation_files']:
            continue
        samples = []
        log = Path(item['logs_path'])
        for row in Path(item['annotation_files'][0]).read_text(encoding='utf-8').splitlines():
            fields = row.split('\t')
            audio = log / '5-wav32k' / fields[0]
            if len(fields) >= 4 and audio.is_file():
                samples.append({'path': str(audio), 'text': fields[-1], 'text_source': 'manual'})
        sample = gpt_reference_sample(samples)
        if sample is None:
            continue
        rid = item['resource_id']
        params = {'resource_id': rid, 'reference_audio': sample['path'], 'prompt_text': sample['text'], 'text_lang': 'zh', 'prompt_lang': 'zh'}
        resource = resources[rid]
        params.update(engine='gpt-sovits', logs_name=log.name, ref_audio_path=sample['path'], gpt_weights_path=resource['gpt_weight'], sovits_weights_path=resource['sovits_weight'])
        params.update(gpt_weights_root=str(Path(resource['gpt_weight']).parent), sovits_weights_root=str(Path(resource['sovits_weight']).parent), logs_root=str(log.parent))
        profile = {'id': rid, 'name': item['name'], 'engine': 'gpt-sovits', 'service_id': 'comfyui-resource-'+rid, 'config': params, 'bindings': [{'binding_id': rid, 'provider_type': 'comfyui', 'service_id': 'comfyui-resource-'+rid, 'capabilities': ['tts','reference_audio_voice','wav_output'], 'config': params}]}
        character = {'id': rid, 'name': item['name']+' '+item['version']+' ('+item['project']+')', 'aliases': [item['name']], 'tags': ['local-model',item['project'],item['version']], 'source_assets': {'logs_path': str(log), 'resource_id': rid}, 'reference_audio_groups': [{'id':rid, 'name':'训练参考音频', 'paths':[x['path'] for x in samples], 'samples':samples}], 'profiles':[profile], 'default_engine':'gpt-sovits', 'default_profile':rid}
        characters = [x for x in characters if x['id'] != rid]
        characters.append(character)
        imported.append({'resource_id':rid, 'references':len(samples)})
    for project in config.get('indextts_projects', []):
        rid = project['id']
        reference = str(source_path(project['reference_audio']))
        if not Path(reference).is_file():
            raise FileNotFoundError(reference)
        profiles = []
        bindings = [(rid, 'comfyui', 'comfyui-resource-'+rid)]
        if project.get('lan_service_id'):
            bindings.append((project.get('lan_profile_id',rid+'-lan'), 'indextts', project['lan_service_id']))
        for profile_id, provider, service in bindings:
            params = {'reference_audio':reference, 'voice':reference, 'engine':'indextts'}
            if provider == 'comfyui':
                params['resource_id'] = rid
            profiles.append({'id':profile_id,'name':profile_id,'engine':'indextts','service_id':service,'config':params,'bindings':[{'binding_id':profile_id,'provider_type':provider,'service_id':service,'capabilities':['tts','reference_audio_voice','wav_output'],'config':params}]})
        characters = [x for x in characters if x['id'] != rid]
        characters.append({'id':rid,'name':project.get('display_name','IndexTTS 示例音色'),'tags':['local-model',rid],'reference_audio_groups':[{'id':rid,'name':'IndexTTS 示例参考音频','paths':[reference],'samples':[{'path':reference}]}],'profiles':profiles,'default_engine':'indextts','default_profile':rid})
    response = client.put('/api/characters', json=characters, headers={'If-Match':revision} if revision else {})
    response.raise_for_status()
    (folder/'voice-import-evidence.json').write_text(json.dumps(imported,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Imported voices:',len(imported),'reference samples:',sum(x['references'] for x in imported))
