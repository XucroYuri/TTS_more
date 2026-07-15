import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";

import { LocalPortableServicesPanel } from "../components/LocalPortableServicesPanel";
import { resources } from "../i18n";
import {
  createPortableOperationPoller,
  portableErrorMessageKey,
  portablePhaseAfterAction,
  portablePhaseLabelKey,
  portableServiceCards,
  shouldRevealManualProxy,
  withControlTokenRetry,
} from "./portableServices";
import { PortableApiError } from "../api";
import type { PortableOperationLogsResponse, PortableOperationResponse } from "../types";

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
      { component: "gpt-sovits", managed: true, status: "ready", package_root: "D:/GPT" },
      { component: "indextts", managed: false, status: "stopped", mode: "external", network_scope: "lan" },
    ]);

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
        { component: "gpt-sovits", managed: true, status: "stopped", package_root: "D:/GPT" },
        { component: "indextts", managed: true, status: "stopped", package_root: "D:/Index" },
      ],
      { "gpt-sovits": "downloading" },
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
  });

  it("keeps stop and immediate-start phases accurate without an operation poll", () => {
    expect(portablePhaseAfterAction("stop", { status: "stopping" })).toBe("stopped");
    expect(portablePhaseAfterAction("repair", { status: "repairing" })).toBeNull();
    expect(portablePhaseAfterAction("open-folder", { status: "opened" })).toBeNull();
    expect(portablePhaseAfterAction("start", { status: "ready" })).toBe("ready");
    expect(portablePhaseAfterAction("start", { status: "starting", operation_id: "op" })).toBe("starting");
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
    const logs = vi.fn<(afterSeq: number, signal: AbortSignal) => Promise<PortableOperationLogsResponse>>().mockResolvedValue({
      status: "checking",
      events: [{ seq: 1, timestamp: "2026-07-15T00:00:00Z", phase: "checking", message: "safe" }],
      next_seq: 1,
    });
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
});
