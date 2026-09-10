import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { initI18n } from "../../i18n";
import type { VoiceRecommendation, VoiceSelectionSnapshot } from "../../types";
import { VoiceCandidatePanel } from "./VoiceCandidatePanel";

initI18n();

const recommendation: VoiceRecommendation = {
  line_id: "line-1",
  catalog_version: "catalog-v1",
  blockers: [],
  candidates: [1, 2, 3, 4].map((number) => ({
    candidate_id: `candidate-${number}`,
    resource_id: `resource-${number}`,
    reference_asset_id: `reference-${number}`,
    score: 92 - number,
    score_breakdown: { character: 35, emotion: 28, duration: 18, language: 10, metadata: 0 },
    auto_fill_eligible: number === 1,
    speed_factor: 1.05,
    engine_type: "gpt-sovits",
    target_duration_seconds: 2.8,
    reasons: ["character_exact", "emotion_exact"],
    blockers: [],
    catalog_version: "catalog-v1",
    training_task: number === 1 ? "1九九-许珺雯-25111925情绪补充-2r" : null,
    gpt_weight_artifact_id: number === 1 ? "gpt-weight-1" : null,
    sovits_weight_artifact_id: number === 1 ? "sovits-weight-1" : null
  }))
};

const selection: VoiceSelectionSnapshot = {
  catalog_version: "catalog-v1",
  candidate_id: "candidate-1",
  resource_id: "resource-1",
  reference_asset_id: "reference-1",
  score: 91,
  speed_factor: 1.05,
  line_id: "line-1",
  source: "automatic",
  resource_fingerprint: "r",
  reference_fingerprint: "a",
  emotion: "happy",
  target_duration_seconds: 2.8,
  prompt_text: "",
  reference_language: "zh",
  text_language: "zh",
  inference_parameters: { training_task: "1九九-许珺雯-25111925情绪补充-2r" },
  selected_at: "2026-09-02T00:00:00Z"
};

describe("VoiceCandidatePanel", () => {
  const views: Array<{ root: ReturnType<typeof createRoot>; dom: JSDOM }> = [];
  afterEach(async () => {
    for (const view of views.splice(0)) await act(async () => view.root.unmount());
  });

  it("shows at most three ranked candidates, score detail, preview, and selection state", async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      Node: dom.window.Node,
      Event: dom.window.Event,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const root = createRoot(dom.window.document.getElementById("root")!);
    views.push({ root, dom });
    const onSelect = vi.fn();
    const onClear = vi.fn();

    await act(async () => root.render(createElement(VoiceCandidatePanel, {
      recommendation,
      selection,
      loading: false,
      selectingCandidateId: null,
      referenceAudioUrl: (assetId: string) => `/preview/${assetId}`,
      onSelect,
      onConfirmIdentity: vi.fn(),
      onClear
    })));

    const text = dom.window.document.body.textContent ?? "";
    expect(text).not.toContain("自动选择");
    expect(text).not.toContain("已选择");
    expect(text).not.toContain("角色完全匹配");
    expect(dom.window.document.querySelector(".voice-candidate-card.selected")).not.toBeNull();
    expect(text).toContain("角色 35");
    expect(text).toContain("情绪 28");
    expect(text).toContain("1.05×");
    expect(text).toContain("2.8 秒");
    expect(text).toContain("训练任务：1九九-许珺雯-25111925情绪补充-2r");
    expect(text).toContain("GPT/SoVITS 权重已配对");
    expect(text).not.toContain("candidate-4");
    expect(dom.window.document.querySelector('audio[src="/preview/reference-1"]')).not.toBeNull();

    const selectButton = [...dom.window.document.querySelectorAll("button")]
      .find((button) => button.textContent?.includes("选择此方案"));
    await act(async () => selectButton?.click());
    expect(onSelect).toHaveBeenCalledWith("candidate-2");
    const clearButton = [...dom.window.document.querySelectorAll("button")]
      .find((button) => button.textContent?.includes("取消选择"));
    await act(async () => clearButton?.click());
    expect(onClear).toHaveBeenCalledOnce();
  });

  it("shows a neutral no-match message without a role-mapping action", async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      Node: dom.window.Node,
      Event: dom.window.Event,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const root = createRoot(dom.window.document.getElementById("root")!);
    views.push({ root, dom });
    await act(async () => root.render(createElement(VoiceCandidatePanel, {
      recommendation: {
        line_id: "line-1",
        catalog_version: "catalog-v1",
        candidates: [],
        blockers: ["no_role_mapping", "voice_assets_unavailable"]
      },
      loading: false,
      selectingCandidateId: null,
      referenceAudioUrl: (assetId: string) => `/preview/${assetId}`,
      onSelect: vi.fn(),
      onConfirmIdentity: vi.fn(),
      onClear: vi.fn()
    })));

    const text = dom.window.document.body.textContent ?? "";
    expect(text).toContain("暂未自动识别到可用音色");
    expect(text).not.toContain("声音资产目录尚未准备好");
    expect(text).not.toContain("关联角色");
  });

  it("asks for confirmation instead of allowing direct selection for a fuzzy folder candidate", async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      Node: dom.window.Node,
      Event: dom.window.Event,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const root = createRoot(dom.window.document.getElementById("root")!);
    views.push({ root, dom });
    const onConfirmIdentity = vi.fn();
    const fuzzyRecommendation: VoiceRecommendation = {
      ...recommendation,
      candidates: [{
        ...recommendation.candidates[0],
        score_breakdown: { ...recommendation.candidates[0].score_breakdown, character: 35 },
        auto_fill_eligible: false,
        identity_match: "folder_fuzzy",
        requires_identity_confirmation: true,
        reasons: ["folder_name_fuzzy_match"],
        training_task: "xxx-胶布tts"
      }]
    };

    await act(async () => root.render(createElement(VoiceCandidatePanel, {
      recommendation: fuzzyRecommendation,
      loading: false,
      selectingCandidateId: null,
      referenceAudioUrl: (assetId: string) => `/preview/${assetId}`,
      onSelect: vi.fn(),
      onConfirmIdentity,
      onClear: vi.fn()
    })));

    const text = dom.window.document.body.textContent ?? "";
    expect(text).toContain("角色 35");
    expect(text).toContain("暂未搜索合适音色，推测音色是否准确？");
    expect(text).not.toContain("选择此方案");
    const confirmButton = [...dom.window.document.querySelectorAll("button")]
      .find((button) => button.textContent?.includes("准确，写入角色映射"));
    await act(async () => confirmButton?.click());
    expect(onConfirmIdentity).toHaveBeenCalledWith("candidate-1");
  });
});
