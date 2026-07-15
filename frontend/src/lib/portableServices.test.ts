import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";

import { LocalPortableServicesPanel } from "../components/LocalPortableServicesPanel";
import { resources } from "../i18n";
import {
  createPortableOperationPoller,
  createPortableActionConvergencePoller,
  mergePortableEvents,
  portableErrorMessageKey,
  portablePhaseAfterAction,
  portablePhaseLabelKey,
  portableServiceCards,
  shouldRevealManualProxy,
  validatePortableProxyUrl,
  PORTABLE_REPAIR_CONVERGENCE_TIMEOUT_MS,
  PORTABLE_STOP_CONVERGENCE_TIMEOUT_MS,
  withControlTokenRetry,
} from "./portableServices";
import { PortableApiError } from "../api";
import type { PortableActionResponse, PortableOperationEvent, PortableOperationLogsResponse, PortableOperationResponse } from "../types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe("portable service card rules", () => {
  it("keeps the three components independent and disables LAN actions", () => {
    const cards = portableServiceCards([
      { service_id: "gpt", component: "gpt-sovits", managed: true, setup_state: "ready", package_root: "D:/GPT" },
      { component: "indextts", managed: false, status: "stopped", mode: "external", network_scope: "lan" },
    ], { runtimes: [{ service_id: "gpt", ready: true, supervisor_state: "running", can_start: false }] });

    expect(cards.map((card) => card.component)).toEqual(["gpt-sovits", "indextts", "cosyvoice"]);
    expect(cards[0].actions.stop).toBe(true);
    expect(cards[1].actions).toMatchObject({ browse: false, start: false, stop: false, repair: false, openFolder: false });
    expect(cards[1].disabledReason).toBe("lan");
    expect(cards[2].status).toBe("not_configured");
    expect(cards[2].actions.browse).toBe(true);
  });

  it("renders three independent semantic cards including an explained disabled LAN card", async () => {
    const instance = createInstance();
    await instance.init({ lng: "zh-CN", resources: { "zh-CN": { translation: resources["zh-CN"] } } });
    const html = renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: instance },
        createElement(LocalPortableServicesPanel, {
          initialServices: [{
            service_id: "lan-index",
            component: "indextts",
            package_id: null,
            display_name: "LAN Index",
            base_url: "http://192.168.1.2:9881",
            mode: "external",
            network_scope: "lan",
            managed: false,
            setup_state: "endpoint_unreachable",
            package_root: null,
            build_id: null,
            port_override: null,
          }],
          onServicesStatusRefresh: async () => undefined,
        }),
      ),
    );

    expect(html.match(/data-portable-component=/g)).toHaveLength(3);
    expect(html).toContain("GPT-SoVITS");
    expect(html).toContain("IndexTTS");
    expect(html).toContain("CosyVoice");
    expect(html).toContain("局域网服务只能使用，不能从这里启动、停止或修复");
    expect(html).toContain("aria-live=\"polite\"");
  });

  it("makes only the busy card inert and preserves actions on its siblings", () => {
    const cards = portableServiceCards(
      [
        { service_id: "gpt", component: "gpt-sovits", managed: true, status: "stopped", package_root: "D:/GPT" },
        { service_id: "index", component: "indextts", managed: true, status: "stopped", package_root: "D:/Index" },
      ],
      {
        phases: { "gpt-sovits": "downloading" },
        runtimes: [
          { service_id: "gpt", ready: false, supervisor_state: "stopped", can_start: true },
          { service_id: "index", ready: false, supervisor_state: "stopped", can_start: true },
        ],
      },
    );

    expect(cards[0].status).toBe("downloading");
    expect(Object.values(cards[0].actions)).not.toContain(true);
    expect(cards[1].actions.start).toBe(true);
  });

  it("uses stable phase, error and proxy reveal rules", () => {
    expect(portablePhaseLabelKey("installing")).toBe("portableServices.phase.installing");
    expect(portableErrorMessageKey("CUDA_PROBE_FAILED")).toBe("portableServices.error.cudaProbeFailed");
    expect(portableErrorMessageKey("A_NEW_BACKEND_CODE")).toBe("portableServices.error.unknown");
    expect(shouldRevealManualProxy("ALL_LOCKED_SOURCES_EXHAUSTED")).toBe(true);
    expect(shouldRevealManualProxy("DOWNLOAD_NETWORK_INTERRUPTED")).toBe(false);
    expect(shouldRevealManualProxy(undefined)).toBe(false);
    expect(validatePortableProxyUrl("http://127.0.0.1:10808")).toBe(true);
    expect(validatePortableProxyUrl("https://user:pass@proxy.example:443")).toBe(true);
    expect(validatePortableProxyUrl("socks5://127.0.0.1:1080")).toBe(false);
    expect(validatePortableProxyUrl("http://proxy.example/path?secret=1")).toBe(false);
  });

  it("keeps stop and repair nonterminal until real status convergence", () => {
    expect(portablePhaseAfterAction("stop", { status: "stopping" })).toBe("stopping");
    expect(portablePhaseAfterAction("repair", { status: "repairing" })).toBe("repairing");
    expect(portablePhaseAfterAction("open-folder", { status: "opened" })).toBeNull();
    expect(portablePhaseAfterAction("start", { status: "ready" })).toBe("ready");
    expect(portablePhaseAfterAction("start", { status: "starting", operation_id: "op" })).toBe("starting");
  });

  it("treats setup_state ready as installed and derives runtime actions by service_id", () => {
    const services = [{
      service_id: "portable-gpt",
      component: "gpt-sovits" as const,
      managed: true,
      setup_state: "ready",
      package_root: "D:/GPT",
      base_url: "http://127.0.0.1:9880",
    }];
    const stopped = portableServiceCards(services, {
      runtimes: [{ service_id: "portable-gpt", ready: false, supervisor_state: "stopped", can_start: true }],
    })[0];
    const running = portableServiceCards(services, {
      // A different service must not affect this card; matching is by service_id.
      runtimes: [
        { service_id: "unrelated-gpt", ready: true, supervisor_state: "running", can_start: false },
        { service_id: "portable-gpt", ready: true, supervisor_state: "running", can_start: false },
      ],
    })[0];
    const runningButUnready = portableServiceCards(services, {
      runtimes: [{ service_id: "portable-gpt", ready: false, supervisor_state: "running", can_start: false }],
    })[0];

    expect(stopped.status).toBe("stopped");
    expect(stopped.actions).toMatchObject({ start: true, stop: false, openService: false });
    expect(running.status).toBe("ready");
    expect(running.actions).toMatchObject({ start: false, stop: true, openService: true });
    expect(runningButUnready.status).toBe("checking");
    expect(runningButUnready.actions).toMatchObject({ start: false, stop: true, openService: false });
  });

  it("models the real completed and open_folder wire response values", () => {
    const repair = {
      component: "gpt-sovits",
      action: "repair",
      status: "completed",
    } satisfies PortableActionResponse;
    const folder = {
      component: "gpt-sovits",
      action: "open_folder",
      status: "opened",
    } satisfies PortableActionResponse;
    expect([repair.status, folder.action]).toEqual(["completed", "open_folder"]);
  });
});

