import { act, createElement, useState, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { initI18n } from "../../i18n";
import type {
  AnalysisRun,
  ScriptProject,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticConfirmResponse
} from "../../types";
import type { AnalysisDraftApi } from "./useAnalysisDraft";
import {
  AnalysisStageGate,
  beginAnalysisSourceRevision,
  buildConfirmedAnalysisHandoff,
  readAnalysisScriptFile,
  reviewConfirmedAnalysis,
  shouldAutosaveWorkspace,
  type WorkspaceStage
} from "./analysisFlow";

const timestamp = "2026-09-01T00:00:00.000Z";
const projectId = "project-task-9";
const sourceRevision: ScriptRevision = {
  revision_id: "script-r009",
  source_markdown: "\u80f6\u5e03\uff1a\u5feb\u8dd1\uff01",
  source_filename: "scene.md",
  source_media_type: "text/markdown",
  source_sha256: "sha-task-9",
  created_at: timestamp
};

initI18n();

describe("reviewConfirmedAnalysis", () => {
  it("reuses the exact active revision identity", () => {
    expect(reviewConfirmedAnalysis(projectId, sourceRevision)).toEqual({
      projectId,
      revisionId: sourceRevision.revision_id,
      sourceSha256: sourceRevision.source_sha256
    });
  });
});

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();
  get length(): number { return this.values.size; }
  clear(): void { this.values.clear(); }
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string): void { this.values.delete(key); }
  setItem(key: string, value: string): void { this.values.set(key, String(value)); }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function revisionPayload(project: ScriptProject = confirmedProject) {
  return { project, script_revision: sourceRevision };
}

const confirmedProject: ScriptProject = {
  title: "Server project",
  default_language: "zh-CN",
  active_script_revision_id: sourceRevision.revision_id,
  active_parse_revision_id: "semantic-semantic-r009",
  script_revisions: [sourceRevision],
  parse_revisions: [],
  project_characters: [{
    project_character_id: "character-glue",
    name: "\u80f6\u5e03",
    mode: "reference",
    library_character_id: null
  }],
  lines: [{
    id: "utterance-9",
    line_uid: "semantic-semantic-r009:utterance-9",
    character_id: "character-glue",
    text: "\u5feb\u8dd1\uff01",
    note: "",
    language: "zh-CN",
    semantic_revision_id: "semantic-r009",
    utterance_id: "utterance-9"
  }]
};

function completedRun(error: AnalysisRun["error"] = null): AnalysisRun {
  return {
    id: "run-task-9",
    project_id: projectId,
    source_revision_id: sourceRevision.revision_id,
    draft_id: "draft-task-9",
    status: error ? "failed" : "completed",
    quality: error ? null : "complete",
    progress: 1,
    warnings: [],
    error,
    trace_id: "trace-task-9",
    created_at: timestamp,
    updated_at: timestamp
  };
}

function confirmableDraft(): SemanticAnalysisDraft {
  return {
    id: "draft-task-9",
    project_id: projectId,
    source_revision_id: sourceRevision.revision_id,
    version: 3,
    annotations: [{
      id: "dialogue-9",
      kind: "dialogue",
      span: {
        source_revision_id: sourceRevision.revision_id,
        start_utf16: 3,
        end_utf16: 6,
        text: "\u5feb\u8dd1\uff01",
        source_sha256: sourceRevision.source_sha256!
      },
      origin: "ai",
      confidence: 0.95,
      status: "accepted",
      created_at: timestamp,
      updated_at: timestamp
    }],
    characters: [{
      id: "candidate-glue",
      canonical_name: "\u80f6\u5e03",
      aliases: [],
      supporting_annotation_ids: [],
      project_character_id: "character-glue",
      confidence: 0.95,
      status: "accepted",
      origin: "ai"
    }],
    utterances: [{
      id: "utterance-9",
      dialogue_annotation_id: "dialogue-9",
      speaker_annotation_id: null,
      character_candidate_id: "candidate-glue",
      emotion_evidence_annotation_ids: [],
      normalized_emotion: null,
      custom_emotion: null,
      emotion_intensity: null,
      emotion_origin: "none",
      language: "zh-CN",
      confidence: 0.95,
      uncertainty_codes: [],
      status: "accepted"
    }],
    unresolved_candidates: [],
    warnings: [],
    provider: "fake-provider",
    model: "fake-model",
    prompt_version: "p9",
    contract_version: "v1",
    confirmed_revision_id: null,
    confirmed_parse_revision_id: null,
    confirmed_parse_fingerprint: null,
    confirm_idempotency_key: null,
    created_at: timestamp,
    updated_at: timestamp
  };
}

