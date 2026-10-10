import { describe, expect, it } from "vitest";

import type { LogsReferenceAudioSample, RoleLibraryCandidate } from "../types";
import { firstReferenceSampleFromModel, gptSovitsExperimentConfig, gptSovitsExperimentInputValue, gptSovitsExperimentOptions, gptSovitsProjectBindingFromModel, resolveGptSovitsExperiment } from "./modelCatalog";

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

  it("automatically selects a verified annotated reference after unsuitable samples", () => {
    const model: RoleLibraryCandidate = {
      id: "hero", name: "Hero", reference_audio_groups: [{id: "refs", name: "References", paths: [], samples: [
        {path: "short.wav", text: "Short", duration_seconds: 2},
        {path: "unannotated.wav", duration_seconds: 4},
        {path: "legacy.wav", text: "Unverified remote"},
        {path: "valid.wav", text: "Valid annotation", duration_seconds: 3},
      ]}],
    };
    expect(firstReferenceSampleFromModel(model)?.path).toBe("valid.wav");
    expect(gptSovitsProjectBindingFromModel("role", model).config.ref_audio_path).toBe("valid.wav");
  });

  it("clears old references and checkpoint aliases when switching to an incomplete experiment", () => {
    const old = { reference_audio: "old.wav", ref_audio_path: "old.wav", prompt_text: "old annotation", gpt_weights_path: "old.ckpt", gpt_checkpoint: "old.ckpt" };
    const model: RoleLibraryCandidate = {id: "empty", name: "Empty", reference_audio_groups: [{id: "refs", name: "References", paths: [], samples: [{path: "short.wav", text: "Short", duration_seconds: 2}]}]};
    const next = {...old, ...gptSovitsExperimentConfig("Empty", model)};
    expect(next).toMatchObject({reference_audio: null, ref_audio_path: null, prompt_text: null, gpt_weights_path: null, gpt_checkpoint: null});
    expect({...old, ...gptSovitsExperimentConfig("New")}).toMatchObject({logs_name: "New", reference_audio: null, ref_audio_path: null, prompt_text: null});
    expect(gptSovitsExperimentConfig("Reference-only", {id: "logs-only", name: "Reference-only", reference_audio_groups: [{id:"refs", name:"References", paths:[], samples:[{path:"valid.wav",text:"Valid annotation",duration_seconds:4}]}]}).ref_audio_path).toBeNull();
  });

  it("requires explicit selection for legacy remote samples with unknown duration", () => {
    const sample = {path: "remote.wav", text: "Remote annotation"};
    const model: RoleLibraryCandidate = {id: "remote", name: "Remote", reference_audio_groups: [{id: "refs", name: "References", paths: [], samples: [sample]}]};
    expect(gptSovitsProjectBindingFromModel("role", model).config.ref_audio_path).toBeNull();
    expect(gptSovitsProjectBindingFromModel("role", model, sample).config.ref_audio_path).toBe("remote.wav");
    expect(gptSovitsProjectBindingFromModel("role", model, {...sample, duration_seconds: 2}).config.ref_audio_path).toBeNull();
  });

  it("selects the second version of the same full experiment name without changing the logs identity", () => {
    const models: RoleLibraryCandidate[] = ["v2", "v2ProPlus"].map((version) => ({
      id: `hero-${version}`, name: "Hero", logs_name: "2Hero-session", service_id: "native-gpt", model_version: version,
      recommended_gpt_weights_path: `${version}/hero-e50.ckpt`, recommended_sovits_weights_path: `${version}/hero_e24_s264.pth`,
      reference_audio_groups: [{id:`refs-${version}`, name:"References", paths:[], samples:[{path:`${version}/reference.wav`,text:`${version} annotation`,duration_seconds:4}]}],
    }));
    const options = gptSovitsExperimentOptions(models);
    expect(options.map((option) => option.value)).toEqual(["2Hero-session [v2]", "2Hero-session [v2ProPlus]"]);
    const selected = resolveGptSovitsExperiment(options[1].value, models);
    expect(selected?.id).toBe("hero-v2ProPlus");
    const config = gptSovitsExperimentConfig(selected!.logs_name!, selected);
    expect(config).toMatchObject({
      logs_name: "2Hero-session", gpt_model_catalog_id: "hero-v2ProPlus", gpt_model_version: "v2ProPlus",
      gpt_weights_path: "v2ProPlus/hero-e50.ckpt", sovits_weights_path: "v2ProPlus/hero_e24_s264.pth", ref_audio_path: "v2ProPlus/reference.wav",
    });
    expect(gptSovitsExperimentInputValue(config, models)).toBe(options[1].value);
    expect(gptSovitsExperimentInputValue({logs_name: config.logs_name, gpt_weights_path: config.gpt_weights_path, sovits_weights_path: config.sovits_weights_path}, models)).toBe(options[1].value);
    expect(resolveGptSovitsExperiment("2Hero-session", models)).toBeUndefined();
  });

  it("gives identical or legacy version labels unique choices instead of resolving an ambiguous name", () => {
    const models: RoleLibraryCandidate[] = [{id:"checkout-a",name:"Hero",logs_name:"1Hero"},{id:"checkout-b",name:"Hero",logs_name:"1Hero",model_version:"v2"},{id:"checkout-c",name:"Hero",logs_name:"1Hero",model_version:"v2"}];
    const options = gptSovitsExperimentOptions(models);
    expect(new Set(options.map((option) => option.value)).size).toBe(3);
    for (const option of options) expect(resolveGptSovitsExperiment(option.value, models)?.id).toBe(option.model.id);
    expect(resolveGptSovitsExperiment("1Hero", models)).toBeUndefined();
  });
});
