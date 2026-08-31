import { act, createElement, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initI18n } from "../../i18n";
import type {
  AnalysisError,
  AnalysisRun,
  CharacterCandidate,
  DraftOperation,
  ScriptProject,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticAnnotation,
  SemanticConfirmResponse,
  SemanticUtterance
} from "../../types";
import { AnalysisResultsPane } from "./AnalysisResultsPane";
import { CharacterAliasEditor } from "./CharacterAliasEditor";
import {
  ScriptAnalysisWorkspace,
  summarizeConfirmableUtterances
} from "./ScriptAnalysisWorkspace";
import { applyDraftOperations, type AnalysisDraftApi } from "./useAnalysisDraft";

const timestamp = "2026-08-31T08:09:10.000Z";
const i18n = initI18n();

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

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const sourceRevision: ScriptRevision = {
  revision_id: "script-r8",
  source_markdown: "甲：第一句\n乙：第二句\n丙：第三句",
  source_sha256: "sha256-task-8",
  created_at: timestamp
};

function annotation(
  id: string,
  kind: SemanticAnnotation["kind"],
  start: number,
  end: number,
  status: SemanticAnnotation["status"] = "accepted"
): SemanticAnnotation {
  return {
    id,
    kind,
    span: {
      source_revision_id: sourceRevision.revision_id,
      source_sha256: sourceRevision.source_sha256!,
      start_utf16: start,
      end_utf16: end,
      text: sourceRevision.source_markdown.slice(start, end)
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
  name: string,
  overrides: Partial<CharacterCandidate> = {}
): CharacterCandidate {
  return {
    id,
    canonical_name: name,
    aliases: [`${name}老师`],
    supporting_annotation_ids: [],
    project_character_id: null,
    confidence: 0.9,
    status: "accepted",
    origin: "ai",
    ...overrides
  };
}

function utterance(
  id: string,
  dialogueAnnotationId: string,
  characterId: string | null,
  overrides: Partial<SemanticUtterance> = {}
): SemanticUtterance {
  return {
    id,
    dialogue_annotation_id: dialogueAnnotationId,
    speaker_annotation_id: null,
    character_candidate_id: characterId,
    emotion_evidence_annotation_ids: [],
    normalized_emotion: null,
    custom_emotion: null,
    emotion_intensity: null,
    emotion_origin: "none",
    language: "zh-CN",
    confidence: 0.95,
    uncertainty_codes: [],
    status: "accepted",
    ...overrides
  };
}

function draft(overrides: Partial<SemanticAnalysisDraft> = {}): SemanticAnalysisDraft {
  return {
    id: "draft-task-8",
    project_id: "project-task-8",
    source_revision_id: sourceRevision.revision_id,
    version: 3,
    annotations: [
      annotation("dialogue-3", "dialogue", 14, 17, "pending"),
      annotation("dialogue-1", "dialogue", 2, 5),
      annotation("dialogue-2", "dialogue", 8, 11),
      annotation("speaker-2", "speaker", 6, 7),
      annotation("emotion-2", "emotion_evidence", 8, 9)
    ],
    characters: [
      character("character-1", "甲", { aliases: ["阿甲", "老甲"] }),
      character("character-2", "乙", { aliases: ["阿乙"] }),
      character("character-3", "丙", { status: "pending" })
    ],
    utterances: [
      utterance("utterance-3", "dialogue-3", "character-3", {
        confidence: 0.9,
        status: "pending"
      }),
      utterance("utterance-1", "dialogue-1", "character-1"),
      utterance("utterance-2", "dialogue-2", "character-2", {
        speaker_annotation_id: "speaker-2",
        emotion_evidence_annotation_ids: ["emotion-2"],
        normalized_emotion: "other",
        custom_emotion: "低沉",
        emotion_intensity: 0.6,
        emotion_origin: "inferred",
        confidence: 0.5,
        uncertainty_codes: ["emotion_inferred"]
      })
    ],
    unresolved_candidates: [],
    warnings: [],
    provider: "test-provider",
    model: "test-model",
    prompt_version: "p8",
    contract_version: "v1",
    confirmed_revision_id: null,
    confirmed_parse_revision_id: null,
    confirmed_parse_fingerprint: null,
    confirm_idempotency_key: null,
    created_at: timestamp,
    updated_at: timestamp,
    ...overrides
  };
}

function run(
  status: AnalysisRun["status"] = "completed",
  overrides: Partial<AnalysisRun> = {}
): AnalysisRun {
  return {
    id: "run-task-8",
    project_id: "project-task-8",
    source_revision_id: sourceRevision.revision_id,
    draft_id: "draft-task-8",
    status,
    quality: status === "completed" ? "complete" : null,
    progress: status === "completed" ? 1 : 0.7,
    warnings: [],
    error: null,
    trace_id: "trace-task-8",
    created_at: timestamp,
    updated_at: timestamp,
    ...overrides
  };
}

const confirmedProject: ScriptProject = {
  title: "Server-authoritative project",
  default_language: "zh-CN",
  lines: []
};

function confirmResponse(serverDraft: SemanticAnalysisDraft): SemanticConfirmResponse {
  return {
    project: confirmedProject,
    semantic_revision: {
      id: "semantic-r8",
      project_id: serverDraft.project_id,
      source_revision_id: serverDraft.source_revision_id,
      annotations: serverDraft.annotations,
      characters: serverDraft.characters,
      utterances: serverDraft.utterances,
      unresolved_candidates: [],
      warnings: [],
      provider: serverDraft.provider,
      model: serverDraft.model,
      prompt_version: serverDraft.prompt_version,
      contract_version: serverDraft.contract_version,
      created_at: timestamp
    },
    parse_revision: {
      revision_id: "parse-r8",
      script_revision_id: sourceRevision.revision_id,
      provider: "semantic",
      warnings: [],
      project_characters: [],
      lines: [],
      created_at: timestamp
    }
  };
}

interface ApiHarness {
  api: AnalysisDraftApi;
  patch: ReturnType<typeof vi.fn<AnalysisDraftApi["patchAnalysisDraft"]>>;
  confirm: ReturnType<typeof vi.fn<AnalysisDraftApi["confirmAnalysisDraft"]>>;
}

function apiHarness(
  initialDraft: SemanticAnalysisDraft,
  analysisRun = run()
): ApiHarness {
  let serverDraft = initialDraft;
  const patch = vi.fn<AnalysisDraftApi["patchAnalysisDraft"]>(
    async (_draftId, expectedVersion, operations) => {
      serverDraft = {
        ...applyDraftOperations(serverDraft, operations),
        version: expectedVersion + 1,
        updated_at: timestamp
      };
      return serverDraft;
    }
  );
  const confirm = vi.fn<AnalysisDraftApi["confirmAnalysisDraft"]>(async () =>
    confirmResponse(serverDraft)
  );
  return {
    patch,
    confirm,
    api: {
      createAnalysisRun: vi.fn(async () => ({
        run_id: analysisRun.id,
        draft_id: analysisRun.draft_id,
        status: analysisRun.status,
        trace_id: analysisRun.trace_id
      })),
      fetchAnalysisRun: vi.fn(async () => analysisRun),
      fetchAnalysisDraft: vi.fn(async () => serverDraft),
      patchAnalysisDraft: patch,
      confirmAnalysisDraft: confirm
    }
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
  const dom = new JSDOM('<div id="root"></div>', {
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
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  await act(async () => root.render(element));
  const view: RenderedView = {
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

async function renderWorkspace(
  initialDraft = draft(),
  analysisRun = run(),
  overrides: Partial<Parameters<typeof ScriptAnalysisWorkspace>[0]> = {}
): Promise<RenderedView & ApiHarness> {
  const harness = apiHarness(initialDraft, analysisRun);
  const view = await renderElement(
    createElement(ScriptAnalysisWorkspace, {
      projectId: "project-task-8",
      sourceRevision,
      onConfirmed: () => undefined,
      onCancel: () => undefined,
      controllerOptions: {
        api: harness.api,
        storage: new MemoryStorage(),
        pollIntervalMs: 50,
        createIdempotencyKey: () => "confirm-task-8"
      },
      ...overrides
    })
  );
  await flushAsync();
  return { ...view, ...harness };
}

async function flushAsync(rounds = 8): Promise<void> {
  await act(async () => {
    for (let index = 0; index < rounds; index += 1) await Promise.resolve();
  });
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  });
}

async function changeValue(element: HTMLInputElement | HTMLSelectElement, value: string) {
  await act(async () => {
    const prototype =
      element instanceof window.HTMLSelectElement
        ? window.HTMLSelectElement.prototype
        : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new window.Event("input", { bubbles: true }));
    element.dispatchEvent(new window.Event("change", { bubbles: true }));
  });
}

async function selectSourceText(view: RenderedView, selectedText: string): Promise<void> {
  const sourceRoot = view.container.querySelector<HTMLElement>(".source-annotation-pane__source")!;
  const walker = view.dom.window.document.createTreeWalker(
    sourceRoot,
    view.dom.window.NodeFilter.SHOW_TEXT
  );
  const nodes: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node as Text);
  const exact = nodes.map((node) => node.data).join("");
  const start = exact.indexOf(selectedText);
  if (start < 0) throw new Error(`source text not found: ${selectedText}`);
  const end = start + selectedText.length;
  let cursor = 0;
  let startNode!: Text;
  let endNode!: Text;
  let startOffset = 0;
  let endOffset = 0;
  for (const node of nodes) {
    const next = cursor + node.data.length;
    if (!startNode && start >= cursor && start < next) {
      startNode = node;
      startOffset = start - cursor;
    }
    if (!endNode && end > cursor && end <= next) {
      endNode = node;
      endOffset = end - cursor;
    }
    cursor = next;
  }
  const range = view.dom.window.document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  const selection = view.dom.window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
  await act(async () =>
    sourceRoot.dispatchEvent(new view.dom.window.MouseEvent("mouseup", { bubbles: true }))
  );
}

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
});

afterEach(async () => {
  while (activeViews.length > 0) await activeViews.pop()!.cleanup();
  vi.restoreAllMocks();
});

describe("AnalysisResultsPane", () => {
  it("sorts by dialogue source span, filters deterministically, and renders all review evidence", async () => {
    const onFilterChange = vi.fn();
    const view = await renderElement(
      createElement(AnalysisResultsPane, {
        draft: draft(),
        filter: "all",
        onFilterChange,
        onOperations: () => undefined,
        onSelectUtterance: () => undefined
      })
    );

    expect(
      [...view.container.querySelectorAll<HTMLElement>(".analysis-result-card[data-utterance-id]")].map(
        (card) => card.dataset.utteranceId
      )
    ).toEqual(["utterance-1", "utterance-2", "utterance-3"]);
    const lowCard = view.container.querySelector<HTMLElement>('[data-utterance-id="utterance-2"]')!;
    expect(lowCard.textContent).toContain("乙");
    expect(lowCard.textContent).toContain("第二句");
    expect(lowCard.textContent).toContain("第");
    expect(lowCard.textContent).toContain("other");
    expect(lowCard.textContent).toContain("低沉");
    expect(lowCard.textContent).toContain("0.6");
    expect(lowCard.textContent).toContain("推断");
    expect(lowCard.textContent).toContain("50%");
    expect(lowCard.textContent).toContain("emotion_inferred");
    expect(lowCard.textContent).toContain("已接受");

    await click(view.container.querySelector('[data-filter="pending"]')!);
    await click(view.container.querySelector('[data-filter="low"]')!);
    expect(onFilterChange.mock.calls.map(([filter]) => filter)).toEqual(["pending", "low"]);
  });

  it("emits backend-valid atomic truth-table batches for accept, reject, and restore", async () => {
    const pendingUtterance = utterance("utterance-pending", "dialogue-pending", "character-pending", {
      status: "pending",
      uncertainty_codes: ["speaker_unknown", "emotion_inferred"]
    });
    const pendingDraft = draft({
      annotations: [annotation("dialogue-pending", "dialogue", 14, 17, "pending")],
      characters: [character("character-pending", "丙", { status: "pending" })],
      utterances: [pendingUtterance]
    });
    const onOperations = vi.fn<(operations: DraftOperation[]) => void>();
    const view = await renderElement(
      createElement(AnalysisResultsPane, {
        draft: pendingDraft,
        filter: "all",
        onFilterChange: () => undefined,
        onOperations,
        onSelectUtterance: () => undefined
      })
    );

    await click(view.container.querySelector('[data-utterance-action="accept"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "set_annotation_status",
        annotation_id: "dialogue-pending",
        status: "accepted"
      },
      {
        op: "set_character_status",
        character_id: "character-pending",
        status: "accepted"
      },
      {
        op: "update_utterance",
        utterance_id: "utterance-pending",
        utterance: { ...pendingUtterance, uncertainty_codes: ["emotion_inferred"] }
      },
      {
        op: "set_utterance_status",
        utterance_id: "utterance-pending",
        status: "accepted"
      }
    ]);

    await click(view.container.querySelector('[data-utterance-action="reject"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "set_annotation_status",
        annotation_id: "dialogue-pending",
        status: "rejected"
      },
      {
        op: "set_utterance_status",
        utterance_id: "utterance-pending",
        status: "rejected"
      }
    ]);

    await click(view.container.querySelector('[data-utterance-action="pending"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "set_annotation_status",
        annotation_id: "dialogue-pending",
        status: "pending"
      },
      {
        op: "set_utterance_status",
        utterance_id: "utterance-pending",
        status: "pending"
      }
    ]);
  });
});

