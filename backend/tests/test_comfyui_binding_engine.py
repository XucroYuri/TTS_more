import pytest

from app.main import _assert_generation_inputs, _enrich_tasks_for_project
from app.models import EngineName, GenerationTask, ScriptLine, ScriptProject, TTSServiceEndpoint
from app.services import ServiceRegistry
from app.storage import ProjectStore


@pytest.mark.parametrize('temporary', [False, True])
def test_registered_endpoint_controls_engine_for_normal_and_temporary_bindings(tmp_path, temporary):
    binding = {'binding_id':'chosen', 'provider_type':'comfyui', 'service_id':'comfy-gpt', 'config':{'engine':'comfyui', 'resource_id':'hero', 'ref_audio_path':'reference.wav', 'prompt_text':'Reference text'}}
    role = {'id':'hero', 'name':'Hero', 'profiles':[{'id':'chosen', 'name':'Chosen', 'engine':'comfyui', 'service_id':'comfy-gpt', 'bindings':[binding]}], 'default_profile':'chosen'}
    line = ScriptLine(id='line',character_id='hero',text='Hello.',temporary_binding=binding if temporary else None)
    store = ProjectStore(tmp_path)
    store.save_project('project', ScriptProject(title='Project', lines=[line], project_characters=[{'project_character_id':'hero','name':'Hero','mode':'snapshot','character_snapshot':role}]))
    registry = ServiceRegistry([TTSServiceEndpoint(service_id='comfy-gpt',provider_type='comfyui',engine='gpt-sovits',base_url='mock://comfy-gpt',api_contract='comfyui-tts-audio-suite-v1',default_params={'resource_id':'hero'})])

    (task,) = _enrich_tasks_for_project(store, 'project', [GenerationTask(line=line,engine=EngineName.COMFYUI,profile='chosen')], registry)

    assert task.engine == EngineName.GPT_SOVITS
    assert task.provider_type.value == 'comfyui'
    assert task.parameters['engine'] == 'gpt-sovits'
    _assert_generation_inputs(task, registry)
    with pytest.raises(ValueError, match='prompt_text'):
        _assert_generation_inputs(task.model_copy(update={'parameters':{**task.parameters,'prompt_text':''}}), registry)
