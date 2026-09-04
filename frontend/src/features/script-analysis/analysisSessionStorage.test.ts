import { describe, expect, it } from "vitest";

import type { ScriptRevision } from "../../types";
import {
  activeAnalysisScopeForRevision,
  clearActiveAnalysisScope,
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
});
