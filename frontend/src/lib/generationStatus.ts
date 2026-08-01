import type { GenerationStatus } from "../types";

export type GenerationStatusTone =
  | "idle"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface GenerationStatusCounts {
  queued: number;
  running: number;
  completed: number;
  failed: number;
  cancelled: number;
  processed: number;
  total: number;
}

export const terminalGenerationStatuses = new Set<GenerationStatus>([
  "completed",
  "failed",
  "cancelled",
]);

export function isTerminalGenerationStatus(status: GenerationStatus): boolean {
  return terminalGenerationStatuses.has(status);
}

export function generationStatusTone(status: GenerationStatus): GenerationStatusTone {
  if (status === "completed") return "completed";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "queued") return "queued";
  if (["loading", "running", "finalizing", "cancelling"].includes(status)) {
    return "running";
  }
  return "idle";
}

export function generationStatusKey(status: GenerationStatus): `status.${GenerationStatus}` {
  return `status.${status}`;
}

export function generationStatusCounts(
  statuses: readonly GenerationStatus[],
): GenerationStatusCounts {
  const counts: GenerationStatusCounts = {
    queued: 0,
    running: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    processed: 0,
    total: statuses.length,
  };
  for (const status of statuses) {
    const tone = generationStatusTone(status);
    if (tone === "queued") counts.queued += 1;
    if (tone === "running") counts.running += 1;
    if (tone === "completed") counts.completed += 1;
    if (tone === "failed") counts.failed += 1;
    if (tone === "cancelled") counts.cancelled += 1;
  }
  counts.processed = counts.completed + counts.failed + counts.cancelled;
  return counts;
}
