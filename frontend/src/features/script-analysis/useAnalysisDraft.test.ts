import { act, createElement, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AnalysisError,
  AnalysisRun,
  AnalysisRunStatus,
  CharacterCandidate,
  DraftOperation,
  ScriptProject,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticAnnotation,
  SemanticConfirmResponse,
  SemanticUtterance
} from "../../types";
import {
  ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY,
  ANALYSIS_DISMISSED_RUNS_STORAGE_KEY,
  ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY,
  ANALYSIS_RUN_SESSIONS_STORAGE_KEY,
  activeAnalysisScopeForRevision,
  applyDraftOperations,
  archiveRestorableAnalysisSession,
  clearActiveAnalysisScope,
  hasReviewableAnalysisSession,
  readActiveAnalysisScope,
  restoreReviewableAnalysisSession,
  useAnalysisDraft,
  writeActiveAnalysisScope,
  type AnalysisDraftApi,
  type UseAnalysisDraftOptions,
  type UseAnalysisDraftResult
} from "./useAnalysisDraft";

const timestamp = "2026-08-31T08:09:10.000Z";
const pollIntervalMs = 1_000;

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, String(value));
  }
}

describe("active analysis scope storage", () => {
  it("rejects corrupt shapes and blank project or revision identities", () => {
    const storage = new MemoryStorage();
    const invalidValues = [
      "{malformed",
      "[]",
      JSON.stringify({ projectId: "", revisionId: "revision-1", sourceSha256: null }),
      JSON.stringify({ projectId: "project-1", revisionId: "   ", sourceSha256: "hash-1" }),
      JSON.stringify({ projectId: "project-1", revisionId: "revision-1", sourceSha256: 7 })
    ];

    for (const invalidValue of invalidValues) {
      storage.setItem(ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY, invalidValue);
      expect(readActiveAnalysisScope(storage)).toBeNull();
      expect(storage.getItem(ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY)).toBeNull();
    }
  });

  it("does not clear a newer active scope when an older scope finishes late", () => {
    const storage = new MemoryStorage();
    const olderRevision = revision("revision-old", "hash-old");
    const newerRevision = revision("revision-new", "hash-new");
    const olderScope = activeAnalysisScopeForRevision("project-old", olderRevision);
    writeActiveAnalysisScope("project-new", newerRevision, storage);

    clearActiveAnalysisScope(olderScope, storage);

    expect(readActiveAnalysisScope(storage)).toEqual({
      projectId: "project-new",
      revisionId: "revision-new",
      sourceSha256: "hash-new"
    });
  });

  it("archives confirmed sessions for explicit review without making them auto-restorable", () => {
    const storage = new MemoryStorage();
    const sourceRevision = revision();
    const scopeId = JSON.stringify(["project-1", sourceRevision.revision_id, sourceRevision.source_sha256]);
    storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({
      [scopeId]: { runId: "run-1", draftId: "draft-1" }
    }));

    archiveRestorableAnalysisSession("project-1", sourceRevision, storage);

    expect(hasReviewableAnalysisSession("project-1", sourceRevision, storage)).toBe(true);
    expect(JSON.parse(storage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY) ?? "{}")).toEqual({});
    expect(JSON.parse(storage.getItem(ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY) ?? "{}")).toEqual({
      [scopeId]: { runId: "run-1", draftId: "draft-1" }
    });

    expect(restoreReviewableAnalysisSession("project-1", sourceRevision, storage)).toBe(true);
    expect(hasReviewableAnalysisSession("project-1", sourceRevision, storage)).toBe(false);
    expect(JSON.parse(storage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY) ?? "{}")).toEqual({
      [scopeId]: { runId: "run-1", draftId: "draft-1" }
    });
  });
});

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function revision(id = "script-r001", hash = "sha256-source"): ScriptRevision {
  return {
    revision_id: id,
    source_markdown: "甲：你好。\n乙：再见。",
    source_sha256: hash,
    created_at: timestamp
  };
}

function annotation(
  id: string,
  kind: SemanticAnnotation["kind"],
  start: number,
  end: number,
  status: SemanticAnnotation["status"] = "accepted"
): SemanticAnnotation {
  const source = revision().source_markdown;
  return {
    id,
    kind,
    span: {
      source_revision_id: "script-r001",
      start_utf16: start,
      end_utf16: end,
      text: source.slice(start, end),
      source_sha256: "sha256-source"
    },
    origin: "ai",
    confidence: 0.9,
    status,
    created_at: timestamp,
    updated_at: timestamp
  };
}

function character(
  id: string,
  overrides: Partial<CharacterCandidate> = {}
): CharacterCandidate {
  return {
    id,
    canonical_name: id === "character-1" ? "甲" : "乙",
    aliases: id === "character-1" ? ["阿甲", "小甲"] : ["阿乙"],
    supporting_annotation_ids: id === "character-1" ? ["speaker-1"] : [],
    project_character_id: null,
    confidence: 0.9,
    status: "accepted",
    origin: "ai",
    ...overrides
  };
}

function utterance(
  id = "utterance-1",
  overrides: Partial<SemanticUtterance> = {}
): SemanticUtterance {
  return {
    id,
    dialogue_annotation_id: id === "utterance-1" ? "dialogue-1" : "dialogue-2",
    speaker_annotation_id: id === "utterance-1" ? "speaker-1" : null,
    character_candidate_id: id === "utterance-1" ? "character-1" : "character-2",
    emotion_evidence_annotation_ids: id === "utterance-1" ? ["emotion-1"] : [],
    normalized_emotion: null,
    custom_emotion: null,
    emotion_intensity: null,
    emotion_origin: "none",
    language: "zh-CN",
    confidence: 0.9,
    uncertainty_codes: [],
    status: "accepted",
    ...overrides
  };
}

