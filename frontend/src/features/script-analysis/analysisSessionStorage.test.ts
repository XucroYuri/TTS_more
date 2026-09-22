import { describe, expect, it } from "vitest";

import type { ScriptRevision } from "../../types";
import {
  ANALYSIS_CONFIRM_KEYS_STORAGE_KEY,
  ANALYSIS_CACHE_RESET_STORAGE_KEY,
  ANALYSIS_CACHE_RESET_VERSION,
  ANALYSIS_DISMISSED_RUNS_STORAGE_KEY,
  ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY,
  ANALYSIS_RUN_SESSIONS_STORAGE_KEY,
  activeAnalysisScopeForRevision,
  clearLegacyAnalysisCacheOnce,
  clearActiveAnalysisScope,
  removeDeletedAnalysisSession,
  readActiveAnalysisScope,
  writeActiveAnalysisScope
} from "./analysisSessionStorage";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();
  get length() { return this.values.size; }
  clear() { this.values.clear(); }
  getItem(key: string) { return this.values.get(key) ?? null; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string) { this.values.delete(key); }
  setItem(key: string, value: string) { this.values.set(key, String(value)); }
}

describe("analysis session storage", () => {
  it("clears only the expected active revision", () => {
    const storage = new MemoryStorage();
    const older = { revision_id: "old", source_sha256: "old-hash" } as ScriptRevision;
    const newer = { revision_id: "new", source_sha256: "new-hash" } as ScriptRevision;
    writeActiveAnalysisScope("project", newer, storage);
    clearActiveAnalysisScope(activeAnalysisScopeForRevision("project", older), storage);
    expect(readActiveAnalysisScope(storage)?.revisionId).toBe("new");
  });

  it("clears legacy analysis cache once and preserves later sessions", () => {
    const storage = new MemoryStorage();
    storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({ old: { runId: "old", draftId: "old" } }));

    expect(clearLegacyAnalysisCacheOnce(storage)).toBe(true);
    expect(storage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY)).toBeNull();
    expect(storage.getItem(ANALYSIS_CACHE_RESET_STORAGE_KEY)).toBe(ANALYSIS_CACHE_RESET_VERSION);

    storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({ current: { runId: "current", draftId: "current" } }));
    expect(clearLegacyAnalysisCacheOnce(storage)).toBe(false);
    expect(storage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY)).toContain("current");
  });

  it("removes a deleted run from resumable, review, confirmation, and dismissed caches", () => {
    const storage = new MemoryStorage();
    storage.setItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY, JSON.stringify({ keep: { runId: "keep", draftId: "keep-draft" }, remove: { runId: "run-1", draftId: "draft-1" } }));
    storage.setItem(ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY, JSON.stringify({ review: { runId: "run-1", draftId: "draft-1" } }));
    storage.setItem(ANALYSIS_CONFIRM_KEYS_STORAGE_KEY, JSON.stringify({ "draft-1": "key-1", "keep-draft": "key-2" }));
    storage.setItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY, JSON.stringify(["run-1", "keep"]));

    removeDeletedAnalysisSession("run-1", "draft-1", storage);

    expect(JSON.parse(storage.getItem(ANALYSIS_RUN_SESSIONS_STORAGE_KEY)!)).toEqual({ keep: { runId: "keep", draftId: "keep-draft" } });
    expect(JSON.parse(storage.getItem(ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY)!)).toEqual({});
    expect(JSON.parse(storage.getItem(ANALYSIS_CONFIRM_KEYS_STORAGE_KEY)!)).toEqual({ "keep-draft": "key-2" });
    expect(JSON.parse(storage.getItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY)!)).toEqual(["keep"]);
  });
});
