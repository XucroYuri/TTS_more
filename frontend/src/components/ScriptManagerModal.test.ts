import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

import { initI18n } from "../i18n";
import { ScriptManagerModal } from "./ScriptManagerModal";

initI18n();

describe("semantic analysis entry", () => {
  function props(overrides: Record<string, unknown> = {}) {
    return {
      open: true,
      variant: "inline",
      projects: [],
      currentProjectId: "alpha",
      selectedProjectId: "alpha",
      selectedProject: null,
      isSelectedProjectLoading: false,
      searchText: "",
      titleDraft: "Alpha",
      sourceDraft: "\u7532\uff1a\u5feb\u8dd1\uff01",
      newScriptTitle: "",
      newScriptSource: "",
      isCreatingScript: false,
      isSavingScript: false,
      isParsingScript: false,
      parseError: null,
      deletingProjectId: null,
      onClose: () => undefined,
      onSearchTextChange: () => undefined,
      onSelectProject: () => undefined,
      onOpenProject: () => undefined,
      onTitleDraftChange: () => undefined,
      onSourceDraftChange: () => undefined,
      onNewScriptTitleChange: () => undefined,
      onNewScriptSourceChange: () => undefined,
      onStartCreateScript: () => undefined,
      onCreateScript: () => undefined,
      onRenameScript: () => undefined,
      onSaveRevision: () => undefined,
      onParseRevision: () => undefined,
      onAnalyzeScript: () => undefined,
      onScriptFileSelected: () => undefined,
      onDismissParseError: () => undefined,
      onDeleteScript: () => undefined,
      ...overrides
    };
  }

  async function renderEntry(overrides: Record<string, unknown> = {}) {
    const dom = new JSDOM('<div id="root"></div>', { url: "http://localhost" });
    const previousWindow = globalThis.window;
    const previousDocument = globalThis.document;
    const previousHTMLElement = globalThis.HTMLElement;
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const root = createRoot(dom.window.document.getElementById("root")!);
    await act(async () => {
      root.render(createElement(ScriptManagerModal as never, props(overrides) as never));
    });
    return {
      dom,
      cleanup: async () => {
        await act(async () => root.unmount());
        Object.assign(globalThis, {
          window: previousWindow,
          document: previousDocument,
          HTMLElement: previousHTMLElement
        });
      }
    };
  }

  it("renders the exact txt/md file contract", async () => {
    const view = await renderEntry();
    try {
      const input = view.dom.window.document.querySelector<HTMLInputElement>('[data-action="script-file-input"]');
      expect(input?.accept).toBe(".txt,.md,text/plain,text/markdown");
      expect(input?.type).toBe("file");
    } finally {
      await view.cleanup();
    }
  });

  it("starts a new script and deletes a list row without opening it", async () => {
    const onStartCreateScript = vi.fn();
    const onDeleteScript = vi.fn();
    const onSelectProject = vi.fn();
    const view = await renderEntry({
      projects: [{ project_id: "alpha", title: "Alpha", default_language: "zh", line_count: 2 }],
      onStartCreateScript,
      onDeleteScript,
      onSelectProject
    });
    try {
      const addButton = view.dom.window.document.querySelector<HTMLButtonElement>('[data-action="add-script"]')!;
      await act(async () => addButton.click());
      expect(onStartCreateScript).toHaveBeenCalledOnce();

      const listTab = view.dom.window.document.querySelector<HTMLButtonElement>('[data-drawer-tab="list"]')!;
      await act(async () => listTab.click());
      const deleteButton = view.dom.window.document.querySelector<HTMLButtonElement>('[data-action="delete-script-alpha"]')!;
      await act(async () => deleteButton.click());
      expect(onDeleteScript).toHaveBeenCalledWith("alpha");
      expect(onSelectProject).not.toHaveBeenCalled();
    } finally {
      await view.cleanup();
    }
  });

  it("renders one primary semantic analysis action", async () => {
    const onAnalyzeScript = vi.fn();
    const view = await renderEntry({ onAnalyzeScript });
    try {
      const analyze = view.dom.window.document.querySelector<HTMLButtonElement>('[data-action="analyze-script"]')!;
      expect(analyze.classList.contains("primary-button")).toBe(true);
      expect(analyze.textContent).toContain("开始分析");
      await act(async () => analyze.click());
      expect(onAnalyzeScript).toHaveBeenCalledOnce();
      expect(view.dom.window.document.querySelector('[data-action="legacy-parse-script"]')).toBeNull();
      expect(view.dom.window.document.querySelector('[data-action="delete-script-bottom"]')).toBeNull();
    } finally {
      await view.cleanup();
    }
  });

  it("forwards the selected File without reading or rewriting it in the modal", async () => {
    const onScriptFileSelected = vi.fn();
    const view = await renderEntry({ onScriptFileSelected });
    try {
      const input = view.dom.window.document.querySelector<HTMLInputElement>('[data-action="script-file-input"]')!;
      const file = new view.dom.window.File(["\u7532\r\n\u53f0\u8bcd"], "scene.md", { type: "text/markdown" });
      Object.defineProperty(input, "files", { configurable: true, value: [file] });
      await act(async () => input.dispatchEvent(new view.dom.window.Event("change", { bubbles: true })));
      expect(onScriptFileSelected).toHaveBeenCalledWith(file, "existing", "picker");
    } finally {
      await view.cleanup();
    }
  });

  it("accepts a single markdown drop for the existing script without starting analysis", async () => {
    const onScriptFileSelected = vi.fn();
    const onAnalyzeScript = vi.fn();
    const view = await renderEntry({ onScriptFileSelected, onAnalyzeScript });
    try {
      const dropZone = view.dom.window.document.querySelector<HTMLElement>('[data-action="script-drop-zone"]')!;
      const file = new view.dom.window.File(["甲：快跑"], "scene.md", { type: "text/markdown" });
      const dragEnter = new view.dom.window.Event("dragenter", { bubbles: true, cancelable: true });
      Object.defineProperty(dragEnter, "dataTransfer", { value: { files: [file] } });
      await act(async () => dropZone.dispatchEvent(dragEnter));
      expect(dropZone.classList.contains("is-dragging")).toBe(true);

      const drop = new view.dom.window.Event("drop", { bubbles: true, cancelable: true });
      Object.defineProperty(drop, "dataTransfer", { value: { files: [file] } });
      await act(async () => dropZone.dispatchEvent(drop));
      expect(onScriptFileSelected).toHaveBeenCalledWith(file, "existing", "drop");
      expect(onAnalyzeScript).not.toHaveBeenCalled();
    } finally {
      await view.cleanup();
    }
  });

  it("keeps the file picker available when creating a script", async () => {
    const onScriptFileSelected = vi.fn();
    const view = await renderEntry({ selectedProjectId: null, currentProjectId: null, onScriptFileSelected });
    try {
      const editTab = view.dom.window.document.querySelector<HTMLButtonElement>('[data-drawer-tab="edit"]')!;
      await act(async () => editTab.click());
      const input = view.dom.window.document.querySelector<HTMLInputElement>('[data-action="script-file-input"]')!;
      expect(input.disabled).toBe(false);
      const file = new view.dom.window.File(["新剧本"], "new.txt", { type: "text/plain" });
      Object.defineProperty(input, "files", { configurable: true, value: [file] });
      await act(async () => input.dispatchEvent(new view.dom.window.Event("change", { bubbles: true })));
      expect(onScriptFileSelected).toHaveBeenCalledWith(file, "new", "picker");
    } finally {
      await view.cleanup();
    }
  });
});