function draft(
  version = 3,
  overrides: Partial<SemanticAnalysisDraft> = {}
): SemanticAnalysisDraft {
  return {
    id: "draft-1",
    project_id: "project-1",
    source_revision_id: "script-r001",
    version,
    annotations: [
      annotation("speaker-1", "speaker", 0, 1),
      annotation("dialogue-1", "dialogue", 2, 5),
      annotation("emotion-1", "emotion_evidence", 2, 4)
    ],
    characters: [character("character-1")],
    utterances: [utterance()],
    unresolved_candidates: [],
    warnings: [
      {
        id: "warning-1",
        code: "review",
        message: "Needs review",
        annotation_id: null,
        utterance_id: "utterance-1",
        details: {}
      }
    ],
    provider: "fake",
    model: "fake-model",
    prompt_version: "prompt-v1",
    contract_version: "contract-v1",
    confirmed_revision_id: null,
    confirmed_parse_revision_id: null,
    confirmed_parse_fingerprint: null,
    confirm_idempotency_key: null,
    created_at: timestamp,
    updated_at: timestamp,
    ...overrides
  };
}

function analysisError(
  runId: string,
  status = 422,
  traceId = "trace-422"
): AnalysisError {
  return {
    code: "semantic_response_invalid",
    http_status: status,
    stage: "decode",
    message: "Semantic response is invalid",
    retryable: false,
    run_id: runId,
    trace_id: traceId,
    occurred_at: timestamp,
    details: { safe: true }
  };
}

function run(
  status: AnalysisRunStatus,
  overrides: Partial<AnalysisRun> = {}
): AnalysisRun {
  const id = overrides.id ?? "run-1";
  return {
    id,
    project_id: "project-1",
    source_revision_id: "script-r001",
    draft_id: overrides.draft_id ?? "draft-1",
    status,
    quality: status === "completed" ? "complete" : null,
    progress: status === "queued" ? 0 : status === "running" ? 0.5 : 1,
    warnings: [],
    error: status === "failed" ? analysisError(id) : null,
    trace_id: status === "failed" ? "trace-422" : "trace-1",
    created_at: timestamp,
    updated_at: timestamp,
    ...overrides
  };
}

function project(title = "Confirmed project"): ScriptProject {
  return { title, default_language: "zh-CN", lines: [] };
}

function confirmResponse(title = "Confirmed project"): SemanticConfirmResponse {
  const sourceDraft = draft();
  return {
    project: project(title),
    semantic_revision: {
      id: "semantic-1",
      project_id: sourceDraft.project_id,
      source_revision_id: sourceDraft.source_revision_id,
      annotations: sourceDraft.annotations,
      characters: sourceDraft.characters,
      utterances: sourceDraft.utterances,
      unresolved_candidates: [],
      warnings: [],
      provider: sourceDraft.provider,
      model: sourceDraft.model,
      prompt_version: sourceDraft.prompt_version,
      contract_version: sourceDraft.contract_version,
      created_at: timestamp
    },
    parse_revision: {
      revision_id: "semantic-semantic-1",
      script_revision_id: sourceDraft.source_revision_id,
      provider: "semantic-confirmed",
      warnings: [],
      project_characters: [],
      lines: [],
      created_at: timestamp
    }
  };
}

function makeApi(overrides: Partial<AnalysisDraftApi> = {}): AnalysisDraftApi {
  return {
    createAnalysisRun: vi.fn(async () => ({
      run_id: "run-1",
      draft_id: "draft-1",
      status: "queued" as const,
      trace_id: "trace-1"
    })),
    fetchAnalysisRun: vi.fn(async () => run("completed")),
    fetchAnalysisDraft: vi.fn(async () => draft()),
    patchAnalysisDraft: vi.fn(async (_draftId, expectedVersion, operations) => ({
      ...applyDraftOperations(draft(expectedVersion), operations),
      version: expectedVersion + 1
    })),
    confirmAnalysisDraft: vi.fn(async () => confirmResponse()),
    ...overrides
  };
}

interface RenderedHook {
  readonly current: UseAnalysisDraftResult;
  rerender: (projectId: string, sourceRevision: ScriptRevision) => Promise<void>;
  cleanup: () => Promise<void>;
}

const activeCleanups = new Set<() => Promise<void>>();

async function flushMicrotasks(turns = 8): Promise<void> {
  await act(async () => {
    for (let index = 0; index < turns; index += 1) await Promise.resolve();
  });
}

async function renderAnalysisHook(
  api: AnalysisDraftApi,
  storage: Storage | null,
  initialProjectId = "project-1",
  initialRevision = revision(),
  overrides: Partial<UseAnalysisDraftOptions> = {},
  strictMode = false
): Promise<RenderedHook> {
  const dom = new JSDOM("<div id=\"root\"></div>", {
    pretendToBeVisual: true,
    url: "http://localhost"
  });
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousHTMLElement = globalThis.HTMLElement;
  const actGlobals = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = actGlobals.IS_REACT_ACT_ENVIRONMENT;
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true
  });

  let current: UseAnalysisDraftResult | null = null;
  let props = { projectId: initialProjectId, sourceRevision: initialRevision };
  const options: UseAnalysisDraftOptions = {
    api,
    storage,
    pollIntervalMs,
    createIdempotencyKey: () => "confirm-key-1",
    ...overrides
  };

  function Harness(nextProps: typeof props) {
    current = useAnalysisDraft(nextProps.projectId, nextProps.sourceRevision, options);
    return null;
  }

  const root: Root = createRoot(dom.window.document.getElementById("root")!);
  const renderHarness = () => strictMode
    ? createElement(StrictMode, null, createElement(Harness, props))
    : createElement(Harness, props);
  await act(async () => root.render(renderHarness()));

  let cleaned = false;
  const cleanup = async () => {
    if (cleaned) return;
    cleaned = true;
    activeCleanups.delete(cleanup);
    await act(async () => root.unmount());
    Object.assign(globalThis, {
      window: previousWindow,
      document: previousDocument,
      HTMLElement: previousHTMLElement,
      IS_REACT_ACT_ENVIRONMENT: previousActEnvironment
    });
  };
  activeCleanups.add(cleanup);

  return {
    get current() {
      if (!current) throw new Error("Hook has not rendered");
      return current;
    },
    rerender: async (projectId, sourceRevision) => {
      props = { projectId, sourceRevision };
      await act(async () => root.render(renderHarness()));
    },
    cleanup
  };
}

