"""Exercise stored roles, preflight, async jobs, manifests and audio delivery."""
import argparse
import json
import time
from pathlib import Path
import httpx
import soundfile
from local_tts_config import ROOT, integration_folder, load_config

def options(parser):
    parser.add_argument('--target', required=True)
    parser.add_argument('--existing-job', help='Resume polling a previously submitted job without another synthesis')
    parser.add_argument('--audio-only', action='store_true', help='Verify the saved completed version without submitting a job')

config, args = load_config(__doc__, options)
root = ROOT
evidence = integration_folder(config)/'validation'
evidence.mkdir(parents=True,exist_ok=True)
validation = config['validation']
project_id=validation['project_id']
targets=validation['targets']
target=targets[args.target]
voices={key:value['character_id'] for key,value in targets.items()}
profiles={key:value['profile'] for key,value in targets.items()}
def save(name,payload):
    (evidence/name).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
with httpx.Client(base_url=config['backend_url'],timeout=30,trust_env=False) as client:
    response=client.get('/api/characters')
    response.raise_for_status()
    characters={c['id']:c for c in response.json()}
    response=client.get('/api/projects/'+project_id)
    if response.status_code == 404:
        project={'title':validation['title'],'project_characters':[{'project_character_id':rid,'name':characters[rid]['name'],'library_character_id':rid,'match_status':'manual'} for rid in dict.fromkeys(voices.values())], 'lines':[{'id':key,'character_id':rid,'profile_override':profiles[key],'text':targets[key]['text'],'language':'zh'} for key,rid in voices.items()]}
        response=client.put('/api/projects/'+project_id,json=project)
        response.raise_for_status()
        response=client.get('/api/projects/'+project_id)
    response.raise_for_status()
    project=response.json()
    save('integration-project.json',project)
    line=next(x for x in project['lines'] if x['id']==args.target)
    task={'line':line,'engine':target['engine'],'profile':profiles[args.target]}
    payload={'project_id':project_id,'tasks':[task]}
    response=client.post('/api/generation/preflight',json=payload)
    response.raise_for_status()
    preflight=response.json()
    save(args.target+'-preflight.json',preflight)
    if preflight['status']!='ready':
        raise RuntimeError('Preflight blocked: '+str(preflight))
    if not args.audio_only:
        if args.existing_job:
            job={'job_id':args.existing_job,'status':'unknown'}
        else:
            response=client.post('/api/jobs/generation',json=payload)
            response.raise_for_status()
            job=response.json()
            save(args.target+'-job.json',job)
            print('Submitted job',job['job_id'],flush=True)
        deadline=time.monotonic()+1000
        while time.monotonic()<deadline:
            try:
                response=client.get('/api/jobs/'+job['job_id'])
            except (httpx.TimeoutException,httpx.ConnectError):
                time.sleep(2)
                continue
            response.raise_for_status()
            job=response.json()
            save(args.target+'-job.json',job)
            if job['status'] in ('completed','failed','cancelled'):
                break
            time.sleep(2)
        if job['status']!='completed':
            raise RuntimeError('Job did not complete: '+str(job))
    response=client.get('/api/projects/'+project_id+'/manifest')
    response.raise_for_status()
    manifest=response.json()
    save(args.target+'-manifest.json',manifest)
    versions=manifest['lines'][line['line_uid']]['versions']
    version=next(x for x in reversed(versions) if x['status']=='completed')
    response=client.get('/api/audio',params={'path':version['audio_path']})
    response.raise_for_status()
    audio_path=evidence/(args.target+'-project.wav')
    audio_path.write_bytes(response.content)
    samples,sample_rate=soundfile.read(audio_path)
    if not samples.size or abs(samples).max()<=1e-5:
        raise RuntimeError('Downloaded audio is empty or silent')
    save(args.target+'-audio.json',{'audio_path':str(audio_path),'sample_rate':sample_rate,'seconds':len(samples)/sample_rate,'peak':float(abs(samples).max())})
    print('Completed',args.target,flush=True)
