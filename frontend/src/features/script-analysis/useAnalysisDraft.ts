import { useCallback, useEffect, useRef, useState } from "react";

import {
  confirmAnalysisDraft,
  createAnalysisRun,
  fetchAnalysisDraft,
  fetchAnalysisRun,
  patchAnalysisDraft
} from "../../api";
import type {
  AnalysisError,
  AnalysisRun,
  AnalysisRunStatus,
  DraftOperation,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticConfirmResponse
} from "../../types";
import { parseAnalysisError } from "./analysisErrors";
import { applyQueuedDraftOperations } from "./analysisOperationQueue";
import {
  analysisScopeStorageId,
  defaultAnalysisStorage,
  readAnalysisConfirmKey,
  readAnalysisRunSession,
  readDismissedAnalysisRuns,
  removeAnalysisRunSession,
  writeAnalysisConfirmKey,
  writeAnalysisRunSession,
  writeDismissedAnalysisRuns
} from "./analysisSessionStorage";

export { applyDraftOperations } from "./analysisOperationQueue";
export {
  ANALYSIS_CACHE_RESET_STORAGE_KEY,
  ANALYSIS_CACHE_RESET_VERSION,
  ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY,
  ANALYSIS_DISMISSED_RUNS_STORAGE_KEY,
  ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY,
  ANALYSIS_RUN_SESSIONS_STORAGE_KEY,
  activeAnalysisScopeForRevision,
  activeAnalysisScopeMatchesRevision,
  archiveRestorableAnalysisSession,
  clearActiveAnalysisScope,
  clearRestorableAnalysisSession,
  hasRestorableAnalysisSession,
  hasReviewableAnalysisSession,
  readActiveAnalysisScope,
  restoreReviewableAnalysisSession,
  writeActiveAnalysisScope,
  type ActiveAnalysisScope
} from "./analysisSessionStorage";

const defaultPollIntervalMs = 1_000;

export interface AnalysisDraftApi {
  createAnalysisRun: (
    projectId: string,
    sourceRevisionId: string
  ) => Promise<{
    run_id: string;
    draft_id: string;
    status: AnalysisRunStatus;
    trace_id: string | null;
  }>;
  fetchAnalysisRun: (runId: string) => Promise<AnalysisRun>;
  fetchAnalysisDraft: (draftId: string) => Promise<SemanticAnalysisDraft>;
  patchAnalysisDraft: (
    draftId: string,
    expectedVersion: number,
    operations: DraftOperation[]
  ) => Promise<SemanticAnalysisDraft>;
  confirmAnalysisDraft: (
    draftId: string,
    expectedVersion: number,
    idempotencyKey: string
  ) => Promise<SemanticConfirmResponse>;
}

export interface AnalysisControllerError {
  kind: "attach" | "create" | "poll" | "draft" | "patch" | "confirm" | "local";
  code: string | null;
  httpStatus: number | null;
  stage: string | null;
  message: string;
}

export interface AnalysisDraftConflict {
  code: string;
  message: string;
}

export interface UseAnalysisDraftOptions {
  api?: AnalysisDraftApi;
  storage?: Storage | null;
  pollIntervalMs?: number;
  createIdempotencyKey?: () => string;
  mode?: "analyze" | "review";
}

export interface UseAnalysisDraftResult {
  run: AnalysisRun | null;
  draft: SemanticAnalysisDraft | null;
  error: AnalysisError | null;
  controllerError: AnalysisControllerError | null;
  conflict: AnalysisDraftConflict | null;
  isRunning: boolean;
  isSaving: boolean;
  isConfirming: boolean;
  isReadOnly: boolean;
  pendingOperationBatches: number;
  queueOperations: (operations: DraftOperation[]) => boolean;
  retryPendingOperations: () => void;
  dismissError: () => void;
  retryAnalysis: () => void;
  confirm: () => Promise<SemanticConfirmResponse>;
}

interface PendingOperationBatch {
  id: number;
  operations: DraftOperation[];
}

