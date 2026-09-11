import { describe, expect, it } from "vitest";

import type { GenerationJob, ScriptLine } from "../types";
import { latestQueueItemForLine, lineHasActiveGeneration, upsertGenerationJob } from "./generationQueue";

const firstLine: ScriptLine = { id: "line-1", line_uid: "uid-1", character_id: "hero", text: "快跑", note: "" };
const secondLine: ScriptLine = { id: "line-2", line_uid: "uid-2", character_id: "hero", text: "等等", note: "" };

function job(jobId: string, line: ScriptLine, status: "queued" | "running" | "completed"): GenerationJob {
  return {
    job_id: jobId,
    project_id: "project-1",
    status,
    progress: status === "completed" ? 1 : 0.4,
    created_at: "now",
    updated_at: "now",
    items: [{
      task_id: `${jobId}-task`,
      line_id: line.id,
      line_uid: line.line_uid,
      status,
      progress: status === "completed" ? 1 : 0.4,
      cluster_key: "cluster"
    }]
  };
}

describe("generation queue helpers", () => {
  it("keeps each line independently available", () => {
    const jobs = [job("job-1", firstLine, "running")];

    expect(lineHasActiveGeneration(jobs, "project-1", firstLine)).toBe(true);
    expect(lineHasActiveGeneration(jobs, "project-1", secondLine)).toBe(false);
  });

  it("keeps multiple submitted jobs and resolves the latest line item", () => {
    const first = job("job-1", firstLine, "running");
    const second = job("job-2", secondLine, "queued");
    const status = upsertGenerationJob(upsertGenerationJob(null, first), second);

    expect(status.jobs.map((item) => item.job_id)).toEqual(["job-1", "job-2"]);
    expect(status.running).toBe(1);
    expect(status.queued).toBe(1);
    expect(latestQueueItemForLine(status.jobs, "project-1", secondLine)?.task_id).toBe("job-2-task");
  });
});
