import { beforeEach, describe, expect, it } from "vitest";

import { getApiToken, patchAnalysisDraft, setApiToken } from "./api";
import { createScriptRevision, saveProject } from "./api";
import type { ScriptProject } from "./types";

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

describe("project save request shape", () => {
  it("writes top-level TTS edits into only the active parse revision without mutating the project", async () => {
    const originalFetch = globalThis.fetch;
    let requestBody: ScriptProject | undefined;
    const previousBinding = {
      binding_id: "line-temp-index",
      provider_type: "indextts" as const,
      service_id: "mock-index",
      fallback_services: [],
      capabilities: ["reference_audio_voice", "emotion_text"],
      config: { voice: "old.wav", emotion_text: "tense" }
    };
    const project: ScriptProject = {
      title: "Demo",
      default_language: "zh",
      active_script_revision_id: "script-r002",
      active_parse_revision_id: "parse-r002",
      lines: [
        {
          id: "l002",
          line_uid: "parse-r002:l002",
          character_id: "role-2",
          text: "edited second line",
          note: "",
          temporary_binding: null
        },
        {
          id: "l001",
          line_uid: "parse-r002:l001",
          character_id: "role-1",
          text: "edited first line",
          note: "new direction"
        }
      ],
      parse_revisions: [
        {
          revision_id: "parse-r001",
          script_revision_id: "script-r001",
          provider: "legacy",
          warnings: [],
          project_characters: [],
          lines: [
            {
              id: "l001",
              line_uid: "parse-r001:l001",
              character_id: "role-1",
              text: "inactive revision line",
              note: ""
            }
          ],
          created_at: "2026-08-31T00:00:00.000Z"
        },
        {
          revision_id: "parse-r002",
          script_revision_id: "script-r002",
          provider: "semantic",
          warnings: ["keep warning"],
          project_characters: [],
          lines: [
            {
              id: "l001",
              line_uid: "parse-r002:l001",
              character_id: "role-1",
              text: "stale first line",
              note: "old direction"
            },
            {
              id: "l002",
              line_uid: "parse-r002:l002",
              character_id: "role-2",
              text: "stale second line",
              note: "",
              temporary_binding: previousBinding
            },
            {
              id: "l999",
              line_uid: "parse-r002:l999",
              character_id: "role-extra",
              text: "revision-only line",
              note: "preserve me"
            }
          ],
          created_at: "2026-09-01T00:00:00.000Z"
        }
      ]
    };
    const originalProject = structuredClone(project);

    globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body)) as ScriptProject;
      return new Response(JSON.stringify({ status: "saved" }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }) as typeof fetch;

    try {
      await saveProject("demo", project);
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(requestBody?.parse_revisions?.[0]).toEqual(originalProject.parse_revisions?.[0]);
    expect(requestBody?.parse_revisions?.[1].lines).toEqual([
      {
        id: "l001",
        line_uid: "parse-r002:l001",
        character_id: "role-1",
        text: "edited first line",
        note: "new direction"
      },
      {
        id: "l002",
        line_uid: "parse-r002:l002",
        character_id: "role-2",
        text: "edited second line",
        note: "",
        temporary_binding: null
      },
      {
        id: "l999",
        line_uid: "parse-r002:l999",
        character_id: "role-extra",
        text: "revision-only line",
        note: "preserve me"
      }
    ]);
    expect(project).toEqual(originalProject);
  });

  it("matches legacy top-level lines without line_uid to the active revision identity", async () => {
    const originalFetch = globalThis.fetch;
    let requestBody: ScriptProject | undefined;
    const project: ScriptProject = {
      title: "Legacy",
      default_language: "zh",
      active_parse_revision_id: "parse-r003",
      lines: [
        { id: "legacy-1", character_id: "role-1", text: "edited legacy text", note: "" }
      ],
      parse_revisions: [
        {
          revision_id: "parse-r003",
          script_revision_id: "script-r001",
          provider: "legacy",
          warnings: [],
          project_characters: [],
          lines: [
            {
              id: "legacy-1",
              line_uid: "parse-r003:legacy-1",
              character_id: "role-1",
              text: "stale legacy text",
              note: ""
            }
          ],
          created_at: "2026-09-01T00:00:00.000Z"
        }
      ]
    };
    const originalProject = structuredClone(project);

    globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body)) as ScriptProject;
      return new Response(JSON.stringify({ status: "saved" }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }) as typeof fetch;

    try {
      await saveProject("legacy", project);
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(requestBody?.parse_revisions?.[0].lines).toEqual([
      {
        id: "legacy-1",
        line_uid: "parse-r003:legacy-1",
        character_id: "role-1",
        text: "edited legacy text",
        note: ""
      }
    ]);
    expect(project).toEqual(originalProject);
  });
});
