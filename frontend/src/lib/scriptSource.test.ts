import { describe, expect, it } from "vitest";

import { projectToScriptSourceText } from "./scriptSource";
import type { Character, ScriptProject } from "../types";

describe("projectToScriptSourceText", () => {
  it("formats project lines with project role names and notes", () => {
    const project: ScriptProject = {
      title: "追逃 Demo",
      default_language: "zh",
      project_characters: [{ project_character_id: "xiao-pin", name: "小品", mode: "reference", library_character_id: null }],
      lines: [{ id: "l1", character_id: "xiao-pin", note: "目光坚定", text: "严镜、小光，我来救你们了！", language: "zh" }]
    };

    expect(projectToScriptSourceText(project, [])).toBe("小品（目光坚定）: 严镜、小光，我来救你们了！");
  });

  it("falls back to global character names and omits empty notes", () => {
    const project: ScriptProject = {
      title: "追逃 Demo",
      default_language: "zh",
      lines: [{ id: "l1", character_id: "narrator", note: "", text: "街道一片混乱。", language: "zh" }]
    };
    const characters: Character[] = [
      { id: "narrator", name: "旁白", aliases: [], notes: "", fallback_profiles: [] }
    ];

    expect(projectToScriptSourceText(project, characters)).toBe("旁白: 街道一片混乱。");
  });

  it("uses the active script revision before the script has parsed lines", () => {
    const project: ScriptProject = {
      title: "新剧本",
      default_language: "zh",
      active_script_revision_id: "script-r001",
      script_revisions: [{
        revision_id: "script-r001",
        source_markdown: "旁白: 第一行\n角色: 第二行",
        created_at: "2026-07-06T00:00:00.000Z"
      }],
      lines: []
    };

    expect(projectToScriptSourceText(project, [])).toBe("旁白: 第一行\n角色: 第二行");
  });
});
