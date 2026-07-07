import { describe, expect, it } from "vitest";

import type { LogsReferenceAudioSample, RoleLibraryCandidate } from "../types";
import { gptSovitsProjectBindingFromModel } from "./modelCatalog";

describe("model catalog helpers", () => {
  it("builds a project-level GPT-SoVITS binding from a catalog model and sample", () => {
    const model: RoleLibraryCandidate = {
      id: "demo-hero-logs",
      name: "主角",
      logs_name: "demo-hero-logs",
      service_id: "local-gpt",
      recommended_gpt_weights_path: "GPT_weights/demo-hero-logs-e40.ckpt",
      recommended_sovits_weights_path: "SoVITS_weights/demo-hero-logs_e24_s264.pth",
      gpt_weights: [],
      sovits_weights: [],
      reference_audio_groups: []
    };
    const sample: LogsReferenceAudioSample = {
      sample_id: "demo-hero-logs:hero_001.wav",
      display_label: "hero_001.wav · 不好！",
      path: "logs/demo-hero-logs/5-wav32k/hero_001.wav",
      text: "不好！",
      text_source: "name2text",
      prompt_lang: "zh",
      source: "logs",
      logs_name: "demo-hero-logs"
    };

    const binding = gptSovitsProjectBindingFromModel("role-1", model, sample);

    expect(binding).toMatchObject({
      binding_id: "role-1-project-gpt",
      provider_type: "gpt-sovits",
      service_id: "local-gpt",
      capabilities: ["trained_weights_voice", "reference_audio_voice"],
      config: {
        logs_name: "demo-hero-logs",
        gpt_weights_path: "GPT_weights/demo-hero-logs-e40.ckpt",
        sovits_weights_path: "SoVITS_weights/demo-hero-logs_e24_s264.pth",
        ref_audio_path: "logs/demo-hero-logs/5-wav32k/hero_001.wav",
        prompt_text: "不好！",
        prompt_lang: "zh",
        logs_reference_sample_id: "demo-hero-logs:hero_001.wav"
      }
    });
  });
});