function confirmResponse(): SemanticConfirmResponse {
  const draft = confirmableDraft();
  return {
    project: confirmedProject,
    semantic_revision: {
      id: "semantic-r009",
      project_id: projectId,
      source_revision_id: sourceRevision.revision_id,
      annotations: draft.annotations,
      characters: draft.characters,
      utterances: draft.utterances,
      unresolved_candidates: [],
      warnings: [],
      provider: draft.provider,
      model: draft.model,
      prompt_version: draft.prompt_version,
      contract_version: draft.contract_version,
      created_at: timestamp
    },
    parse_revision: {
      revision_id: "semantic-semantic-r009",
      script_revision_id: sourceRevision.revision_id,
      provider: "semantic-confirmed",
      warnings: [],
      project_characters: confirmedProject.project_characters ?? [],
      lines: confirmedProject.lines,
      created_at: timestamp
    }
  };
}

function fakeApi(run = completedRun()): AnalysisDraftApi {
  return {
    createAnalysisRun: vi.fn(async () => ({
      run_id: run.id,
      draft_id: run.draft_id,
      status: run.status,
      trace_id: run.trace_id
    })),
    fetchAnalysisRun: vi.fn(async () => run),
    fetchAnalysisDraft: vi.fn(async () => confirmableDraft()),
    patchAnalysisDraft: vi.fn(async () => confirmableDraft()),
    confirmAnalysisDraft: vi.fn(async () => confirmResponse())
  };
}

interface RenderedView {
  dom: JSDOM;
  container: HTMLElement;
  root: Root;
  cleanup: () => Promise<void>;
}

const activeViews: RenderedView[] = [];

async function renderElement(element: ReactElement): Promise<RenderedView> {
  const dom = new JSDOM('<div id="root"></div>', { pretendToBeVisual: true, url: "http://localhost" });
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousHTMLElement = globalThis.HTMLElement;
  const globals = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = globals.IS_REACT_ACT_ENVIRONMENT;
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true
  });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  await act(async () => root.render(element));
  const view = {
    dom,
    container,
    root,
    cleanup: async () => {
      await act(async () => root.unmount());
      Object.assign(globalThis, {
        window: previousWindow,
        document: previousDocument,
        HTMLElement: previousHTMLElement,
        IS_REACT_ACT_ENVIRONMENT: previousActEnvironment
      });
    }
  };
  activeViews.push(view);
  return view;
}

async function flushAsync(rounds = 10): Promise<void> {
  await act(async () => {
    for (let index = 0; index < rounds; index += 1) await Promise.resolve();
  });
}

afterEach(async () => {
  while (activeViews.length) await activeViews.pop()!.cleanup();
  vi.restoreAllMocks();
});

