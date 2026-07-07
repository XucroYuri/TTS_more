import { describe, expect, it } from "vitest";

import type { ProjectSummary, ScriptProject } from "../types";
import { filterAndSortProjectSummaries, nextProjectAfterDelete, projectPreviewStats } from "./scriptManagement";

const projects: ProjectSummary[] = [
  {
    project_id: "alpha-script",
    title: "Alpha Script",
    default_language: "zh",
    line_count: 12,
    updated_at: "2026-07-06T10:00:00+00:00"
  },
  {
    project_id: "blackridge",
    title: "Signal Over Blackridge",
    default_language: "en",
    line_count: 48,
    updated_at: "2026-07-07T10:00:00+00:00"
  },
  {
    project_id: "untimed",
    title: "Untimed Draft",
    default_language: "zh",
    line_count: 2
  }
];

describe("script management helpers", () => {
  it("filters projects by title or id and sorts newest first", () => {
    expect(filterAndSortProjectSummaries(projects, "").map((project) => project.project_id)).toEqual([
      "blackridge",
      "alpha-script",
      "untimed"
    ]);

    expect(filterAndSortProjectSummaries(projects, "alpha").map((project) => project.project_id)).toEqual(["alpha-script"]);
    expect(filterAndSortProjectSummaries(projects, "black").map((project) => project.project_id)).toEqual(["blackridge"]);
  });

  it("summarizes preview statistics and active source text", () => {
    const project: ScriptProject = {
      title: "Preview Demo",
      default_language: "zh",
      project_characters: [
        { project_character_id: "alice", name: "Alice", mode: "reference", library_character_id: null },
        { project_character_id: "bob", name: "Bob", mode: "reference", library_character_id: null }
      ],
      active_script_revision_id: "script-r002",
      active_parse_revision_id: "parse-r001",
      script_revisions: [
        { revision_id: "script-r001", source_markdown: "Alice: old", created_at: "2026-07-06T00:00:00Z" },
        { revision_id: "script-r002", source_markdown: "Alice: new", created_at: "2026-07-07T00:00:00Z" }
      ],
      parse_revisions: [
        { revision_id: "parse-r001", script_revision_id: "script-r002", provider: "rule", warnings: [], project_characters: [], lines: [], created_at: "2026-07-07T00:01:00Z" }
      ],
      lines: [{ id: "l001", character_id: "alice", text: "new", note: "" }]
    };

    expect(projectPreviewStats(project)).toEqual({
      lineCount: 1,
      characterCount: 2,
      scriptRevisionCount: 2,
      parseRevisionCount: 1,
      activeSourceMarkdown: "Alice: new"
    });
  });

  it("selects the next sensible project after deletion", () => {
    expect(nextProjectAfterDelete(projects, "blackridge", "blackridge")).toBe("alpha-script");
    expect(nextProjectAfterDelete(projects, "untimed", "untimed")).toBe("blackridge");
    expect(nextProjectAfterDelete(projects, "blackridge", "alpha-script")).toBe("alpha-script");
    expect(nextProjectAfterDelete([projects[1]], "blackridge", "blackridge")).toBeNull();
  });
});
