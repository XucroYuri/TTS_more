import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkbenchShell } from "./WorkbenchShell";

describe("WorkbenchShell", () => {
  let root: ReturnType<typeof createRoot> | null = null;
  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
  });

  it("renders stage navigation and collapsible notifications", async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>');
    Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
    const onStageChange = vi.fn();
    root = createRoot(dom.window.document.getElementById("root")!);
    await act(async () => root?.render(createElement(WorkbenchShell, {
      stage: "tts",
      sidebar: createElement("span", null, "剧本"),
      warnings: [{ id: "warning-1", title: "草稿警告", content: "需要确认" }],
      onStageChange,
      children: createElement("section", null, "台词")
    })));

    expect(dom.window.document.querySelector('[aria-current="step"]')?.textContent).toBe("配音工作台");
    expect(dom.window.document.querySelector("details")?.open).toBe(false);
    const analysis = [...dom.window.document.querySelectorAll("button")].find((button) => button.textContent === "剧本分析");
    await act(async () => analysis?.click());
    expect(onStageChange).toHaveBeenCalledWith("analysis");
  });
});
