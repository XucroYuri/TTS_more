import { describe, expect, it } from "vitest";

import { formatScriptNote } from "./lineNote";

describe("formatScriptNote", () => {
  it("wraps bare notes in full-width Chinese parentheses", () => {
    expect(formatScriptNote("张开双臂，护在两人身前")).toBe("（张开双臂，护在两人身前）");
  });

  it("normalizes existing full-width or half-width wrappers", () => {
    expect(formatScriptNote("（声音颤抖）")).toBe("（声音颤抖）");
    expect(formatScriptNote("(声音颤抖)")).toBe("（声音颤抖）");
  });

  it("omits empty notes", () => {
    expect(formatScriptNote("  ")).toBe("");
  });
});
