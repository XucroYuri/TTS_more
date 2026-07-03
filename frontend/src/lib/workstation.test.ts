import { describe, expect, it } from "vitest";

import type { GenerationManifest, RuntimeMode, ScriptLine, VoiceCandidates, WorkerHealth } from "../types";
import { filterScriptLines, serviceTopbarSummary, standardProjectName, toggleLineSelection, validationRunState } from "./workstation";

const lines: ScriptLine[] = [
  { id: "l1", character_id: "narrator", text: "Rain stopped.", note: "calm" },
  { id: "l2", character_id: "alice", text: "你终于来了。", note: "low", binding_override: "alice-gpt" },
  { id: "l3", character_id: "bob", text: "我没有忘。", note: "angry", binding_override: "bob-index" }
];

const services: WorkerHealth[] = [
  { service_id: "local-gpt-sovits", engine: "gpt-sovits", provider_type: "gpt-sovits", ready: true, base_url: "http://127.0.0.1:9880", supervisor: { service_id: "local-gpt-sovits", manageable: true, running: true } },
  { service_id: "local-indextts", engine: "indextts", provider_type: "indextts", ready: true, base_url: "http://127.0.0.1:9881", supervisor: { service_id: "local-indextts", manageable: true, running: true } }
];

const realRuntime: RuntimeMode = { service_mode: "real", data_root: "data", runtime_root: "data/.runtime", services: [] };
const mockRuntime: RuntimeMode = { ...realRuntime, service_mode: "mock" };

const readyCandidates: VoiceCandidates = {
  ready: true,
  reference_audio: { path: "refs", exists: true, is_dir: true, groups: [] },
  gpt_sovits: { gpt_weights: [], sovits_weights: [], diagnostics: [] },
  indextts: { reference_audio: [], model: { path: "checkpoints", ready: true, missing: [] }, diagnostics: [] }
};

const emptyManifest: GenerationManifest = { project_id: "validation", lines: {} };

describe("workstation helpers", () => {
  it("blocks real validation in mock mode and reports a clear reason", () => {
    const state = validationRunState(
      mockRuntime,
      services,
      readyCandidates,
      emptyManifest,
      false,
      false
    );

    expect(state.disabled).toBe(true);
    expect(state.reasonKey).toBe("validation.reason.mockMode");
  });

  it("allows real validation only when mode, services, and resources are ready", () => {
    const state = validationRunState(
      realRuntime,
      services,
      readyCandidates,
      emptyManifest,
      false,
      false
    );

    expect(state).toMatchObject({ disabled: false, reasonKey: null });
  });

  it("reports the first missing local service before resource problems", () => {
    const state = validationRunState(
      realRuntime,
      [{ ...services[0], ready: false }, services[1]],
      { ...readyCandidates, ready: false },
      emptyManifest,
      false,
      false
    );

    expect(state.disabled).toBe(true);
    expect(state.reasonKey).toBe("validation.reason.serviceNotReady");
    expect(state.serviceId).toBe("local-gpt-sovits");
  });

  it("filters script lines by character, provider, status, and search text", () => {
    const manifest: GenerationManifest = {
      project_id: "demo",
      lines: {
        l2: {
          line_id: "l2",
          versions: [{ version_id: "v001", engine: "gpt-sovits", profile: "p", status: "completed", created_at: "now" }]
        }
      }
    };

    expect(filterScriptLines(lines, manifest, { characterId: "alice", provider: "gpt-sovits", status: "completed", search: "终于" })).toEqual([lines[1]]);
    expect(filterScriptLines(lines, manifest, { status: "not-generated" }).map((line) => line.id)).toEqual(["l1", "l3"]);
  });

  it("toggles line selection without disturbing other selected lines", () => {
    expect(toggleLineSelection(["l1", "l3"], "l2")).toEqual(["l1", "l3", "l2"]);
    expect(toggleLineSelection(["l1", "l3"], "l1")).toEqual(["l3"]);
  });

  it("formats slug-like project names as standard display names", () => {
    expect(standardProjectName("demo-script")).toBe("Demo Script");
    expect(standardProjectName("my_new_project")).toBe("My New Project");
    expect(standardProjectName("剧本配音项目")).toBe("剧本配音项目");
  });

  it("summarizes key service state for the topbar", () => {
    const summary = serviceTopbarSummary(
      [
        ...services,
        { service_id: "openai-tts", engine: "commercial", provider_type: "openai", ready: false, base_url: "https://api.openai.com/v1", capabilities: ["paid_provider"], key_configured: false },
        { service_id: "gemini-tts", engine: "commercial", provider_type: "gemini", ready: true, base_url: "https://generativelanguage.googleapis.com/v1beta", capabilities: ["paid_provider"], key_configured: true },
      ],
      readyCandidates,
      [
        { enabled: true, key_configured: true },
        { enabled: true, key_configured: false },
      ]
    );

    expect(summary.local).toEqual({ ready: 2, total: 2, tone: "ready" });
    expect(summary.paid).toEqual({ ready: 1, total: 2, tone: "attention" });
    expect(summary.parser).toEqual({ ready: 1, total: 2, tone: "attention" });
    expect(summary.resources).toEqual({ ready: true, tone: "ready" });
    expect(summary.overallTone).toBe("attention");
  });

  it("ignores disabled optional local endpoints when LAN Gradio core services are ready", () => {
    const summary = serviceTopbarSummary(
      [
        { service_id: "local-gpt-sovits", engine: "gpt-sovits", provider_type: "gpt-sovits", ready: false, enabled: false, base_url: "http://127.0.0.1:9880" },
        { service_id: "local-indextts", engine: "indextts", provider_type: "indextts", ready: false, enabled: false, base_url: "http://127.0.0.1:9881" },
        { service_id: "lan-gpt", engine: "gpt-sovits", provider_type: "gpt-sovits", ready: true, enabled: true, base_url: "http://192.0.2.166:9872", network_scope: "lan", capabilities: ["gradio_webui"] },
        { service_id: "lan-index", engine: "indextts", provider_type: "indextts", ready: true, enabled: true, base_url: "http://192.0.2.166:7860", network_scope: "lan", capabilities: ["gradio_webui"] }
      ],
      readyCandidates,
      []
    );

    expect(summary.local).toEqual({ ready: 2, total: 2, tone: "ready" });
    expect(summary.overallTone).toBe("ready");
  });
});