describe("CharacterAliasEditor", () => {
  it("emits complete controlled operations for status, edit, merge, split, and reassignment", async () => {
    const onOperations = vi.fn<(operations: DraftOperation[]) => void>();
    const currentDraft = draft();
    const view = await renderElement(
      createElement(CharacterAliasEditor, {
        characters: currentDraft.characters,
        utterances: currentDraft.utterances,
        onOperations,
        createCharacterId: () => "character-split-stable"
      })
    );

    await click(view.container.querySelector('[data-character-action="reject"][data-character-id="character-1"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      { op: "set_character_status", character_id: "character-1", status: "rejected" }
    ]);
    await click(view.container.querySelector('[data-character-action="restore"][data-character-id="character-1"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      { op: "set_character_status", character_id: "character-1", status: "pending" }
    ]);

    await changeValue(
      view.container.querySelector<HTMLInputElement>('[data-character-name="character-1"]')!,
      "甲改名"
    );
    await changeValue(
      view.container.querySelector<HTMLInputElement>('[data-character-aliases="character-1"]')!,
      "阿甲, 老甲, 王"
    );
    await click(view.container.querySelector('[data-character-action="save"][data-character-id="character-1"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "upsert_character",
        character: {
          ...currentDraft.characters[0],
          canonical_name: "甲改名",
          aliases: ["阿甲", "老甲", "王"]
        }
      }
    ]);

    await changeValue(view.container.querySelector<HTMLSelectElement>('[data-merge-target]')!, "character-1");
    await changeValue(view.container.querySelector<HTMLSelectElement>('[data-merge-source]')!, "character-2");
    await click(view.container.querySelector('[data-character-action="merge"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "merge_characters",
        target_character_id: "character-1",
        source_character_ids: ["character-2"]
      }
    ]);

    await click(view.container.querySelector('[data-split-alias="老甲"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "split_alias",
        character_id: "character-1",
        alias: "老甲",
        character: {
          id: "character-split-stable",
          canonical_name: "老甲",
          aliases: [],
          supporting_annotation_ids: [],
          project_character_id: null,
          confidence: null,
          status: "accepted",
          origin: "human"
        }
      }
    ]);

    await changeValue(
      view.container.querySelector<HTMLSelectElement>('[data-utterance-character="utterance-1"]')!,
      "character-2"
    );
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "update_utterance",
        utterance_id: "utterance-1",
        utterance: { ...currentDraft.utterances[1], character_candidate_id: "character-2" }
      }
    ]);
    expect(onOperations).toHaveBeenCalledTimes(6);
  });

  it("never infers a merge merely because one controlled alias contains another", async () => {
    const onOperations = vi.fn<(operations: DraftOperation[]) => void>();
    await renderElement(
      createElement(CharacterAliasEditor, {
        characters: [
          character("character-wang", "王", { aliases: ["小王"] }),
          character("character-laowang", "老王", { aliases: ["王老师"] })
        ],
        utterances: [],
        onOperations
      })
    );
    expect(onOperations).not.toHaveBeenCalled();
  });

  it("creates human characters and keeps assignment and merge targets dependency-safe", async () => {
    const acceptedUtterance = utterance("utterance-accepted", "dialogue-1", "character-1");
    const pendingUtterance = utterance("utterance-pending", "dialogue-3", "character-1", {
      status: "pending",
      uncertainty_codes: ["speaker_unknown", "speaker_ambiguous", "emotion_inferred"]
    });
    const characters = [
      character("character-1", "甲"),
      character("character-2", "乙"),
      character("character-pending", "丙", { status: "pending" }),
      character("character-rejected", "丁", { status: "rejected" })
    ];
    const onOperations = vi.fn<(operations: DraftOperation[]) => void>();
    const view = await renderElement(
      createElement(CharacterAliasEditor, {
        characters,
        utterances: [acceptedUtterance, pendingUtterance],
        onOperations,
        createCharacterId: () => "character-human-stable"
      })
    );

    const assignmentOptions = [
      ...view.container.querySelector<HTMLSelectElement>(
        '[data-utterance-character="utterance-accepted"]'
      )!.options
    ].map((option) => option.value);
    expect(assignmentOptions).toEqual(["", "character-1", "character-2"]);
    const mergeTargets = [
      ...view.container.querySelector<HTMLSelectElement>('[data-merge-target]')!.options
    ].map((option) => option.value);
    expect(mergeTargets).toEqual(["character-1", "character-2"]);

    await changeValue(
      view.container.querySelector<HTMLSelectElement>(
        '[data-utterance-character="utterance-accepted"]'
      )!,
      ""
    );
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "update_utterance",
        utterance_id: "utterance-accepted",
        utterance: {
          ...acceptedUtterance,
          character_candidate_id: null,
          status: "pending",
          uncertainty_codes: ["speaker_unknown"]
        }
      }
    ]);

    await changeValue(
      view.container.querySelector<HTMLSelectElement>(
        '[data-utterance-character="utterance-pending"]'
      )!,
      "character-2"
    );
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "update_utterance",
        utterance_id: "utterance-pending",
        utterance: {
          ...pendingUtterance,
          character_candidate_id: "character-2",
          uncertainty_codes: ["emotion_inferred"]
        }
      }
    ]);

    await changeValue(view.container.querySelector<HTMLInputElement>('[data-new-character-name]')!, "戊");
    await click(view.container.querySelector('[data-character-action="create"]')!);
    expect(onOperations).toHaveBeenLastCalledWith([
      {
        op: "upsert_character",
        character: {
          id: "character-human-stable",
          canonical_name: "戊",
          aliases: [],
          supporting_annotation_ids: [],
          project_character_id: null,
          confidence: null,
          status: "accepted",
          origin: "human"
        }
      }
    ]);
  });
});

