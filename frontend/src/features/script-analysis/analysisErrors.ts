export interface ParsedAnalysisError {
  status: number | null;
  code: string | null;
  stage: string | null;
  fieldPaths: string[];
  runId: string | null;
  traceId: string | null;
  message: string;
  retryable: boolean;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function fieldPaths(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const record = value as Record<string, unknown>;
  const candidates = record.field_paths ?? record.fieldPaths;
  return Array.isArray(candidates)
    ? [...new Set(candidates.filter((item): item is string => typeof item === "string"))]
    : [];
}

export function parseAnalysisError(error: unknown): ParsedAnalysisError {
  const record = error && typeof error === "object"
    ? error as Record<string, unknown>
    : {};
  const fallbackMessage = stringValue(record.message) ?? String(error);
  const fallbackStatus = numberValue(record.status);
  const fallbackCode = stringValue(record.code);
  const responseBody = stringValue(record.responseBody) ?? fallbackMessage;
  let payload: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = JSON.parse(responseBody);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      payload = parsed as Record<string, unknown>;
    }
  } catch {
    // Plain-text failures use the safe fallbacks below.
  }
  const detail = payload?.detail;
  if (typeof detail === "string") {
    return {
      status: fallbackStatus,
      code: fallbackCode,
      stage: null,
      fieldPaths: [],
      runId: null,
      traceId: null,
      message: detail,
      retryable: false
    };
  }
  const structured = detail && typeof detail === "object" && !Array.isArray(detail)
    ? detail as Record<string, unknown>
    : payload;
  return {
    status: numberValue(structured?.http_status) ?? numberValue(structured?.status) ?? fallbackStatus,
    code: stringValue(structured?.code) ?? fallbackCode,
    stage: stringValue(structured?.stage),
    fieldPaths: fieldPaths(structured?.details),
    runId: stringValue(structured?.run_id),
    traceId: stringValue(structured?.trace_id),
    message: stringValue(structured?.message) ?? fallbackMessage,
    retryable: typeof structured?.retryable === "boolean" ? structured.retryable : false
  };
}
