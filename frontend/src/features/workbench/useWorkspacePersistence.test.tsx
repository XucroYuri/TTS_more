import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Character, ScriptProject } from "../../types";
import {
  useWorkspacePersistence,
  type WorkspacePersistenceController,
  type WorkspaceSnapshot
} from "./useWorkspacePersistence";

const project: ScriptProject = { title: "demo", default_language: "zh-CN", lines: [] };
const characters: Character[] = [];

function snapshot(projectId: string, title = "demo"): WorkspaceSnapshot {
  return { projectId, project: { ...project, title }, characters };
}

describe("useWorkspacePersistence", () => {
  let root: ReturnType<typeof createRoot> | null = null;
  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
    vi.useRealTimers();
  });

  async function mount(
    persist: (value: WorkspaceSnapshot) => Promise<void>
  ): Promise<WorkspacePersistenceController> {
    const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    let controller: WorkspacePersistenceController | null = null;
    function Harness() {
      controller = useWorkspacePersistence({ persist, debounceMs: 700 });
      return null;
    }
    root = createRoot(dom.window.document.getElementById("root")!);
    await act(async () => root?.render(createElement(Harness)));
    return controller!;
  }

  it("does not persist hydration updates or an unchanged render", async () => {
    vi.useFakeTimers();
    const persist = vi.fn(async () => undefined);
    const controller = await mount(persist);

    controller.hydrate(snapshot("a"));
    controller.hydrate(snapshot("a", "mapped"));
    expect(controller.schedule(snapshot("a", "mapped"))).toBe(false);
    await vi.advanceTimersByTimeAsync(1000);

    expect(persist).not.toHaveBeenCalled();
  });

  it("debounces a user edit and persists it once", async () => {
    vi.useFakeTimers();
    const persist = vi.fn(async () => undefined);
    const controller = await mount(persist);
    controller.hydrate(snapshot("a"));

    expect(controller.schedule(snapshot("a", "edited"))).toBe(true);
    await vi.advanceTimersByTimeAsync(700);

    expect(persist).toHaveBeenCalledTimes(1);
    expect(persist).toHaveBeenCalledWith(snapshot("a", "edited"));
  });

  it("does not let a completed stale save become the new baseline after a switch", async () => {
    vi.useFakeTimers();
    let release!: () => void;
    const persist = vi.fn(() => new Promise<void>((resolve) => { release = resolve; }));
    const controller = await mount(persist);
    controller.hydrate(snapshot("a"));
    controller.schedule(snapshot("a", "edited"));
    const saving = controller.flush("a");
    await Promise.resolve();

    controller.cancel("a");
    controller.hydrate(snapshot("b"));
    release();
    await saving;
    controller.schedule(snapshot("a", "edited"));
    await vi.advanceTimersByTimeAsync(700);

    expect(persist).toHaveBeenCalledTimes(2);
  });
});