describe("ScriptAnalysisWorkspace", () => {
  it("wraps human annotations as create operations and links result/source focus both ways", async () => {
    const view = await renderWorkspace(draft(), run(), {
      createUtteranceId: () => "utterance-created-stable"
    });
    const scrollIntoView = vi.fn();
    Object.defineProperty(view.dom.window.HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView
    });

    const card = view.container.querySelector<HTMLElement>('[data-utterance-id="utterance-1"]')!;
    await click(card);
    const sourceSegment = view.container.querySelector<HTMLElement>(
      '.source-annotation-pane__segment[data-start-utf16="2"]'
    )!;
    expect(view.dom.window.document.activeElement).toBe(sourceSegment);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);

    await click(sourceSegment);
    expect(view.dom.window.document.activeElement).toBe(card);

    const sourceRoot = view.container.querySelector<HTMLElement>(".source-annotation-pane__source")!;
    const textNode = [...sourceRoot.childNodes].find((node) => node.textContent?.includes("第三句"));
    expect(textNode).toBeTruthy();
    const walker = view.dom.window.document.createTreeWalker(sourceRoot, view.dom.window.NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node as Text);
    const exact = nodes.map((node) => node.data).join("");
    const start = exact.indexOf("第三句");
    let cursor = 0;
    let startNode!: Text;
    let endNode!: Text;
    let startOffset = 0;
    let endOffset = 0;
    for (const node of nodes) {
      const next = cursor + node.data.length;
      if (!startNode && start >= cursor && start < next) {
        startNode = node;
        startOffset = start - cursor;
      }
      if (!endNode && start + 3 > cursor && start + 3 <= next) {
        endNode = node;
        endOffset = start + 3 - cursor;
      }
      cursor = next;
    }
    const range = view.dom.window.document.createRange();
    range.setStart(startNode, startOffset);
    range.setEnd(endNode, endOffset);
    const selection = view.dom.window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    await act(async () => sourceRoot.dispatchEvent(new view.dom.window.MouseEvent("mouseup", { bubbles: true })));
    await click([...view.container.querySelectorAll("button")].find((button) => button.textContent === "标记为台词")!);
    await flushAsync();
    const operations = view.patch.mock.calls.at(-1)?.[2];
    const createdAnnotation = operations?.[0].op === "create_annotation"
      ? operations[0].annotation
      : null;
    expect(createdAnnotation).toEqual(
      expect.objectContaining({
        kind: "dialogue",
        origin: "human",
        status: "accepted",
        span: expect.objectContaining({ start_utf16: 14, end_utf16: 17, text: "第三句" })
      })
    );
    expect(operations).toEqual([
      { op: "create_annotation", annotation: createdAnnotation },
      {
        op: "create_utterance",
        utterance: {
          id: "utterance-created-stable",
          dialogue_annotation_id: createdAnnotation?.id,
          speaker_annotation_id: null,
          character_candidate_id: null,
          emotion_evidence_annotation_ids: [],
          normalized_emotion: null,
          custom_emotion: null,
          emotion_intensity: null,
          emotion_origin: "none",
          language: "zh-CN",
          confidence: 1,
          uncertainty_codes: ["speaker_unknown"],
          status: "pending"
        }
      }
    ]);
  });

  it("builds an empty draft into one importable human utterance through real controls", async () => {
    const view = await renderWorkspace(
      draft({ annotations: [], characters: [], utterances: [], warnings: [] }),
      run(),
      {
        createUtteranceId: () => "utterance-human-stable",
        createCharacterId: () => "character-human-stable"
      }
    );

    await selectSourceText(view, "第一句");
    await click(
      [...view.container.querySelectorAll("button")].find(
        (button) => button.textContent === "标记为台词"
      )!
    );
    await flushAsync();
    expect(view.patch).toHaveBeenCalledTimes(1);
    const creationBatch = view.patch.mock.calls[0][2];
    expect(creationBatch.map((operation) => operation.op)).toEqual([
      "create_annotation",
      "create_utterance"
    ]);
    expect(
      creationBatch[1].op === "create_utterance" ? creationBatch[1].utterance : null
    ).toEqual(
      expect.objectContaining({
        id: "utterance-human-stable",
        character_candidate_id: null,
        confidence: 1,
        uncertainty_codes: ["speaker_unknown"],
        status: "pending"
      })
    );

    await changeValue(
      view.container.querySelector<HTMLInputElement>('[data-new-character-name]')!,
      "人工角色"
    );
    await click(view.container.querySelector('[data-character-action="create"]')!);
    await flushAsync();
    expect(view.patch.mock.calls[1][2]).toEqual([
      {
        op: "upsert_character",
        character: {
          id: "character-human-stable",
          canonical_name: "人工角色",
          aliases: [],
          supporting_annotation_ids: [],
          project_character_id: null,
          confidence: null,
          status: "accepted",
          origin: "human"
        }
      }
    ]);

    await changeValue(
      view.container.querySelector<HTMLSelectElement>(
        '[data-utterance-character="utterance-human-stable"]'
      )!,
      "character-human-stable"
    );
    await flushAsync();
    const assignment = view.patch.mock.calls[2][2];
    expect(assignment[0]).toEqual(
      expect.objectContaining({
        op: "update_utterance",
        utterance_id: "utterance-human-stable",
        utterance: expect.objectContaining({
          character_candidate_id: "character-human-stable",
          uncertainty_codes: []
        })
      })
    );

    await click(
      view.container.querySelector(
        '[data-utterance-action="accept"][data-utterance-id="utterance-human-stable"]'
      )!
    );
    await flushAsync();
    expect(view.patch.mock.calls[3][2].at(-1)).toEqual({
      op: "set_utterance_status",
      utterance_id: "utterance-human-stable",
      status: "accepted"
    });

    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    expect(view.container.querySelector('[role="dialog"]')?.textContent).toContain(
      "将导入 1 条台词"
    );
  });

  it("keeps structured 422 failures visible until explicit dismissal and copies diagnostics", async () => {
    const error: AnalysisError = {
      code: "semantic_contract_invalid",
      http_status: 422,
      stage: "validation",
      message: "模型输出不符合契约",
      retryable: false,
      run_id: "run-task-8",
      trace_id: "trace-422",
      occurred_at: timestamp,
      details: { field: "utterances" }
    };
    const copyDiagnostics = vi.fn(async (_value: string) => undefined);
    const view = await renderWorkspace(draft(), run("failed", { error }), { copyDiagnostics });
    const alert = view.container.querySelector<HTMLElement>('[data-analysis-error="run"]')!;
    expect(alert.textContent).toContain("semantic_contract_invalid");
    expect(alert.textContent).toContain("422");
    expect(alert.textContent).toContain("validation");
    expect(alert.textContent).toContain("模型输出不符合契约");
    expect(alert.textContent).toContain("trace-422");
    await click(alert.querySelector('[data-error-action="copy"]')!);
    expect(JSON.parse(copyDiagnostics.mock.calls[0][0])).toEqual(
      expect.objectContaining({ code: "semantic_contract_invalid", http_status: 422, trace_id: "trace-422" })
    );
    await click(alert.querySelector('[data-error-action="dismiss"]')!);
    expect(view.container.querySelector('[data-analysis-error="run"]')).toBeNull();
  });

  it("retains optimistic edits on a 409 and exposes conflict retry guidance", async () => {
    const view = await renderWorkspace();
    view.patch.mockRejectedValueOnce(
      new Error(
        JSON.stringify({
          detail: { code: "draft_version_conflict", message: "expected version 4" },
          status: 409
        })
      )
    );
    await click(
      view.container.querySelector(
        '[data-utterance-action="pending"][data-utterance-id="utterance-1"]'
      )!
    );
    await flushAsync();
    const card = view.container.querySelector<HTMLElement>('[data-utterance-id="utterance-1"]')!;
    expect(card.textContent).toContain("待确认");
    const conflict = view.container.querySelector<HTMLElement>('[data-analysis-error="conflict"]')!;
    expect(conflict.textContent).toContain("版本冲突");
    expect(conflict.textContent).toContain("重试");
    await click(conflict.querySelector('[data-error-action="retry"]')!);
    expect(card.textContent).toContain("待确认");
  });

  it("keeps interrupted and empty drafts open for manual review", async () => {
    const view = await renderWorkspace(
      draft({ annotations: [], characters: [], utterances: [] }),
      run("interrupted")
    );
    expect(view.container.textContent).toContain("已中断");
    expect(view.container.textContent).toContain("暂无分析结果");
    expect(view.container.querySelector(".source-annotation-pane__source")).not.toBeNull();
    expect(view.container.querySelector<HTMLButtonElement>('[data-action="confirm-open"]')!.disabled).toBe(false);
  });

  it("renders partial quality, run/draft warnings, and unresolved candidates even when empty", async () => {
    const view = await renderWorkspace(
      draft({
        annotations: [],
        characters: [],
        utterances: [],
        warnings: [
          {
            id: "draft-warning",
            code: "draft_review",
            message: "Draft warning visible",
            annotation_id: null,
            utterance_id: null,
            details: { source: "draft" }
          }
        ],
        unresolved_candidates: [
          {
            id: "unresolved-1",
            code: "source_anchor_ambiguous",
            candidate_type: "dialogue",
            message: "Unresolved source anchor visible",
            details: { start: 2 }
          }
        ]
      }),
      run("completed", {
        quality: "partial",
        warnings: [
          {
            id: "run-warning",
            code: "chunk_failed",
            message: "Run warning visible",
            annotation_id: null,
            utterance_id: null,
            details: { chunk: 2 }
          }
        ]
      })
    );

    expect(view.container.textContent).toContain("部分结果");
    expect(view.container.textContent).toContain("Run warning visible");
    expect(view.container.textContent).toContain("Draft warning visible");
    expect(view.container.textContent).toContain("Unresolved source anchor visible");
    expect(
      view.container.querySelector('[data-warning-source="run"] [data-warning-action="dismiss"]')
    ).toBeNull();
    await click(
      view.container.querySelector(
        '[data-warning-source="draft"] [data-warning-action="dismiss"]'
      )!
    );
    await flushAsync();
    expect(view.patch.mock.calls.at(-1)?.[2]).toEqual([
      { op: "dismiss_warning", warning_id: "draft-warning" }
    ]);
  });

  it("summarizes only accepted, grounded, assigned utterances and confirms once with server project", async () => {
    const currentDraft = draft();
    expect(summarizeConfirmableUtterances(currentDraft)).toEqual({ importable: 2, excluded: 1 });
    const patchGate = deferred<SemanticAnalysisDraft>();
    const confirmGate = deferred<SemanticConfirmResponse>();
    const onConfirmed = vi.fn();
    const view = await renderWorkspace(currentDraft, run(), { onConfirmed });
    view.patch.mockImplementationOnce(() => patchGate.promise);
    view.confirm.mockImplementationOnce(() => confirmGate.promise);

    await click(
      view.container.querySelector(
        '[data-utterance-action="pending"][data-utterance-id="utterance-1"]'
      )!
    );
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    const dialog = view.container.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(dialog.textContent).toContain("将导入 1 条台词");
    const confirmButton = dialog.querySelector('[data-action="confirm-submit"]')!;
    await act(async () => {
      confirmButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      confirmButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    });
    expect(view.confirm).not.toHaveBeenCalled();
    patchGate.resolve({
      ...applyDraftOperations(currentDraft, view.patch.mock.calls[0][2]),
      version: 4
    });
    await flushAsync();
    expect(view.confirm).toHaveBeenCalledTimes(1);
    confirmGate.resolve(confirmResponse(currentDraft));
    await flushAsync();
    expect(onConfirmed).toHaveBeenCalledTimes(1);
    expect(onConfirmed).toHaveBeenCalledWith(confirmedProject);
  });

  it("cancels the confirmation dialog without calling the server", async () => {
    const view = await renderWorkspace();
    await click(view.container.querySelector('[data-action="confirm-open"]')!);
    await click(view.container.querySelector('[data-action="confirm-cancel"]')!);
    expect(view.container.querySelector('[role="dialog"]')).toBeNull();
    expect(view.confirm).not.toHaveBeenCalled();
  });

  it("recovers a confirmed draft with the server key and forwards the authoritative project", async () => {
    const onConfirmed = vi.fn();
    const view = await renderWorkspace(
      draft({
        confirmed_revision_id: "semantic-r8",
        confirmed_parse_revision_id: "parse-r8",
        confirmed_parse_fingerprint: "fingerprint-r8",
        confirm_idempotency_key: "server-confirm-key"
      }),
      run(),
      { onConfirmed }
    );

    const recovery = view.container.querySelector<HTMLButtonElement>(
      '[data-action="confirm-recover"]'
    )!;
    expect(recovery.disabled).toBe(false);
    expect(recovery.textContent).toContain("恢复确认");
    await click(recovery);
    await flushAsync();
    expect(view.confirm).toHaveBeenCalledWith(
      "draft-task-8",
      3,
      "server-confirm-key"
    );
    expect(onConfirmed).toHaveBeenCalledWith(confirmedProject);
  });
});
