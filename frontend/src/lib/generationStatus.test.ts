import { describe, expect, it } from "vitest";

import {
  generationStatusCounts,
  generationStatusKey,
  generationStatusTone,
  isTerminalGenerationStatus,
} from "./generationStatus";

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
});
