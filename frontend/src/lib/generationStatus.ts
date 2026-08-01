import type { GenerationJob, GenerationStatus, QueueItemStatus } from "../types";

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

export type GenerationTerminalNotice =
  | { key: "notice.generated"; level: "success" }
  | { key: "notice.generationFailed"; level: "error" }
  | { key: "notice.generationCancelled"; level: "warning" };

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

export function generationTerminalNotice(
  status: GenerationStatus,
): GenerationTerminalNotice | null {
  if (status === "completed") return { key: "notice.generated", level: "success" };
  if (status === "failed") return { key: "notice.generationFailed", level: "error" };
  if (status === "cancelled") {
    return { key: "notice.generationCancelled", level: "warning" };
  }
  return null;
}

export function reconcileGenerationJobSnapshot(
  current: GenerationJob | null,
  incoming: GenerationJob,
  cancellationRequested: boolean,
): GenerationJob {
  if (
    current
    && current.job_id === incoming.job_id
    && isTerminalGenerationStatus(current.status)
    && !isTerminalGenerationStatus(incoming.status)
  ) {
    return current;
  }
  if (
    !current
    || current.job_id !== incoming.job_id
    || !cancellationRequested
    || isTerminalGenerationStatus(incoming.status)
  ) {
    return incoming;
  }

  const currentItems = new Map(current.items.map((item) => [item.task_id, item]));
  return {
    ...incoming,
    status: cancellationProtectedStatus(current.status, incoming.status),
    progress: Math.max(current.progress, incoming.progress),
    items: incoming.items.map((item) => {
      const currentItem = currentItems.get(item.task_id);
      if (!currentItem) return item;
      return {
        ...item,
        status: cancellationProtectedStatus(currentItem.status, item.status),
        progress: Math.max(currentItem.progress, item.progress),
      };
    }),
  };
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

function cancellationProtectedStatus(
  current: QueueItemStatus,
  incoming: QueueItemStatus,
): QueueItemStatus {
  if (
    (current === "cancelling" || current === "cancelled")
    && !isTerminalGenerationStatus(incoming)
  ) {
    return current;
  }
  return incoming;
}
