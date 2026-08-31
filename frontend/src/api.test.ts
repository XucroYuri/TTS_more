import { beforeEach, describe, expect, it } from "vitest";

import { getApiToken, patchAnalysisDraft, setApiToken } from "./api";
import { createScriptRevision } from "./api";

describe("api token storage", () => {
  // The token lives in a module-level variable, so tests share state.
  // Reset before each test to keep assertions deterministic.
  beforeEach(() => {
    setApiToken("");
  });

  it("returns empty string when no token is set", () => {
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

describe("semantic analysis API request shapes", () => {
  it("preserves exact source text and includes optional upload metadata in script revisions", async () => {
    const originalFetch = globalThis.fetch;
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify({
        project: { title: "Demo", default_language: "zh", lines: [] },
        script_revision: {
          revision_id: "script-r009",
          source_markdown: "\r\n  \u7532\uff1a\u5feb\u8dd1\uff01  \r\n",
          source_filename: "scene.Md",
          source_media_type: "text/markdown",
          source_sha256: "268c518e000bed760cce0df64a724ac41db87e96188426119edd4eb7d5397f24",
          created_at: "2026-09-01T00:00:00.000Z"
        }
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }) as typeof fetch;

    try {
      const uploaded = await createScriptRevision(
        "demo/project",
        "\r\n  \u7532\uff1a\u5feb\u8dd1\uff01  \r\n",
        "Analyze source",
        { source_filename: "scene.Md", source_media_type: "text/markdown" }
      );
      expect(uploaded.script_revision).toMatchObject({
        source_markdown: "\r\n  \u7532\uff1a\u5feb\u8dd1\uff01  \r\n",
        source_filename: "scene.Md",
        source_media_type: "text/markdown",
        source_sha256: "268c518e000bed760cce0df64a724ac41db87e96188426119edd4eb7d5397f24"
      });
      await createScriptRevision("demo/project", "  pasted source  ", "Analyze paste");
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(calls.map(({ url }) => url)).toEqual([
      "/api/projects/demo%2Fproject/script-revisions",
      "/api/projects/demo%2Fproject/script-revisions"
    ]);
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      source_markdown: "\r\n  \u7532\uff1a\u5feb\u8dd1\uff01  \r\n",
      summary: "Analyze source",
      source_filename: "scene.Md",
      source_media_type: "text/markdown"
    });
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({
      source_markdown: "  pasted source  ",
      summary: "Analyze paste"
    });
  });

  it("encodes resource ids and sends the authoritative create, patch, and confirm payloads", async () => {
    const originalFetch = globalThis.fetch;
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      { run_id: "run-1", draft_id: "draft-1", status: "queued", trace_id: "trace-1" },
      { id: "run/1" },
      { id: "draft/1", version: 3 },
      { id: "draft/1", version: 4 },
      { semantic_revision: { id: "semantic-1" }, parse_revision: { revision_id: "semantic-semantic-1" }, project: { title: "Demo", default_language: "zh", lines: [] } }
    ];

    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify(responses.shift()), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }) as typeof fetch;

    try {
      const api = await import("./api");
      await api.createAnalysisRun("demo/project", "script-r001");
      await api.fetchAnalysisRun("run/1");
      await api.fetchAnalysisDraft("draft/1");
      await api.patchAnalysisDraft("draft/1", 3, [{ op: "dismiss_warning", warning_id: "warning-1" }]);
      await api.confirmAnalysisDraft("draft/1", 4, "key-1");
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(calls.map(({ url }) => url)).toEqual([
      "/api/projects/demo%2Fproject/analysis-runs",
      "/api/analysis-runs/run%2F1",
      "/api/analysis-drafts/draft%2F1",
      "/api/analysis-drafts/draft%2F1",
      "/api/analysis-drafts/draft%2F1/confirm"
    ]);
    expect(calls.map(({ init }) => init?.method)).toEqual(["POST", undefined, undefined, "PATCH", "POST"]);
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ source_revision_id: "script-r001" });
    expect(JSON.parse(String(calls[3].init?.body))).toEqual({
      expected_version: 3,
      operations: [{ op: "dismiss_warning", warning_id: "warning-1" }]
    });
    expect(JSON.parse(String(calls[4].init?.body))).toEqual({
      expected_version: 4,
      idempotency_key: "key-1"
    });
    expect(calls[0].init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(calls[3].init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(calls[4].init?.headers).toEqual({ "Content-Type": "application/json" });
  });

  it("retains the real HTTP status and response body for semantic conflicts", async () => {
    const originalFetch = globalThis.fetch;
    const responseBody = JSON.stringify({
      detail: { code: "semantic_conflict", message: "semantic state conflict" }
    });
    globalThis.fetch = (async () =>
      new Response(responseBody, {
        status: 409,
        statusText: "Conflict",
        headers: { "Content-Type": "application/json" }
      })) as typeof fetch;

    let caught: unknown;
    try {
      await patchAnalysisDraft("draft-1", 3, []);
    } catch (error) {
      caught = error;
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(caught).toMatchObject({
      name: "ApiRequestError",
      status: 409,
      responseBody
    });
  });
});