async function advancePoll(milliseconds = pollIntervalMs): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
  await flushMicrotasks();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(async () => {
  for (const cleanup of [...activeCleanups].reverse()) await cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("applyDraftOperations", () => {
  it("applies all controlled collection edits and server-equivalent cascades without mutating the base draft", () => {
    const base = draft();
    const dialogue2 = annotation("dialogue-2", "dialogue", 8, 11, "pending");
    const replacementDialogue2 = { ...dialogue2, status: "accepted" as const };
    const character2 = character("character-2", {
      aliases: ["阿乙", "小乙"],
      supporting_annotation_ids: []
    });
    const utterance2 = utterance("utterance-2", { status: "pending" });
    const updatedUtterance2 = {
      ...utterance2,
      confidence: 0.55,
      normalized_emotion: "happy" as const
    };
    const splitCharacter = character("character-3", {
      canonical_name: "小甲",
      aliases: ["小甲"],
      supporting_annotation_ids: [],
      origin: "human",
      confidence: null,
      status: "pending"
    });
    const operations: DraftOperation[] = [
      { op: "create_annotation", annotation: dialogue2 },
      {
        op: "replace_annotation",
        annotation_id: "dialogue-2",
        annotation: replacementDialogue2
      },
      { op: "upsert_character", character: character2 },
      { op: "create_utterance", utterance: utterance2 },
      {
        op: "update_utterance",
        utterance_id: "utterance-2",
        utterance: updatedUtterance2
      },
      { op: "set_annotation_status", annotation_id: "dialogue-1", status: "pending" },
      { op: "set_character_status", character_id: "character-2", status: "pending" },
      {
        op: "merge_characters",
        target_character_id: "character-1",
        source_character_ids: ["character-2"]
      },
      {
        op: "split_alias",
        character_id: "character-1",
        alias: "小甲",
        character: splitCharacter
      },
      { op: "set_utterance_status", utterance_id: "utterance-2", status: "accepted" },
      { op: "dismiss_warning", warning_id: "warning-1" },
      { op: "delete_utterance", utterance_id: "utterance-2" },
      { op: "delete_annotation", annotation_id: "emotion-1" }
    ];

    const result = applyDraftOperations(base, operations);

    expect(base.annotations.map((item) => item.id)).toEqual([
      "speaker-1",
      "dialogue-1",
      "emotion-1"
    ]);
    expect(result.annotations.map((item) => item.id)).toEqual([
      "speaker-1",
      "dialogue-1",
      "dialogue-2"
    ]);
    expect(result.annotations.find((item) => item.id === "dialogue-2")?.status).toBe("accepted");
    expect(result.characters.map((item) => item.id)).toEqual(["character-1", "character-3"]);
    expect(result.characters[0].aliases).toEqual(["阿甲", "阿乙", "小乙"]);
    expect(result.utterances).toEqual([
      expect.objectContaining({
        id: "utterance-1",
        status: "pending",
        emotion_evidence_annotation_ids: []
      })
    ]);
    expect(result.warnings).toEqual([]);
  });

  it("deleting speaker and dialogue annotations clears dependent references and utterances", () => {
    const withSecondUtterance = draft(3, {
      utterances: [
        utterance(),
        utterance("utterance-2", {
          dialogue_annotation_id: "dialogue-1",
          speaker_annotation_id: "speaker-1",
          character_candidate_id: "character-1"
        })
      ]
    });

    const speakerDeleted = applyDraftOperations(withSecondUtterance, [
      { op: "delete_annotation", annotation_id: "speaker-1" }
    ]);
    expect(speakerDeleted.utterances.map((item) => item.speaker_annotation_id)).toEqual([
      null,
      null
    ]);
    expect(speakerDeleted.characters[0].supporting_annotation_ids).toEqual([]);

    const dialogueDeleted = applyDraftOperations(speakerDeleted, [
      { op: "delete_annotation", annotation_id: "dialogue-1" }
    ]);
    expect(dialogueDeleted.utterances).toEqual([]);
  });
});

describe("analysis run restoration and polling", () => {
  it("creates one run when StrictMode replays initialization before the POST resolves", async () => {
    const response = deferred<Awaited<ReturnType<AnalysisDraftApi["createAnalysisRun"]>>>();
    const api = makeApi({ createAnalysisRun: vi.fn(() => response.promise) });
    const storage = new MemoryStorage();
    const view = await renderAnalysisHook(api, storage, "project-1", revision(), {}, true);
    expect(api.createAnalysisRun).toHaveBeenCalledOnce();
    await act(async () => response.resolve({ run_id: "run-1", draft_id: "draft-1", status: "queued", trace_id: "trace-1" }));
    await flushMicrotasks();
    expect(view.current.run?.id).toBe("run-1");
    expect(view.current.draft?.id).toBe("draft-1");
    expect(api.createAnalysisRun).toHaveBeenCalledOnce();
    expect(storage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY)).toContain("run-1");
  });
  it("attaches to the persisted project/revision run after reload without creating another run", async () => {
    const storage = new MemoryStorage();
    const firstApi = makeApi();
    const first = await renderAnalysisHook(firstApi, storage);
    await flushMicrotasks();
    expect(first.current.draft?.id).toBe("draft-1");
    expect(vi.mocked(firstApi.createAnalysisRun)).toHaveBeenCalledOnce();
    await first.cleanup();

    const secondApi = makeApi({
      createAnalysisRun: vi.fn(async () => {
        throw new Error("duplicate run should not be created");
      })
    });
    const second = await renderAnalysisHook(secondApi, storage);
    await flushMicrotasks();

    expect(second.current.run?.id).toBe("run-1");
    expect(second.current.draft?.id).toBe("draft-1");
    expect(vi.mocked(secondApi.createAnalysisRun)).not.toHaveBeenCalled();
  });

  it("ignores malformed session storage and creates one fresh run", async () => {
    const storage = new MemoryStorage();
    storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, "{malformed");
    const api = makeApi();
    const view = await renderAnalysisHook(api, storage);
    await flushMicrotasks();

    expect(view.current.run?.id).toBe("run-1");
    expect(vi.mocked(api.createAnalysisRun)).toHaveBeenCalledOnce();
    expect(() => JSON.parse(storage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY)!)).not.toThrow();
  });

  it("replaces a stale 404 session with a fresh run but does not replace non-404 attach failures", async () => {
    const storage = new MemoryStorage();
    const seeded = await renderAnalysisHook(makeApi(), storage);
    await flushMicrotasks();
    await seeded.cleanup();

    const staleApi = makeApi({
      createAnalysisRun: vi.fn(async () => ({
        run_id: "run-2",
        draft_id: "draft-2",
        status: "queued" as const,
        trace_id: "trace-2"
      })),
      fetchAnalysisRun: vi
        .fn()
        .mockRejectedValueOnce(
          new Error('{"detail":{"code":"run_not_found","message":"not found"}}')
        )
        .mockResolvedValueOnce(
          run("completed", { id: "run-2", draft_id: "draft-2", trace_id: "trace-2" })
        ),
      fetchAnalysisDraft: vi.fn(async () =>
        draft(1, { id: "draft-2", confirmed_revision_id: null })
      )
    });
    const recovered = await renderAnalysisHook(staleApi, storage);
    await flushMicrotasks();
    expect(recovered.current.run?.id).toBe("run-2");
    expect(vi.mocked(staleApi.createAnalysisRun)).toHaveBeenCalledOnce();
    await recovered.cleanup();

    const unavailableApi = makeApi({
      createAnalysisRun: vi.fn(async () => {
        throw new Error("must not create on non-404");
      }),
      fetchAnalysisRun: vi.fn(async () => {
        throw new Error(
          '{"detail":{"code":"semantic_internal_error","message":"temporarily unavailable"}}'
        );
      })
    });
    const unavailable = await renderAnalysisHook(unavailableApi, storage);
    await flushMicrotasks();
    expect(unavailable.current.controllerError?.code).toBe("semantic_internal_error");
    expect(unavailable.current.controllerError?.message).toContain("temporarily unavailable");
    expect(vi.mocked(unavailableApi.createAnalysisRun)).not.toHaveBeenCalled();
  });

  it("never creates a replacement run when a review-only session is stale", async () => {
    const storage = new MemoryStorage();
    const seeded = await renderAnalysisHook(makeApi(), storage);
    await flushMicrotasks();
    await seeded.cleanup();

    const reviewApi = makeApi({
      createAnalysisRun: vi.fn(async () => {
        throw new Error("review-only mode must not create a new analysis run");
      }),
      fetchAnalysisRun: vi.fn(async () => {
        throw new Error('{"detail":{"code":"run_not_found","message":"not found"}}');
      })
    });
    const review = await renderAnalysisHook(
      reviewApi,
      storage,
      "project-1",
      revision(),
      { mode: "review" }
    );
    await flushMicrotasks();

    expect(vi.mocked(reviewApi.createAnalysisRun)).not.toHaveBeenCalled();
    expect(review.current.isRunning).toBe(false);
    expect(review.current.controllerError?.code).toBe("analysis_review_not_found");

    await act(async () => review.current.retryAnalysis());
    await flushMicrotasks();
    expect(vi.mocked(reviewApi.createAnalysisRun)).not.toHaveBeenCalled();
    expect(review.current.controllerError?.code).toBe("analysis_review_not_found");
  });

  it("never overlaps polls and stops after terminal state while fetching the draft once", async () => {
    const nextPoll = deferred<AnalysisRun>();
    const api = makeApi({
      fetchAnalysisRun: vi
        .fn()
        .mockResolvedValueOnce(run("running"))
        .mockReturnValueOnce(nextPoll.promise),
      fetchAnalysisDraft: vi.fn(async () => draft())
    });
    const view = await renderAnalysisHook(api, new MemoryStorage());
    await flushMicrotasks();
    expect(view.current.isRunning).toBe(true);
    expect(vi.getTimerCount()).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(pollIntervalMs);
    });
    expect(vi.mocked(api.fetchAnalysisRun)).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);

    await advancePoll(10 * pollIntervalMs);
    expect(vi.mocked(api.fetchAnalysisRun)).toHaveBeenCalledTimes(2);

    await act(async () => nextPoll.resolve(run("completed")));
    await flushMicrotasks();
    expect(view.current.isRunning).toBe(false);
    expect(view.current.draft?.version).toBe(3);
    expect(vi.mocked(api.fetchAnalysisDraft)).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);

    await advancePoll(10 * pollIntervalMs);
    expect(vi.mocked(api.fetchAnalysisRun)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.fetchAnalysisDraft)).toHaveBeenCalledOnce();
  });

  it.each(["completed", "failed", "interrupted"] as const)(
    "treats %s as terminal and fetches its draft exactly once",
    async (status) => {
      const api = makeApi({
        fetchAnalysisRun: vi.fn(async () => run(status)),
        fetchAnalysisDraft: vi.fn(async () => draft())
      });
      const view = await renderAnalysisHook(api, new MemoryStorage());
      await flushMicrotasks();

      expect(view.current.isRunning).toBe(false);
      expect(view.current.draft?.id).toBe("draft-1");
      expect(vi.mocked(api.fetchAnalysisDraft)).toHaveBeenCalledOnce();
      expect(vi.getTimerCount()).toBe(0);
      await advancePoll(4 * pollIntervalMs);
      expect(vi.mocked(api.fetchAnalysisDraft)).toHaveBeenCalledOnce();
    }
  );

  it("clears timers on unmount and ignores the late run response", async () => {
    const lateRun = deferred<AnalysisRun>();
    const api = makeApi({
      fetchAnalysisRun: vi.fn(() => lateRun.promise),
      fetchAnalysisDraft: vi.fn(async () => draft())
    });
    const view = await renderAnalysisHook(api, new MemoryStorage());
    await flushMicrotasks();
    await view.cleanup();
    await act(async () => lateRun.resolve(run("completed")));
    await flushMicrotasks();

    expect(vi.mocked(api.fetchAnalysisDraft)).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ignores a late response from the previous project and revision scope", async () => {
    const oldRun = deferred<AnalysisRun>();
    const api = makeApi({
      createAnalysisRun: vi.fn(async (projectId) =>
        projectId === "project-1"
          ? {
              run_id: "run-old",
              draft_id: "draft-old",
              status: "queued" as const,
              trace_id: "trace-old"
            }
          : {
              run_id: "run-new",
              draft_id: "draft-new",
              status: "queued" as const,
              trace_id: "trace-new"
            }
      ),
      fetchAnalysisRun: vi.fn((runId) =>
        runId === "run-old"
          ? oldRun.promise
          : Promise.resolve(
              run("completed", {
                id: "run-new",
                project_id: "project-2",
                source_revision_id: "script-r002",
                draft_id: "draft-new"
              })
            )
      ),
      fetchAnalysisDraft: vi.fn(async () =>
        draft(1, {
          id: "draft-new",
          project_id: "project-2",
          source_revision_id: "script-r002"
        })
      )
    });
    const view = await renderAnalysisHook(api, new MemoryStorage());
    await flushMicrotasks();

    await view.rerender("project-2", revision("script-r002", "sha256-source-2"));
    await flushMicrotasks();
    expect(view.current.run?.id).toBe("run-new");
    expect(view.current.draft?.id).toBe("draft-new");

    await act(async () => oldRun.resolve(run("failed", { id: "run-old" })));
    await flushMicrotasks();
    expect(view.current.run?.id).toBe("run-new");
    expect(view.current.error).toBeNull();
  });
});

