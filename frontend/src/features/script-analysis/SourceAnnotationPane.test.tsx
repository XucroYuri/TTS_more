import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

import type { AnnotationKind, ScriptRevision, SemanticAnnotation } from "../../types";
import { SourceAnnotationPane, type SourceAnnotationPaneProps } from "./SourceAnnotationPane";

const createdAt = "2026-08-31T08:09:10.000Z";

function revision(
  source: string,
  revisionId = "script-r007",
  sourceSha256 = "sha256-exact-source"
): ScriptRevision {
  return {
    revision_id: revisionId,
    source_markdown: source,
    source_sha256: sourceSha256,
    created_at: "2026-08-31T00:00:00.000Z"
  };
}

function annotation(
  id: string,
  kind: AnnotationKind,
  source: string,
  start: number,
  end: number,
  revisionId = "script-r007",
  sourceSha256 = "sha256-exact-source"
): SemanticAnnotation {
  return {
    id,
    kind,
    span: {
      source_revision_id: revisionId,
      start_utf16: start,
      end_utf16: end,
      text: source.slice(start, end),
      source_sha256: sourceSha256
    },
    origin: "ai",
    confidence: 0.9,
    status: "accepted",
    created_at: "2026-08-31T00:00:00.000Z",
    updated_at: "2026-08-31T00:00:00.000Z"
  };
}

interface RenderedPane {
  dom: JSDOM;
  container: HTMLElement;
  sourceRoot: HTMLElement;
  root: Root;
  rerender: (overrides: Partial<SourceAnnotationPaneProps>) => Promise<void>;
  cleanup: () => Promise<void>;
}

async function renderPane(overrides: Partial<SourceAnnotationPaneProps> = {}): Promise<RenderedPane> {
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

  const sourceRevision = overrides.sourceRevision ?? revision("胶布：快跑！");
  let props: SourceAnnotationPaneProps = {
    sourceRevision,
    annotations: [],
    onCreateAnnotation: () => undefined,
    onSelectAnnotation: () => undefined,
    ...overrides
  };
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  await act(async () => root.render(createElement(SourceAnnotationPane, props)));
  const sourceRoot = container.querySelector<HTMLElement>(".source-annotation-pane__source")!;

  return {
    dom,
    container,
    sourceRoot,
    root,
    rerender: async (nextOverrides) => {
      props = { ...props, ...nextOverrides };
      await act(async () => root.render(createElement(SourceAnnotationPane, props)));
    },
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
}

function selectVisibleText(view: RenderedPane, selectedText: string): void {
  const showText = view.dom.window.NodeFilter.SHOW_TEXT;
  const walker = view.dom.window.document.createTreeWalker(view.sourceRoot, showText);
  const textNodes: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) textNodes.push(node as Text);
  const exactSource = textNodes.map((node) => node.data).join("");
  const start = exactSource.indexOf(selectedText);
  if (start < 0) throw new Error(`Selection text not found: ${selectedText}`);
  const end = start + selectedText.length;

  let cursor = 0;
  let startNode: Text | null = null;
  let startOffset = 0;
  let endNode: Text | null = null;
  let endOffset = 0;
  for (const node of textNodes) {
    const nextCursor = cursor + node.data.length;
    if (!startNode && start >= cursor && start < nextCursor) {
      startNode = node;
      startOffset = start - cursor;
    }
    if (!endNode && end > cursor && end <= nextCursor) {
      endNode = node;
      endOffset = end - cursor;
    }
    cursor = nextCursor;
  }
  if (!startNode || !endNode) throw new Error("Selection boundaries could not be mapped in test");

  const range = view.dom.window.document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  const selection = view.dom.window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
}

function buttonWithText(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (candidate) => candidate.textContent === text
  );
  if (!button) throw new Error(`Button not found: ${text}`);
  return button;
}

async function capturePointerSelection(view: RenderedPane, selectedText: string): Promise<void> {
  selectVisibleText(view, selectedText);
  await act(async () => {
    view.sourceRoot.dispatchEvent(new view.dom.window.MouseEvent("mouseup", { bubbles: true }));
  });
}

async function pressKey(view: RenderedPane, key: string, shiftKey = false): Promise<void> {
  await act(async () => {
    view.sourceRoot.dispatchEvent(
      new view.dom.window.KeyboardEvent("keydown", { bubbles: true, key, shiftKey })
    );
    view.sourceRoot.dispatchEvent(
      new view.dom.window.KeyboardEvent("keyup", { bubbles: true, key, shiftKey })
    );
  });
}

async function click(view: RenderedPane, element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new view.dom.window.MouseEvent("click", { bubbles: true }));
  });
}

