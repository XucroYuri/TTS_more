import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

import { VoiceConfigurationDrawer } from "./VoiceConfigurationDrawer";

describe("VoiceConfigurationDrawer", () => {
  it("does not display the backdrop when closed so the workspace remains interactive", async () => {
    const dom = new JSDOM('<div id="root"></div>', { url: "http://localhost" });
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      Node: dom.window.Node,
      Event: dom.window.Event,
      KeyboardEvent: dom.window.KeyboardEvent,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const root = createRoot(dom.window.document.getElementById("root")!);

    await act(async () => root.render(createElement(VoiceConfigurationDrawer, {
      open: false,
      title: "配音配置",
      closeLabel: "关闭",
      onClose: vi.fn(),
      children: createElement("p", null, "声音与模型")
    })));

    const backdrop = dom.window.document.querySelector<HTMLElement>(".voice-config-backdrop")!;
    expect(backdrop).not.toBeNull();
    expect(backdrop.style.display).toBe("none");
    await act(async () => root.unmount());
  });

  it("is an accessible dialog, focuses close, closes with Escape, and restores focus", async () => {
    const dom = new JSDOM('<button id="trigger">配置</button><div id="root"></div>', { url: "http://localhost" });
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      Node: dom.window.Node,
      Event: dom.window.Event,
      KeyboardEvent: dom.window.KeyboardEvent,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const trigger = dom.window.document.getElementById("trigger") as HTMLButtonElement;
    trigger.focus();
    const onClose = vi.fn();
    const root = createRoot(dom.window.document.getElementById("root")!);
    await act(async () => root.render(createElement(VoiceConfigurationDrawer, {
      open: true,
      title: "配音配置",
      closeLabel: "关闭",
      onClose,
      children: createElement("p", null, "声音与模型")
    })));

    expect(dom.window.document.querySelector('[role="dialog"]')?.getAttribute("aria-label")).toBe("配音配置");
    expect(dom.window.document.activeElement?.getAttribute("aria-label")).toBe("关闭");
    await act(async () => dom.window.document.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape" })));
    expect(onClose).toHaveBeenCalledOnce();

    await act(async () => root.render(createElement(VoiceConfigurationDrawer, {
      open: false,
      title: "配音配置",
      closeLabel: "关闭",
      onClose,
      children: null
    })));
    expect(dom.window.document.activeElement).toBe(trigger);
    await act(async () => root.unmount());
  });
});
