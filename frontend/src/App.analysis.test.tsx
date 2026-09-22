import { act, createElement, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initI18n } from "./i18n";
import type {
  AnalysisRun,
  ProjectSummary,
  ScriptProject,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticConfirmResponse
} from "./types";
import {
  ANALYSIS_CACHE_RESET_STORAGE_KEY,
  ANALYSIS_CACHE_RESET_VERSION,
  ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY,
  ANALYSIS_RUN_SESSIONS_STORAGE_KEY
} from "./features/script-analysis/useAnalysisDraft";

const apiMocks = vi.hoisted(() => ({
  fetchAuthStatus: vi.fn(),
  fetchServices: vi.fn(),
  fetchServiceSettings: vi.fn(),
  fetchServicesStatus: vi.fn(),
  fetchOpenSourceTTSCatalog: vi.fn(),
  fetchRuntimeMode: vi.fn(),
  fetchVoiceCandidates: vi.fn(),
  fetchLogsReferenceAudio: vi.fn(),
  fetchGptSovitsModelSamples: vi.fn(),
  fetchQueueStatus: vi.fn(),
  fetchProjects: vi.fn(),
  fetchProject: vi.fn(),
  fetchProjectCharacters: vi.fn(),
  fetchManifest: vi.fn(),
  fetchParserProviders: vi.fn(),
  fetchCharacters: vi.fn(),
  saveProject: vi.fn(),
  saveCharacters: vi.fn(),
  createScriptRevision: vi.fn(),
  createParseRevision: vi.fn(),
  createAnalysisRun: vi.fn(),
  fetchAnalysisReviewSession: vi.fn(),
  fetchAnalysisHistory: vi.fn(),
  deleteAnalysisRun: vi.fn(),
  fetchAnalysisRun: vi.fn(),
  fetchAnalysisDraft: vi.fn(),
  patchAnalysisDraft: vi.fn(),
  confirmAnalysisDraft: vi.fn()
}));

vi.mock("./api", async () => ({
  ...await vi.importActual<typeof import("./api")>("./api"),
  ...apiMocks
}));

import App from "./App";
import { ApiRequestError } from "./api";

const timestamp = "2026-09-01T00:00:00.000Z";
const activeAnalysisScopeStorageKey = "tts-more:active-analysis-scope";

initI18n();

interface RenderedApp {
  dom: JSDOM;
  container: HTMLElement;
  root: Root;
  cleanup: () => Promise<void>;
}

const activeViews: RenderedApp[] = [];
let backendProjects = new Map<string, ScriptProject>();

function cloneProject(project: ScriptProject): ScriptProject {
  return structuredClone(project);
}

function projectSummary(projectId: string, project: ScriptProject): ProjectSummary {
  return {
    project_id: projectId,
    title: project.title,
    default_language: project.default_language,
    line_count: project.lines.length,
    script_revision_count: project.script_revisions?.length ?? 0,
    parse_revision_count: project.parse_revisions?.length ?? 0,
    character_count: project.project_characters?.length ?? 0
  };
}

function scriptRevision(
  revisionId: string,
  source: string,
  sourceSha = `sha-${revisionId}`
): ScriptRevision {
  return {
    revision_id: revisionId,
    source_markdown: source,
    source_filename: null,
    source_media_type: null,
    source_sha256: sourceSha,
    created_at: timestamp
  };
}

function scriptProject(title: string, revision?: ScriptRevision): ScriptProject {
  return {
    title,
    default_language: "zh-CN",
    project_characters: [],
    active_script_revision_id: revision?.revision_id ?? null,
    active_parse_revision_id: null,
    script_revisions: revision ? [revision] : [],
    parse_revisions: [],
    lines: []
  };
}

function completedRun(projectId: string, revisionId: string): AnalysisRun {
  return {
    id: `run-${projectId}`,
    project_id: projectId,
    source_revision_id: revisionId,
    draft_id: `draft-${projectId}`,
    status: "completed",
    quality: "complete",
    progress: 1,
    warnings: [],
    error: null,
    trace_id: `trace-${projectId}`,
    created_at: timestamp,
    updated_at: timestamp
  };
}

function analysisDraft(projectId: string, revisionId: string): SemanticAnalysisDraft {
  return {
    id: `draft-${projectId}`,
    project_id: projectId,
    source_revision_id: revisionId,
    version: 1,
    annotations: [],
    characters: [],
    utterances: [],
    unresolved_candidates: [],
    warnings: [],
    provider: "fake",
    model: "fake",
    prompt_version: "p1",
    contract_version: "v1",
    confirmed_revision_id: null,
    confirmed_parse_revision_id: null,
    confirmed_parse_fingerprint: null,
    confirm_idempotency_key: null,
    created_at: timestamp,
    updated_at: timestamp
  };
}

function semanticConfirmation(
  projectId: string,
  revision: ScriptRevision,
  lineText: string
): SemanticConfirmResponse {
  const line = {
    id: `confirmed-${projectId}`,
    line_uid: `parse-${projectId}:confirmed-${projectId}`,
    character_id: "narrator",
    text: lineText,
    note: "",
    language: "zh-CN",
    semantic_revision_id: `semantic-${projectId}`,
    utterance_id: `utterance-${projectId}`
  };
  const project: ScriptProject = {
    ...scriptProject(`Confirmed ${projectId}`, revision),
    active_parse_revision_id: `parse-${projectId}`,
    lines: [line]
  };
  return {
    project,
    semantic_revision: {
      id: `semantic-${projectId}`,
      project_id: projectId,
      source_revision_id: revision.revision_id,
      annotations: [],
      characters: [],
      utterances: [],
      unresolved_candidates: [],
      warnings: [],
      provider: "fake",
      model: "fake",
      prompt_version: "p1",
      contract_version: "v1",
      created_at: timestamp
    },
    parse_revision: {
      revision_id: `parse-${projectId}`,
      script_revision_id: revision.revision_id,
      provider: "semantic-confirmed",
      warnings: [],
      project_characters: [],
      lines: [line],
      created_at: timestamp
    }
  };
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

function resetApiDefaults(): void {
  vi.resetAllMocks();
  backendProjects = new Map();
  apiMocks.fetchAuthStatus.mockImplementation(async () => ({ auth_required: false }));
  apiMocks.fetchServices.mockImplementation(async () => ({ services: [] }));
  apiMocks.fetchServiceSettings.mockImplementation(async () => ({ services: [] }));
  apiMocks.fetchServicesStatus.mockImplementation(async () => ({ services: [], hardware: {} }));
  apiMocks.fetchOpenSourceTTSCatalog.mockImplementation(async () => ({ providers: [] }));
  apiMocks.fetchRuntimeMode.mockImplementation(async () => ({
    service_mode: "test",
    data_root: "",
    runtime_root: "",
    services: []
  }));
  apiMocks.fetchVoiceCandidates.mockImplementation(async () => null);
  apiMocks.fetchLogsReferenceAudio.mockImplementation(async () => ({
    service_id: null,
    logs_name: "",
    samples: [],
    diagnostics: []
  }));
  apiMocks.fetchGptSovitsModelSamples.mockImplementation(async () => ({
    service_id: null,
    logs_name: "",
    samples: [],
    diagnostics: []
  }));
  apiMocks.fetchQueueStatus.mockImplementation(async () => null);
  apiMocks.fetchProjects.mockImplementation(async () => ({
    projects: [...backendProjects].map(([projectId, project]) => projectSummary(projectId, project))
  }));
  apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
    const project = backendProjects.get(projectId);
    if (!project) throw new Error(`missing project: ${projectId}`);
    return cloneProject(project);
  });
  apiMocks.fetchProjectCharacters.mockImplementation(async () => ({ project_characters: [] }));
  apiMocks.fetchManifest.mockImplementation(async (projectId: string) => ({ project_id: projectId, lines: {} }));
  apiMocks.fetchParserProviders.mockImplementation(async () => ({ providers: [] }));
  apiMocks.fetchCharacters.mockImplementation(async () => []);
  apiMocks.fetchAnalysisHistory.mockImplementation(async () => ({ runs: [] }));
  apiMocks.deleteAnalysisRun.mockImplementation(async (runId: string) => ({ deleted_run_id: runId, deleted_draft_id: "draft-deleted" }));
  apiMocks.saveProject.mockImplementation(async (projectId: string, project: ScriptProject) => {
    backendProjects.set(projectId, cloneProject(project));
  });
  apiMocks.saveCharacters.mockImplementation(async () => undefined);
  apiMocks.createScriptRevision.mockImplementation(async (projectId: string, source: string) => {
    const current = backendProjects.get(projectId);
    if (!current) throw new Error(`missing project: ${projectId}`);
    const revision = scriptRevision(`revision-${(current.script_revisions?.length ?? 0) + 1}`, source);
    const project: ScriptProject = {
      ...cloneProject(current),
      active_script_revision_id: revision.revision_id,
      script_revisions: [...(current.script_revisions ?? []), revision]
    };
    backendProjects.set(projectId, cloneProject(project));
    return { project, script_revision: revision };
  });
}

