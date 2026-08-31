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
import { ANALYSIS_RUN_SESSIONS_STORAGE_KEY } from "./features/script-analysis/useAnalysisDraft";

const apiMocks = vi.hoisted(() => ({
  fetchAuthStatus: vi.fn(),
  fetchServices: vi.fn(),
  fetchServiceSettings: vi.fn(),
  fetchServicesStatus: vi.fn(),
  fetchOpenSourceTTSCatalog: vi.fn(),
  fetchRuntimeMode: vi.fn(),
  fetchVoiceCandidates: vi.fn(),
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
  const globals = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = globals.IS_REACT_ACT_ENVIRONMENT;
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    Event: dom.window.Event,
    File: dom.window.File,
    IS_REACT_ACT_ENVIRONMENT: true
  });
  if (storedProjectId) dom.window.localStorage.setItem("tts-more.currentProjectId", storedProjectId);
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

async function changeReactValueExact(element: HTMLInputElement | HTMLTextAreaElement, value: string): Promise<void> {
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

  it("passes exact source bytes through save-revision and legacy parse entry points", async () => {
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

    apiMocks.createScriptRevision.mockClear();
    const exactParseSource = "  旧解析入口\r\n仍须精确 \r\n";
    await changeReactValueExact(
      view.container.querySelector<HTMLTextAreaElement>(".script-manager-source-editor")!,
      exactParseSource
    );
    apiMocks.createParseRevision.mockImplementation(async (targetProjectId: string, revisionId: string) => {
      const current = backendProjects.get(targetProjectId)!;
      return {
        project: current,
        parse_revision: {
          revision_id: "parse-legacy-exact",
          script_revision_id: revisionId,
          provider: "legacy-fake",
          warnings: [],
          project_characters: [],
          lines: current.lines,
          created_at: timestamp
        }
      };
    });
    await click(view.container.querySelector('[data-action="legacy-parse-script"]')!);
    await flushAsync();
    expect(apiMocks.createScriptRevision.mock.calls[0][1]).toBe(exactParseSource);
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
    expect(view.container.querySelector('[data-analysis-error="run"]')?.textContent).toContain("trace-resume-422");
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
    activeViews.splice(activeViews.indexOf(view), 1);
    await view.cleanup();
    const remounted = await renderApp(projectId, (storage) => {
      if (persistedSessions) storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, persistedSessions);
    });
    await flushAsync(40);

    expect(remounted.container.querySelector(".script-analysis-workspace")).toBeNull();
    expect(remounted.container.textContent).toContain("确认后不应重开分析");
    expect(apiMocks.createAnalysisRun).toHaveBeenCalledOnce();
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
});
