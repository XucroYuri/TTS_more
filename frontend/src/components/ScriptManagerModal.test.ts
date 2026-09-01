import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

import { initI18n } from "../i18n";
import { canStartScriptParse, isCurrentScriptParseOperation, reduceScriptParseError, ScriptManagerModal, type ScriptParseError } from "./ScriptManagerModal";

initI18n();

describe("script parse error state", () => {
  const failedParse: ScriptParseError = {
    projectId: "alpha",
    message: "Unprocessable script source"
  };

  it("keeps a parse failure for its project until the user dismisses it", () => {
    expect(reduceScriptParseError(null, { type: "failed", error: failedParse })).toEqual(failedParse);
    expect(reduceScriptParseError(failedParse, { type: "dismissed" })).toBeNull();
  });

  it("clears a previous parse failure when another parse starts or succeeds", () => {
    expect(reduceScriptParseError(failedParse, { type: "started" })).toBeNull();
    expect(reduceScriptParseError(failedParse, { type: "succeeded" })).toBeNull();
  });

  it("does not expose a project error after selecting a different project", () => {
    expect(reduceScriptParseError(failedParse, { type: "project-selected", projectId: "beta" })).toBeNull();
    expect(reduceScriptParseError(failedParse, { type: "project-selected", projectId: "alpha" })).toEqual(failedParse);
  });

  it("rejects a parse result after the user has selected another project", () => {
    expect(isCurrentScriptParseOperation({
      operationToken: 4,
      activeOperationToken: 4,
      targetProjectId: "alpha",
      managedProjectId: "beta",
      currentProjectId: "beta"
    })).toBe(false);
  });

  it("accepts a selected script parse even when a different script is open in the workspace", () => {
    expect(isCurrentScriptParseOperation({
      operationToken: 4,
      activeOperationToken: 4,
      targetProjectId: "alpha",
      managedProjectId: "alpha",
      currentProjectId: "beta"
    })).toBe(true);
  });

  it("does not start a parse when confirmation is cancelled or becomes stale", () => {
    expect(canStartScriptParse({ confirmed: false, targetProjectId: "alpha", managedProjectId: "alpha" })).toBe(false);
    expect(canStartScriptParse({ confirmed: true, targetProjectId: "alpha", managedProjectId: "beta" })).toBe(false);
  });

  it("renders the inline parse error outside hidden tab panels with a dismiss control", () => {
    const markup = renderToStaticMarkup(createElement(ScriptManagerModal, {
      open: true,
      variant: "inline",
      projects: [],
      currentProjectId: "alpha",
      selectedProjectId: "alpha",
      selectedProject: null,
      isSelectedProjectLoading: false,
      searchText: "",
      titleDraft: "Alpha",
      sourceDraft: "Source",
      newScriptTitle: "",
      newScriptSource: "",
      isCreatingScript: false,
      isSavingScript: false,
      isParsingScript: false,
      parseError: failedParse,
      deletingProjectId: null,
      onClose: () => undefined,
      onSearchTextChange: () => undefined,
      onSelectProject: () => undefined,
      onOpenProject: () => undefined,
      onTitleDraftChange: () => undefined,
      onSourceDraftChange: () => undefined,
      onNewScriptTitleChange: () => undefined,
      onNewScriptSourceChange: () => undefined,
      onCreateScript: () => undefined,
      onRenameScript: () => undefined,
      onSaveRevision: () => undefined,
      onParseRevision: () => undefined,
      onAnalyzeScript: () => undefined,
      onScriptFileSelected: () => undefined,
      onDismissParseError: () => undefined,
      onDeleteScript: () => undefined
    }));
    const alertIndex = markup.indexOf('role="alert"');
    const bodyIndex = markup.indexOf('class="script-manager-body"');

    expect(markup).toContain("Unprocessable script source");
    expect(markup).toMatch(/role="alert"[\s\S]*?<button[^>]*aria-label=/);
    expect(alertIndex).toBeGreaterThan(-1);
    expect(alertIndex).toBeLessThan(bodyIndex);
  });

  it("invokes the dismiss callback when the alert close button is clicked", async () => {
    const dom = new JSDOM("<div id=\"root\"></div>", { url: "http://localhost" });
    const previousWindow = globalThis.window;
    const previousDocument = globalThis.document;
    const previousHTMLElement = globalThis.HTMLElement;
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const onDismissParseError = vi.fn();
    const root = createRoot(dom.window.document.getElementById("root")!);

    try {
      await act(async () => {
        root.render(createElement(ScriptManagerModal, {
          open: true,
          variant: "inline",
          projects: [],
          currentProjectId: "alpha",
          selectedProjectId: "alpha",
          selectedProject: null,
          isSelectedProjectLoading: false,
          searchText: "",
          titleDraft: "Alpha",
          sourceDraft: "Source",
          newScriptTitle: "",
          newScriptSource: "",
          isCreatingScript: false,
          isSavingScript: false,
          isParsingScript: false,
          parseError: failedParse,
          deletingProjectId: null,
          onClose: () => undefined,
          onSearchTextChange: () => undefined,
          onSelectProject: () => undefined,
          onOpenProject: () => undefined,
          onTitleDraftChange: () => undefined,
          onSourceDraftChange: () => undefined,
          onNewScriptTitleChange: () => undefined,
          onNewScriptSourceChange: () => undefined,
          onCreateScript: () => undefined,
          onRenameScript: () => undefined,
          onSaveRevision: () => undefined,
          onParseRevision: () => undefined,
          onAnalyzeScript: () => undefined,
          onScriptFileSelected: () => undefined,
          onDismissParseError,
          onDeleteScript: () => undefined
        }));
      });
      const dismissButton = dom.window.document.querySelector('[role="alert"] button')!;
      await act(async () => {
        dismissButton.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
      });
      expect(onDismissParseError).toHaveBeenCalledOnce();
    } finally {
      await act(async () => root.unmount());
      Object.assign(globalThis, {
        window: previousWindow,
        document: previousDocument,
        HTMLElement: previousHTMLElement
      });
    }
  });
});

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

  it("routes the primary analysis action without invoking legacy parse", async () => {
    const onAnalyzeScript = vi.fn();
    const onParseRevision = vi.fn();
    const view = await renderEntry({ onAnalyzeScript, onParseRevision });
    try {
      const analyze = view.dom.window.document.querySelector<HTMLButtonElement>('[data-action="analyze-script"]')!;
      expect(analyze.classList.contains("primary-button")).toBe(true);
      expect(analyze.textContent).toContain("\u5206\u6790\u5267\u672c");
      await act(async () => analyze.click());
      expect(onAnalyzeScript).toHaveBeenCalledOnce();
      expect(onParseRevision).not.toHaveBeenCalled();

      const legacy = view.dom.window.document.querySelector<HTMLButtonElement>('[data-action="legacy-parse-script"]')!;
      expect(legacy.textContent).toContain("\u63d0\u53d6\u53f0\u8bcd");
      await act(async () => legacy.click());
      expect(onParseRevision).toHaveBeenCalledOnce();
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
      expect(onScriptFileSelected).toHaveBeenCalledWith(file);
    } finally {
      await view.cleanup();
    }
  });
});
