import type { GenerationJob, GenerationQueueItem, QueueStatus, ScriptLine } from "../types";
import { isTerminalGenerationStatus } from "./generationStatus";

export function generationLineKey(projectId: string, line: ScriptLine): string {
  return `${projectId}|${line.line_uid ?? line.id}`;
}

export function latestQueueItemForLine(
  jobs: GenerationJob[],
  projectId: string | null | undefined,
  line: ScriptLine
): GenerationQueueItem | undefined {
  if (!projectId) return undefined;
  for (let jobIndex = jobs.length - 1; jobIndex >= 0; jobIndex -= 1) {
    const job = jobs[jobIndex];
    if (job.project_id !== projectId) continue;
    const item = job.items.find((candidate) => candidate.line_uid
      ? candidate.line_uid === (line.line_uid ?? line.id)
      : candidate.line_id === line.id);
    if (item) return item;
  }
  return undefined;
}

export function lineHasActiveGeneration(
  jobs: GenerationJob[],
  projectId: string | null | undefined,
  line: ScriptLine
): boolean {
  if (!projectId) return false;
  return jobs.some((job) => (
    job.project_id === projectId
    && job.items.some((item) => (
      (item.line_uid ? item.line_uid === (line.line_uid ?? line.id) : item.line_id === line.id)
      && !isTerminalGenerationStatus(item.status)
    ))
  ));
}

export function upsertGenerationJob(current: QueueStatus | null, job: GenerationJob): QueueStatus {
  const jobs = [...(current?.jobs ?? [])];
  const existingIndex = jobs.findIndex((item) => item.job_id === job.job_id);
  if (existingIndex >= 0) jobs[existingIndex] = job;
  else jobs.push(job);
  const items = jobs.flatMap((item) => item.items);
  return {
    jobs,
    queued: items.filter((item) => item.status === "queued").length,
    running: items.filter((item) => ["loading", "running", "finalizing", "cancelling"].includes(item.status)).length,
  };
}
