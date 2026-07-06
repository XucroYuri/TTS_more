import { describe, expect, it } from "vitest";

import type { Character, GenerationVersion, ScriptLine, WorkerHealth } from "../types";
import { generationMethodForProvider, generationMethodOptions, generationMethodRouteLabels, historyPlayerSummary, inspectorBackupReferenceVisible, inspectorConfigPanelLayout, inspectorDiagnosticsState, inspectorPanelMode, inspectorSections, inspectorSpeechWorkbenchLayout, inspectorVersionContextVisible, lineCardSecondaryBadges, lineFocusTransition, paginateItems, preflightFallbackAction, preflightLineLabelKey, preflightLineTone, preflightLoadLabelKey, preflightLoadTone, referenceResourcePanelLayout, roleAccentClass, roleChipInteractionState, roleFilterCardView, scriptConsoleActionPlacement, scriptConsoleBodyMode, scriptDrawerTabs, scriptExcerptLines, shouldRequestRevisionConfirmation, trustedBackupReferenceGroups } from "./workbenchView";

describe("workbench view helpers", () => {
  it("keeps role filter names and count badges as separate visual parts", () => {
    expect(roleFilterCardView("小品", 9, "小")).toEqual({
      name: "小品",
      countLabel: "9",
      avatarLabel: "小",
      ariaLabel: "小品 · 9 行"
    });
    expect(roleFilterCardView("全部", 30, "全").ariaLabel).toBe("全部 · 30 行");
    expect(roleAccentClass(0)).toBe("role-accent-0");
    expect(roleAccentClass(9)).toBe("role-accent-1");
  });

  it("expands the clicked line while leaving checkbox selection independent", () => {
    expect(lineFocusTransition({ activeLineId: "l001", expandedLineId: "l001" }, "l002", "card")).toEqual({
      activeLineId: "l002",
      expandedLineId: "l002"
    });
    expect(lineFocusTransition({ activeLineId: "l002", expandedLineId: "l002" }, "l002", "card")).toEqual({
      activeLineId: "l002",
      expandedLineId: "l002"
    });
    expect(lineFocusTransition({ activeLineId: "l001", expandedLineId: "l001" }, "l002", "checkbox")).toEqual({
      activeLineId: "l001",
      expandedLineId: "l001"
    });
  });

  it("groups generation providers into user-facing method tabs", () => {
    expect(generationMethodForProvider("gpt-sovits")).toBe("gpt-sovits");
    expect(generationMethodForProvider("indextts")).toBe("indextts");
    expect(generationMethodForProvider("cosyvoice")).toBe("cosyvoice");
    expect(generationMethodForProvider("openai")).toBe("commercial");
    expect(generationMethodOptions().map((item) => item.id)).toEqual(["gpt-sovits", "indextts", "cosyvoice", "commercial"]);
  });

  it("uses method-specific route labels instead of provider internals", () => {
    expect(generationMethodRouteLabels("gpt-sovits")).toEqual({
      profileLabelKey: "inspector.gptRolePreset",
      bindingLabelKey: "inspector.gptVoiceBinding",
      serviceLabelKey: "inspector.gptService"
    });
    expect(generationMethodRouteLabels("indextts").serviceLabelKey).toBe("inspector.indexService");
    expect(generationMethodRouteLabels("cosyvoice")).toEqual({
      profileLabelKey: "inspector.cosyVoicePreset",
      bindingLabelKey: "inspector.cosyVoiceBinding",
      serviceLabelKey: "inspector.cosyVoiceService"
    });
    expect(generationMethodRouteLabels("commercial").bindingLabelKey).toBe("inspector.commercialVoiceBinding");
  });

  it("keeps the current line summary inside the generation workbench", () => {
    expect(inspectorSpeechWorkbenchLayout()).toEqual({
      currentLineSummary: "dock",
      separateHero: false,
      primaryAction: "dock",
      serviceStatus: "config",
      diagnosticsAction: "config"
    });
  });

  it("keeps voice resources as compact mixed-axis controls instead of explanatory text", () => {
    expect(referenceResourcePanelLayout()).toEqual({
      summary: "compact_grid",
      controls: "model_reference_columns",
      manualFallback: "inline",
      standaloneHelpText: false
    });
  });

  it("keeps performance prompts out of the primary generation route controls", () => {
    expect(inspectorConfigPanelLayout()).toEqual({
      generationMethodTabs: true,
      routeControls: "method_scoped",
      standalonePerformancePrompt: false
    });
  });

  it("paginates line lists with bounded page metadata", () => {
    const items = Array.from({ length: 30 }, (_, index) => `l${index + 1}`);

    expect(paginateItems(items, 2, 10)).toMatchObject({
      items: ["l11", "l12", "l13", "l14", "l15", "l16", "l17", "l18", "l19", "l20"],
      page: 2,
      pageSize: 10,
      totalItems: 30,
      totalPages: 3,
      startItem: 11,
      endItem: 20,
      hasPrevious: true,
      hasNext: true
    });

    expect(paginateItems(items, 99, 10)).toMatchObject({
      page: 3,
      startItem: 21,
      endItem: 30,
      hasNext: false
    });

    expect(paginateItems([], 4, 10)).toMatchObject({
      items: [],
      page: 1,
      totalItems: 0,
      totalPages: 1,
      startItem: 0,
      endItem: 0
    });
  });

  it("keeps script management as a header-level action", () => {
    expect(scriptConsoleActionPlacement()).toEqual({
      management: "header",
      parseRevision: "footer"
    });
  });

  it("keeps the sidebar script body in markdown preview until editing is requested", () => {
    expect(scriptConsoleBodyMode(false)).toBe("preview");
    expect(scriptConsoleBodyMode(true)).toBe("edit");
  });

  it("distinguishes focused and filtered role chip state", () => {
    expect(roleChipInteractionState("xiao-pin", "xiao-pin", "all")).toEqual({
      isFocused: true,
      isFiltered: false,
      ariaPressed: false
    });
    expect(roleChipInteractionState("xiao-pin", "dao-shi", "xiao-pin")).toEqual({
      isFocused: false,
      isFiltered: true,
      ariaPressed: true
    });
  });

  it("keeps technical service details out of the collapsed line card", () => {
    const latest: GenerationVersion = {
      version_id: "v004",
      engine: "gpt-sovits",
      profile: "xiao-pin-gpt",
      provider_type: "gpt-sovits",
      service_id: "example-gpt-sovits-gradio",
      binding_id: "xiaopin-gpt-logs-binding",
      status: "completed",
      audio_path: "data/demo/audio/l001.wav",
      created_at: "now",
    };

    const labels = lineCardSecondaryBadges(latest, 4);

    expect(labels).toEqual([
      { kind: "latest_playable" },
      { kind: "version_count", count: 4 }
    ]);
    expect(JSON.stringify(labels)).not.toContain("v004");
    expect(JSON.stringify(labels)).not.toContain("gpt-sovits");
    expect(JSON.stringify(labels)).not.toContain("lan-gpt");
    expect(JSON.stringify(labels)).not.toContain("binding");
  });

  it("summarizes empty and failed histories without raw ids", () => {
    expect(lineCardSecondaryBadges(undefined, 0)).toEqual([{ kind: "no_versions" }]);
    expect(lineCardSecondaryBadges({ version_id: "v002", engine: "gpt-sovits", profile: "p", status: "failed", created_at: "now" }, 2)).toEqual([
      { kind: "latest_failed" },
      { kind: "version_count", count: 2 }
    ]);
  });

  it("separates inspector modes for current line config and selected version params", () => {
    expect(inspectorPanelMode(null)).toBe("line_config");
    expect(inspectorPanelMode(undefined)).toBe("line_config");
    expect(inspectorPanelMode("v004")).toBe("version_params");
  });

  it("orders inspector sections by task instead of showing every technical block", () => {
    expect(inspectorSections("line_config")).toEqual(["config", "reference"]);
    expect(inspectorSections("version_params")).toEqual(["version", "config", "reference"]);
  });

  it("keeps passive latest generation summaries in the line history instead of the inspector", () => {
    expect(inspectorVersionContextVisible("line_config", null)).toBe(false);
    expect(inspectorVersionContextVisible("version_params", null)).toBe(false);
    expect(inspectorVersionContextVisible("version_params", "v002")).toBe(true);
  });

  it("only surfaces service diagnostics when loading state needs attention or user expands it", () => {
    expect(inspectorDiagnosticsState({
      loaded: true,
      loadedSignature: "sig-a",
      expectedSignature: "sig-a",
      expanded: false
    })).toEqual({ visible: false, expanded: false, tone: "neutral", reason: "ready" });

    expect(inspectorDiagnosticsState({
      loaded: false,
      loadedSignature: null,
      expectedSignature: "sig-a",
      expanded: false
    })).toMatchObject({ visible: true, expanded: false, tone: "warn", reason: "not_loaded" });

    expect(inspectorDiagnosticsState({
      loaded: true,
      loadedSignature: "sig-old",
      expectedSignature: "sig-new",
      expanded: false
    })).toMatchObject({ visible: true, expanded: false, tone: "warn", reason: "signature_mismatch" });

    expect(inspectorDiagnosticsState({
      loaded: true,
      loadedSignature: "sig-a",
      expectedSignature: "sig-a",
      lastError: "load failed",
      expanded: false
    })).toMatchObject({ visible: true, expanded: true, tone: "danger", reason: "error" });
  });

  it("summarizes history versions for batch audio playback", () => {
    expect(historyPlayerSummary({
      version_id: "v004",
      engine: "gpt-sovits",
      profile: "xiao-pin",
      status: "completed",
      audio_path: "data/demo/audio/l001.wav",
      created_at: "now"
    })).toEqual({
      versionId: "v004",
      playable: true,
      status: "completed",
      audioPath: "data/demo/audio/l001.wav"
    });

    expect(historyPlayerSummary({
      version_id: "v005",
      engine: "gpt-sovits",
      profile: "xiao-pin",
      status: "failed",
      created_at: "now"
    })).toMatchObject({ versionId: "v005", playable: false, audioPath: null });
  });

  it("uses only current role library reference audio as backup inspector sources", () => {
    const line: ScriptLine = { id: "l001", character_id: "zhu-jue", text: "呼……", note: "" };
    const characters: Character[] = [
      {
        id: "zhu-jue",
        name: "光头",
        aliases: [],
        notes: "",
        fallback_profiles: [],
        reference_audio_groups: [
          {
            id: "gt-local",
            name: "光头测试音",
            paths: ["refs/gt-01.wav"],
            copied_paths: ["refs/gt-02.wav"],
            samples: [{ path: "refs/gt-01.wav", text: "样本文本" }]
          }
        ]
      }
    ];

    expect(trustedBackupReferenceGroups(line, characters)).toEqual([
      {
        id: "gt-local",
        name: "光头测试音",
        path: "refs/gt-01.wav",
        audio_count: 2,
        samples: ["refs/gt-01.wav", "refs/gt-02.wav"]
      }
    ]);

    expect(trustedBackupReferenceGroups({ ...line, temporary_binding: { binding_id: "tmp", provider_type: "indextts", config: {}, capabilities: [], fallback_services: [] } }, characters)).toEqual([]);
    expect(trustedBackupReferenceGroups({ ...line, character_id: "missing" }, characters)).toEqual([]);
  });

  it("keeps backup reference indexes out of the line inspector", () => {
    expect(inspectorBackupReferenceVisible("gpt-sovits", 2)).toBe(false);
    expect(inspectorBackupReferenceVisible("indextts", 2)).toBe(false);
    expect(inspectorBackupReferenceVisible("openai", 0)).toBe(false);
  });

  it("maps generation preflight user action to a local fallback service", () => {
    const services: WorkerHealth[] = [
      { service_id: "local-gpt", display_name: "GPT-SoVITS Local", engine: "gpt-sovits", provider_type: "gpt-sovits", ready: false }
    ];

    const action = preflightFallbackAction(
      {
        status: "needs_user_action",
        items: [
          {
            line_id: "l001",
            status: "needs_user_action",
            selected_service_id: null,
            fallback_action: { type: "start_service", service_id: "local-gpt" },
            reason: "no ready service",
          }
        ],
      },
      services
    );

    expect(action).toEqual({ type: "start_service", serviceId: "local-gpt", serviceName: "GPT-SoVITS Local" });
  });

  it("maps preflight readiness and load signatures to compact line chips", () => {
    const ready = { line_id: "l001", status: "ready", selected_service_id: "local-gpt", load_signature: "sig-a" };
    const action = { line_id: "l002", status: "needs_user_action", selected_service_id: null, reason: "service stopped" };
    const blocked = { line_id: "l003", status: "blocked", selected_service_id: null, reason: "missing reference" };

    expect(preflightLineTone(ready)).toBe("ok");
    expect(preflightLineLabelKey(action)).toBe("preflight.needsAction");
    expect(preflightLineTone(blocked)).toBe("danger");
    expect(preflightLoadTone(ready, "sig-a")).toBe("ok");
    expect(preflightLoadLabelKey(ready, "sig-b")).toBe("preflight.switchNeeded");
    expect(preflightLoadLabelKey(ready, null)).toBe("preflight.notLoaded");
    expect(preflightLoadTone({ ...ready, load_state: "loaded", current_loaded_signature: "sig-a", load_match: true }, null)).toBe("ok");
    expect(preflightLoadLabelKey({ ...ready, load_state: "switch_required", current_loaded_signature: "sig-old", load_match: false }, null)).toBe("preflight.switchNeeded");
  });

  it("defines the script drawer tabs in production workflow order", () => {
    expect(scriptDrawerTabs().map((tab) => tab.id)).toEqual(["list", "edit", "preview", "history"]);
  });

  it("builds a compact script excerpt for the left console without editing controls", () => {
    const excerpt = scriptExcerptLines("第一行\n\n第二行\n第三行\n第四行\n第五行\n第六行\n第七行", 4);

    expect(excerpt).toEqual(["第一行", "第二行", "第三行", "第四行", "…"]);
  });

  it("only requests revision confirmation after history exists", () => {
    expect(shouldRequestRevisionConfirmation(0, 0)).toBe(false);
    expect(shouldRequestRevisionConfirmation(1, 1)).toBe(false);
    expect(shouldRequestRevisionConfirmation(2, 1)).toBe(true);
    expect(shouldRequestRevisionConfirmation(1, 2)).toBe(true);
  });
});
