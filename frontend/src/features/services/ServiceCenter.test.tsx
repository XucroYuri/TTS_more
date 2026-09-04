import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ServiceCenter } from "./ServiceCenter";

describe("ServiceCenter", () => {
  let root: ReturnType<typeof createRoot> | null = null;
  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
  });

  it("is an accessible dialog with initial focus, close, and Escape behavior", async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>');
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      KeyboardEvent: dom.window.KeyboardEvent,
      Event: dom.window.Event,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const onClose = vi.fn();
    root = createRoot(dom.window.document.getElementById("root")!);
    await act(async () => root?.render(createElement(ServiceCenter, {
      title: "服务中心",
      refreshLabel: "刷新",
      closeLabel: "关闭",
      onRefresh: vi.fn(),
      onClose,
      children: createElement("div", null, "服务内容")
    })));

    expect(dom.window.document.querySelector('[role="dialog"]')?.getAttribute("aria-label")).toBe("服务中心");
    expect(dom.window.document.activeElement?.getAttribute("aria-label")).toBe("关闭");
    await act(async () => dom.window.document.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape" })));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
