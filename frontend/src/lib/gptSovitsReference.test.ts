import { describe, expect, it } from "vitest";

import {
  CATALOG_STAGED_REFERENCE_OPTION,
  applyLogsReferenceSampleToConfig,
  selectedDynamicWeightOption,
  selectedLogsReferenceOptionValue,
  selectedLogsReferenceSample,
} from "./gptSovitsReference";

describe("GPT-SoVITS logs reference helpers", () => {
  const sample = {
    sample_id: "demo-mentor-logs:mentor_001.wav",
    display_label: "导师：注意右侧通道！",
    path: "/fixtures/logs/demo-mentor-logs/5-wav32k/mentor_001.wav",
    text: "注意右侧通道！",
    text_source: "name2text",
    character: "导师",
    emotion: "紧张",
    remark: "",
    prompt_lang: "zh",
    source: "logs",
    logs_name: "demo-mentor-logs"
  } as const;

  it("applies a selected logs reference audio sample to the current binding config", () => {
    const next = applyLogsReferenceSampleToConfig(
      { top_p: 0.8, prompt_text: "old text" },
      sample,
      { serviceId: "lan-gpt-a" }
    );

    expect(next).toMatchObject({
      top_p: 0.8,
      ref_audio_path: "/fixtures/logs/demo-mentor-logs/5-wav32k/mentor_001.wav",
      prompt_text: "注意右侧通道！",
      prompt_lang: "zh",
      logs_reference_sample_id: "demo-mentor-logs:mentor_001.wav",
      logs_reference_label: "导师：注意右侧通道！",
      logs_reference_service_id: "lan-gpt-a",
      logs_reference_logs_name: "demo-mentor-logs"
    });
  });

  it("does not reuse a selected logs sample across different services", () => {
    const config = applyLogsReferenceSampleToConfig({}, sample, { serviceId: "lan-gpt-a" });

    expect(selectedLogsReferenceSample([sample], config, { serviceId: "lan-gpt-a" })).toEqual(sample);
    expect(selectedLogsReferenceSample([sample], config, { serviceId: "lan-gpt-b" })).toBeUndefined();
  });

  it("keeps catalog-relative weights visible without replacing the secure binding", () => {
    expect(selectedDynamicWeightOption({
      gpt_weights_relative_path: "GPT_weights_v2ProPlus/task/model.ckpt"
    }, "gpt")).toEqual({
      value: "__catalog_weight__:gpt",
      relativePath: "GPT_weights_v2ProPlus/task/model.ckpt"
    });

    expect(selectedDynamicWeightOption({
      gpt_weights_path: "D:/legacy/model.ckpt",
      gpt_weights_relative_path: "GPT_weights_v2ProPlus/task/model.ckpt"
    }, "gpt")).toEqual({
      value: "D:/legacy/model.ckpt",
      relativePath: ""
    });
  });

  it("keeps a staged catalog reference visible until a logs sample is chosen", () => {
    expect(selectedLogsReferenceOptionValue(undefined, {
      ref_audio_path: "D:/staged/reference.wav"
    })).toBe(CATALOG_STAGED_REFERENCE_OPTION);
    expect(selectedLogsReferenceOptionValue(sample, {
      ref_audio_path: "D:/staged/reference.wav"
    })).toBe(sample.sample_id);
  });
});
