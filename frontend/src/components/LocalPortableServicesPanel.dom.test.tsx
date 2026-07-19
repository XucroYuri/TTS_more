// @vitest-environment jsdom

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resources } from "../i18n";
import type { LocalPortableService, PortableImportPlanResponse, WorkerHealth } from "../types";

const apiMocks = vi.hoisted(() => ({
  applyImport: vi.fn(),
  fetchLocalControlToken: vi.fn(),
  fetchLocalPortableServices: vi.fn(),
  fetchServicesStatus: vi.fn(),
  planImport: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    applyLocalPortableImport: apiMocks.applyImport,
    fetchLocalControlToken: apiMocks.fetchLocalControlToken,
    fetchLocalPortableServices: apiMocks.fetchLocalPortableServices,
    fetchServicesStatus: apiMocks.fetchServicesStatus,
    planLocalPortableImport: apiMocks.planImport,
  };
});

import { LocalPortableServicesPanel } from "./LocalPortableServicesPanel";

const services: LocalPortableService[] = [
  {
    service_id: "portable-gpt",
    component: "gpt-sovits",
    package_id: "gpt-main",
    display_name: "GPT-SoVITS",
    base_url: "http://127.0.0.1:9880",
    mode: "local",
    network_scope: "localhost",
    managed: true,
    setup_state: "ready",
    package_root: "D:/Portable/GPT-SoVITS",
    build_id: "gpt-build-2",
    port_override: null,
  },
  {
    service_id: "portable-index",
    component: "indextts",
    package_id: "index-main",
    display_name: "IndexTTS",
    base_url: "http://127.0.0.1:9881",
    mode: "local",
    network_scope: "localhost",
    managed: true,
    setup_state: "ready",
    package_root: "D:/Portable/IndexTTS",
    build_id: "index-build-2",
    port_override: null,
  },
];

const stoppedRuntimes: WorkerHealth[] = [
  { service_id: "portable-gpt", engine: "gpt-sovits", ready: false, supervisor_state: "stopped" },
  { service_id: "portable-index", engine: "indextts", ready: false, supervisor_state: "stopped" },
];

