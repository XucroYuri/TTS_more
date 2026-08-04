import type { LineHistory, WorkerHealth } from "../types";
import { generationStatusTone, type GenerationStatusTone } from "./generationStatus";

export type StatusTone = GenerationStatusTone;

export interface LineSummary {
  label: string;
  latestVersionId?: string;
  canPlay: boolean;
  tone: StatusTone;
}

export function summarizeLineHistory(history?: LineHistory): LineSummary {
  if (!history || history.versions.length === 0) {
    return { label: "not generated", canPlay: false, tone: "idle" };
  }
  const latest = history.versions[history.versions.length - 1];
  return {
    label: latest.status,
    latestVersionId: latest.version_id,
    canPlay: latest.status === "completed" && Boolean(latest.audio_path),
    tone: generationStatusTone(latest.status)
  };
}

export function workerReadinessLabel(worker: Pick<WorkerHealth, "ready" | "engine">): string {
  return worker.ready ? "ready" : "needs setup";
}