export const defaultAnalysisDraftApi: AnalysisDraftApi = {
  createAnalysisRun,
  fetchAnalysisRun,
  fetchAnalysisDraft,
  patchAnalysisDraft,
  confirmAnalysisDraft
};

function defaultIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `confirm-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function controllerError(
  kind: AnalysisControllerError["kind"],
  error: unknown
): AnalysisControllerError {
  const parsed = parseAnalysisError(error);
  return {
    kind,
    code: parsed.code,
    httpStatus: parsed.status,
    stage: parsed.stage,
    message: parsed.message
  };
}

function notFoundError(error: unknown): boolean {
  const parsed = parseAnalysisError(error);
  return (
    parsed.status === 404 ||
    parsed.code === "semantic_not_found" ||
    parsed.code === "run_not_found" ||
    parsed.code === "draft_not_found"
  );
}

function staleSessionError(code: "run_not_found" | "draft_not_found"): Error {
  return new Error(JSON.stringify({ detail: { code, message: "stored analysis session is stale" } }));
}

function reviewSessionUnavailableError(): Error {
  return new Error(JSON.stringify({
    detail: {
      code: "analysis_review_not_found",
      message: "confirmed analysis result is unavailable"
    }
  }));
}

function isTerminal(status: AnalysisRunStatus): boolean {
  return status === "completed" || status === "failed" || status === "interrupted";
}

export function useAnalysisDraft(
  projectId: string,
  sourceRevision: ScriptRevision,
  options: UseAnalysisDraftOptions = {}
): UseAnalysisDraftResult {
  const api = options.api ?? defaultAnalysisDraftApi;
  const storage = options.storage === undefined ? defaultAnalysisStorage() : options.storage;
  const pollIntervalMs = options.pollIntervalMs ?? defaultPollIntervalMs;
  const createIdempotencyKey = options.createIdempotencyKey ?? defaultIdempotencyKey;
  const mode = options.mode ?? "analyze";
  const sourceIdentity = `${sourceRevision.revision_id}\u0000${
    sourceRevision.source_sha256 ?? ""
  }`;
  const scopeId = analysisScopeStorageId(projectId, sourceRevision);

  const [runState, setRunState] = useState<AnalysisRun | null>(null);
  const [draftState, setDraftState] = useState<SemanticAnalysisDraft | null>(null);
  const [analysisErrorState, setAnalysisErrorState] = useState<AnalysisError | null>(null);
  const [controllerErrorState, setControllerErrorState] =
    useState<AnalysisControllerError | null>(null);
  const [conflictState, setConflictState] = useState<AnalysisDraftConflict | null>(null);
  const [isRunning, setIsRunning] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [isReadOnly, setIsReadOnly] = useState(true);
  const [pendingOperationBatches, setPendingOperationBatches] = useState(0);
  const [analysisRetry, setAnalysisRetry] = useState(0);

  const lifecycleEpochRef = useRef(0);
  const queueGenerationRef = useRef(0);
  const runRef = useRef<AnalysisRun | null>(null);
  const serverDraftRef = useRef<SemanticAnalysisDraft | null>(null);
  const visibleDraftRef = useRef<SemanticAnalysisDraft | null>(null);
  const editableRef = useRef(false);
  const dismissedRunsRef = useRef<Set<string>>(new Set());
  const pendingBatchesRef = useRef<PendingOperationBatch[]>([]);
  const nextBatchIdRef = useRef(1);
  const patchInFlightRef = useRef<Promise<void> | null>(null);
  const queueFrozenRef = useRef(false);
  const patchFailedRef = useRef(false);
  const confirmInFlightRef = useRef<Promise<SemanticConfirmResponse> | null>(null);
  const rebaseInFlightRef = useRef<Promise<void> | null>(null);
  const confirmKeysRef = useRef<Map<string, string>>(new Map());
  const activeScopeRef = useRef<string | null>(null);
  const displayedErrorRunIdRef = useRef<string | null>(null);

  const rebuildVisibleDraft = useCallback((queueGeneration: number): void => {
    if (queueGenerationRef.current !== queueGeneration) return;
    const serverDraft = serverDraftRef.current;
    if (!serverDraft) return;
    const visible = applyQueuedDraftOperations(serverDraft, pendingBatchesRef.current);
    visibleDraftRef.current = visible;
    setDraftState(visible);
  }, []);

  const drainPendingOperations = useCallback((): Promise<void> => {
    if (patchInFlightRef.current) return patchInFlightRef.current;
    if (queueFrozenRef.current || !serverDraftRef.current || pendingBatchesRef.current.length === 0) {
      return Promise.resolve();
    }

    const queueGeneration = queueGenerationRef.current;
    let trackedPromise: Promise<void>;
    const work = async () => {
      setIsSaving(true);
      patchFailedRef.current = false;
      while (
        queueGenerationRef.current === queueGeneration &&
        !queueFrozenRef.current &&
        pendingBatchesRef.current.length > 0
      ) {
        const batch = pendingBatchesRef.current[0];
        const serverDraft = serverDraftRef.current;
        if (!serverDraft) return;
        try {
          const updated = await api.patchAnalysisDraft(
            serverDraft.id,
            serverDraft.version,
            batch.operations
          );
          if (queueGenerationRef.current !== queueGeneration) return;
          if (pendingBatchesRef.current[0]?.id !== batch.id) return;
          pendingBatchesRef.current.shift();
          serverDraftRef.current = updated;
          setPendingOperationBatches(pendingBatchesRef.current.length);
          rebuildVisibleDraft(queueGeneration);
          setControllerErrorState(null);
        } catch (error) {
          if (queueGenerationRef.current !== queueGeneration) return;
          const parsed = parseAnalysisError(error);
          if (
            parsed.code === "draft_version_conflict" ||
            parsed.code === "semantic_conflict" ||
            parsed.status === 409
          ) {
            const conflict = {
              code: parsed.code ?? "draft_version_conflict",
              message: parsed.message
            };
            queueFrozenRef.current = true;
            setConflictState(conflict);
          } else {
            patchFailedRef.current = true;
            setControllerErrorState(controllerError("patch", error));
          }
          return;
        }
      }
    };

    trackedPromise = work().finally(() => {
      if (queueGenerationRef.current !== queueGeneration) return;
      if (patchInFlightRef.current === trackedPromise) patchInFlightRef.current = null;
      setIsSaving(false);
    });
    patchInFlightRef.current = trackedPromise;
    return trackedPromise;
  }, [api, rebuildVisibleDraft]);

  useEffect(() => {
    const epoch = ++lifecycleEpochRef.current;
    const queueGeneration = ++queueGenerationRef.current;
    const scopeChanged = activeScopeRef.current !== scopeId;
    activeScopeRef.current = scopeId;
    let cancelled = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let staleFallbackUsed = false;

    const active = () => !cancelled && lifecycleEpochRef.current === epoch;
    const clearPollTimer = () => {
      if (pollTimer !== null) clearTimeout(pollTimer);
      pollTimer = null;
    };

    runRef.current = null;
    serverDraftRef.current = null;
    visibleDraftRef.current = null;
    editableRef.current = false;
    dismissedRunsRef.current = readDismissedAnalysisRuns(storage);
    pendingBatchesRef.current = [];
    nextBatchIdRef.current = 1;
    patchInFlightRef.current = null;
    queueFrozenRef.current = false;
    patchFailedRef.current = false;
    confirmInFlightRef.current = null;
    rebaseInFlightRef.current = null;
    setRunState(null);
    setDraftState(null);
    if (scopeChanged) {
      displayedErrorRunIdRef.current = null;
      setAnalysisErrorState(null);
    }
    setControllerErrorState(null);
    setConflictState(null);
    setIsRunning(true);
    setIsSaving(false);
    setIsConfirming(false);
    setIsReadOnly(true);
    setPendingOperationBatches(0);

    const acceptDraft = (loadedDraft: SemanticAnalysisDraft, expectedDraftId: string) => {
      if (
        loadedDraft.id !== expectedDraftId ||
        loadedDraft.project_id !== projectId ||
        loadedDraft.source_revision_id !== sourceRevision.revision_id
      ) {
        throw staleSessionError("draft_not_found");
      }
      serverDraftRef.current = loadedDraft;
      visibleDraftRef.current = loadedDraft;
      editableRef.current = !loadedDraft.confirmed_revision_id;
      setDraftState(loadedDraft);
      setIsReadOnly(!editableRef.current);
      if (loadedDraft.confirm_idempotency_key) {
        confirmKeysRef.current.set(loadedDraft.id, loadedDraft.confirm_idempotency_key);
        writeAnalysisConfirmKey(storage, loadedDraft.id, loadedDraft.confirm_idempotency_key);
      }
    };

    const loadTerminalDraft = async (record: AnalysisRun): Promise<void> => {
      const loadedDraft = await api.fetchAnalysisDraft(record.draft_id);
      if (!active()) return;
      acceptDraft(loadedDraft, record.draft_id);
    };

    const validateRun = (record: AnalysisRun, expectedDraftId: string): void => {
      if (
        record.project_id !== projectId ||
        record.source_revision_id !== sourceRevision.revision_id ||
        record.draft_id !== expectedDraftId
      ) {
        throw staleSessionError("run_not_found");
      }
    };

    let poll: (runId: string, draftId: string) => Promise<void>;

    const acceptRun = async (record: AnalysisRun, expectedDraftId: string): Promise<void> => {
      validateRun(record, expectedDraftId);
      if (!active()) return;
      runRef.current = record;
      setRunState(record);
      setControllerErrorState(null);
      const terminal = isTerminal(record.status);
      setIsRunning(!terminal);
      if (record.status === "completed") {
        displayedErrorRunIdRef.current = null;
        setAnalysisErrorState(null);
      } else if (record.status === "failed" || record.status === "interrupted") {
        const visibleError =
          record.error && !dismissedRunsRef.current.has(record.id) ? record.error : null;
        displayedErrorRunIdRef.current = visibleError ? record.id : null;
        setAnalysisErrorState(visibleError);
      }
      if (terminal) {
        clearPollTimer();
        await loadTerminalDraft(record);
        return;
      }
      pollTimer = setTimeout(() => {
        pollTimer = null;
        void poll(record.id, record.draft_id);
      }, pollIntervalMs);
    };

    const createFresh = async (): Promise<void> => {
      setRunState(null);
      setDraftState(null);
      setControllerErrorState(null);
      setConflictState(null);
      if (mode === "review") {
        setIsRunning(false);
        setControllerErrorState(controllerError("attach", reviewSessionUnavailableError()));
        return;
      }
      setIsRunning(true);
      try {
        const created = await api.createAnalysisRun(projectId, sourceRevision.revision_id);
        if (!active()) return;
        writeAnalysisRunSession(storage, scopeId, {
          runId: created.run_id,
          draftId: created.draft_id
        });
        const record = await api.fetchAnalysisRun(created.run_id);
        if (!active()) return;
        await acceptRun(record, created.draft_id);
      } catch (error) {
        if (!active()) return;
        setIsRunning(false);
        setControllerErrorState(controllerError("create", error));
      }
    };

    const fallbackFromStaleSession = async (): Promise<void> => {
      if (staleFallbackUsed) throw staleSessionError("run_not_found");
      staleFallbackUsed = true;
      removeAnalysisRunSession(storage, scopeId);
      await createFresh();
    };

    poll = async (runId: string, draftId: string): Promise<void> => {
      try {
        const record = await api.fetchAnalysisRun(runId);
        if (!active()) return;
        await acceptRun(record, draftId);
      } catch (error) {
        if (!active()) return;
        if (notFoundError(error) && !staleFallbackUsed) {
          try {
            await fallbackFromStaleSession();
          } catch (fallbackError) {
            if (!active()) return;
            setIsRunning(false);
            setControllerErrorState(controllerError("poll", fallbackError));
          }
          return;
        }
        setIsRunning(false);
        setControllerErrorState(controllerError("poll", error));
      }
    };

    const attachOrCreate = async (): Promise<void> => {
      const session = readAnalysisRunSession(storage, scopeId);
      if (!session) {
        await createFresh();
        return;
      }
      try {
        const record = await api.fetchAnalysisRun(session.runId);
        if (!active()) return;
        await acceptRun(record, session.draftId);
      } catch (error) {
        if (!active()) return;
        if (notFoundError(error)) {
          try {
            await fallbackFromStaleSession();
          } catch (fallbackError) {
            if (!active()) return;
            setIsRunning(false);
            setControllerErrorState(controllerError("attach", fallbackError));
          }
          return;
        }
        setIsRunning(false);
        setControllerErrorState(controllerError("attach", error));
      }
    };

    void attachOrCreate();

    return () => {
      cancelled = true;
      clearPollTimer();
      if (lifecycleEpochRef.current === epoch) lifecycleEpochRef.current += 1;
      if (queueGenerationRef.current === queueGeneration) queueGenerationRef.current += 1;
      editableRef.current = false;
    };
  }, [
    analysisRetry,
    api,
    mode,
    pollIntervalMs,
    projectId,
    scopeId,
    sourceIdentity,
    sourceRevision.revision_id,
    storage
  ]);

  const queueOperations = useCallback(
    (operations: DraftOperation[]): boolean => {
      if (!editableRef.current || operations.length === 0 || queueFrozenRef.current) return false;
      const queueGeneration = queueGenerationRef.current;
      const batch: PendingOperationBatch = {
        id: nextBatchIdRef.current,
        operations
      };
      nextBatchIdRef.current += 1;
      pendingBatchesRef.current.push(batch);
      try {
        rebuildVisibleDraft(queueGeneration);
      } catch (error) {
        pendingBatchesRef.current.pop();
        setControllerErrorState(controllerError("local", error));
        return false;
      }
      setPendingOperationBatches(pendingBatchesRef.current.length);
      setControllerErrorState(null);
      patchFailedRef.current = false;
      void drainPendingOperations();
      return true;
    },
    [drainPendingOperations, rebuildVisibleDraft]
  );

  const retryPendingOperations = useCallback((): void => {
    const serverDraft = serverDraftRef.current;
    if (pendingBatchesRef.current.length === 0 || !serverDraft) return;
    if (queueFrozenRef.current) {
      if (rebaseInFlightRef.current) return;
      const queueGeneration = queueGenerationRef.current;
      let trackedPromise: Promise<void>;
      const rebase = async () => {
        setIsSaving(true);
        setControllerErrorState(null);
        try {
          const authoritative = await api.fetchAnalysisDraft(serverDraft.id);
          if (queueGenerationRef.current !== queueGeneration) return;
          if (
            authoritative.id !== serverDraft.id ||
            authoritative.project_id !== projectId ||
            authoritative.source_revision_id !== sourceRevision.revision_id ||
            !Number.isInteger(authoritative.version) ||
            authoritative.version < serverDraft.version ||
            authoritative.confirmed_revision_id !== null
          ) {
            throw new Error("analysis_rebase_identity_invalid");
          }
          const visible = applyQueuedDraftOperations(authoritative, pendingBatchesRef.current);
          serverDraftRef.current = authoritative;
          visibleDraftRef.current = visible;
          setDraftState(visible);
          queueFrozenRef.current = false;
          patchFailedRef.current = false;
          setConflictState(null);
          await drainPendingOperations();
        } catch (error) {
          if (queueGenerationRef.current !== queueGeneration) return;
          setControllerErrorState(controllerError("patch", error));
        } finally {
          if (queueGenerationRef.current === queueGeneration) setIsSaving(false);
        }
      };
      trackedPromise = rebase().finally(() => {
        if (rebaseInFlightRef.current === trackedPromise) rebaseInFlightRef.current = null;
      });
      rebaseInFlightRef.current = trackedPromise;
      return;
    }
    patchFailedRef.current = false;
    setControllerErrorState(null);
    void drainPendingOperations();
  }, [api, drainPendingOperations, projectId, sourceRevision.revision_id]);

  const dismissError = useCallback((): void => {
    const displayedRunId = displayedErrorRunIdRef.current;
    if (!displayedRunId) return;
    const dismissed = new Set(dismissedRunsRef.current);
    dismissed.add(displayedRunId);
    dismissedRunsRef.current = dismissed;
    writeDismissedAnalysisRuns(storage, dismissed);
    displayedErrorRunIdRef.current = null;
    setAnalysisErrorState(null);
  }, [storage]);

  const retryAnalysis = useCallback((): void => {
    removeAnalysisRunSession(storage, scopeId);
    setAnalysisRetry((value) => value + 1);
  }, [scopeId, storage]);

  const confirm = useCallback((): Promise<SemanticConfirmResponse> => {
    if (confirmInFlightRef.current) return confirmInFlightRef.current;
    const queueGeneration = queueGenerationRef.current;
    let trackedPromise: Promise<SemanticConfirmResponse>;
    const work = async (): Promise<SemanticConfirmResponse> => {
      if (queueFrozenRef.current) {
        throw new Error(conflictState?.message ?? "draft_version_conflict");
      }
      if (pendingBatchesRef.current.length > 0 || patchInFlightRef.current) {
        if (patchFailedRef.current) {
          patchFailedRef.current = false;
          setControllerErrorState(null);
        }
        await drainPendingOperations();
      }
      if (queueGenerationRef.current !== queueGeneration) {
        throw new Error("analysis_scope_changed");
      }
      if (
        queueFrozenRef.current ||
        patchFailedRef.current ||
        pendingBatchesRef.current.length > 0
      ) {
        throw new Error(
          queueFrozenRef.current ? "draft_version_conflict" : "draft_patch_pending"
        );
      }
      const serverDraft = serverDraftRef.current;
      if (!serverDraft || !runRef.current || !isTerminal(runRef.current.status)) {
        throw new Error("analysis_draft_unavailable");
      }
      const idempotencyKey =
        serverDraft.confirm_idempotency_key ??
        confirmKeysRef.current.get(serverDraft.id) ??
        readAnalysisConfirmKey(storage, serverDraft.id) ??
        createIdempotencyKey();
      confirmKeysRef.current.set(serverDraft.id, idempotencyKey);
      writeAnalysisConfirmKey(storage, serverDraft.id, idempotencyKey);
      setIsConfirming(true);
      setControllerErrorState(null);
      try {
        const response = await api.confirmAnalysisDraft(
          serverDraft.id,
          serverDraft.version,
          idempotencyKey
        );
        if (queueGenerationRef.current === queueGeneration) {
          editableRef.current = false;
          setIsReadOnly(true);
        }
        return response;
      } catch (error) {
        if (queueGenerationRef.current === queueGeneration) {
          setControllerErrorState(controllerError("confirm", error));
        }
        throw error;
      } finally {
        if (queueGenerationRef.current === queueGeneration) setIsConfirming(false);
      }
    };

    trackedPromise = work().finally(() => {
      if (confirmInFlightRef.current === trackedPromise) confirmInFlightRef.current = null;
    });
    confirmInFlightRef.current = trackedPromise;
    return trackedPromise;
  }, [
    api,
    conflictState?.message,
    createIdempotencyKey,
    drainPendingOperations,
    storage
  ]);

  return {
    run: runState,
    draft: draftState,
    error: analysisErrorState,
    controllerError: controllerErrorState,
    conflict: conflictState,
    isRunning,
    isSaving,
    isConfirming,
    isReadOnly,
    pendingOperationBatches,
    queueOperations,
    retryPendingOperations,
    dismissError,
    retryAnalysis,
    confirm
  };
}
