import { describe, expect, it } from "vitest";

import { readScriptFile } from "./fileInput";

describe("readScriptFile", () => {
  it("accepts md case-insensitively, removes one leading BOM, and preserves CRLF", async () => {
    const markdown = new File(["\uFEFF甲\r\n台词"], "script.Md", { type: "text/markdown" });
    await expect(readScriptFile(markdown)).resolves.toEqual({
      text: "甲\r\n台词",
      filename: "script.Md",
      mediaType: "text/markdown"
    });
  });

  it("preserves one U+FEFF from a real double-BOM text file", async () => {
    const text = new File(["\uFEFF\uFEFFx"], "SCRIPT.TXT", { type: "text/plain" });
    await expect(readScriptFile(text)).resolves.toEqual({
      text: "\uFEFFx",
      filename: "SCRIPT.TXT",
      mediaType: "text/plain"
    });
  });

  it("rejects unsupported file extensions", async () => {
    await expect(readScriptFile(new File(["x"], "script.docx"))).rejects.toThrow("unsupported_script_file");
  });

  it("returns a non-blocking warning above 200,000 Unicode code points", async () => {
    const withinLimit = await readScriptFile(new File(["😀".repeat(100_001)], "emoji.txt", { type: "text/plain" }));
    expect(withinLimit.warning).toBeUndefined();

    const overLimit = await readScriptFile(new File(["😀".repeat(200_001)], "large.txt", { type: "text/plain" }));
    expect(overLimit.warning).toEqual({
      code: "script_file_exceeds_recommended_limit",
      codePointCount: 200_001,
      limit: 200_000
    });
    expect(overLimit.text).toHaveLength(400_002);
  });
});