describe("analysis source entry", () => {
  it("reads txt/md with exact CRLF and keeps upload metadata only for unchanged source", async () => {
    const outcome = await readAnalysisScriptFile(
      new File(["\uFEFF\u7532\r\n  \u53f0\u8bcd  "], "scene.Md", { type: "text/markdown" }),
      "old source",
      () => true
    );

    expect(outcome).toEqual({
      status: "loaded",
      source: "\u7532\r\n  \u53f0\u8bcd  ",
      metadata: {
        sourceText: "\u7532\r\n  \u53f0\u8bcd  ",
        filename: "scene.Md",
        mediaType: "text/markdown",
        warning: undefined
      }
    });
  });

  it("keeps the old source for unsupported, failed, and stale file reads", async () => {
    const unsupported = await readAnalysisScriptFile(
      new File(["x"], "scene.docx"),
      "keep me",
      () => true
    );
    const readFailure = await readAnalysisScriptFile(
      new File(["x"], "scene.txt"),
      "keep me",
      () => true,
      async () => { throw new Error("disk failed"); }
    );
    const staleRead = await readAnalysisScriptFile(
      new File(["new"], "scene.txt"),
      "keep me",
      () => false
    );

    expect(unsupported).toEqual({ status: "error", source: "keep me", errorCode: "unsupported_script_file" });
    expect(readFailure).toEqual({ status: "error", source: "keep me", errorCode: "script_file_read_failed" });
    expect(staleRead).toEqual({ status: "stale", source: "keep me" });
  });

  it("keeps the file warning with the exact loaded source metadata", async () => {
    const warning = {
      code: "script_file_exceeds_recommended_limit" as const,
      codePointCount: 200_001,
      limit: 200_000
    };
    const outcome = await readAnalysisScriptFile(
      new File(["large source"], "large.txt", { type: "text/plain" }),
      "old source",
      () => true,
      async () => ({
        text: "large source",
        filename: "large.txt",
        mediaType: "text/plain",
        warning
      })
    );

    expect(outcome).toEqual({
      status: "loaded",
      source: "large source",
      metadata: {
        sourceText: "large source",
        filename: "large.txt",
        mediaType: "text/plain",
        warning
      }
    });
  });

  it("waits for an exact source revision before entering analysis and discards stale completions", async () => {
    const pending = deferred<ReturnType<typeof revisionPayload>>();
    const onReady = vi.fn();
    const createRevision = vi.fn(async () => pending.promise);
    const exactSource = "\r\n  \u7532\uff1a\u5feb\u8dd1\uff01  \r\n";
    const start = beginAnalysisSourceRevision({
      projectId,
      source: exactSource,
      summary: "Analyze source",
      metadata: {
        sourceText: exactSource,
        filename: "scene.md",
        mediaType: "text/markdown"
      },
      isCurrent: () => true,
      onReady,
      createRevision
    });

    await Promise.resolve();
    expect(onReady).not.toHaveBeenCalled();
    pending.resolve(revisionPayload());
    await expect(start).resolves.toBe("started");
    expect(onReady).toHaveBeenCalledWith(revisionPayload());
    expect(createRevision).toHaveBeenCalledWith(projectId, exactSource, "Analyze source", {
      source_filename: "scene.md",
      source_media_type: "text/markdown"
    });

    const staleReady = vi.fn();
    await expect(beginAnalysisSourceRevision({
      projectId,
      source: "pasted source",
      summary: "Analyze paste",
      isCurrent: () => false,
      onReady: staleReady,
      createRevision: async () => revisionPayload()
    })).resolves.toBe("stale");
    expect(staleReady).not.toHaveBeenCalled();
  });

  it("never enters analysis when revision creation fails", async () => {
    const onReady = vi.fn();
    await expect(beginAnalysisSourceRevision({
      projectId,
      source: "  exact source  ",
      summary: "Analyze source",
      isCurrent: () => true,
      onReady,
      createRevision: async () => { throw new Error("revision failed"); }
    })).rejects.toThrow("revision failed");
    expect(onReady).not.toHaveBeenCalled();
  });

  it("omits upload metadata after the loaded source is edited", async () => {
    const createRevision = vi.fn(async () => revisionPayload());

    await expect(beginAnalysisSourceRevision({
      projectId,
      source: "edited source\r\n  ",
      summary: "Analyze edited source",
      metadata: {
        sourceText: "original source\r\n  ",
        filename: "scene.md",
        mediaType: "text/markdown"
      },
      isCurrent: () => true,
      onReady: vi.fn(),
      createRevision
    })).resolves.toBe("started");

    expect(createRevision).toHaveBeenCalledWith(
      projectId,
      "edited source\r\n  ",
      "Analyze edited source",
      undefined
    );
  });
});

