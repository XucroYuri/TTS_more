import { describe, expect, it } from "vitest";

import type { GenerationVersion } from "../types";
import { generationFailureView, generationVersionTags, groupGenerationVersions, newestPlayableVersion, versionToInspectorDraft } from "./generationHistory";

const versions: GenerationVersion[] = [
  {
    version_id: "v001",
    engine: "gpt-sovits",
    profile: "xiaopin",
    provider_type: "gpt-sovits",
    binding_id: "xiaopin-gpt",
    service_id: "lan-gpt",
    status: "completed",
    audio_path: "data/demo/audio/v001.wav",
    created_at: "2026-07-01T10:00:00Z",
    parameters: { top_p: 0.9, prompt_text: "参考文本" },
    metadata: { job_id: "job-a", cluster_key: "cluster-a" }
  },
  {
    version_id: "v002",
    engine: "gpt-sovits",
    profile: "xiaopin",
    provider_type: "gpt-sovits",
    binding_id: "xiaopin-gpt",
    service_id: "lan-gpt",
    status: "failed",
    error: "remote timeout",
    created_at: "2026-07-01T10:01:00Z",
    metadata: { job_id: "job-a", cluster_key: "cluster-a" }
  },
  {
    version_id: "v003",
    engine: "indextts",
    profile: "temp-index",
    provider_type: "indextts",
    binding_id: "temp-index",
    service_id: "lan-index",
    status: "completed",
    audio_path: "data/demo/audio/v003.wav",
    created_at: "2026-07-01T10:02:00Z",
    parameters: { emotion_mode: "emotion_text", emotion_text: "焦急" },
    metadata: { batch_id: "batch-b", cluster_key: "cluster-b" }
  }
];

describe("generation history helpers", () => {
  it("groups versions by generation batch or job while preserving chronology", () => {
    const groups = groupGenerationVersions(versions);

    expect(groups).toEqual([
      { groupId: "job-a", label: "job-a", versions: [versions[0], versions[1]], latestStatus: "failed" },
      { groupId: "batch-b", label: "batch-b", versions: [versions[2]], latestStatus: "completed" }
    ]);
  });

  it("finds the newest completed version that has audio", () => {
    expect(newestPlayableVersion(versions)).toBe(versions[2]);
  });

  it("turns a selected version into an editable inspector draft", () => {
    expect(versionToInspectorDraft(versions[2])).toEqual({
      provider_type: "indextts",
      service_id: "lan-index",
      profile: "temp-index",
      binding_id: "temp-index",
      parameters: { emotion_mode: "emotion_text", emotion_text: "焦急" }
    });
  });

  it("summarizes failed versions by failure stage for the history panel", () => {
    expect(generationFailureView({
      version_id: "v004",
      engine: "gpt-sovits",
      profile: "xiaopin",
      status: "failed",
      error: "service missing-gpt not found",
      created_at: "2026-07-01T10:03:00Z",
      metadata: { failure_stage: "routing" }
    })).toEqual({
      labelKey: "history.failure.routing",
      detail: "service missing-gpt not found"
    });

    expect(generationFailureView({
      version_id: "v005",
      engine: "gpt-sovits",
      profile: "xiaopin",
      status: "failed",
      created_at: "2026-07-01T10:04:00Z"
    })).toEqual({
      labelKey: "history.failure.generic",
      detail: ""
    });
  });

  it("summarizes service, config and verification tags for history rows", () => {
    expect(generationVersionTags({
      version_id: "v010",
      engine: "gpt-sovits",
      profile: "xiao-pin-gpt",
      provider_type: "gpt-sovits",
      service_id: "lan-gpt",
      binding_id: "xiaopin-gpt",
      status: "completed",
      created_at: "2026-07-01T10:10:00Z",
      requested_load_signature: "requested",
      verified_load_signature: "verified",
      parameters: { logs_name: "小品TTS", gpt_weights_path: "xiao-pin.ckpt", sovits_weights_path: "xiao-pin.pth" }
    }, "GPT-SoVITS WebUI")).toEqual({
      service: "GPT-SoVITS WebUI",
      config: "小品TTS · xiao-pin-gpt · xiaopin-gpt",
      verification: "verified"
    });
  });
});
