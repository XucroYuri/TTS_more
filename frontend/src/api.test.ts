import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchLocalControlToken,
  fetchLocalPortableServices,
  fetchPortableOperationLogs,
  getApiToken,
  PortableApiError,
  portableServiceAction,
  registerLocalPortableService,
  selectLocalPortableFolder,
  setApiToken,
} from "./api";

// vitest defaults to the node environment (no localStorage). Stub a minimal
// localStorage so the token helpers can be exercised.
function makeLocalStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  };
}

describe("api token storage", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", makeLocalStorage());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns empty string when no token is stored", () => {
    expect(getApiToken()).toBe("");
  });

  it("stores and retrieves a token", () => {
    setApiToken("secret-abc");
    expect(getApiToken()).toBe("secret-abc");
  });

  it("clears the token when given an empty string", () => {
    setApiToken("secret-abc");
    setApiToken("");
    expect(getApiToken()).toBe("");
  });
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("local portable control API", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", makeLocalStorage());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses exact control routes and keeps the control token out of storage", async () => {
    const setItem = vi.spyOn(localStorage, "setItem");
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ token: "memory-only" }))
      .mockResolvedValueOnce(jsonResponse({ services: [] }))
      .mockResolvedValueOnce(jsonResponse({ component: "gpt-sovits", action: "start", status: "starting", operation_id: "op" }));
    vi.stubGlobal("fetch", fetchMock);

    const token = await fetchLocalControlToken();
    await fetchLocalPortableServices(token);
    await portableServiceAction("gpt-sovits", "start", token);

    expect(token).toBe("memory-only");
    expect(setItem).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls[0]).toEqual(["/api/local-control/token", expect.any(Object)]);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/local-portable-services");
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("X-TTS-More-Control")).toBe("memory-only");
    expect(fetchMock.mock.calls[2][0]).toBe("/api/local-portable-services/gpt-sovits/start");
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: "POST" });
    expect(fetchMock.mock.calls[2][1]?.body).toBeUndefined();
  });

  it("sends only the strict folder and registration payload fields", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ status: "selected", package: { component: "cosyvoice", package_id: "cosy-main", package_root: "D:/Cosy" } }))
      .mockResolvedValueOnce(jsonResponse({ package: {}, service: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await selectLocalPortableFolder("cosyvoice", "control");
    await registerLocalPortableService(
      { component: "cosyvoice", package_id: "cosy-main", path: "D:/Cosy" },
      "control",
    );

    expect(fetchMock.mock.calls[0][0]).toBe("/api/local-portable-services/select-folder");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ component: "cosyvoice" });
    expect(fetchMock.mock.calls[1][0]).toBe("/api/local-portable-services/register");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      component: "cosyvoice",
      package_id: "cosy-main",
      path: "D:/Cosy",
    });
  });

  it("encodes bounded log pagination and forwards cancellation", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ status: "checking", events: [], next_seq: 12 }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await fetchPortableOperationLogs("indextts", "11111111-1111-4111-8111-111111111111", "control", 12, 100, controller.signal);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/local-portable-services/indextts/operations/11111111-1111-4111-8111-111111111111/logs?after_seq=12&limit=100");
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
    await expect(fetchPortableOperationLogs("indextts", "op", "control", -1, 100)).rejects.toBeInstanceOf(RangeError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("projects structured 403, 409 and 422 API failures", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ detail: { code: "LOCAL_CONTROL_FORBIDDEN", message: "no" } }, 403))
      .mockResolvedValueOnce(jsonResponse({ detail: { code: "LOCAL_CONTROL_NOT_MANAGEABLE", message: "no" } }, 409))
      .mockResolvedValueOnce(jsonResponse({ detail: { code: "LOCAL_CONTROL_INVALID_REQUEST", message: "bad" } }, 422));
    vi.stubGlobal("fetch", fetchMock);

    for (const expectedStatus of [403, 409, 422]) {
      await expect(fetchLocalPortableServices("control")).rejects.toMatchObject({
        status: expectedStatus,
      } satisfies Partial<PortableApiError>);
    }
  });
});