describe("SourceAnnotationPane", () => {
  it.each([
    ["标记为说话者", "speaker"],
    ["标记为情感证据", "emotion_evidence"],
    ["标记为台词", "dialogue"]
  ] as const)("creates an accepted human %s annotation from a pointer-selected DOM range", async (label, kind) => {
    const onCreateAnnotation = vi.fn();
    const view = await renderPane({
      onCreateAnnotation,
      createAnnotationId: () => `human-${kind}`,
      now: () => createdAt
    });

    try {
      await capturePointerSelection(view, "快跑！");

      const menu = view.container.querySelector('[role="toolbar"]')!;
      expect(menu).not.toBeNull();
      expect(buttonWithText(view.container, label).tagName).toBe("BUTTON");
      await click(view, buttonWithText(view.container, label));

      expect(onCreateAnnotation).toHaveBeenCalledWith({
        id: `human-${kind}`,
        kind,
        span: {
          source_revision_id: "script-r007",
          start_utf16: 3,
          end_utf16: 6,
          text: "快跑！",
          source_sha256: "sha256-exact-source"
        },
        origin: "human",
        confidence: null,
        status: "accepted",
        created_at: createdAt,
        updated_at: createdAt
      });
    } finally {
      await view.cleanup();
    }
  });

  it("creates an exact annotation from a real Unicode-safe keyboard selection", async () => {
    const source = "甲😀台词";
    const onCreateAnnotation = vi.fn();
    const view = await renderPane({
      sourceRevision: revision(source),
      onCreateAnnotation,
      createAnnotationId: () => "human-keyboard",
      now: () => createdAt
    });

    try {
      await act(async () => view.sourceRoot.focus());
      await pressKey(view, "Home");
      await pressKey(view, "ArrowRight");
      await pressKey(view, "ArrowRight", true);
      await pressKey(view, "ArrowRight", true);

      expect(view.dom.window.getSelection()!.toString()).toBe("😀台");
      expect(view.container.querySelector('[role="toolbar"]')).not.toBeNull();
      await click(view, buttonWithText(view.container, "标记为台词"));

      expect(onCreateAnnotation).toHaveBeenCalledWith(
        expect.objectContaining({
          id: "human-keyboard",
          kind: "dialogue",
          span: {
            source_revision_id: "script-r007",
            start_utf16: 1,
            end_utf16: 4,
            text: "😀台",
            source_sha256: "sha256-exact-source"
          }
        })
      );
      expect(view.dom.window.getSelection()!.rangeCount).toBe(0);
    } finally {
      await view.cleanup();
    }
  });

  it("supports End, ArrowLeft, and Shift+Home and clears native selection on cancel", async () => {
    const source = "甲😀台词";
    const view = await renderPane({ sourceRevision: revision(source) });

    try {
      await act(async () => view.sourceRoot.focus());
      await pressKey(view, "End");
      await pressKey(view, "ArrowLeft");
      await pressKey(view, "Home", true);

      expect(view.dom.window.getSelection()!.toString()).toBe("甲😀台");
      expect(view.container.querySelector('[role="toolbar"]')).not.toBeNull();
      await pressKey(view, "ArrowRight", true);
      expect(view.dom.window.getSelection()!.toString()).toBe("😀台");
      await click(view, buttonWithText(view.container, "取消 / Cancel"));
      expect(view.dom.window.getSelection()!.rangeCount).toBe(0);
      expect(view.container.querySelector('[role="toolbar"]')).toBeNull();
    } finally {
      await view.cleanup();
    }
  });

  it("invalidates a captured span and native selection across same-text and changed-text revisions", async () => {
    const source = "胶布：快跑！";
    const revisionA = revision(source, "script-a", "sha-a");
    const revisionB = revision(source, "script-b", "sha-b");
    const revisionC = revision("胶布：慢跑！", "script-c", "sha-c");
    const onCreateAnnotation = vi.fn();
    const view = await renderPane({ sourceRevision: revisionA, onCreateAnnotation });

    try {
      await capturePointerSelection(view, "快跑！");
      const staleButton = buttonWithText(view.container, "标记为台词");
      expect(view.dom.window.getSelection()!.toString()).toBe("快跑！");

      await view.rerender({ sourceRevision: revisionB });
      await click(view, staleButton);
      expect(onCreateAnnotation).not.toHaveBeenCalled();
      expect(view.container.querySelector('[role="toolbar"]')).toBeNull();
      expect(view.dom.window.getSelection()!.rangeCount).toBe(0);

      await capturePointerSelection(view, "快跑！");
      expect(view.container.querySelector('[role="toolbar"]')).not.toBeNull();
      await view.rerender({ sourceRevision: revisionC });
      expect(view.sourceRoot.textContent).toBe("胶布：慢跑！");
      expect(view.container.querySelector('[role="toolbar"]')).toBeNull();
      expect(view.dom.window.getSelection()!.rangeCount).toBe(0);
      expect(onCreateAnnotation).not.toHaveBeenCalled();
    } finally {
      await view.cleanup();
    }
  });

  it("revalidates a captured span identity immediately before annotation creation", async () => {
    const mutableRevision = revision("胶布：快跑！", "script-a", "sha-a");
    const onCreateAnnotation = vi.fn();
    const view = await renderPane({ sourceRevision: mutableRevision, onCreateAnnotation });

    try {
      await capturePointerSelection(view, "快跑！");
      mutableRevision.revision_id = "script-b";
      mutableRevision.source_sha256 = "sha-b";
      await click(view, buttonWithText(view.container, "标记为台词"));

      expect(onCreateAnnotation).not.toHaveBeenCalled();
      expect(view.container.querySelector('[role="toolbar"]')).toBeNull();
      expect(view.dom.window.getSelection()!.rangeCount).toBe(0);
    } finally {
      await view.cleanup();
    }
  });

  it("clears layered state and filters annotations from another revision or hash", async () => {
    const source = "角色台词";
    const revisionA = revision(source, "script-a", "sha-a");
    const revisionB = revision(source, "script-b", "sha-b");
    const oldSpeaker = annotation("old-speaker", "speaker", source, 0, 2, "script-a", "sha-a");
    const oldDialogue = annotation("old-dialogue", "dialogue", source, 0, 4, "script-a", "sha-a");
    const currentSpeaker = annotation(
      "current-speaker",
      "speaker",
      source,
      0,
      2,
      "script-b",
      "sha-b"
    );
    const onSelectAnnotation = vi.fn();
    const view = await renderPane({
      sourceRevision: revisionA,
      annotations: [oldSpeaker, oldDialogue],
      onSelectAnnotation
    });

    try {
      await click(view, view.sourceRoot.querySelector('[data-annotation-count="2"]')!);
      expect(view.container.querySelector('[role="dialog"]')).not.toBeNull();

      await view.rerender({
        sourceRevision: revisionB,
        annotations: [oldSpeaker, oldDialogue, currentSpeaker]
      });
      expect(view.container.querySelector('[role="dialog"]')).toBeNull();
      const currentButtons = view.sourceRoot.querySelectorAll<HTMLButtonElement>("button");
      expect(currentButtons).toHaveLength(1);
      await click(view, currentButtons[0]);
      expect(onSelectAnnotation).toHaveBeenCalledOnce();
      expect(onSelectAnnotation).toHaveBeenCalledWith("current-speaker");

      await expect(
        view.rerender({
          sourceRevision: revision("新台词", "script-c", "sha-c"),
          annotations: [oldSpeaker, oldDialogue]
        })
      ).resolves.toBeUndefined();
      expect(view.sourceRoot.textContent).toBe("新台词");
      expect(view.sourceRoot.querySelectorAll("button")).toHaveLength(0);
    } finally {
      await view.cleanup();
    }
  });

  it("preserves exact source whitespace and exposes visible non-color-only annotation semantics", async () => {
    const source = " 角色\r\n😀（惊喜）：台词  ";
    const annotations = [
      annotation("speaker-1", "speaker", source, 1, 3),
      annotation("emotion-1", "emotion_evidence", source, 8, 10),
      annotation("dialogue-1", "dialogue", source, 8, 14)
    ];
    const view = await renderPane({ sourceRevision: revision(source), annotations });

    try {
      expect(view.sourceRoot.textContent).toBe(source);
      expect(view.sourceRoot.style.whiteSpace).toBe("pre-wrap");
      expect(view.sourceRoot.tabIndex).toBe(0);
      expect(view.sourceRoot.getAttribute("aria-readonly")).toBe("true");
      expect(view.container.textContent).toContain("说话者 / Speaker");
      expect(view.container.textContent).toContain("情感证据 / Emotion evidence");
      expect(view.container.textContent).toContain("台词 / Dialogue");
      expect(view.container.querySelector(".annotation-layer--speaker")).not.toBeNull();
      expect(view.container.querySelector(".annotation-layer--emotion-evidence")).not.toBeNull();
      expect(view.container.querySelector(".source-annotation-pane__segment--dialogue")).not.toBeNull();
    } finally {
      await view.cleanup();
    }
  });

  it("lists every annotation as a real button when a layered segment is clicked", async () => {
    const source = "角色（惊喜）：台词";
    const annotations = [
      annotation("emotion-1", "emotion_evidence", source, 3, 5),
      annotation("dialogue-1", "dialogue", source, 3, 9)
    ];
    const onSelectAnnotation = vi.fn();
    const view = await renderPane({
      sourceRevision: revision(source),
      annotations,
      onSelectAnnotation
    });

    try {
      const layered = view.container.querySelector<HTMLButtonElement>('[data-annotation-count="2"]')!;
      expect(layered.tagName).toBe("BUTTON");
      await click(view, layered);

      const chooser = view.container.querySelector('[role="dialog"]')!;
      expect(chooser).not.toBeNull();
      const emotionButton = buttonWithText(
        chooser as HTMLElement,
        "情感证据 / Emotion evidence · emotion-1"
      );
      const dialogueButton = buttonWithText(chooser as HTMLElement, "台词 / Dialogue · dialogue-1");
      expect(emotionButton.tagName).toBe("BUTTON");
      expect(dialogueButton.tagName).toBe("BUTTON");

      await click(view, emotionButton);
      expect(onSelectAnnotation).toHaveBeenCalledWith("emotion-1");
    } finally {
      await view.cleanup();
    }
  });
});