async function renderApp(
  storedProjectId: string | null = null,
  setupStorage?: (storage: Storage) => void
): Promise<RenderedApp> {
  const dom = new JSDOM('<div id="root"></div>', { pretendToBeVisual: true, url: "http://localhost" });
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousHTMLElement = globalThis.HTMLElement;
  const previousNode = globalThis.Node;
  const previousEvent = globalThis.Event;
  const previousFile = globalThis.File;
  const previousGetComputedStyle = globalThis.getComputedStyle;
  const globals = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = globals.IS_REACT_ACT_ENVIRONMENT;
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    Event: dom.window.Event,
    File: dom.window.File,
    getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
    IS_REACT_ACT_ENVIRONMENT: true
  });
  if (storedProjectId) dom.window.localStorage.setItem("tts-more.currentProjectId", storedProjectId);
  dom.window.localStorage.setItem(ANALYSIS_CACHE_RESET_STORAGE_KEY, ANALYSIS_CACHE_RESET_VERSION);
  setupStorage?.(dom.window.localStorage);
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  await act(async () => root.render(createElement(App)));
  const view: RenderedApp = {
    dom,
    container,
    root,
    cleanup: async () => {
      await act(async () => root.unmount());
      Object.assign(globalThis, {
        window: previousWindow,
        document: previousDocument,
        HTMLElement: previousHTMLElement,
        Node: previousNode,
        Event: previousEvent,
        File: previousFile,
        getComputedStyle: previousGetComputedStyle,
        IS_REACT_ACT_ENVIRONMENT: previousActEnvironment
      });
    }
  };
  activeViews.push(view);
  return view;
}

async function flushAsync(rounds = 20): Promise<void> {
  await act(async () => {
    for (let index = 0; index < rounds; index += 1) await Promise.resolve();
  });
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    (element as HTMLElement).click();
  });
}

async function changeReactValueExact(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string): Promise<void> {
  const reactPropsKey = Object.keys(element).find((key) => key.startsWith("__reactProps$"));
  if (!reactPropsKey) throw new Error("React props were not attached to the field");
  const props = (element as unknown as Record<string, { onChange?: (event: { target: { value: string } }) => void }>)[reactPropsKey];
  await act(async () => {
    props.onChange?.({ target: { value } });
  });
}

beforeEach(() => {
  resetApiDefaults();
});

afterEach(async () => {
  while (activeViews.length) await activeViews.pop()!.cleanup();
  vi.useRealTimers();
});