describe("portable control token retry", () => {
  it("invalidates and reacquires once after a 403", async () => {
    const tokenCalls: boolean[] = [];
    const requestTokens: string[] = [];
    const result = await withControlTokenRetry(
      async (force) => {
        tokenCalls.push(force);
        return force ? "fresh-token" : "old-token";
      },
      async (token) => {
        requestTokens.push(token);
        if (token === "old-token") throw new PortableApiError(403, "LOCAL_CONTROL_FORBIDDEN", "expired");
        return "ok";
      },
    );

    expect(result).toBe("ok");
    expect(tokenCalls).toEqual([false, true]);
    expect(requestTokens).toEqual(["old-token", "fresh-token"]);
  });

  it("never loops after the replacement token is also rejected", async () => {
    const request = vi.fn(async () => {
      throw new PortableApiError(403, "LOCAL_CONTROL_FORBIDDEN", "still forbidden");
    });

    await expect(withControlTokenRetry(async (force) => (force ? "b" : "a"), request)).rejects.toMatchObject({ status: 403 });
    expect(request).toHaveBeenCalledTimes(2);
  });
});

describe("portable operation polling", () => {
  it("polls at 500ms without overlap and stops at a terminal phase", async () => {
    const statusOne = deferred<PortableOperationResponse>();
    const schedules: Array<{ callback: () => void; delay: number }> = [];
    const snapshots: string[] = [];
    const status = vi
      .fn<(signal: AbortSignal) => Promise<PortableOperationResponse>>()
      .mockImplementationOnce(() => statusOne.promise)
      .mockResolvedValueOnce({ status: "ready", operation: { operation_id: "op", status: "ready" }, running: true });
    const logs = vi.fn<(afterSeq: number, signal: AbortSignal) => Promise<PortableOperationLogsResponse>>()
      .mockImplementation(async (afterSeq) => ({
        status: "checking",
        events: afterSeq < 1
          ? [{ seq: 1, timestamp: "2026-07-15T00:00:00Z", phase: "checking", message: "safe" }]
          : [],
        next_seq: Math.max(afterSeq, 1),
      }));
    const onTerminal = vi.fn();
    const poller = createPortableOperationPoller({
      pollStatus: status,
      pollLogs: logs,
      onSnapshot: (snapshot) => snapshots.push(snapshot.phase),
      onTerminal,
      schedule: (callback, delay) => {
        schedules.push({ callback, delay });
        return schedules.length;
      },
      clearSchedule: vi.fn(),
    });

    poller.start();
    poller.resume();
    expect(status).toHaveBeenCalledTimes(1);
    statusOne.resolve({ status: "checking", operation: { operation_id: "op", status: "checking" }, running: null });
    await vi.waitFor(() => expect(schedules).toHaveLength(1));
    expect(schedules[0].delay).toBe(500);
    schedules[0].callback();
    await vi.waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(1));
    expect(snapshots).toEqual(["checking", "ready"]);
    expect(status).toHaveBeenCalledTimes(2);
  });

  it("aborts on cleanup and ignores a stale response", async () => {
    const late = deferred<PortableOperationResponse>();
    const snapshots = vi.fn();
    const poller = createPortableOperationPoller({
      pollStatus: () => late.promise,
      pollLogs: async () => ({ status: "checking", events: [], next_seq: 0 }),
      onSnapshot: snapshots,
      onTerminal: vi.fn(),
    });

    poller.start();
    poller.stop();
    late.resolve({ status: "ready", operation: { operation_id: "old", status: "ready" }, running: true });
    await Promise.resolve();
    await Promise.resolve();
    expect(snapshots).not.toHaveBeenCalled();
  });

  it("uses a slower hidden cadence and refreshes immediately when resumed", async () => {
    const schedules: Array<{ callback: () => void; delay: number }> = [];
    let hidden = true;
    const status = vi.fn(async () => ({ status: "checking", operation: { operation_id: "op", status: "checking" }, running: null } satisfies PortableOperationResponse));
    const poller = createPortableOperationPoller({
      pollStatus: status,
      pollLogs: async () => ({ status: "checking", events: [], next_seq: 0 }),
      onSnapshot: vi.fn(),
      onTerminal: vi.fn(),
      isHidden: () => hidden,
      schedule: (callback, delay) => {
        schedules.push({ callback, delay });
        return schedules.length;
      },
      clearSchedule: vi.fn(),
    });

    poller.start();
    await vi.waitFor(() => expect(schedules[0]?.delay).toBe(2000));
    hidden = false;
    poller.resume();
    await vi.waitFor(() => expect(status).toHaveBeenCalledTimes(2));
    poller.stop();
  });

  it("drains every terminal log page and retains the latest 200 including the final error", async () => {
    const allEvents = Array.from({ length: 250 }, (_, index) => ({
      seq: index + 1,
      timestamp: "2026-07-15T00:00:00Z",
      phase: index === 249 ? "blocked" as const : "checking" as const,
      message: `event ${index + 1}`,
      ...(index === 249 ? { error_code: "ALL_LOCKED_SOURCES_EXHAUSTED" } : {}),
    }));
    const logCalls: number[] = [];
    const onTerminal = vi.fn();
    const poller = createPortableOperationPoller({
      pollStatus: async () => ({
        status: "blocked",
        operation: { operation_id: "op", status: "blocked" },
        running: false,
      }),
      pollLogs: async (afterSeq) => {
        logCalls.push(afterSeq);
        const events = allEvents.filter((event) => event.seq > afterSeq).slice(0, 100);
        return { status: "blocked", events, next_seq: events.at(-1)?.seq ?? afterSeq };
      },
      onSnapshot: vi.fn(),
      onTerminal,
    });

    poller.start();
    await vi.waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(1));
    const snapshot = onTerminal.mock.calls[0][0] as { events: PortableOperationEvent[] };
    const projected = mergePortableEvents([], snapshot.events);
    expect(logCalls).toEqual([0, 100, 200, 250]);
    expect(snapshot.events).toHaveLength(250);
    expect(projected).toHaveLength(200);
    expect(projected[0].seq).toBe(51);
    expect(projected.at(-1)).toMatchObject({ seq: 250, error_code: "ALL_LOCKED_SOURCES_EXHAUSTED" });
  });

  it("drains exactly the backend 500-event bound and then one empty page", async () => {
    const allEvents = Array.from({ length: 500 }, (_, index) => ({
      seq: index + 1,
      timestamp: "2026-07-15T00:00:00Z",
      phase: "ready" as const,
      message: `event ${index + 1}`,
    }));
    const cursors: number[] = [];
    const onTerminal = vi.fn();
    const poller = createPortableOperationPoller({
      pollStatus: async () => ({ status: "ready", operation: { operation_id: "op", status: "ready" }, running: true }),
      pollLogs: async (afterSeq) => {
        cursors.push(afterSeq);
        const events = allEvents.filter((event) => event.seq > afterSeq).slice(0, 100);
        return { status: "ready", events, next_seq: events.at(-1)?.seq ?? afterSeq };
      },
      onSnapshot: vi.fn(),
      onTerminal,
    });

    poller.start();
    await vi.waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(1));
    expect(cursors).toEqual([0, 100, 200, 300, 400, 500]);
    expect(onTerminal.mock.calls[0][0].events).toHaveLength(500);
  });

  it("fails closed when a terminal log cursor does not advance", async () => {
    const onTerminal = vi.fn();
    const onError = vi.fn();
    const schedules: Array<() => void> = [];
    const poller = createPortableOperationPoller({
      pollStatus: async () => ({ status: "blocked", operation: { operation_id: "op", status: "blocked" }, running: false }),
      pollLogs: async () => ({
        status: "blocked",
        events: [{ seq: 1, timestamp: "2026-07-15T00:00:00Z", phase: "blocked", message: "bad cursor" }],
        next_seq: 0,
      }),
      onSnapshot: vi.fn(),
      onTerminal,
      onError,
      schedule: (callback) => {
        schedules.push(callback);
        return schedules.length;
      },
      clearSchedule: vi.fn(),
    });

    poller.start();
    await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    expect(onTerminal).not.toHaveBeenCalled();
    expect(schedules).toHaveLength(0);
  });

  it("rejects terminal log streams beyond the backend event bound", async () => {
    const events = Array.from({ length: 501 }, (_, index) => ({
      seq: index + 1,
      timestamp: "2026-07-15T00:00:00Z",
      phase: "blocked" as const,
      message: `event ${index + 1}`,
    }));
    const onTerminal = vi.fn();
    const onError = vi.fn();
    const poller = createPortableOperationPoller({
      pollStatus: async () => ({ status: "blocked", operation: { operation_id: "op", status: "blocked" }, running: false }),
      pollLogs: async (afterSeq) => {
        const page = events.filter((event) => event.seq > afterSeq).slice(0, 100);
        return { status: "blocked", events: page, next_seq: page.at(-1)?.seq ?? afterSeq };
      },
      onSnapshot: vi.fn(),
      onTerminal,
      onError,
    });

    poller.start();
    await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    expect(onTerminal).not.toHaveBeenCalled();
  });
});

