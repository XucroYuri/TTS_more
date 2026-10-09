"""Publish endpoints for the machine-local registered models."""
import json
from pathlib import Path
import httpx
from local_tts_config import ROOT, integration_folder, load_config

config, _ = load_config(__doc__)
root = ROOT
folder = integration_folder(config)
resources = json.loads((folder/'resources.yaml').read_text(encoding='utf-8'))['resources']
inventory = {x['resource_id']: x for x in json.loads((folder/'model-inventory.json').read_text(encoding='utf-8')) if x.get('paths_exist')}
path = root/'data/local/services.json'
services = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else []
backup = path.with_name('services.before-comfyui.json')
if not backup.exists():
    backup.write_text(json.dumps(services,ensure_ascii=False,indent=2),encoding='utf-8')
services = [x for x in services if not x['service_id'].startswith('comfyui-resource-')]
for endpoint in config.get('lan_indextts', []):
    service_id = endpoint['service_id']
    services = [x for x in services if x['service_id'] != service_id]
    services.append(dict(service_id=service_id,display_name=endpoint.get('display_name',service_id),provider_type='indextts',engine='indextts',api_contract='gradio-indextts2-webui',base_url=endpoint['base_url'],mode='external',network_scope='lan',enabled=True,resource_group=service_id,capacity=1,priority=20,capabilities=['tts','reference_audio_voice','emotion_text','emotion_audio','wav_output','gradio_webui','artifact-transfer'],default_params={'timeout_seconds':900}))
for rid, resource in resources.items():
    engine = 'indextts' if resource['engine'] == 'index_tts' else 'gpt-sovits'
    item = inventory.get(rid,{})
    label = item.get('name',rid)+' '+item.get('version','')+' ('+item.get('project',rid)+')'
    params = {'resource_id':rid,'engine':engine,'timeout_seconds':900}
    capabilities = ['tts','reference_audio_voice','wav_output','artifact-transfer','comfyui','tts-audio-suite']
    if engine == 'gpt-sovits':
        params.update(gpt_weights_root=str(Path(resource['gpt_weight']).parent), sovits_weights_root=str(Path(resource['sovits_weight']).parent), logs_root=str(Path(resource['source_root'])/'logs'))
        capabilities.extend(['trained_weights_voice','model_catalog'])
    services.append(dict(service_id='comfyui-resource-'+rid,display_name='ComfyUI '+label,provider_type='comfyui',engine=engine,api_contract='comfyui-tts-audio-suite-v1',base_url=config['comfyui']['base_url'],mode='external',network_scope='localhost',enabled=True,resource_group=config['comfyui'].get('resource_group','comfyui-local-0'),capacity=1,priority=10,capabilities=capabilities,default_params=params))
path.write_text(json.dumps(services,ensure_ascii=False,indent=2),encoding='utf-8')
with httpx.Client(timeout=30,trust_env=False) as client:
    response=client.post(config['backend_url'].rstrip('/')+'/api/settings/services/reload')
    response.raise_for_status()
print('Published',len(resources),'local ComfyUI services')
