import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { initI18n } from "../../i18n";
import { QueuePanel } from "./QueuePanel";

initI18n();

describe("QueuePanel", () => {
  it("renders partial queue progress and recent history", () => {
    const markup = renderToStaticMarkup(createElement(QueuePanel, {
      jobs: [{ job_id: "job-1", project_id: "demo", status: "running", progress: 0.5, items: [], created_at: "now", updated_at: "now" }],
      activeJob: null,
      queued: 2,
      running: 1,
      completed: 3,
      failed: 1,
      cancelled: 0,
      total: 7,
      processed: 4,
      progressPercent: 57,
      statusLabel: "生成中",
      statusTone: "running"
    }));
    expect(markup).toContain("57%");
    expect(markup).toContain("job-1");
    expect(markup).toContain("3");
  });
});
