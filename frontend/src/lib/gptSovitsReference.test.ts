import { describe, expect, it } from "vitest";

import { applyLogsReferenceSampleToConfig, gptReferenceAudioConfig, selectedLogsReferenceSample } from "./gptSovitsReference";

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

  it("clears previous annotation and sample identity when a new unannotated recording is selected", () => {
    const previous = applyLogsReferenceSampleToConfig({reference_audio: "stale.wav"}, sample, {serviceId: "lan-gpt-a"});
    const next = {...previous, ...gptReferenceAudioConfig({path: "new-upload.wav"})};
    expect(next.ref_audio_path).toBe("new-upload.wav");
    expect(next.prompt_text).toBeNull();
    expect(next.reference_audio).toBeNull();
    expect(selectedLogsReferenceSample([sample], next, {serviceId: "lan-gpt-a"})).toBeUndefined();
    expect({...next, ...gptReferenceAudioConfig(null)}.ref_audio_path).toBeNull();
  });
});
