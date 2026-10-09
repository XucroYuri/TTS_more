import { describe, expect, it } from "vitest";
import { configForService, engineProvider, serviceEngineProvider } from "./ttsProvider";
import { buildGenerationTask } from "./routing";
import { catalogServiceOptions } from "./roleLibraryView";
import { resolveProjectCharacters } from "./projectCharacters";
import type { WorkerHealth } from "../types";

describe("ComfyUI engine controls and routing", () => {
  const service: WorkerHealth = {service_id:"comfy-gpt", provider_type:"comfyui", engine:"gpt-sovits", ready:true, api_contract:"comfyui-tts-audio-suite-v1"};
  it("shows engine controls and the model catalog for a ComfyUI endpoint", () => {
    expect(engineProvider("comfyui", "indextts")).toBe("indextts");
    expect(serviceEngineProvider(service)).toBe("gpt-sovits");
    expect(catalogServiceOptions([service]).map(item => item.serviceId)).toEqual(["comfy-gpt"]);
  });
  it("preserves the transport and selected engine when editing a temporary binding", () => {
    const task = buildGenerationTask({id:"l1", character_id:"hero", text:"继续走。", note:"", temporary_binding:{binding_id:"tmp", provider_type:"comfyui", service_id:"comfy-gpt", capabilities:[], fallback_services:[], config:{engine:"gpt-sovits", resource_id:"hero", ref_audio_path:"chosen.wav"}}}, []);
    expect(task).toMatchObject({engine:"gpt-sovits", provider_type:"comfyui", service_id:"comfy-gpt", parameters:{ref_audio_path:"chosen.wav"}});
  });
  it("switches resource identity and clears the previous voice's checkpoint choices", () => {
    const next = configForService({resource_id:"old", gpt_weights_path:"old.ckpt", prompt_text:"old prompt", ref_audio_path:"old.wav", temperature:0.7}, {...service,default_params:{resource_id:"new",gpt_weights_root:"/models/GPT_weights"}});
    expect(next).toEqual({resource_id:"new",engine:"gpt-sovits",gpt_weights_root:"/models/GPT_weights",temperature:0.7});
  });
  it("retains the synthesis engine for a project role's ComfyUI binding", () => {
    const roles = resolveProjectCharacters({title:"demo",default_language:"zh",lines:[],project_characters:[{project_character_id:"hero",name:"Hero",mode:"snapshot",library_character_id:null,project_binding:{binding_id:"project",provider_type:"comfyui",service_id:"comfy-gpt",capabilities:[],fallback_services:[],config:{engine:"gpt-sovits",resource_id:"hero"}}}]}, []);
    expect(roles[0].default_engine).toBe("gpt-sovits");
    expect(roles[0].profiles?.[0].engine).toBe("gpt-sovits");
    expect(roles[0].profiles?.[0].bindings?.[0].provider_type).toBe("comfyui");
  });
});
