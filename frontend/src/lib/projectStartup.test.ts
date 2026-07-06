import { describe, expect, it } from "vitest";

import type { ProjectSummary } from "../types";
import { createEmptyManifest, createEmptyProject, createProjectId, selectStartupProjectId } from "./projectStartup";

describe("project startup helpers", () => {
  it("selects no project when the backend has no saved projects", () => {
    expect(selectStartupProjectId([], null)).toBeNull();
    expect(createEmptyProject()).toMatchObject({ title: "", default_language: "zh", lines: [] });
    expect(createEmptyManifest(null)).toEqual({ project_id: "", lines: {} });
  });

  it("prefers the last selected project when it still exists", () => {
    const projects: ProjectSummary[] = [
      { project_id: "official-script", title: "正式剧本", default_language: "zh", line_count: 30 },
      { project_id: "another-script", title: "另一个剧本", default_language: "zh", line_count: 8 }
    ];

    expect(selectStartupProjectId(projects, "another-script")).toBe("another-script");
  });

  it("falls back to the first real saved project instead of a hardcoded demo id", () => {
    const projects: ProjectSummary[] = [
      { project_id: "official-script", title: "正式剧本", default_language: "zh", line_count: 30 }
    ];

    expect(selectStartupProjectId(projects, "demo")).toBe("official-script");
  });

  it("creates stable safe ids for new scripts", () => {
    expect(createProjectId("  Demo Script  ", "m001")).toBe("demo-script-m001");
    expect(createProjectId("测试剧本", "m002")).toBe("script-m002");
  });
});
