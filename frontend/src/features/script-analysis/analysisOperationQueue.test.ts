import { describe, expect, it } from "vitest";

import type { SemanticAnalysisDraft } from "../../types";
import { applyQueuedDraftOperations } from "./analysisOperationQueue";

describe("applyQueuedDraftOperations", () => {
  it("layers pending batches over the authoritative draft without mutating it", () => {
    const draft = {
      id: "draft-1",
      version: 1,
      annotations: [{ id: "dialogue-1", kind: "dialogue", status: "pending", span: {} }],
      characters: [],
      utterances: [],
      unresolved_candidates: [],
      warnings: []
    } as unknown as SemanticAnalysisDraft;
    const visible = applyQueuedDraftOperations(draft, [{
      operations: [{ op: "set_annotation_status", annotation_id: "dialogue-1", status: "accepted" }]
    }]);
    expect(visible.annotations[0].status).toBe("accepted");
    expect(draft.annotations[0].status).toBe("pending");
  });
});