describe("App semantic analysis entry", () => {
  it("shows global analysis history and deletes records of any status", async () => {
    const projectId = "history-project";
    backendProjects.set(projectId, scriptProject("历史剧本"));
    apiMocks.fetchAnalysisHistory.mockResolvedValue({
      runs: [{
        run_id: "run-history",
        draft_id: "draft-history",
        project_id: projectId,
        project_title: "历史剧本",
        source_revision_id: "script-r001",
        status: "running",
        progress: 0.35,
        error_code: null,
        created_at: timestamp,
        updated_at: timestamp
      }]
    });
    apiMocks.deleteAnalysisRun.mockResolvedValue({ deleted_run_id: "run-history", deleted_draft_id: "draft-history" });
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector('[data-action="analysis-history"]')!);
    await flushAsync();

    expect(view.container.querySelector(".analysis-history-popover")?.textContent).toContain("历史剧本");
    expect(view.container.querySelector(".analysis-history-popover")?.textContent).toContain("进行中");
    expect(view.container.querySelector(".analysis-history-popover")?.textContent).toContain("35%");

    await click(view.container.querySelector(".analysis-history-row .icon-button.danger")!);
    await click(view.container.querySelector(".confirm-modal .primary-button")!);
    await flushAsync();

    expect(apiMocks.deleteAnalysisRun).toHaveBeenCalledWith("run-history");
    expect(view.container.querySelector(".analysis-history-popover")?.textContent).toContain("暂无分析记录");
  });

  it("reuses analysis history in semantic review and exits when the current run is deleted", async () => {
    const projectId = "review-history-project";
    backendProjects.set(projectId, scriptProject("审阅历史剧本"));
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (targetProjectId: string, revisionId: string) => {
      const currentRun = completedRun(targetProjectId, revisionId);
      const currentDraft = analysisDraft(targetProjectId, revisionId);
      runs.set(currentRun.id, currentRun);
      drafts.set(currentDraft.id, currentDraft);
      return {
        run_id: currentRun.id,
        draft_id: currentRun.draft_id,
        status: currentRun.status,
        trace_id: currentRun.trace_id
      };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    apiMocks.fetchAnalysisHistory.mockImplementation(async () => ({
      runs: [...runs.values()].map((item) => ({
        run_id: item.id,
        draft_id: item.draft_id,
        project_id: item.project_id,
        project_title: "审阅历史剧本",
        source_revision_id: item.source_revision_id,
        status: item.status,
        progress: item.progress,
        error_code: item.error?.code ?? null,
        created_at: item.created_at,
        updated_at: item.updated_at
      }))
    }));
    apiMocks.deleteAnalysisRun.mockImplementation(async (runId: string) => ({
      deleted_run_id: runId,
      deleted_draft_id: runs.get(runId)!.draft_id
    }));
    const view = await renderApp(projectId);
    await flushAsync();

    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "旁白：进入语义分析审阅"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);

    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    await click(view.container.querySelector('[data-action="analysis-history"]')!);
    await flushAsync();
    expect(view.container.querySelector(".analysis-history-popover")?.textContent).toContain("审阅历史剧本");

    await click(view.container.querySelector(".analysis-history-row .icon-button.danger")!);
    await click(view.container.querySelector(".confirm-modal .primary-button")!);
    await flushAsync();

    expect(apiMocks.deleteAnalysisRun).toHaveBeenCalledWith(`run-${projectId}`);
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
  });

  it("loads portable logs references when a GPT-SoVITS binding has no logs_name", async () => {
    const projectId = "project-gpt-logs-root";
    const project = scriptProject("GPT logs root");
    project.lines = [{
      id: "gpt-line",
      character_id: "jiu-jiu",
      text: "听幽灵的，再等等……",
      note: "",
      language: "zh-CN",
      temporary_binding: {
        binding_id: "line-temp-gpt-sovits",
        provider_type: "gpt-sovits",
        service_id: "local-gpt",
        fallback_services: [],
        capabilities: ["tts", "reference_audio_voice"],
        config: {}
      }
    }];
    backendProjects.set(projectId, project);
    apiMocks.fetchServicesStatus.mockResolvedValue({
      services: [{
        service_id: "local-gpt",
        engine: "gpt-sovits",
        provider_type: "gpt-sovits",
        api_contract: "gpt-sovits-api-v2",
        ready: true,
        enabled: true,
        state: "ready",
        capabilities: ["tts", "reference_audio_voice", "model_catalog"]
      }],
      hardware: {}
    });
    apiMocks.fetchLogsReferenceAudio.mockResolvedValue({
      service_id: "local-gpt",
      logs_name: "",
      samples: [{
        sample_id: "task-alpha:jiu-jiu.wav",
        display_label: "九九：听幽灵的，再等等……",
        path: "E:\\portable\\logs\\task-alpha\\5-wav32k\\jiu-jiu.wav",
        text: "听幽灵的，再等等……",
        text_source: "name2text",
        character: "九九",
        emotion: "平静",
        remark: "",
        prompt_lang: "zh",
        source: "logs",
        logs_name: "task-alpha"
      }],
      diagnostics: []
    });
    const view = await renderApp(projectId);
    await flushAsync(40);

    const picker = view.container.querySelector<HTMLSelectElement>(".logs-reference-picker select")!;

    expect(picker).not.toBeNull();
    expect(picker.disabled).toBe(false);
    expect(picker.textContent).toContain("九九：听幽灵的，再等等……");
    expect(apiMocks.fetchLogsReferenceAudio).toHaveBeenCalledWith({
      serviceId: "local-gpt",
      logsName: "",
      gptWeightsPath: "",
      sovitsWeightsPath: ""
    });
    expect(apiMocks.fetchGptSovitsModelSamples).not.toHaveBeenCalled();
  });

  it("scopes portable logs to the sole auto-route ComfyUI GPT-SoVITS service", async () => {
    vi.useFakeTimers();
    const projectId = "project-gpt-auto-route-logs";
    const project = scriptProject("GPT auto-route logs");
    project.lines = [{
      id: "gpt-auto-line",
      character_id: "jiu-jiu",
      text: "再等等……",
      note: "",
      language: "zh-CN",
      temporary_binding: {
        binding_id: "line-temp-gpt-sovits",
        provider_type: "gpt-sovits",
        service_id: null,
        fallback_services: [],
        capabilities: ["tts", "reference_audio_voice"],
        config: {}
      }
    }];
    backendProjects.set(projectId, project);
    apiMocks.fetchServicesStatus.mockResolvedValue({
      services: [{
        service_id: "local-gpt-comfy",
        engine: "gpt-sovits",
        provider_type: "gpt-sovits",
        api_contract: "comfyui-tts-audio-suite-v1",
        base_url: "http://127.0.0.1:8188",
        ready: true,
        enabled: true,
        state: "ready",
        capabilities: ["tts", "reference_audio_voice", "comfyui"]
      }],
      hardware: {}
    });
    apiMocks.fetchLogsReferenceAudio.mockResolvedValue({
      service_id: "local-gpt-comfy",
      logs_name: "",
      samples: [{
        sample_id: "task-alpha:auto.wav",
        display_label: "九九：自动路由参考",
        path: "E:\\portable\\logs\\task-alpha\\5-wav32k\\auto.wav",
        text: "自动路由参考",
        text_source: "name2text",
        character: "九九",
        emotion: "平静",
        remark: "",
        prompt_lang: "zh",
        source: "logs",
        logs_name: "task-alpha"
      }],
      diagnostics: []
    });
    const view = await renderApp(projectId);
    await flushAsync(40);

    const picker = view.container.querySelector<HTMLSelectElement>(".logs-reference-picker select")!;
    expect(picker.disabled).toBe(false);
    expect(apiMocks.fetchLogsReferenceAudio).toHaveBeenCalledWith({
      serviceId: "local-gpt-comfy",
      logsName: "",
      gptWeightsPath: "",
      sovitsWeightsPath: ""
    });
    await changeReactValueExact(picker, "task-alpha:auto.wav");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(701);
    });
    await flushAsync(40);
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding?.config.logs_reference_service_id)
      .toBe("local-gpt-comfy");
  });

  it("passes a new project's exact leading/trailing whitespace and CRLF to its initial revision", async () => {
    const exactSource = "  第一行\r\n😀 第二行 \r\n";
    const view = await renderApp();
    await flushAsync();

    await click(view.container.querySelector('[data-drawer-tab="edit"]')!);
    const editPanel = view.container.querySelector<HTMLElement>('.script-manager-drawer-panel[aria-hidden="false"]')!;
    const titleInput = editPanel.querySelector<HTMLInputElement>('input:not([type="file"])')!;
    const sourceInput = editPanel.querySelector<HTMLTextAreaElement>("textarea")!;
    await changeReactValueExact(titleInput, "Exact source project");
    await changeReactValueExact(sourceInput, exactSource);
    await click(editPanel.querySelector("button.primary-button")!);
    await flushAsync();

    expect(apiMocks.createScriptRevision).toHaveBeenCalledOnce();
    expect(apiMocks.createScriptRevision.mock.calls[0][1]).toBe(exactSource);
  });

  it("passes exact source bytes through save-revision and exposes no legacy parse entry", async () => {
    const projectId = "project-exact-existing";
    const project = scriptProject("Exact existing");
    project.lines = [{ id: "old-line", character_id: "narrator", text: "旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    const exactSaveSource = "  保存版本\r\n尾部空白 \r\n";
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      exactSaveSource
    );
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync();
    expect(apiMocks.createScriptRevision.mock.calls[0][1]).toBe(exactSaveSource);

    expect(view.container.querySelector('[data-action="legacy-parse-script"]')).toBeNull();
  });

  it("single-flights two synchronous Analyze clicks before revision creation begins", async () => {
    const projectId = "project-double-click";
    const project = scriptProject("Double click");
    project.lines = [{
      id: "old-line",
      character_id: "narrator",
      text: "旧台词",
      note: "",
      language: "zh-CN"
    }];
    backendProjects.set(projectId, project);
    const pending = deferred<Awaited<ReturnType<typeof apiMocks.createScriptRevision>>>();
    apiMocks.createScriptRevision.mockImplementation(() => pending.promise);
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    const sourceInput = view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!;
    await changeReactValueExact(sourceInput, "要分析的台词");
    const analyze = view.container.querySelector<HTMLButtonElement>('[data-action="analyze-script"]')!;
    await act(async () => {
      analyze.click();
      analyze.click();
    });
    await flushAsync();

    expect(apiMocks.createScriptRevision).toHaveBeenCalledOnce();
  });

  it("invalidates a deferred revision when the source changes and keeps the newer text", async () => {
    const projectId = "project-source-generation";
    const project = scriptProject("Source generation");
    project.lines = [{
      id: "old-line",
      character_id: "narrator",
      text: "旧文本",
      note: "",
      language: "zh-CN"
    }];
    backendProjects.set(projectId, project);
    const pending = deferred<{
      project: ScriptProject;
      script_revision: ScriptRevision;
    }>();
    apiMocks.createScriptRevision.mockImplementation(() => pending.promise);
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    const sourceInput = view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!;
    await changeReactValueExact(sourceInput, "等待创建的旧文本");
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync();
    expect(apiMocks.createScriptRevision).toHaveBeenCalledOnce();

    await changeReactValueExact(sourceInput, "用户刚输入的新文本");
    const staleRevision = scriptRevision("stale-revision", "等待创建的旧文本");
    pending.resolve({
      project: {
        ...cloneProject(project),
        active_script_revision_id: staleRevision.revision_id,
        script_revisions: [staleRevision]
      },
      script_revision: staleRevision
    });
    await flushAsync();

    expect(view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")?.value)
      .toBe("用户刚输入的新文本");
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
  });

  it("releases Analyze busy state when a deferred start becomes stale after project selection", async () => {
    const projectAId = "project-stale-a";
    const projectBId = "project-stale-b";
    const projectA = scriptProject("Project A");
    projectA.lines = [{ id: "a-line", character_id: "a", text: "A 台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Project B");
    projectB.lines = [{ id: "b-line", character_id: "b", text: "B 台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    const pending = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pending.promise);
    const view = await renderApp(projectAId);
    await flushAsync();

    const rowFor = (title: string) => [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes(title))!;
    await click(rowFor("Project A"));
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "A 等待中的分析"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync();

    await click(view.container.querySelector('[data-drawer-tab="list"]')!);
    await click(rowFor("Project B"));
    await flushAsync();
    const staleRevision = scriptRevision("stale-a", "A 等待中的分析");
    pending.resolve({
      project: { ...cloneProject(projectA), active_script_revision_id: staleRevision.revision_id, script_revisions: [staleRevision] },
      script_revision: staleRevision
    });
    await flushAsync();

    expect(view.container.querySelector<HTMLButtonElement>('[data-action="analyze-script"]')?.disabled).toBe(false);
  });

  it("invalidates a deferred revision when a script file replaces the source", async () => {
    const projectId = "project-file-generation";
    const project = scriptProject("File generation");
    project.lines = [{ id: "old-line", character_id: "narrator", text: "旧文件文本", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const pending = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pending.promise);
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "等待创建的粘贴文本"
    );
    const fileInput = view.container.querySelector<HTMLInputElement>('[data-action="script-file-input"]')!;
    const file = new view.dom.window.File(["文件中的新文本"], "replacement.md", { type: "text/markdown" });
    const fileRead = deferred<string>();
    Object.defineProperty(file, "text", { value: () => fileRead.promise });
    Object.defineProperty(fileInput, "files", { configurable: true, value: [file] });
    await act(async () => {
      fileInput.dispatchEvent(new view.dom.window.Event("change", { bubbles: true }));
    });
    expect(view.container.querySelector(".confirm-modal")?.textContent).toContain("覆盖当前草稿");
    await click(view.container.querySelector(".confirm-modal-actions .primary-button")!);
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync();
    fileRead.resolve("文件中的新文本");
    await flushAsync();
    const staleRevision = scriptRevision("stale-file-revision", "等待创建的粘贴文本");
    pending.resolve({
      project: { ...cloneProject(project), active_script_revision_id: staleRevision.revision_id, script_revisions: [staleRevision] },
      script_revision: staleRevision
    });
    await flushAsync();

    expect(view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")?.value)
      .toBe("文件中的新文本");
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
  });

  it("restores a matching analysis session on reload without creating a new run", async () => {
    const projectId = "project-resume";
    const revision = scriptRevision("resume-revision", "甲：恢复失败分析", "sha-resume");
    backendProjects.set(projectId, scriptProject("Resume project", revision));
    const failedRun: AnalysisRun = {
      id: "run-resume",
      project_id: projectId,
      source_revision_id: revision.revision_id,
      draft_id: "draft-resume",
      status: "failed",
      quality: null,
      progress: 1,
      warnings: [],
      error: {
        code: "semantic_contract_invalid",
        http_status: 422,
        stage: "decode",
        message: "Invalid semantic response",
        retryable: false,
        run_id: "run-resume",
        trace_id: "trace-resume-422",
        occurred_at: timestamp,
        details: {}
      },
      trace_id: "trace-resume-422",
      created_at: timestamp,
      updated_at: timestamp
    };
    const draft: SemanticAnalysisDraft = {
      id: "draft-resume",
      project_id: projectId,
      source_revision_id: revision.revision_id,
      version: 1,
      annotations: [],
      characters: [],
      utterances: [],
      unresolved_candidates: [],
      warnings: [],
      provider: "fake",
      model: "fake",
      prompt_version: "p1",
      contract_version: "v1",
      confirmed_revision_id: null,
      confirmed_parse_revision_id: null,
      confirmed_parse_fingerprint: null,
      confirm_idempotency_key: null,
      created_at: timestamp,
      updated_at: timestamp
    };
    apiMocks.fetchAnalysisRun.mockImplementation(async () => failedRun);
    apiMocks.fetchAnalysisDraft.mockImplementation(async () => draft);
    const scope = JSON.stringify([projectId, revision.revision_id, revision.source_sha256]);
    const view = await renderApp(projectId, (storage) => {
      storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({
        [scope]: { runId: failedRun.id, draftId: failedRun.draft_id }
      }));
    });
    await flushAsync(40);

    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    const errorDialog = view.container.querySelector<HTMLElement>('[data-analysis-error="run"]')!;
    expect(errorDialog.textContent).not.toContain("trace-resume-422");
    await click(errorDialog.querySelector('[data-error-action="details"]')!);
    expect(errorDialog.textContent).toContain("trace-resume-422");
    expect(apiMocks.createAnalysisRun).not.toHaveBeenCalled();
    expect(apiMocks.fetchAnalysisRun).toHaveBeenCalledWith("run-resume");
  });

  it("keeps TTS mounted on reload when the active revision has no stored analysis session", async () => {
    const projectId = "project-no-session";
    const revision = scriptRevision("no-session-revision", "甲：无需恢复", "sha-no-session");
    const project = scriptProject("No session", revision);
    project.lines = [{ id: "tts-line", character_id: "narrator", text: "继续留在 TTS", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const view = await renderApp(projectId);
    await flushAsync(40);

    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(view.container.textContent).toContain("继续留在 TTS");
    expect(apiMocks.createAnalysisRun).not.toHaveBeenCalled();
    expect(apiMocks.fetchAnalysisRun).not.toHaveBeenCalled();
  });

  it("does not write hydrated workspace data and saves one later user edit", async () => {
    const projectId = "project-hydration-baseline";
    const project = scriptProject("Hydration baseline");
    project.lines = [{
      id: "baseline-line",
      character_id: "narrator",
      text: "保持只读加载",
      note: "",
      language: "zh-CN"
    }];
    backendProjects.set(projectId, project);
    const view = await renderApp(projectId);
    await flushAsync(40);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 750));
    });

    expect(apiMocks.saveProject).not.toHaveBeenCalled();
    expect(apiMocks.saveCharacters).not.toHaveBeenCalled();

    const methodTabs = view.container.querySelectorAll(".generation-method-tab");
    expect(methodTabs.length).toBeGreaterThan(1);
    await click(methodTabs[1]);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 750));
    });

    expect(apiMocks.saveProject).toHaveBeenCalledOnce();
    expect(apiMocks.saveCharacters).toHaveBeenCalledOnce();
  });

  it("clears a deleted active scope on 404 and restores the current project session", async () => {
    const currentProjectId = "scope-fallback-current";
    const deletedProjectId = "scope-deleted-target";
    const currentRevision = scriptRevision("scope-fallback-revision", "A：恢复当前分析", "sha-fallback-current");
    backendProjects.set(currentProjectId, scriptProject("Scope fallback current", currentRevision));
    const currentRun = completedRun(currentProjectId, currentRevision.revision_id);
    const currentDraft = analysisDraft(currentProjectId, currentRevision.revision_id);
    apiMocks.fetchAnalysisRun.mockImplementation(async () => currentRun);
    apiMocks.fetchAnalysisDraft.mockImplementation(async () => currentDraft);
    const deletedFetch = deferred<ScriptProject>();
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === deletedProjectId) return deletedFetch.promise;
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const currentSessionScope = JSON.stringify([
      currentProjectId,
      currentRevision.revision_id,
      currentRevision.source_sha256
    ]);
    const view = await renderApp(currentProjectId, (storage) => {
      storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({
        [currentSessionScope]: { runId: currentRun.id, draftId: currentRun.draft_id }
      }));
      storage.setItem(activeAnalysisScopeStorageKey, JSON.stringify({
        projectId: deletedProjectId,
        revisionId: "deleted-revision",
        sourceSha256: "sha-deleted"
      }));
    });
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();

    deletedFetch.reject(new ApiRequestError(404, '{"detail":"Project not found"}', "Project not found"));
    await flushAsync(60);

    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.container.textContent).toContain("A：恢复当前分析");
    expect(view.dom.window.localStorage.getItem(activeAnalysisScopeStorageKey)).toBeNull();
    expect(apiMocks.createAnalysisRun).not.toHaveBeenCalled();
  });

  it("does not restore a stale fallback project after the current project switches", async () => {
    const projectAId = "scope-stale-fallback-a";
    const deletedProjectId = "scope-stale-deleted-b";
    const projectCId = "scope-stale-current-c";
    const revisionA = scriptRevision("scope-stale-revision-a", "A：不应迟到挂载", "sha-scope-stale-a");
    const projectA = scriptProject("Scope stale A", revisionA);
    const projectC = scriptProject("Scope current C");
    projectC.lines = [{ id: "scope-c-line", character_id: "c", text: "C 当前 TTS 台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectCId, projectC);
    const runA = completedRun(projectAId, revisionA.revision_id);
    const draftA = analysisDraft(projectAId, revisionA.revision_id);
    apiMocks.fetchAnalysisRun.mockImplementation(async () => runA);
    apiMocks.fetchAnalysisDraft.mockImplementation(async () => draftA);
    const deletedFetch = deferred<ScriptProject>();
    const staleFallbackFetch = deferred<ScriptProject>();
    let projectAFetchCount = 0;
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === deletedProjectId) return deletedFetch.promise;
      if (projectId === projectAId) {
        projectAFetchCount += 1;
        if (projectAFetchCount > 2) return staleFallbackFetch.promise;
      }
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const sessionScope = JSON.stringify([projectAId, revisionA.revision_id, revisionA.source_sha256]);
    const view = await renderApp(projectAId, (storage) => {
      storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({
        [sessionScope]: { runId: runA.id, draftId: runA.draft_id }
      }));
      storage.setItem(activeAnalysisScopeStorageKey, JSON.stringify({
        projectId: deletedProjectId,
        revisionId: "deleted-stale-revision",
        sourceSha256: "sha-deleted-stale"
      }));
    });
    await flushAsync(40);

    deletedFetch.reject(new ApiRequestError(404, '{"detail":"Project not found"}', "Project not found"));
    await flushAsync(40);
    expect(projectAFetchCount).toBe(3);

    const rowC = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Scope current C"))!;
    await click(rowC);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(40);
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectCId);
    expect(view.container.textContent).toContain("C 当前 TTS 台词");

    staleFallbackFetch.resolve(cloneProject(projectA));
    await flushAsync(60);

    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectCId);
    expect(view.container.textContent).toContain("C 当前 TTS 台词");
  });

  it("clears an invalid active revision scope and restores the current project session", async () => {
    const currentProjectId = "scope-invalid-fallback-current";
    const invalidProjectId = "scope-invalid-target";
    const currentRevision = scriptRevision(
      "scope-invalid-fallback-revision",
      "A：恢复无效 scope 后的分析",
      "sha-invalid-fallback-current"
    );
    backendProjects.set(currentProjectId, scriptProject("Invalid scope fallback current", currentRevision));
    const currentRun = completedRun(currentProjectId, currentRevision.revision_id);
    const currentDraft = analysisDraft(currentProjectId, currentRevision.revision_id);
    apiMocks.fetchAnalysisRun.mockImplementation(async () => currentRun);
    apiMocks.fetchAnalysisDraft.mockImplementation(async () => currentDraft);
    const invalidFetch = deferred<ScriptProject>();
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === invalidProjectId) return invalidFetch.promise;
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const currentSessionScope = JSON.stringify([
      currentProjectId,
      currentRevision.revision_id,
      currentRevision.source_sha256
    ]);
    const view = await renderApp(currentProjectId, (storage) => {
      storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({
        [currentSessionScope]: { runId: currentRun.id, draftId: currentRun.draft_id }
      }));
      storage.setItem(activeAnalysisScopeStorageKey, JSON.stringify({
        projectId: invalidProjectId,
        revisionId: "missing-revision",
        sourceSha256: "sha-missing"
      }));
    });
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();

    invalidFetch.resolve(scriptProject(
      "Invalid scope target",
      scriptRevision("different-revision", "B：已被替换", "sha-different")
    ));
    await flushAsync(60);

    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.container.textContent).toContain("A：恢复无效 scope 后的分析");
    expect(view.dom.window.localStorage.getItem(activeAnalysisScopeStorageKey)).toBeNull();
    expect(apiMocks.createAnalysisRun).not.toHaveBeenCalled();
  });

  it("retains an active analysis scope across a transient target failure", async () => {
    const currentProjectId = "scope-transient-current";
    const targetProjectId = "scope-transient-target";
    const currentRevision = scriptRevision(
      "scope-transient-current-revision",
      "A：当前可恢复分析",
      "sha-transient-current"
    );
    backendProjects.set(currentProjectId, scriptProject("Transient scope current", currentRevision));
    const currentRun = completedRun(currentProjectId, currentRevision.revision_id);
    const currentDraft = analysisDraft(currentProjectId, currentRevision.revision_id);
    apiMocks.fetchAnalysisRun.mockImplementation(async () => currentRun);
    apiMocks.fetchAnalysisDraft.mockImplementation(async () => currentDraft);
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === targetProjectId) {
        throw new ApiRequestError(503, '{"detail":"temporarily unavailable"}', "temporarily unavailable");
      }
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const currentSessionScope = JSON.stringify([
      currentProjectId,
      currentRevision.revision_id,
      currentRevision.source_sha256
    ]);
    const expectedScope = {
      projectId: targetProjectId,
      revisionId: "scope-transient-revision",
      sourceSha256: "sha-transient-target"
    };
    const view = await renderApp(currentProjectId, (storage) => {
      storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({
        [currentSessionScope]: { runId: currentRun.id, draftId: currentRun.draft_id }
      }));
      storage.setItem(activeAnalysisScopeStorageKey, JSON.stringify(expectedScope));
    });
    await flushAsync(60);

    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(JSON.parse(view.dom.window.localStorage.getItem(activeAnalysisScopeStorageKey)!)).toEqual(expectedScope);
    expect(apiMocks.createAnalysisRun).not.toHaveBeenCalled();
    expect(apiMocks.fetchAnalysisRun).not.toHaveBeenCalled();
  });

  it("ignores a late confirmation from a cancelled workspace after a new analysis starts", async () => {
    const projectAId = "late-a";
    const projectBId = "late-b";
    const projectA = scriptProject("Late A");
    projectA.lines = [{ id: "a-line", character_id: "a", text: "A 旧台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Late B");
    projectB.lines = [{ id: "b-line", character_id: "b", text: "B 旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (projectId: string, revisionId: string) => {
      const run = completedRun(projectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(projectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    const pendingAConfirmation = deferred<SemanticConfirmResponse>();
    apiMocks.confirmAnalysisDraft.mockImplementation(() => pendingAConfirmation.promise);
    const view = await renderApp(projectAId);
    await flushAsync();

    const rowFor = (title: string) => [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes(title))!;
    await click(rowFor("Late A"));
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "A 分析源"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-submit"]')!);
    await flushAsync();
    expect(apiMocks.confirmAnalysisDraft).toHaveBeenCalledOnce();

    await click(view.container.querySelector('[data-action="cancel-workspace"]')!);
    await click(view.container.querySelector('[data-drawer-tab="list"]')!);
    await click(rowFor("Late B"));
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "B 分析源"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();

    const revisionA = backendProjects.get(projectAId)!.script_revisions![0];
    const projectRefreshesBeforeLateConfirm = apiMocks.fetchProjects.mock.calls.length;
    pendingAConfirmation.resolve(semanticConfirmation(projectAId, revisionA, "A 确认台词"));
    await flushAsync(40);

    expect(apiMocks.fetchProjects.mock.calls.length).toBe(projectRefreshesBeforeLateConfirm);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.container.textContent).toContain("B 分析源");
    expect(view.container.textContent).not.toContain("A 确认台词");
  });

  it("ignores a late confirmation when a new project analysis replaces the workspace scope", async () => {
    const projectAId = "scope-a";
    const projectBId = "scope-b";
    const projectA = scriptProject("Scope A");
    projectA.lines = [{ id: "scope-a-line", character_id: "a", text: "A 旧台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Scope B");
    projectB.lines = [{ id: "scope-b-line", character_id: "b", text: "B 旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (projectId: string, revisionId: string) => {
      const run = completedRun(projectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(projectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    const pendingAConfirmation = deferred<SemanticConfirmResponse>();
    apiMocks.confirmAnalysisDraft.mockImplementation(() => pendingAConfirmation.promise);
    const view = await renderApp(projectAId);
    await flushAsync();

    const rowFor = (title: string) => [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes(title))!;
    await click(rowFor("Scope A"));
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "A scope 分析源"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-submit"]')!);
    await flushAsync();
    expect(apiMocks.confirmAnalysisDraft).toHaveBeenCalledOnce();

    await click(view.container.querySelector('[data-drawer-tab="list"]')!);
    await click(rowFor("Scope B"));
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "B scope 分析源"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.container.textContent).toContain("B scope 分析源");

    const revisionA = backendProjects.get(projectAId)!.script_revisions![0];
    const projectRefreshesBeforeLateConfirm = apiMocks.fetchProjects.mock.calls.length;
    pendingAConfirmation.resolve(semanticConfirmation(projectAId, revisionA, "A scope 确认台词"));
    await flushAsync(40);

    expect(apiMocks.fetchProjects.mock.calls.length).toBe(projectRefreshesBeforeLateConfirm);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.container.textContent).toContain("B scope 分析源");
    expect(view.container.textContent).not.toContain("A scope 确认台词");
  });

  it("keeps the server-confirmed project when the post-confirm summary refresh fails", async () => {
    const projectId = "confirm-refresh-failure";
    const project = scriptProject("Confirm refresh failure");
    project.lines = [{ id: "old-line", character_id: "old", text: "确认前旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (targetProjectId: string, revisionId: string) => {
      const run = completedRun(targetProjectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(targetProjectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    let failNextProjectRefresh = false;
    apiMocks.fetchProjects.mockImplementation(async () => {
      if (failNextProjectRefresh) {
        failNextProjectRefresh = false;
        throw new Error("summaries unavailable");
      }
      return {
        projects: [...backendProjects].map(([id, item]) => projectSummary(id, item))
      };
    });
    apiMocks.confirmAnalysisDraft.mockImplementation(async () => {
      failNextProjectRefresh = true;
      const revision = backendProjects.get(projectId)!.script_revisions![0];
      return semanticConfirmation(projectId, revision, "服务器确认后的精确台词");
    });
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：等待语义确认"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-submit"]')!);
    await flushAsync(40);

    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(view.container.textContent).toContain("服务器确认后的精确台词");
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectId);
  });

  it("does not restore a confirmed analysis session after App remount", async () => {
    const projectId = "confirmed-session-remount";
    const project = scriptProject("Confirmed session remount");
    project.lines = [{ id: "old-confirm-line", character_id: "old", text: "确认前台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (targetProjectId: string, revisionId: string) => {
      const run = completedRun(targetProjectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(targetProjectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    apiMocks.confirmAnalysisDraft.mockImplementation(async () => {
      const revision = backendProjects.get(projectId)!.script_revisions![0];
      const response = semanticConfirmation(projectId, revision, "确认后不应重开分析");
      backendProjects.set(projectId, cloneProject(response.project));
      return response;
    });
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：确认后关闭 session"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-submit"]')!);
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();

    const persistedSessions = view.dom.window.localStorage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY);
    const persistedReviewSessions = view.dom.window.localStorage.getItem(ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY);
    activeViews.splice(activeViews.indexOf(view), 1);
    await view.cleanup();
    const remounted = await renderApp(projectId, (storage) => {
      if (persistedSessions) storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, persistedSessions);
      if (persistedReviewSessions) storage.setItem(ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY, persistedReviewSessions);
    });
    await flushAsync(40);

    expect(remounted.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(remounted.container.textContent).toContain("确认后不应重开分析");
    const confirmedRun = [...runs.values()][0];
    apiMocks.fetchAnalysisReviewSession.mockResolvedValue({
      run_id: confirmedRun.id,
      draft_id: confirmedRun.draft_id,
      source_revision_id: confirmedRun.source_revision_id
    });
    await click(remounted.container.querySelector('[data-action="review-confirmed-annotations"]')!);
    await flushAsync(40);
    expect(remounted.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(apiMocks.fetchAnalysisReviewSession).toHaveBeenCalledWith(projectId);
    expect(apiMocks.createAnalysisRun).toHaveBeenCalledOnce();
  });

  it("keeps a return-to-analysis button visible and restores a confirmed server session without rerunning analysis", async () => {
    const projectId = "server-review-return";
    const analyzedRevision = scriptRevision("script-review-return", "甲：保留分析结果");
    const currentRevision = scriptRevision("script-current-edit", "乙：当前配音编辑版本");
    const project = scriptProject("Server review return", currentRevision);
    project.script_revisions = [analyzedRevision, currentRevision];
    project.lines = [{ id: "confirmed-line", character_id: "speaker", text: "保留分析结果", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const run = completedRun(projectId, analyzedRevision.revision_id);
    const draft = {
      ...analysisDraft(projectId, analyzedRevision.revision_id),
      confirmed_revision_id: "semantic-review-return"
    };
    apiMocks.fetchAnalysisReviewSession.mockResolvedValue({
      run_id: run.id,
      draft_id: draft.id,
      source_revision_id: analyzedRevision.revision_id
    });
    apiMocks.fetchAnalysisRun.mockResolvedValue(run);
    apiMocks.fetchAnalysisDraft.mockResolvedValue(draft);
    const view = await renderApp(projectId);
    await flushAsync(40);

    const returnButton = view.container.querySelector<HTMLButtonElement>('[data-action="review-confirmed-annotations"]');
    expect(returnButton).not.toBeNull();
    expect(returnButton?.textContent).toContain("返回分析结果");
    expect(returnButton?.disabled).toBe(false);

    await click(returnButton!);
    await flushAsync(40);

    expect(apiMocks.fetchAnalysisReviewSession).toHaveBeenCalledWith(projectId);
    expect(apiMocks.createAnalysisRun).not.toHaveBeenCalled();
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.container.textContent).toContain("甲：保留分析结果");
  });

  it("never starts a new analysis when a returned review session disappears", async () => {
    const projectId = "stale-server-review-return";
    const analyzedRevision = scriptRevision("script-stale-review", "甲：已确认但随后被清理");
    backendProjects.set(projectId, scriptProject("Stale server review", analyzedRevision));
    apiMocks.fetchAnalysisReviewSession.mockResolvedValue({
      run_id: "run-stale-review",
      draft_id: "draft-stale-review",
      source_revision_id: analyzedRevision.revision_id
    });
    apiMocks.fetchAnalysisRun.mockRejectedValue(
      new Error('{"detail":{"code":"run_not_found","message":"not found"}}')
    );
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector('[data-action="review-confirmed-annotations"]')!);
    await flushAsync(40);

    expect(apiMocks.fetchAnalysisReviewSession).toHaveBeenCalledWith(projectId);
    expect(apiMocks.createAnalysisRun).not.toHaveBeenCalled();
    expect(view.container.querySelector('[data-analysis-error="controller"]')).not.toBeNull();
  });

  it("restores managed project B analysis while current TTS project remains A", async () => {
    const projectAId = "restore-current-a";
    const projectBId = "restore-analysis-b";
    const projectA = scriptProject("Restore current A");
    projectA.lines = [{ id: "restore-a-line", character_id: "a", text: "A 的 TTS 台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Restore analysis B");
    projectB.lines = [{ id: "restore-b-line", character_id: "b", text: "B 的旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (projectId: string, revisionId: string) => {
      const run = completedRun(projectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(projectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    const view = await renderApp(projectAId);
    await flushAsync();

    const rowB = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Restore analysis B"))!;
    await click(rowB);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "B：跨项目恢复的分析原文"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectAId);

    const persistedSessions = view.dom.window.localStorage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY);
    const persistedScope = view.dom.window.localStorage.getItem(activeAnalysisScopeStorageKey);
    activeViews.splice(activeViews.indexOf(view), 1);
    await view.cleanup();
    const remounted = await renderApp(projectAId, (storage) => {
      if (persistedSessions) storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, persistedSessions);
      if (persistedScope) storage.setItem(activeAnalysisScopeStorageKey, persistedScope);
    });
    await flushAsync(40);

    expect(remounted.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    expect(remounted.container.textContent).toContain("B：跨项目恢复的分析原文");
    expect(remounted.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectAId);
    expect(apiMocks.createAnalysisRun).toHaveBeenCalledOnce();
  });

  it("keeps authoritative project B when its redundant post-confirm reload fails", async () => {
    const projectAId = "confirm-current-a";
    const projectBId = "confirm-analysis-b";
    const projectA = scriptProject("Confirm current A");
    projectA.lines = [{ id: "confirm-a-line", character_id: "a", text: "A 当前台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Confirm analysis B");
    projectB.lines = [{ id: "confirm-b-old-line", character_id: "b", text: "B 确认前台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    let rejectConfirmedProjectReload = false;
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === projectBId && rejectConfirmedProjectReload) {
        throw new Error("redundant confirmed project reload failed");
      }
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (projectId: string, revisionId: string) => {
      const run = completedRun(projectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(projectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    apiMocks.confirmAnalysisDraft.mockImplementation(async () => {
      const revision = backendProjects.get(projectBId)!.script_revisions![0];
      const response = semanticConfirmation(projectBId, revision, "B 服务端确认权威台词");
      backendProjects.set(projectBId, cloneProject(response.project));
      rejectConfirmedProjectReload = true;
      return response;
    });
    const view = await renderApp(projectAId);
    await flushAsync();

    const rowB = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Confirm analysis B"))!;
    await click(rowB);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "B：等待跨项目确认"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-submit"]')!);
    await flushAsync(60);

    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(view.container.textContent).toContain("B 服务端确认权威台词");
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectBId);
  });

  it("drains an old in-flight autosave before creating the analysis revision", async () => {
    vi.useFakeTimers();
    const projectId = "late-autosave";
    const initialRevision = scriptRevision("initial-revision", "旧原文", "sha-initial");
    const project = scriptProject("Late autosave", initialRevision);
    project.lines = [{ id: "old-line", character_id: "narrator", text: "旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const oldSave = deferred<void>();
    let deferNextSave = false;
    apiMocks.saveProject.mockImplementation(async (targetProjectId: string, payload: ScriptProject) => {
      if (deferNextSave) {
        deferNextSave = false;
        await oldSave.promise;
      }
      backendProjects.set(targetProjectId, cloneProject(payload));
    });
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (targetProjectId: string, revisionId: string) => {
      const run = completedRun(targetProjectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(targetProjectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    apiMocks.confirmAnalysisDraft.mockImplementation(async () => {
      const revision = backendProjects.get(projectId)!.script_revisions!.at(-1)!;
      const response = semanticConfirmation(projectId, revision, "语义确认权威台词");
      backendProjects.set(projectId, cloneProject(response.project));
      return response;
    });
    const view = await renderApp(projectId);
    await flushAsync(40);

    deferNextSave = true;
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(701);
    });
    await flushAsync();
    expect(apiMocks.saveProject).toHaveBeenCalledOnce();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：新分析原文"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(apiMocks.createScriptRevision).not.toHaveBeenCalled();
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();

    oldSave.resolve();
    await flushAsync(40);
    expect(apiMocks.createScriptRevision).toHaveBeenCalledOnce();
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-submit"]')!);
    await flushAsync(40);
    expect(view.container.textContent).toContain("语义确认权威台词");

    expect(backendProjects.get(projectId)?.active_parse_revision_id).toBe(`parse-${projectId}`);
    expect(backendProjects.get(projectId)?.lines[0]?.text).toBe("语义确认权威台词");
    expect(apiMocks.saveProject).toHaveBeenCalledOnce();
  });

  it("flushes a pending TTS autosave before analysis can replace the project snapshot", async () => {
    vi.useFakeTimers();
    const projectId = "pending-autosave";
    const project = scriptProject("Pending autosave");
    project.lines = [{ id: "pending-line", character_id: "narrator", text: "旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    expect(view.container.querySelector(".reference-setup-callout")).toBeNull();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：分析不能丢掉待保存的 TTS 编辑"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();

    await click(view.container.querySelector('[data-action="cancel-workspace"]')!);
    await flushAsync();

    expect(view.container.querySelector(".reference-setup-callout")).toBeNull();
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
  });

  it("preserves a same-project TTS change made while revision creation is deferred", async () => {
    vi.useFakeTimers();
    const projectId = "deferred-create-pending-change";
    const project = scriptProject("Deferred create pending change");
    project.lines = [{ id: "deferred-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const pendingRevision = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingRevision.promise);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：创建 revision 期间仍可能修改 TTS"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(apiMocks.createScriptRevision).toHaveBeenCalledOnce();
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();

    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    expect(view.container.querySelector(".reference-setup-callout")).toBeNull();

    const revision = scriptRevision(
      "deferred-create-revision",
      "甲：创建 revision 期间仍可能修改 TTS",
      "sha-deferred-create"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    pendingRevision.resolve({ project: serverProject, script_revision: revision });
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();

    await click(view.container.querySelector('[data-action="cancel-workspace"]')!);
    await flushAsync();
    activeViews.splice(activeViews.indexOf(view), 1);
    await view.cleanup();
    const remounted = await renderApp(projectId);
    await flushAsync(40);

    expect(remounted.container.querySelector(".reference-setup-callout")).toBeNull();
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
  });

  it("preserves a server-created revision when source invalidates the operation before onReady", async () => {
    vi.useFakeTimers();
    const projectId = "stale-after-create";
    const project = scriptProject("Stale after create");
    project.lines = [{ id: "stale-after-create-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const pendingRevision = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingRevision.promise);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    const sourceEditor = view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!;
    await changeReactValueExact(sourceEditor, "甲：即将失效的分析原文");
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(apiMocks.createScriptRevision).toHaveBeenCalledOnce();

    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    await changeReactValueExact(sourceEditor, "甲：使旧操作失效的新原文");

    const revision = scriptRevision(
      "stale-server-revision",
      "甲：即将失效的分析原文",
      "sha-stale-server-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    pendingRevision.resolve({ project: serverProject, script_revision: revision });
    await flushAsync(60);

    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");

    activeViews.splice(activeViews.indexOf(view), 1);
    await view.cleanup();
    const remounted = await renderApp(projectId);
    await flushAsync(40);

    expect(remounted.container.querySelector(".reference-setup-callout")).toBeNull();
    expect(remounted.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")?.value)
      .toBe(revision.source_markdown);
    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
  });

  it("keeps a reconciled revision in live state before a later same-mount autosave", async () => {
    vi.useFakeTimers();
    const projectId = "stale-live-authority";
    const project = scriptProject("Stale live authority");
    project.lines = [{ id: "stale-live-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const pendingRevision = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingRevision.promise);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    const sourceEditor = view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!;
    await changeReactValueExact(sourceEditor, "甲：提交后会失效");
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    await changeReactValueExact(sourceEditor, "甲：使旧操作失效的新原文");

    const revision = scriptRevision(
      "stale-live-server-revision",
      "甲：提交后会失效",
      "sha-stale-live-server-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    pendingRevision.resolve({ project: serverProject, script_revision: revision });
    await flushAsync(60);
    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);

    await click(view.container.querySelector(".route-clear-temporary")!);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(701);
    });
    await flushAsync(60);

    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding ?? null).toBeNull();
  });

  it("refetches authority after an ambiguous create rejection before saving pending edits", async () => {
    vi.useFakeTimers();
    const projectId = "ambiguous-create-refetch";
    const project = scriptProject("Ambiguous create refetch");
    project.lines = [{ id: "ambiguous-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const pendingCreate = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingCreate.promise);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：响应丢失但服务端已提交"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();

    const revision = scriptRevision(
      "ambiguous-server-revision",
      "甲：响应丢失但服务端已提交",
      "sha-ambiguous-server-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    pendingCreate.reject(new Error("connection lost after commit"));
    await flushAsync(80);

    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
  });

  it("blocks later autosaves while ambiguous create authority cannot be refetched", async () => {
    vi.useFakeTimers();
    const projectId = "ambiguous-create-unavailable";
    const project = scriptProject("Ambiguous create unavailable");
    project.lines = [{ id: "ambiguous-unavailable-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    let rejectAuthorityFetch = false;
    apiMocks.fetchProject.mockImplementation(async (targetProjectId: string) => {
      if (targetProjectId === projectId && rejectAuthorityFetch) {
        throw new Error("authority temporarily unavailable");
      }
      const stored = backendProjects.get(targetProjectId);
      if (!stored) throw new Error(`missing project: ${targetProjectId}`);
      return cloneProject(stored);
    });
    const pendingCreate = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingCreate.promise);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：权威暂时不可读取"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();

    const revision = scriptRevision(
      "ambiguous-unavailable-revision",
      "甲：权威暂时不可读取",
      "sha-ambiguous-unavailable-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    rejectAuthorityFetch = true;
    pendingCreate.reject(new Error("connection lost after commit"));
    await flushAsync(80);

    expect(apiMocks.saveProject).not.toHaveBeenCalled();
    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);

    await click(view.container.querySelector(".route-clear-temporary")!);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(701);
    });
    await flushAsync(60);

    expect(apiMocks.saveProject).not.toHaveBeenCalled();
    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
  });

  it("refetches unknown authority before a later autosave once the server recovers", async () => {
    vi.useFakeTimers();
    const projectId = "ambiguous-create-recover-later";
    const project = scriptProject("Ambiguous create recover later");
    project.lines = [{ id: "ambiguous-recover-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    let rejectAuthorityFetch = false;
    apiMocks.fetchProject.mockImplementation(async (targetProjectId: string) => {
      if (targetProjectId === projectId && rejectAuthorityFetch) {
        throw new Error("authority temporarily unavailable");
      }
      const stored = backendProjects.get(targetProjectId);
      if (!stored) throw new Error(`missing project: ${targetProjectId}`);
      return cloneProject(stored);
    });
    const pendingCreate = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingCreate.promise);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：服务恢复后安全保存"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();

    const revision = scriptRevision(
      "ambiguous-recover-revision",
      "甲：服务恢复后安全保存",
      "sha-ambiguous-recover-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    rejectAuthorityFetch = true;
    pendingCreate.reject(new Error("connection lost after commit"));
    await flushAsync(80);
    expect(apiMocks.saveProject).not.toHaveBeenCalled();

    rejectAuthorityFetch = false;
    await click(view.container.querySelector(".route-clear-temporary")!);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(701);
    });
    await flushAsync(80);

    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding ?? null).toBeNull();
  });

  it("keeps unknown authority guarded while another edit arrives during recovery", async () => {
    vi.useFakeTimers();
    const projectId = "ambiguous-create-recovery-race";
    const project = scriptProject("Ambiguous create recovery race");
    project.lines = [{ id: "ambiguous-race-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    let ambiguousRecoveryStarted = false;
    let recoveryFetchCount = 0;
    const delayedRecoveryFetch = deferred<ScriptProject>();
    apiMocks.fetchProject.mockImplementation(async (targetProjectId: string) => {
      if (targetProjectId === projectId && ambiguousRecoveryStarted) {
        recoveryFetchCount += 1;
        if (recoveryFetchCount === 1) throw new Error("authority temporarily unavailable");
        if (recoveryFetchCount === 2) return delayedRecoveryFetch.promise;
      }
      const stored = backendProjects.get(targetProjectId);
      if (!stored) throw new Error(`missing project: ${targetProjectId}`);
      return cloneProject(stored);
    });
    const pendingCreate = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingCreate.promise);
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：恢复期间继续编辑"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();

    const revision = scriptRevision(
      "ambiguous-race-revision",
      "甲：恢复期间继续编辑",
      "sha-ambiguous-race-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    ambiguousRecoveryStarted = true;
    pendingCreate.reject(new Error("connection lost after commit"));
    await flushAsync(80);

    await click(view.container.querySelector(".route-clear-temporary")!);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(701);
    });
    await flushAsync(40);
    expect(recoveryFetchCount).toBe(2);
    expect(apiMocks.saveProject).not.toHaveBeenCalled();

    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    delayedRecoveryFetch.resolve(cloneProject(serverProject));
    await flushAsync(80);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(701);
    });
    await flushAsync(80);

    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
  });

  it("preserves the latest pending TTS edit while an authoritative rebase save is deferred", async () => {
    vi.useFakeTimers();
    const projectId = "second-edit-during-rebase";
    const project = scriptProject("Second edit during rebase");
    project.lines = [{ id: "second-edit-line", character_id: "narrator", text: "待配置台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const pendingRevision = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingRevision.promise);
    const firstAuthoritativeSave = deferred<void>();
    let saveCallCount = 0;
    apiMocks.saveProject.mockImplementation(async (targetProjectId: string, payload: ScriptProject) => {
      saveCallCount += 1;
      if (saveCallCount === 1) await firstAuthoritativeSave.promise;
      backendProjects.set(targetProjectId, cloneProject(payload));
    });
    const view = await renderApp(projectId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：权威保存期间继续编辑"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);

    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    const revision = scriptRevision(
      "second-edit-server-revision",
      "甲：权威保存期间继续编辑",
      "sha-second-edit-server-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectId, cloneProject(serverProject));
    pendingRevision.resolve({ project: serverProject, script_revision: revision });
    await flushAsync(40);
    expect(apiMocks.saveProject).toHaveBeenCalledOnce();
    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();

    await click(view.container.querySelector(".route-clear-temporary")!);
    await flushAsync();
    firstAuthoritativeSave.resolve();
    await flushAsync(80);

    expect(backendProjects.get(projectId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectId)?.lines[0]?.temporary_binding ?? null).toBeNull();
  });

  it("flushes current project A changes made while managed project B revision creation is deferred", async () => {
    vi.useFakeTimers();
    const projectAId = "deferred-create-current-a";
    const projectBId = "deferred-create-target-b";
    const projectA = scriptProject("Deferred create current A");
    projectA.lines = [{ id: "deferred-a-line", character_id: "a", text: "A 待配置台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Deferred create target B");
    projectB.lines = [{ id: "deferred-b-line", character_id: "b", text: "B 旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    const pendingRevision = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingRevision.promise);
    const view = await renderApp(projectAId);
    await flushAsync(40);

    const rowB = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Deferred create target B"))!;
    await click(rowB);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "B：创建 revision 期间 A 仍可能修改"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(apiMocks.createScriptRevision).toHaveBeenCalledOnce();

    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    expect(view.container.querySelector(".reference-setup-callout")).toBeNull();

    const revision = scriptRevision(
      "deferred-create-target-revision",
      "B：创建 revision 期间 A 仍可能修改",
      "sha-deferred-target"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectBId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectBId, cloneProject(serverProject));
    pendingRevision.resolve({ project: serverProject, script_revision: revision });
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();

    await click(view.container.querySelector('[data-action="cancel-workspace"]')!);
    await flushAsync();
    activeViews.splice(activeViews.indexOf(view), 1);
    await view.cleanup();
    const remounted = await renderApp(projectAId);
    await flushAsync(40);

    expect(remounted.container.querySelector(".reference-setup-callout")).toBeNull();
    expect(backendProjects.get(projectAId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
    expect(backendProjects.get(projectBId)?.active_script_revision_id).toBe(revision.revision_id);
  });

  it("flushes current project A pending autosave before analyzing managed project B", async () => {
    vi.useFakeTimers();
    const projectAId = "pending-current-a";
    const projectBId = "analysis-target-b";
    const projectA = scriptProject("Pending current A");
    projectA.lines = [{ id: "pending-a-line", character_id: "a", text: "A 待保存台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Analysis target B");
    projectB.lines = [{ id: "target-b-line", character_id: "b", text: "B 旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    const view = await renderApp(projectAId);
    await flushAsync(40);

    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();
    expect(view.container.querySelector(".reference-setup-callout")).toBeNull();

    const rowB = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Analysis target B"))!;
    await click(rowB);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "B：分析前必须先保存 A"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    expect(view.container.querySelector(".script-analysis-workspace")).not.toBeNull();
    await click(view.container.querySelector('[data-action="cancel-workspace"]')!);
    await flushAsync();

    activeViews.splice(activeViews.indexOf(view), 1);
    await view.cleanup();
    const remounted = await renderApp(projectAId);
    await flushAsync(40);

    expect(remounted.container.querySelector(".reference-setup-callout")).toBeNull();
    expect(backendProjects.get(projectAId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
  });

  it("shows confirmed TTS immediately while the summary refresh remains pending", async () => {
    const projectId = "confirm-refresh-pending";
    const project = scriptProject("Confirm refresh pending");
    project.lines = [{ id: "old-line", character_id: "old", text: "旧台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectId, project);
    const runs = new Map<string, AnalysisRun>();
    const drafts = new Map<string, SemanticAnalysisDraft>();
    apiMocks.createAnalysisRun.mockImplementation(async (targetProjectId: string, revisionId: string) => {
      const run = completedRun(targetProjectId, revisionId);
      runs.set(run.id, run);
      drafts.set(run.draft_id, analysisDraft(targetProjectId, revisionId));
      return { run_id: run.id, draft_id: run.draft_id, status: run.status, trace_id: run.trace_id };
    });
    apiMocks.fetchAnalysisRun.mockImplementation(async (runId: string) => runs.get(runId)!);
    apiMocks.fetchAnalysisDraft.mockImplementation(async (draftId: string) => drafts.get(draftId)!);
    const pendingRefresh = deferred<{ projects: ProjectSummary[] }>();
    let deferNextProjectRefresh = false;
    apiMocks.fetchProjects.mockImplementation(async () => {
      if (deferNextProjectRefresh) {
        deferNextProjectRefresh = false;
        return pendingRefresh.promise;
      }
      return { projects: [...backendProjects].map(([id, item]) => projectSummary(id, item)) };
    });
    apiMocks.confirmAnalysisDraft.mockImplementation(async () => {
      deferNextProjectRefresh = true;
      const revision = backendProjects.get(projectId)!.script_revisions![0];
      return semanticConfirmation(projectId, revision, "无需等待摘要的确认台词");
    });
    const view = await renderApp(projectId);
    await flushAsync();

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：等待确认"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-submit"]')!);
    await flushAsync(40);

    expect(view.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(view.container.textContent).toContain("无需等待摘要的确认台词");

    pendingRefresh.resolve({
      projects: [...backendProjects].map(([id, item]) => projectSummary(id, item))
    });
    await flushAsync();
    expect(view.container.textContent).toContain("无需等待摘要的确认台词");
  });

  it("keeps an ambiguous revision and pending TTS edit when project switching is retried after recovery", async () => {
    vi.useFakeTimers();
    const projectAId = "switch-guard-current-a";
    const projectBId = "switch-guard-target-b";
    const projectCId = "switch-guard-latest-c";
    const projectA = scriptProject("Switch guard current A");
    projectA.lines = [{ id: "switch-guard-a-line", character_id: "a", text: "A 待保存台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Switch guard target B");
    projectB.lines = [{ id: "switch-guard-b-line", character_id: "b", text: "B 当前台词", note: "", language: "zh-CN" }];
    const projectC = scriptProject("Switch guard latest C");
    projectC.lines = [{ id: "switch-guard-c-line", character_id: "c", text: "C 最新选择台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    backendProjects.set(projectCId, projectC);
    let authorityUnavailable = false;
    let deferAuthorityRecovery = false;
    const authorityRecovery = deferred<ScriptProject>();
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === projectAId && authorityUnavailable) {
        throw new Error("authority temporarily unavailable");
      }
      if (projectId === projectAId && deferAuthorityRecovery) return authorityRecovery.promise;
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const pendingCreate = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingCreate.promise);
    const view = await renderApp(projectAId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：服务端已提交但响应丢失"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();

    const revision = scriptRevision(
      "switch-guard-server-revision",
      "甲：服务端已提交但响应丢失",
      "sha-switch-guard-server-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectAId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectAId, cloneProject(serverProject));
    authorityUnavailable = true;
    pendingCreate.reject(new Error("connection lost after commit"));
    await flushAsync(80);
    expect(apiMocks.saveProject).not.toHaveBeenCalled();

    const rowB = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Switch guard target B"))!;
    await click(rowB);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(80);

    expect.soft(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectAId);
    expect.soft(view.container.textContent).toContain("A 待保存台词");

    authorityUnavailable = false;
    deferAuthorityRecovery = true;
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(40);
    const rowC = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Switch guard latest C"))!;
    await click(rowC);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(40);
    deferAuthorityRecovery = false;
    authorityRecovery.resolve(cloneProject(serverProject));
    await flushAsync(100);
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectCId);
    expect(view.container.textContent).toContain("C 最新选择台词");

    const rowA = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Switch guard current A"))!;
    await click(rowA);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(80);

    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectAId);
    expect(backendProjects.get(projectAId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectAId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectAId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
  });

  it.each([
    {
      label: "a 409 conflict",
      createSaveError: () => new ApiRequestError(409, '{"detail":"revision conflict"}', "revision conflict"),
      expectedNotice: "revision conflict"
    },
    {
      label: "a network error",
      createSaveError: () => new TypeError("network unavailable"),
      expectedNotice: "network unavailable"
    }
  ])("keeps protected project edits after $label until a retry saves them", async ({ createSaveError, expectedNotice }) => {
    vi.useFakeTimers();
    const projectAId = `switch-save-failure-a-${expectedNotice.replaceAll(" ", "-")}`;
    const projectBId = `switch-save-failure-b-${expectedNotice.replaceAll(" ", "-")}`;
    const projectA = scriptProject("Switch save failure A");
    projectA.lines = [{ id: "switch-save-failure-a-line", character_id: "a", text: "A 待安全保存台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Switch save failure B");
    projectB.lines = [{ id: "switch-save-failure-b-line", character_id: "b", text: "B 目标台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    let authorityUnavailable = false;
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === projectAId && authorityUnavailable) {
        throw new Error("authority temporarily unavailable");
      }
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const pendingCreate = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingCreate.promise);
    const view = await renderApp(projectAId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：PUT 失败也不能丢"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);
    await click(view.container.querySelector(".reference-setup-callout button")!);
    await flushAsync();

    const revision = scriptRevision(
      `switch-save-failure-revision-${expectedNotice.replaceAll(" ", "-")}`,
      "甲：PUT 失败也不能丢",
      `sha-switch-save-failure-${expectedNotice.replaceAll(" ", "-")}`
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectAId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectAId, cloneProject(serverProject));
    authorityUnavailable = true;
    pendingCreate.reject(new Error("connection lost after commit"));
    await flushAsync(80);

    authorityUnavailable = false;
    apiMocks.saveProject.mockRejectedValueOnce(createSaveError());
    const rowB = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Switch save failure B"))!;
    await click(rowB);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(100);

    expect.soft(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectAId);
    expect.soft(view.container.textContent).toContain("A 待安全保存台词");
    expect.soft(view.container.querySelector(".notice")?.textContent).toContain(expectedNotice);

    if (view.dom.window.localStorage.getItem("tts-more.currentProjectId") === projectAId) {
      await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
      await flushAsync(100);
    }
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectBId);

    const rowA = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Switch save failure A"))!;
    await click(rowA);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(80);

    expect(backendProjects.get(projectAId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(backendProjects.get(projectAId)?.script_revisions?.map((item) => item.revision_id))
      .toContain(revision.revision_id);
    expect(backendProjects.get(projectAId)?.lines[0]?.temporary_binding?.provider_type).toBe("indextts");
  });

  it("recovers unknown revision authority without a pending TTS edit before switching projects", async () => {
    vi.useFakeTimers();
    const projectAId = "switch-no-pending-current-a";
    const projectBId = "switch-no-pending-target-b";
    const projectA = scriptProject("Switch no pending A");
    projectA.lines = [{ id: "switch-no-pending-a-line", character_id: "a", text: "A 未修改台词", note: "", language: "zh-CN" }];
    const projectB = scriptProject("Switch no pending B");
    projectB.lines = [{ id: "switch-no-pending-b-line", character_id: "b", text: "B 目标台词", note: "", language: "zh-CN" }];
    backendProjects.set(projectAId, projectA);
    backendProjects.set(projectBId, projectB);
    let authorityUnavailable = false;
    apiMocks.fetchProject.mockImplementation(async (projectId: string) => {
      if (projectId === projectAId && authorityUnavailable) {
        throw new Error("authority temporarily unavailable");
      }
      const stored = backendProjects.get(projectId);
      if (!stored) throw new Error(`missing project: ${projectId}`);
      return cloneProject(stored);
    });
    const pendingCreate = deferred<{ project: ScriptProject; script_revision: ScriptRevision }>();
    apiMocks.createScriptRevision.mockImplementation(() => pendingCreate.promise);
    const view = await renderApp(projectAId);
    await flushAsync(40);

    await click(view.container.querySelector(".script-manager-row")!);
    await flushAsync();
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      "甲：没有 TTS pending 也要恢复权威"
    );
    await click(view.container.querySelector('[data-action="analyze-script"]')!);
    await flushAsync(40);

    const revision = scriptRevision(
      "switch-no-pending-server-revision",
      "甲：没有 TTS pending 也要恢复权威",
      "sha-switch-no-pending-server-revision"
    );
    const serverProject: ScriptProject = {
      ...cloneProject(backendProjects.get(projectAId)!),
      active_script_revision_id: revision.revision_id,
      script_revisions: [revision]
    };
    backendProjects.set(projectAId, cloneProject(serverProject));
    authorityUnavailable = true;
    pendingCreate.reject(new Error("connection lost after commit"));
    await flushAsync(80);
    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectAId);

    authorityUnavailable = false;
    const rowB = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Switch no pending B"))!;
    await click(rowB);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(100);

    expect(view.dom.window.localStorage.getItem("tts-more.currentProjectId")).toBe(projectBId);
    expect(view.container.textContent).toContain("B 目标台词");

    const rowA = [...view.container.querySelectorAll<HTMLElement>(".script-manager-row")]
      .find((row) => row.textContent?.includes("Switch no pending A"))!;
    await click(rowA);
    await flushAsync();
    await click(view.container.querySelector(".script-manager-inline-actions button.secondary-button")!);
    await flushAsync(80);

    expect(backendProjects.get(projectAId)?.active_script_revision_id).toBe(revision.revision_id);
    expect(view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")?.value)
      .toBe(revision.source_markdown);
  });
});
