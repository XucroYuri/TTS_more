import type { LineHistory, WorkerHealth } from "../types";

export type StatusTone = "idle" | "queued" | "running" | "completed" | "failed";

export interface LineSummary {
  label: string;
  canPlay: boolean;
  tone: StatusTone;
}

export function summarizeLineHistory(history?: LineHistory): LineSummary {
  if (!history || history.versions.length === 0) {
    return { label: "not generated", canPlay: false, tone: "idle" };
  }
  const latest = history.versions[history.versions.length - 1];
  return {
    label: `${latest.version_id} ${latest.status}`,
    canPlay: latest.status === "completed" && Boolean(latest.audio_path),
    tone: statusTone(latest.status)
  };
}

export function statusTone(status: string): StatusTone {
  if (status === "completed") return "completed";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "queued") return "queued";
  if (status === "loading" || status === "running" || status === "finalizing") return "running";
  return "idle";
}

export function workerReadinessLabel(worker: Pick<WorkerHealth, "ready" | "engine">): string {
  return worker.ready ? "ready" : "needs setup";
}
