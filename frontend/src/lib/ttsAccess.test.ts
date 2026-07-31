import { describe, expect, it } from "vitest";

import {
  buildComfyUIEndpointRequest,
  buildGradioEndpointRequest,
  gradioContractForProvider,
  sourceProfileForEndpointUrl,
  ttsAudioSuiteContractForProvider,
} from "./ttsAccess";

describe("TTS Gradio endpoint access helpers", () => {
  it("maps every open-source provider to its Gradio WebUI contract", () => {
    expect(gradioContractForProvider("gpt-sovits")).toBe("gradio-gpt-sovits-webui");
    expect(gradioContractForProvider("indextts")).toBe("gradio-indextts2-webui");
    expect(gradioContractForProvider("cosyvoice")).toBe("gradio-cosyvoice-webui");
  });

  it("maps every open-source provider to the ComfyUI TTS-Audio-Suite contract", () => {
    expect(ttsAudioSuiteContractForProvider("gpt-sovits")).toBe("comfyui-tts-audio-suite-v1");
    expect(ttsAudioSuiteContractForProvider("indextts")).toBe("comfyui-tts-audio-suite-v1");
    expect(ttsAudioSuiteContractForProvider("cosyvoice")).toBe("comfyui-tts-audio-suite-v1");
  });

  it("classifies pasted localhost and LAN Gradio URLs without requiring repo mode", () => {
    expect(sourceProfileForEndpointUrl("http://127.0.0.1:9872")).toBe("local_endpoint");
    expect(sourceProfileForEndpointUrl("http://localhost:9872")).toBe("local_endpoint");
    expect(sourceProfileForEndpointUrl("http://192.168.2.50:9872")).toBe("lan_endpoint");
    expect(sourceProfileForEndpointUrl("https://tts.example.com/gradio")).toBe("cloud_endpoint");
  });

  it("builds a minimal endpoint-only configure payload", () => {
    expect(buildGradioEndpointRequest({
      provider_type: "gpt-sovits",
      display_name: "GPT-SoVITS Studio",
      base_url: "http://192.168.2.50:9872",
      resource_group: "tts-gradio",
      capacity: 1,
      enabled: true,
    })).toEqual({
      provider_type: "gpt-sovits",
      service_id: null,
      display_name: "GPT-SoVITS Studio",
      source_profile: "lan_endpoint",
      repo_path: null,
      base_url: "http://192.168.2.50:9872",
      api_contract: "gradio-gpt-sovits-webui",
      network_scope: "lan",
      managed: false,
      enabled: true,
      resource_group: "tts-gradio",
      capacity: 1,
      start_command: [],
      start_cwd: null,
    });
  });

  it("builds a ComfyUI endpoint configure payload with resource id", () => {
    expect(buildComfyUIEndpointRequest({
      provider_type: "cosyvoice",
      display_name: "ComfyUI CosyVoice",
      base_url: "http://127.0.0.1:8188",
      resource_group: "comfyui-local-0",
      capacity: 3,
      enabled: true,
      resource_id: "cosyvoice-local",
    })).toEqual({
      provider_type: "cosyvoice",
      service_id: null,
      display_name: "ComfyUI CosyVoice",
      source_profile: "local_endpoint",
      repo_path: null,
      base_url: "http://127.0.0.1:8188",
      api_contract: "comfyui-tts-audio-suite-v1",
      network_scope: "localhost",
      managed: false,
      enabled: true,
      resource_group: "comfyui-local-0",
      capacity: 3,
      resource_id: "cosyvoice-local",
      start_command: [],
      start_cwd: null,
    });
  });
});