describe("persistent run errors", () => {
  it("restores an undismissed 422 trace after reload and hides only explicitly dismissed run ids", async () => {
    const storage = new MemoryStorage();
    const failed = run("failed", {
      id: "run-1",
      error: analysisError("run-1", 422, "trace-persisted")
    });
    const first = await renderAnalysisHook(
      makeApi({ fetchAnalysisRun: vi.fn(async () => failed) }),
      storage
    );
    await flushMicrotasks();
    expect(first.current.error?.http_status).toBe(422);
    expect(first.current.error?.trace_id).toBe("trace-persisted");
    await first.cleanup();

    const reloaded = await renderAnalysisHook(
      makeApi({ fetchAnalysisRun: vi.fn(async () => failed) }),
      storage
    );
    await flushMicrotasks();
    expect(reloaded.current.error?.trace_id).toBe("trace-persisted");
    await act(async () => reloaded.current.dismissError());
    expect(reloaded.current.error).toBeNull();
    expect(JSON.parse(storage.getItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY)!)).toEqual(["run-1"]);
    await reloaded.cleanup();

    const dismissedReload = await renderAnalysisHook(
      makeApi({ fetchAnalysisRun: vi.fn(async () => failed) }),
      storage
    );
    await flushMicrotasks();
    expect(dismissedReload.current.error).toBeNull();
  });

  it("shows a different new-run failure and clears the old error when a later run succeeds", async () => {
    const storage = new MemoryStorage();
    storage.setItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY, JSON.stringify(["run-1"]));
    const api = makeApi({
      createAnalysisRun: vi
        .fn()
        .mockResolvedValueOnce({
          run_id: "run-1",
          draft_id: "draft-1",
          status: "queued",
          trace_id: "trace-1"
        })
        .mockResolvedValueOnce({
          run_id: "run-2",
          draft_id: "draft-2",
          status: "queued",
          trace_id: "trace-2"
        })
        .mockResolvedValueOnce({
          run_id: "run-3",
          draft_id: "draft-3",
          status: "queued",
          trace_id: "trace-3"
        }),
      fetchAnalysisRun: vi
        .fn()
        .mockResolvedValueOnce(run("failed", { id: "run-1" }))
        .mockResolvedValueOnce(
          run("failed", {
            id: "run-2",
            draft_id: "draft-2",
            error: analysisError("run-2", 502, "trace-new-failure")
          })
        )
        .mockResolvedValueOnce(
          run("completed", { id: "run-3", draft_id: "draft-3", error: null })
        ),
      fetchAnalysisDraft: vi.fn(async (draftId) => draft(1, { id: draftId }))
    });
    const view = await renderAnalysisHook(api, storage);
    await flushMicrotasks();
    expect(view.current.error).toBeNull();

    await act(async () => view.current.retryAnalysis());
    await flushMicrotasks();
    expect(view.current.error?.trace_id).toBe("trace-new-failure");

    await act(async () => view.current.retryAnalysis());
    await flushMicrotasks();
    expect(view.current.run?.id).toBe("run-3");
    expect(view.current.error).toBeNull();
  });

  it("keeps the old error through create failure, queued, and running until completion succeeds", async () => {
    const create = vi
      .fn<AnalysisDraftApi["createAnalysisRun"]>()
      .mockResolvedValueOnce({
        run_id: "run-old",
        draft_id: "draft-old",
        status: "queued",
        trace_id: "trace-old"
      })
      .mockRejectedValueOnce(new Error("create unavailable"))
      .mockResolvedValueOnce({
        run_id: "run-new",
        draft_id: "draft-new",
        status: "queued",
        trace_id: "trace-new"
      });
    const fetchRun = vi
      .fn<AnalysisDraftApi["fetchAnalysisRun"]>()
      .mockResolvedValueOnce(
        run("failed", {
          id: "run-old",
          draft_id: "draft-old",
          error: analysisError("run-old", 422, "trace-old-error")
        })
      )
      .mockResolvedValueOnce(run("queued", { id: "run-new", draft_id: "draft-new" }))
      .mockResolvedValueOnce(run("running", { id: "run-new", draft_id: "draft-new" }))
      .mockResolvedValueOnce(run("completed", { id: "run-new", draft_id: "draft-new" }));
    const api = makeApi({
      createAnalysisRun: create,
      fetchAnalysisRun: fetchRun,
      fetchAnalysisDraft: vi.fn(async (draftId) => draft(1, { id: draftId }))
    });
    const view = await renderAnalysisHook(api, new MemoryStorage());
    await flushMicrotasks();
    expect(view.current.error?.trace_id).toBe("trace-old-error");

    await act(async () => view.current.retryAnalysis());
    await flushMicrotasks();
    expect(view.current.controllerError?.kind).toBe("create");
    expect(view.current.error?.trace_id).toBe("trace-old-error");

    await act(async () => view.current.retryAnalysis());
    await flushMicrotasks();
    expect(view.current.run?.status).toBe("queued");
    expect(view.current.error?.trace_id).toBe("trace-old-error");

    await advancePoll();
    expect(view.current.run?.status).toBe("running");
    expect(view.current.error?.trace_id).toBe("trace-old-error");

    await advancePoll();
    expect(view.current.run?.status).toBe("completed");
    expect(view.current.error).toBeNull();
  });

  it("dismisses the displayed old error owner while a replacement run is queued", async () => {
    const storage = new MemoryStorage();
    const api = makeApi({
      createAnalysisRun: vi
        .fn<AnalysisDraftApi["createAnalysisRun"]>()
        .mockResolvedValueOnce({
          run_id: "run-old",
          draft_id: "draft-old",
          status: "queued",
          trace_id: "trace-old"
        })
        .mockResolvedValueOnce({
          run_id: "run-new",
          draft_id: "draft-new",
          status: "queued",
          trace_id: "trace-new"
        }),
      fetchAnalysisRun: vi
        .fn<AnalysisDraftApi["fetchAnalysisRun"]>()
        .mockResolvedValueOnce(
          run("failed", {
            id: "run-old",
            draft_id: "draft-old",
            error: analysisError("run-old", 422, "trace-old-error")
          })
        )
        .mockResolvedValueOnce(run("queued", { id: "run-new", draft_id: "draft-new" })),
      fetchAnalysisDraft: vi.fn(async (draftId) => draft(1, { id: draftId }))
    });
    const view = await renderAnalysisHook(api, storage);
    await flushMicrotasks();
    await act(async () => view.current.retryAnalysis());
    await flushMicrotasks();
    expect(view.current.run?.id).toBe("run-new");
    expect(view.current.error?.run_id).toBe("run-old");

    await act(async () => view.current.dismissError());
    expect(view.current.error).toBeNull();
    expect(JSON.parse(storage.getItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY)!)).toEqual([
      "run-old"
    ]);
  });
});