describe("analysis stage gate", () => {
  it("blocks the broad project autosave throughout semantic review", () => {
    expect(shouldAutosaveWorkspace("analysis")).toBe(false);
    expect(shouldAutosaveWorkspace("tts")).toBe(true);
  });

  it("keeps TTS hidden until the real workspace confirms the server-authoritative project", async () => {
    const api = fakeApi();
    const oldProject: ScriptProject = {
      title: "Old project",
      default_language: "zh-CN",
      active_parse_revision_id: "parse-old",
      lines: [{ id: "old-line", character_id: "old", text: "\u65e7\u53f0\u8bcd", note: "", language: "zh-CN" }]
    };

    function Harness() {
      const [stage, setStage] = useState<WorkspaceStage>("analysis");
      const [project, setProject] = useState(oldProject);
      return createElement(AnalysisStageGate, {
        stage,
        projectId,
        sourceRevision,
        onConfirmed: (confirmed) => {
          setProject(confirmed);
          setStage("tts");
        },
        onCancel: () => setStage("tts"),
        controllerOptions: {
          api,
          storage: new MemoryStorage(),
          pollIntervalMs: 1,
          createIdempotencyKey: () => "confirm-task-9"
        },
        ttsWorkbench: createElement(
          "section",
          { className: "workbench-grid", "data-active-parse": project.active_parse_revision_id },
          project.lines.map((line) => createElement("article", { key: line.id }, line.text))
        )
      });
    }

    const view = await renderElement(createElement(Harness));
    await flushAsync();
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.container.querySelector(".workbench-grid")).toBeNull();

    await act(async () => {
      view.container.querySelector<HTMLButtonElement>('[data-action="confirm-open"]')!.click();
    });
    await act(async () => {
      view.container.querySelector<HTMLButtonElement>('[data-action="confirm-submit"]')!.click();
    });
    await flushAsync();

    const workbench = view.container.querySelector(".workbench-grid");
    expect(workbench).not.toBeNull();
    expect(workbench?.getAttribute("data-active-parse")).toBe("semantic-semantic-r009");
    expect(workbench?.textContent).toContain("\u5feb\u8dd1\uff01");
  });

  it("cancels back to the unchanged TTS project without confirming", async () => {
    const api = fakeApi();
    const oldProject: ScriptProject = {
      title: "Old project",
      default_language: "zh-CN",
      active_parse_revision_id: "parse-old",
      lines: [{ id: "old-line", character_id: "old", text: "\u65e7\u53f0\u8bcd", note: "", language: "zh-CN" }]
    };

    function Harness() {
      const [stage, setStage] = useState<WorkspaceStage>("analysis");
      return createElement(AnalysisStageGate, {
        stage,
        projectId,
        sourceRevision,
        onConfirmed: () => undefined,
        onCancel: () => setStage("tts"),
        controllerOptions: { api, storage: new MemoryStorage(), pollIntervalMs: 1 },
        ttsWorkbench: createElement("section", {
          className: "workbench-grid",
          "data-active-parse": oldProject.active_parse_revision_id
        }, oldProject.lines[0].text)
      });
    }

    const view = await renderElement(createElement(Harness));
    await flushAsync();
    await act(async () => {
      view.container.querySelector<HTMLButtonElement>('[data-action="cancel-workspace"]')!.click();
    });

    const workbench = view.container.querySelector(".workbench-grid");
    expect(workbench?.getAttribute("data-active-parse")).toBe("parse-old");
    expect(workbench?.textContent).toContain("\u65e7\u53f0\u8bcd");
    expect(api.confirmAnalysisDraft).not.toHaveBeenCalled();
  });

  it("keeps semantic 422 errors inside the persistent analysis panel", async () => {
    const error = {
      code: "semantic_contract_invalid",
      http_status: 422,
      stage: "decode",
      message: "Invalid semantic response",
      retryable: false,
      run_id: "run-task-9",
      trace_id: "trace-422",
      occurred_at: timestamp,
      details: {}
    };
    const view = await renderElement(createElement(AnalysisStageGate, {
      stage: "analysis",
      projectId,
      sourceRevision,
      onConfirmed: () => undefined,
      onCancel: () => undefined,
      controllerOptions: {
        api: fakeApi(completedRun(error)),
        storage: new MemoryStorage(),
        pollIntervalMs: 1
      },
      ttsWorkbench: createElement("section", { className: "workbench-grid" }, "TTS")
    }));
    await flushAsync();

    const errorDialog = view.container.querySelector<HTMLElement>('[data-analysis-error="run"]')!;
    expect(errorDialog.textContent).not.toContain("trace-422");
    await act(async () => {
      errorDialog
        .querySelector('[data-error-action="details"]')!
        .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    });
    expect(errorDialog.textContent).toContain("trace-422");
    expect(view.container.querySelector(".script-manager-parse-error")).toBeNull();
    expect(view.container.querySelector(".workbench-grid")).toBeNull();
  });
});

describe("confirmed analysis handoff", () => {
  it("selects the first server line, clears TTS-local state, and keeps exact revision source", () => {
    const handoff = buildConfirmedAnalysisHandoff(projectId, confirmedProject, sourceRevision);

    expect(handoff).toEqual({
      currentProjectId: projectId,
      project: confirmedProject,
      activeLineId: "utterance-9",
      expandedLineId: null,
      selectedLineIds: [],
      selectedHistoryVersions: {},
      versionDrafts: {},
      lineTextDrafts: {},
      managedProjectId: projectId,
      managedProject: confirmedProject,
      managerTitleDraft: "Server project",
      managerSourceDraft: "\u80f6\u5e03\uff1a\u5feb\u8dd1\uff01"
    });
    expect(handoff.project.active_parse_revision_id).toBe("semantic-semantic-r009");
  });
});
