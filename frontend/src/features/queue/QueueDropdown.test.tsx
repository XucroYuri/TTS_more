import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { initI18n } from "../../i18n";
import { QueueDropdown } from "./QueueDropdown";

initI18n();

describe("QueueDropdown", () => {
  it("renders every queued line with status and progress", () => {
    const markup = renderToStaticMarkup(createElement(QueueDropdown, {
      currentProjectId: "project-1",
      lineLabels: {
        "uid-1": "弱弱 · 快跑",
        "uid-2": "幽灵 · 等等",
        "uid-3": "九九 · 找到了",
        "uid-4": "胶布 · 再试一次"
      },
      jobs: [{
        job_id: "job-1",
        project_id: "project-1",
        status: "running",
        progress: 0.5,
        created_at: "now",
        updated_at: "now",
        items: [
          { task_id: "task-1", line_id: "line-1", line_uid: "uid-1", status: "running", progress: 0.4, cluster_key: "a" },
          { task_id: "task-2", line_id: "line-2", line_uid: "uid-2", status: "queued", progress: 0, cluster_key: "b" },
          { task_id: "task-3", line_id: "line-3", line_uid: "uid-3", status: "completed", progress: 1, cluster_key: "c" },
          { task_id: "task-4", line_id: "line-4", line_uid: "uid-4", status: "failed", progress: 0.2, cluster_key: "d" }
        ]
      }]
    }));

    expect(markup).toContain("弱弱 · 快跑");
    expect(markup).toContain("幽灵 · 等等");
    expect(markup).toContain("40%");
    expect(markup).toContain("0%");
    expect(markup).toContain("status-running");
    expect(markup).toContain("status-queued");
    expect(markup).toContain("status-completed");
    expect(markup).toContain("status-failed");
  });
});