describe("serial optimistic draft patches", () => {
  it("keeps optimistic batch B visible when A resolves and sends versions strictly 3 then 4", async () => {
    const firstPatch = deferred<SemanticAnalysisDraft>();
    const secondPatch = deferred<SemanticAnalysisDraft>();
    const patch = vi
      .fn()
      .mockReturnValueOnce(firstPatch.promise)
      .mockReturnValueOnce(secondPatch.promise);
    const api = makeApi({ patchAnalysisDraft: patch });
    const view = await renderAnalysisHook(api, new MemoryStorage());
    await flushMicrotasks();

    const accepted: DraftOperation[] = [
      { op: "set_utterance_status", utterance_id: "utterance-1", status: "accepted" }
    ];
    const happyUtterance = utterance("utterance-1", {
      status: "accepted",
      normalized_emotion: "happy",
      emotion_origin: "inferred"
    });
    const happy: DraftOperation[] = [
      {
        op: "update_utterance",
        utterance_id: "utterance-1",
        utterance: happyUtterance
      }
    ];

    await act(async () => {
      expect(view.current.queueOperations(accepted)).toBe(true);
      expect(view.current.queueOperations(happy)).toBe(true);
    });
    expect(view.current.draft?.utterances[0].normalized_emotion).toBe("happy");
    expect(patch).toHaveBeenCalledTimes(1);
    expect(patch.mock.calls[0][1]).toBe(3);

    await act(async () =>
      firstPatch.resolve(
        draft(4, { utterances: [utterance("utterance-1", { status: "accepted" })] })
      )
    );
    await flushMicrotasks();
    expect(view.current.draft?.utterances[0].normalized_emotion).toBe("happy");
    expect(patch).toHaveBeenCalledTimes(2);
    expect(patch.mock.calls[1][1]).toBe(4);

    await act(async () =>
      secondPatch.resolve(draft(5, { utterances: [happyUtterance] }))
    );
    await flushMicrotasks();
    expect(view.current.draft?.version).toBe(5);
    expect(view.current.pendingOperationBatches).toBe(0);
    expect(view.current.isSaving).toBe(false);
  });

  it("rebases a typed semantic 409 on the latest draft without hiding the local overlay", async () => {
    const latestDraft = deferred<SemanticAnalysisDraft>();
    const conflictError = Object.assign(new Error("semantic state conflict"), {
      status: 409,
      responseBody:
        '{"detail":{"code":"semantic_conflict","message":"semantic state conflict"}}'
    });
    const patch = vi
      .fn<AnalysisDraftApi["patchAnalysisDraft"]>()
      .mockRejectedValueOnce(conflictError)
      .mockImplementationOnce(async (_draftId, expectedVersion, operations) => ({
        ...applyDraftOperations(draft(expectedVersion), operations),
        version: expectedVersion + 1
      }));
    const fetchDraft = vi
      .fn<AnalysisDraftApi["fetchAnalysisDraft"]>()
      .mockResolvedValueOnce(draft())
      .mockReturnValueOnce(latestDraft.promise);
    const api = makeApi({ patchAnalysisDraft: patch, fetchAnalysisDraft: fetchDraft });
    const view = await renderAnalysisHook(api, new MemoryStorage());
    await flushMicrotasks();

    await act(async () => {
      view.current.queueOperations([
        { op: "set_utterance_status", utterance_id: "utterance-1", status: "pending" }
      ]);
    });
    await flushMicrotasks();

    expect(view.current.draft?.utterances[0].status).toBe("pending");
    expect(view.current.conflict?.code).toBe("semantic_conflict");
    expect(view.current.pendingOperationBatches).toBe(1);
    expect(view.current.isSaving).toBe(false);
    expect(fetchDraft).toHaveBeenCalledOnce();

    await act(async () => view.current.retryPendingOperations());
    await flushMicrotasks();
    expect(fetchDraft).toHaveBeenCalledTimes(2);
    expect(view.current.draft?.utterances[0].status).toBe("pending");

    await act(async () => latestDraft.resolve(draft(7)));
    await flushMicrotasks();
    expect(patch).toHaveBeenCalledTimes(2);
    expect(patch.mock.calls[1][1]).toBe(7);
    expect(view.current.conflict).toBeNull();
    expect(view.current.pendingOperationBatches).toBe(0);
    expect(view.current.draft?.version).toBe(8);
    expect(view.current.draft?.utterances[0].status).toBe("pending");
  });

  it("keeps the queue frozen and the local edit visible when a conflict rebase cannot apply", async () => {
    const conflictError = Object.assign(new Error("semantic state conflict"), {
      status: 409,
      responseBody:
        '{"detail":{"code":"semantic_conflict","message":"semantic state conflict"}}'
    });
    const patch = vi
      .fn<AnalysisDraftApi["patchAnalysisDraft"]>()
      .mockRejectedValueOnce(conflictError);
    const fetchDraft = vi
      .fn<AnalysisDraftApi["fetchAnalysisDraft"]>()
      .mockResolvedValueOnce(draft())
      .mockResolvedValueOnce(draft(7, { utterances: [] }));
    const view = await renderAnalysisHook(
      makeApi({ patchAnalysisDraft: patch, fetchAnalysisDraft: fetchDraft }),
      new MemoryStorage()
    );
    await flushMicrotasks();

    await act(async () => {
      view.current.queueOperations([
        { op: "set_utterance_status", utterance_id: "utterance-1", status: "pending" }
      ]);
    });
    await flushMicrotasks();
    expect(view.current.draft?.utterances[0].status).toBe("pending");

    await act(async () => view.current.retryPendingOperations());
    await flushMicrotasks();
    expect(fetchDraft).toHaveBeenCalledTimes(2);
    expect(patch).toHaveBeenCalledOnce();
    expect(view.current.conflict?.code).toBe("semantic_conflict");
    expect(view.current.pendingOperationBatches).toBe(1);
    expect(view.current.draft?.utterances[0].status).toBe("pending");
  });

  it("shows a non-conflict patch error and retries the preserved batch safely", async () => {
    const patch = vi
      .fn()
      .mockRejectedValueOnce(new Error("Gateway unavailable"))
      .mockResolvedValueOnce(
        draft(4, { utterances: [utterance("utterance-1", { status: "pending" })] })
      );
    const api = makeApi({ patchAnalysisDraft: patch });
    const view = await renderAnalysisHook(api, new MemoryStorage());
    await flushMicrotasks();

    await act(async () => {
      view.current.queueOperations([
        { op: "set_utterance_status", utterance_id: "utterance-1", status: "pending" }
      ]);
    });
    await flushMicrotasks();
    expect(view.current.controllerError?.kind).toBe("patch");
    expect(view.current.controllerError?.message).toContain("Gateway unavailable");
    expect(view.current.pendingOperationBatches).toBe(1);
    expect(view.current.draft?.utterances[0].status).toBe("pending");

    await act(async () => view.current.retryPendingOperations());
    await flushMicrotasks();
    expect(patch).toHaveBeenCalledTimes(2);
    expect(view.current.controllerError).toBeNull();
    expect(view.current.pendingOperationBatches).toBe(0);
    expect(view.current.draft?.version).toBe(4);
  });

  it("rejects edits while the run is queued or running and after the draft is confirmed", async () => {
    const runningApi = makeApi({ fetchAnalysisRun: vi.fn(async () => run("running")) });
    const runningView = await renderAnalysisHook(runningApi, new MemoryStorage());
    await flushMicrotasks();
    expect(
      runningView.current.queueOperations([
        { op: "dismiss_warning", warning_id: "warning-1" }
      ])
    ).toBe(false);
    expect(vi.mocked(runningApi.patchAnalysisDraft)).not.toHaveBeenCalled();
    await runningView.cleanup();

    const confirmedApi = makeApi({
      fetchAnalysisDraft: vi.fn(async () =>
        draft(3, {
          confirmed_revision_id: "semantic-1",
          confirmed_parse_revision_id: "semantic-semantic-1",
          confirm_idempotency_key: "server-confirm-key"
        })
      )
    });
    const confirmedView = await renderAnalysisHook(confirmedApi, new MemoryStorage());
    await flushMicrotasks();
    expect(confirmedView.current.isReadOnly).toBe(true);
    expect(
      confirmedView.current.queueOperations([
        { op: "dismiss_warning", warning_id: "warning-1" }
      ])
    ).toBe(false);
    expect(vi.mocked(confirmedApi.patchAnalysisDraft)).not.toHaveBeenCalled();
  });
});