describe("portable stop and repair convergence", () => {
  it("uses separate bounded windows for stop and long-running repair", () => {
    expect(PORTABLE_STOP_CONVERGENCE_TIMEOUT_MS).toBe(2 * 60 * 1000);
    expect(PORTABLE_REPAIR_CONVERGENCE_TIMEOUT_MS).toBe(6 * 60 * 60 * 1000);
  });
  it("keeps controls busy until a real state check converges", async () => {
    const schedules: Array<() => void> = [];
    const check = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const onSettled = vi.fn();
    const poller = createPortableActionConvergencePoller({
      check,
      onSettled,
      onTimeout: vi.fn(),
      schedule: (callback) => {
        schedules.push(callback);
        return schedules.length;
      },
      clearSchedule: vi.fn(),
    });

    poller.start();
    await vi.waitFor(() => expect(schedules).toHaveLength(1));
    expect(onSettled).not.toHaveBeenCalled();
    schedules[0]();
    await vi.waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1));
    expect(check).toHaveBeenCalledTimes(2);
  });

  it("times out from checked state instead of fabricating completion or staying busy forever", async () => {
    const schedules: Array<() => void> = [];
    let now = 0;
    const onTimeout = vi.fn();
    const poller = createPortableActionConvergencePoller({
      check: vi.fn().mockResolvedValue(false),
      onSettled: vi.fn(),
      onTimeout,
      timeoutMs: 1000,
      now: () => now,
      schedule: (callback) => {
        schedules.push(callback);
        return schedules.length;
      },
      clearSchedule: vi.fn(),
    });

    poller.start();
    await vi.waitFor(() => expect(schedules).toHaveLength(1));
    now = 1000;
    schedules[0]();
    await vi.waitFor(() => expect(onTimeout).toHaveBeenCalledTimes(1));
  });
});
