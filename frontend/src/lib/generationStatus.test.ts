import { describe, expect, it } from "vitest";

import type { GenerationJob, QueueItemStatus } from "../types";
import {
  generationTerminalNotice,
  generationStatusCounts,
  generationStatusKey,
  generationStatusTone,
  isTerminalGenerationStatus,
  reconcileGenerationJobSnapshot,
} from "./generationStatus";

function job(
  status: QueueItemStatus,
  options: {
    jobId?: string;
    progress?: number;
    itemStatus?: QueueItemStatus;
    itemProgress?: number;
    externalStatus?: string;
  } = {},
): GenerationJob {
  return {
    job_id: options.jobId ?? "job-1",
    project_id: "project-1",
    status,
    progress: options.progress ?? 0.5,
    items: [
      {
        task_id: "task-1",
        line_id: "line-1",
        status: options.itemStatus ?? status,
        progress: options.itemProgress ?? options.progress ?? 0.5,
        cluster_key: "cluster-1",
        external_status: options.externalStatus,
      },
    ],
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:01Z",
  };
}

describe("generation status semantics", () => {
  it("keeps cancelling active and cancelled non-failure", () => {
    expect(isTerminalGenerationStatus("cancelling")).toBe(false);
    expect(isTerminalGenerationStatus("cancelled")).toBe(true);
    expect(generationStatusTone("cancelling")).toBe("running");
    expect(generationStatusTone("cancelled")).toBe("cancelled");
    expect(generationStatusKey("cancelling")).toBe("status.cancelling");
    expect(generationStatusKey("cancelled")).toBe("status.cancelled");
  });

  it("counts failed and cancelled separately while processing both", () => {
    expect(
      generationStatusCounts([
        "queued",
        "loading",
        "running",
        "finalizing",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
      ]),
    ).toEqual({
      queued: 1,
      running: 4,
      completed: 1,
      failed: 1,
      cancelled: 1,
      processed: 3,
      total: 8,
    });
  });

  it("uses final backend status for the notice after cancellation acceptance", () => {
    expect(generationTerminalNotice("failed")).toEqual({
      key: "notice.generationFailed",
      level: "error",
    });
    expect(generationTerminalNotice("cancelled")).toEqual({
      key: "notice.generationCancelled",
      level: "warning",
    });
    expect(generationTerminalNotice("completed")).toEqual({
      key: "notice.generated",
      level: "success",
    });
  });

  it("does not let stale active snapshots regress accepted cancellation", () => {
    const cancelling = job("cancelling", {
      progress: 0.5,
      itemStatus: "cancelling",
      itemProgress: 0.5,
      externalStatus: "interrupt_requested",
    });
    const staleRunning = job("running", {
      progress: 0.7,
      itemStatus: "running",
      itemProgress: 0.7,
      externalStatus: "execution_cached",
    });

    expect(reconcileGenerationJobSnapshot(cancelling, staleRunning, true)).toEqual({
      ...staleRunning,
      status: "cancelling",
      progress: 0.7,
      items: [
        {
          ...staleRunning.items[0],
          status: "cancelling",
          progress: 0.7,
        },
      ],
    });

    for (const staleStatus of ["queued", "loading", "running", "finalizing"] as const) {
      expect(
        reconcileGenerationJobSnapshot(job("cancelled"), job(staleStatus), true).status,
      ).toBe("cancelled");
    }
  });

  it("accepts newer terminal truth and does not carry cancellation to a new job", () => {
    const failed = job("failed", { progress: 1, itemStatus: "failed", itemProgress: 1 });
    expect(reconcileGenerationJobSnapshot(job("cancelling"), failed, true)).toBe(failed);
    expect(reconcileGenerationJobSnapshot(failed, job("cancelling"), true)).toBe(failed);

    const nextJob = job("running", { jobId: "job-2" });
    expect(reconcileGenerationJobSnapshot(job("cancelling"), nextJob, true)).toBe(nextJob);
  });
});
