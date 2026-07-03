import { describe, expect, it } from "vitest";

import { summarizeLineHistory, workerReadinessLabel } from "./status";

describe("status helpers", () => {
  it("summarizes the latest generation version", () => {
    const summary = summarizeLineHistory({
      line_id: "l001",
      versions: [
        { version_id: "v001", engine: "gpt-sovits", profile: "alice", status: "failed", created_at: "2026-06-30T00:00:00Z" },
        { version_id: "v002", engine: "indextts", profile: "alice-emo", status: "completed", audio_path: "a.wav", created_at: "2026-06-30T00:01:00Z" }
      ]
    });

    expect(summary.label).toBe("completed");
    expect(summary.latestVersionId).toBe("v002");
    expect(summary.canPlay).toBe(true);
  });

  it("labels workers with missing resources as attention needed", () => {
    expect(workerReadinessLabel({ engine: "vibevoice", ready: false })).toBe("needs setup");
    expect(workerReadinessLabel({ engine: "vibevoice", ready: true })).toBe("ready");
  });
});