function planned(id: string, expires = 5): PortableImportPlanResponse {
  return {
    plan_id: id,
    plan_digest: "a".repeat(64),
    expires_in_seconds: expires,
    user_file_count: 1,
    user_bytes: 10,
    reusable_assets: ["models/base.bin"],
    reusable_asset_bytes: 20,
    skipped_assets: [],
    already_present: [],
    old_package_preserved: true,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function button(scope: ParentNode, name: string): HTMLButtonElement {
  const match = Array.from(scope.querySelectorAll("button")).find((item) => item.textContent?.trim() === name);
  if (!match) throw new Error(`button not found: ${name}`);
  return match;
}

function card(container: HTMLElement, component: string): HTMLElement {
  const match = container.querySelector<HTMLElement>(`[data-portable-component="${component}"]`);
  if (!match) throw new Error(`card not found: ${component}`);
  return match;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("local portable import DOM lifecycle", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  let currentRuntimes: WorkerHealth[];
  let onServicesStatusRefresh: () => Promise<void>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = null;
    currentRuntimes = stoppedRuntimes;
    onServicesStatusRefresh = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
    apiMocks.fetchLocalControlToken.mockReset().mockResolvedValue("memory-token");
    apiMocks.fetchLocalPortableServices.mockReset().mockResolvedValue({ services });
    apiMocks.fetchServicesStatus.mockReset().mockImplementation(async () => ({
      services: currentRuntimes,
      hardware: {},
    }));
    apiMocks.planImport.mockReset();
    apiMocks.applyImport.mockReset().mockResolvedValue({
      copied_user_files: 1,
      reused_assets: ["models/base.bin"],
      skipped_assets: [],
      already_present: [],
    });
  });

  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    container.remove();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  async function renderPanel() {
    const instance = createInstance();
    await instance.init({ lng: "en-US", resources: { "en-US": { translation: resources["en-US"] } } });
    root = createRoot(container);
    await act(async () => {
      root?.render(createElement(
        I18nextProvider,
        { i18n: instance },
        createElement(LocalPortableServicesPanel, { initialServices: services, onServicesStatusRefresh }),
      ));
    });
    await flush();
  }

  it("moves focus into ready confirmation, announces it, and restores the import trigger on cancel", async () => {
    const pending = deferred<PortableImportPlanResponse>();
    apiMocks.planImport.mockReturnValue(pending.promise);
    await renderPanel();
    const gpt = card(container, "gpt-sovits");
    const trigger = button(gpt, "Import previous version");
    trigger.focus();
    await act(async () => trigger.click());

    await act(async () => pending.resolve(planned("focus-plan")));
    await flush();
    const confirm = button(gpt, "Confirm import");
    expect(document.activeElement).toBe(confirm);
    expect(gpt.querySelector('[role="status"]')?.textContent).toContain("Ready to import");
    expect(container.querySelector(".portable-panel-live")?.textContent).toContain("Ready to import");

    await act(async () => button(gpt, "Cancel").click());
    expect(document.activeElement).toBe(button(gpt, "Import previous version"));
  });

  it("expires once at the deadline, clears confirmation, and cleans the timer on unmount", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1000);
    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    apiMocks.planImport.mockResolvedValue(planned("expiring-plan", 1));
    await renderPanel();
    const gpt = card(container, "gpt-sovits");
    setTimeoutSpy.mockClear();
    clearTimeoutSpy.mockClear();
    await act(async () => button(gpt, "Import previous version").click());
    await flush();
    expect(button(gpt, "Confirm import")).toBeTruthy();
    expect(setTimeoutSpy.mock.calls.filter((call) => call[1] === 1000)).toHaveLength(1);

    await act(async () => vi.advanceTimersByTime(1000));
    expect(gpt.textContent).toContain("expired or became unavailable");
    expect(gpt.querySelectorAll(".portable-import-error")).toHaveLength(1);
    expect(gpt.textContent).not.toContain("Confirm import");
    await act(async () => vi.advanceTimersByTime(5000));
    expect(gpt.querySelectorAll(".portable-import-error")).toHaveLength(1);
    expect(setTimeoutSpy.mock.calls.filter((call) => call[1] === 1000)).toHaveLength(1);

    apiMocks.planImport.mockResolvedValue(planned("unmounted-plan", 10));
    setTimeoutSpy.mockClear();
    clearTimeoutSpy.mockClear();
    await act(async () => button(gpt, "Choose previous version again").click());
    await flush();
    const deadlineCallIndex = setTimeoutSpy.mock.calls.findIndex((call) => call[1] === 10000);
    expect(deadlineCallIndex).toBeGreaterThanOrEqual(0);
    expect(setTimeoutSpy.mock.calls.filter((call) => call[1] === 10000)).toHaveLength(1);
    const deadlineTimer = setTimeoutSpy.mock.results[deadlineCallIndex]?.value;
    await act(async () => root?.unmount());
    root = null;
    expect(clearTimeoutSpy).toHaveBeenCalledWith(deadlineTimer);
  });

  it("aborts an invalidated picker and ignores its stale success while a new attempt owns the card", async () => {
    const first = deferred<PortableImportPlanResponse>();
    const second = deferred<PortableImportPlanResponse>();
    const signals: Array<AbortSignal | undefined> = [];
    apiMocks.planImport
      .mockImplementationOnce((_component, _token, signal) => {
        signals.push(signal);
        return first.promise;
      })
      .mockImplementationOnce((_component, _token, signal) => {
        signals.push(signal);
        return second.promise;
      });
    await renderPanel();
    const gpt = card(container, "gpt-sovits");
    const importTrigger = button(gpt, "Import previous version");
    importTrigger.focus();
    await act(async () => importTrigger.click());

    currentRuntimes = [
      { ...stoppedRuntimes[0], ready: true, supervisor_state: "running" },
      stoppedRuntimes[1],
    ];
    await act(async () => button(container, "Refresh").click());
    await flush();
    expect(signals[0]?.aborted).toBe(true);
    const invalidatedAlert = gpt.querySelector<HTMLElement>(".portable-import-error");
    expect(invalidatedAlert).toBeTruthy();
    expect(button(gpt, "Choose previous version again").disabled).toBe(true);
    expect(document.activeElement).toBe(invalidatedAlert);
    expect(document.activeElement).not.toBe(document.body);

    currentRuntimes = stoppedRuntimes;
    await act(async () => button(container, "Refresh").click());
    await flush();
    await act(async () => button(gpt, "Choose previous version again").click());
    await act(async () => first.resolve(planned("stale-plan")));
    await flush();
    expect(gpt.textContent).not.toContain("Confirm import");
    await act(async () => second.resolve(planned("current-plan")));
    await flush();
    expect(gpt.textContent).toContain("Confirm import");
    expect(signals[1]?.aborted).toBe(false);
  });

  it("keeps a sibling confirmation intact when another component identity is invalidated", async () => {
    const gptPending = deferred<PortableImportPlanResponse>();
    apiMocks.planImport.mockImplementation((component) => (
      component === "indextts" ? Promise.resolve(planned("index-plan")) : gptPending.promise
    ));
    await renderPanel();
    const index = card(container, "indextts");
    const gpt = card(container, "gpt-sovits");
    await act(async () => button(index, "Import previous version").click());
    await flush();
    expect(index.textContent).toContain("Confirm import");
    await act(async () => button(gpt, "Import previous version").click());

    currentRuntimes = [
      { ...stoppedRuntimes[0], ready: true, supervisor_state: "running" },
      stoppedRuntimes[1],
    ];
    await act(async () => button(container, "Refresh").click());
    await flush();
    expect(index.textContent).toContain("Confirm import");
  });

  it("keeps successful apply status when best-effort refresh fails", async () => {
    apiMocks.planImport.mockResolvedValue(planned("apply-plan"));
    apiMocks.fetchLocalPortableServices
      .mockReset()
      .mockResolvedValueOnce({ services })
      .mockRejectedValueOnce(new Error("refresh failed"));
    await renderPanel();
    const gpt = card(container, "gpt-sovits");
    await act(async () => button(gpt, "Import previous version").click());
    await flush();
    await act(async () => button(gpt, "Confirm import").click());
    await flush();

    const successStatus = gpt.querySelector<HTMLElement>(".portable-import-success");
    expect(successStatus?.textContent).toContain("Import complete");
    expect(document.activeElement).toBe(successStatus);
    expect(document.activeElement).not.toBe(document.body);
    expect(container.querySelector(".portable-panel-live")?.textContent).toContain("Import complete");
    expect(container.querySelector(".portable-panel-live")?.textContent).not.toContain("did not complete");
    expect(apiMocks.applyImport).toHaveBeenCalledTimes(1);
  });
});