describe("confirmation", () => {
  it("flushes patches first, deduplicates concurrent clicks, and returns the authoritative response", async () => {
    const pendingPatch = deferred<SemanticAnalysisDraft>();
    const pendingConfirm = deferred<SemanticConfirmResponse>();
    const confirm = vi.fn(() => pendingConfirm.promise);
    const api = makeApi({
      patchAnalysisDraft: vi.fn(() => pendingPatch.promise),
      confirmAnalysisDraft: confirm
    });
    const view = await renderAnalysisHook(api, new MemoryStorage(), "project-1", revision(), {
      createIdempotencyKey: () => "confirm-key-stable"
    });
    await flushMicrotasks();

    await act(async () => {
      view.current.queueOperations([
        { op: "set_utterance_status", utterance_id: "utterance-1", status: "pending" }
      ]);
    });
    const firstConfirm = view.current.confirm();
    const secondConfirm = view.current.confirm();
    await flushMicrotasks();
    expect(confirm).not.toHaveBeenCalled();

    await act(async () =>
      pendingPatch.resolve(
        draft(4, { utterances: [utterance("utterance-1", { status: "pending" })] })
      )
    );
    await flushMicrotasks();
    expect(confirm).toHaveBeenCalledOnce();
    expect(confirm).toHaveBeenCalledWith("draft-1", 4, "confirm-key-stable");

    const authoritative = confirmResponse("Server authoritative project");
    await act(async () => pendingConfirm.resolve(authoritative));
    await expect(firstConfirm).resolves.toBe(authoritative);
    await expect(secondConfirm).resolves.toBe(authoritative);
    expect(view.current.isConfirming).toBe(false);
    expect(view.current.isReadOnly).toBe(true);
  });

  it("reuses one persisted key across retry and reload, preferring a server-confirmed key", async () => {
    const storage = new MemoryStorage();
    const firstConfirm = vi.fn(async (_draftId: string, _version: number, _key: string) => {
      throw new Error("response lost");
    });
    const firstApi = makeApi({ confirmAnalysisDraft: firstConfirm });
    const first = await renderAnalysisHook(firstApi, storage, "project-1", revision(), {
      createIdempotencyKey: () => "client-key-original"
    });
    await flushMicrotasks();
    await expect(first.current.confirm()).rejects.toThrow("response lost");
    await expect(first.current.confirm()).rejects.toThrow("response lost");
    expect(firstConfirm.mock.calls.map((call) => call[2])).toEqual([
      "client-key-original",
      "client-key-original"
    ]);
    await first.cleanup();

    const reloadedConfirm = vi.fn(async () => confirmResponse());
    const reloadedApi = makeApi({
      fetchAnalysisDraft: vi.fn(async () => draft()),
      confirmAnalysisDraft: reloadedConfirm
    });
    const reloaded = await renderAnalysisHook(
      reloadedApi,
      storage,
      "project-1",
      revision(),
      { createIdempotencyKey: () => "must-not-replace-persisted-key" }
    );
    await flushMicrotasks();
    await expect(reloaded.current.confirm()).resolves.toEqual(confirmResponse());
    expect(reloadedConfirm).toHaveBeenCalledWith("draft-1", 3, "client-key-original");
    await reloaded.cleanup();

    const serverConfirm = vi.fn(async () => confirmResponse());
    const serverApi = makeApi({
      fetchAnalysisDraft: vi.fn(async () =>
        draft(3, {
          confirmed_revision_id: "semantic-1",
          confirmed_parse_revision_id: "semantic-semantic-1",
          confirm_idempotency_key: "server-key-authoritative"
        })
      ),
      confirmAnalysisDraft: serverConfirm
    });
    const serverReload = await renderAnalysisHook(serverApi, storage);
    await flushMicrotasks();
    await expect(serverReload.current.confirm()).resolves.toEqual(confirmResponse());
    expect(serverConfirm).toHaveBeenCalledWith("draft-1", 3, "server-key-authoritative");
  });

  it("reuses one in-memory idempotency key across response-loss retries without storage", async () => {
    const createKey = vi
      .fn<() => string>()
      .mockReturnValueOnce("memory-key-stable")
      .mockReturnValueOnce("must-not-be-used");
    const confirm = vi.fn<AnalysisDraftApi["confirmAnalysisDraft"]>(async () => {
      throw new Error("response lost");
    });
    const view = await renderAnalysisHook(
      makeApi({ confirmAnalysisDraft: confirm }),
      null,
      "project-1",
      revision(),
      { createIdempotencyKey: createKey }
    );
    await flushMicrotasks();

    let firstError: unknown;
    let secondError: unknown;
    await act(async () => {
      try {
        await view.current.confirm();
      } catch (error) {
        firstError = error;
      }
    });
    await act(async () => {
      try {
        await view.current.confirm();
      } catch (error) {
        secondError = error;
      }
    });
    expect(firstError).toBeInstanceOf(Error);
    expect(secondError).toBeInstanceOf(Error);
    expect((firstError as Error).message).toBe("response lost");
    expect((secondError as Error).message).toBe("response lost");
    expect(confirm.mock.calls.map((call) => call[2])).toEqual([
      "memory-key-stable",
      "memory-key-stable"
    ]);
    expect(createKey).toHaveBeenCalledOnce();
  });
});
