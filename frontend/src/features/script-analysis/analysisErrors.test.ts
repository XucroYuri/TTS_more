import { describe, expect, it } from "vitest";

import { parseAnalysisError } from "./analysisErrors";

describe("parseAnalysisError", () => {
  it("prefers safe structured fields from a 422 response", () => {
    const parsed = parseAnalysisError({
      status: 422,
      message: "request failed",
      responseBody: JSON.stringify({
        detail: {
          code: "semantic_contract_invalid",
          http_status: 422,
          stage: "analysis",
          message: "Semantic provider response failed validation.",
          retryable: false,
          run_id: "run-safe",
          trace_id: "trace-safe",
          details: { field_paths: ["utterances.3.dialogue_annotation_id"] }
        }
      })
    });
    expect(parsed).toEqual({
      status: 422,
      code: "semantic_contract_invalid",
      stage: "analysis",
      fieldPaths: ["utterances.3.dialogue_annotation_id"],
      runId: "run-safe",
      traceId: "trace-safe",
      message: "Semantic provider response failed validation.",
      retryable: false
    });
  });
});
