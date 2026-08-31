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
  CharacterCandidate,
  DraftOperation,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticAnnotation,
  SemanticConfirmResponse,
  SemanticUtterance
} from "../../types";

export const ANALYSIS_DISMISSED_RUNS_STORAGE_KEY = "tts-more:analysis-dismissed-runs";
export const ANALYSIS_RUN_SESSIONS_STORAGE_KEY = "tts-more:analysis-run-sessions";
const ANALYSIS_CONFIRM_KEYS_STORAGE_KEY = "tts-more:analysis-confirm-keys";
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

interface AnalysisRunSession {
  runId: string;
  draftId: string;
}

interface PendingOperationBatch {
  id: number;
  operations: DraftOperation[];
}

interface ParsedError {
  code: string | null;
  message: string;
  status: number | null;
}

export const defaultAnalysisDraftApi: AnalysisDraftApi = {
  createAnalysisRun,
  fetchAnalysisRun,
  fetchAnalysisDraft,
  patchAnalysisDraft,
  confirmAnalysisDraft
};

function cloneAnnotation(item: SemanticAnnotation): SemanticAnnotation {
  return { ...item, span: { ...item.span } };
}

function cloneCharacter(item: CharacterCandidate): CharacterCandidate {
  return {
    ...item,
    aliases: [...item.aliases],
    supporting_annotation_ids: [...item.supporting_annotation_ids]
  };
}

function cloneUtterance(item: SemanticUtterance): SemanticUtterance {
  return {
    ...item,
    emotion_evidence_annotation_ids: [...item.emotion_evidence_annotation_ids],
    uncertainty_codes: [...item.uncertainty_codes]
  };
}

function operationError(message: string): Error {
  return new Error(`analysis_operation_invalid:${message}`);
}

function findIndex<T extends { id: string }>(items: T[], id: string): number {
  const index = items.findIndex((item) => item.id === id);
  if (index < 0) throw operationError(`missing:${id}`);
  return index;
}

function ensureMissing<T extends { id: string }>(items: T[], id: string): void {
  if (items.some((item) => item.id === id)) throw operationError(`duplicate:${id}`);
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)];
}

/**
 * Applies the backend's controlled draft-operation semantics to an immutable
 * client overlay. The server response remains authoritative after each PATCH;
 * this reducer only keeps later local batches visible while earlier batches
 * are in flight.
 */
export function applyDraftOperations(
  sourceDraft: SemanticAnalysisDraft,
  operations: DraftOperation[]
): SemanticAnalysisDraft {
  const next: SemanticAnalysisDraft = {
    ...sourceDraft,
    annotations: sourceDraft.annotations.map(cloneAnnotation),
    characters: sourceDraft.characters.map(cloneCharacter),
    utterances: sourceDraft.utterances.map(cloneUtterance),
    unresolved_candidates: sourceDraft.unresolved_candidates.map((item) => ({
      ...item,
      details: { ...item.details }
    })),
    warnings: sourceDraft.warnings.map((item) => ({ ...item, details: { ...item.details } }))
  };

  for (const operation of operations) {
    switch (operation.op) {
      case "create_annotation": {
        ensureMissing(next.annotations, operation.annotation.id);
        next.annotations.push(cloneAnnotation(operation.annotation));
        break;
      }
      case "replace_annotation": {
        if (operation.annotation_id !== operation.annotation.id) {
          throw operationError("annotation_id_mismatch");
        }
        next.annotations[findIndex(next.annotations, operation.annotation_id)] = cloneAnnotation(
          operation.annotation
        );
        break;
      }
      case "delete_annotation": {
        const index = findIndex(next.annotations, operation.annotation_id);
        const [removed] = next.annotations.splice(index, 1);
        if (removed.kind === "dialogue") {
          next.utterances = next.utterances.filter(
            (item) => item.dialogue_annotation_id !== removed.id
          );
        } else if (removed.kind === "speaker") {
          next.utterances = next.utterances.map((item) =>
            item.speaker_annotation_id === removed.id
              ? { ...item, speaker_annotation_id: null }
              : item
          );
          next.characters = next.characters.map((item) => ({
            ...item,
            supporting_annotation_ids: item.supporting_annotation_ids.filter(
              (supportId) => supportId !== removed.id
            )
          }));
        } else {
          next.utterances = next.utterances.map((item) => ({
            ...item,
            emotion_evidence_annotation_ids: item.emotion_evidence_annotation_ids.filter(
              (evidenceId) => evidenceId !== removed.id
            )
          }));
        }
        break;
      }
      case "set_annotation_status": {
        const index = findIndex(next.annotations, operation.annotation_id);
        const current = next.annotations[index];
        next.annotations[index] = { ...current, status: operation.status };
        if (current.kind === "dialogue" && operation.status === "rejected") {
          next.utterances = next.utterances.map((item) =>
            item.dialogue_annotation_id === current.id ? { ...item, status: "rejected" } : item
          );
        } else if (current.kind === "dialogue" && operation.status === "pending") {
          next.utterances = next.utterances.map((item) =>
            item.dialogue_annotation_id === current.id && item.status === "accepted"
              ? { ...item, status: "pending" }
              : item
          );
        }
        break;
      }
      case "upsert_character": {
        const index = next.characters.findIndex((item) => item.id === operation.character.id);
        if (index < 0) next.characters.push(cloneCharacter(operation.character));
        else next.characters[index] = cloneCharacter(operation.character);
        break;
      }
      case "set_character_status": {
        const index = findIndex(next.characters, operation.character_id);
        next.characters[index] = { ...next.characters[index], status: operation.status };
        if (operation.status === "rejected") {
          next.utterances = next.utterances.map((item) =>
            item.character_candidate_id === operation.character_id
              ? { ...item, status: "rejected" }
              : item
          );
        } else if (operation.status === "pending") {
          next.utterances = next.utterances.map((item) =>
            item.character_candidate_id === operation.character_id && item.status === "accepted"
              ? { ...item, status: "pending" }
              : item
          );
        }
        break;
      }
      case "merge_characters": {
        if (
          operation.source_character_ids.includes(operation.target_character_id) ||
          new Set(operation.source_character_ids).size !== operation.source_character_ids.length
        ) {
          throw operationError("character_merge_invalid");
        }
        const targetIndex = findIndex(next.characters, operation.target_character_id);
        const sources = operation.source_character_ids.map(
          (sourceId) => next.characters[findIndex(next.characters, sourceId)]
        );
        const target = next.characters[targetIndex];
        next.characters[targetIndex] = {
          ...target,
          aliases: uniqueStrings([
            ...target.aliases,
            ...sources.flatMap((source) => source.aliases)
          ]),
          supporting_annotation_ids: uniqueStrings([
            ...target.supporting_annotation_ids,
            ...sources.flatMap((source) => source.supporting_annotation_ids)
          ])
        };
        const sourceIds = new Set(operation.source_character_ids);
        next.characters = next.characters.filter((item) => !sourceIds.has(item.id));
        next.utterances = next.utterances.map((item) =>
          item.character_candidate_id && sourceIds.has(item.character_candidate_id)
            ? { ...item, character_candidate_id: operation.target_character_id }
            : item
        );
        break;
      }
      case "split_alias": {
        const sourceIndex = findIndex(next.characters, operation.character_id);
        const source = next.characters[sourceIndex];
        if (!source.aliases.includes(operation.alias)) {
          throw operationError("character_alias_missing");
        }
        ensureMissing(next.characters, operation.character.id);
        next.characters[sourceIndex] = {
          ...source,
          aliases: source.aliases.filter((alias) => alias !== operation.alias)
        };
        next.characters.push(cloneCharacter(operation.character));
        break;
      }
      case "create_utterance": {
        ensureMissing(next.utterances, operation.utterance.id);
        next.utterances.push(cloneUtterance(operation.utterance));
        break;
      }
      case "update_utterance": {
        if (operation.utterance_id !== operation.utterance.id) {
          throw operationError("utterance_id_mismatch");
        }
        next.utterances[findIndex(next.utterances, operation.utterance_id)] = cloneUtterance(
          operation.utterance
        );
        break;
      }
      case "delete_utterance": {
        findIndex(next.utterances, operation.utterance_id);
        next.utterances = next.utterances.filter(
          (item) => item.id !== operation.utterance_id
        );
        break;
      }
      case "set_utterance_status": {
        const index = findIndex(next.utterances, operation.utterance_id);
        next.utterances[index] = { ...next.utterances[index], status: operation.status };
        break;
      }
      case "dismiss_warning": {
        findIndex(next.warnings, operation.warning_id);
        next.warnings = next.warnings.filter((item) => item.id !== operation.warning_id);
        break;
      }
    }
  }

  return next;
}

function defaultIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `confirm-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function defaultStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function safeRemove(storage: Storage | null, key: string): void {
  try {
    storage?.removeItem(key);
  } catch {
    // Persistence is best-effort; controller state remains usable in memory.
  }
}

function readObject(storage: Storage | null, key: string): Record<string, unknown> {
  if (!storage) return {};
  try {
    const raw = storage.getItem(key);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("storage_shape_invalid");
    }
    return parsed as Record<string, unknown>;
  } catch {
    safeRemove(storage, key);
    return {};
  }
}

function writeObject(storage: Storage | null, key: string, value: Record<string, unknown>): void {
  if (!storage) return;
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch {
    // Local persistence failures must not break editing in the current tab.
  }
}

function readDismissedRuns(storage: Storage | null): Set<string> {
  if (!storage) return new Set();
  try {
    const raw = storage.getItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string")) {
      throw new Error("dismissed_runs_invalid");
    }
    return new Set(parsed);
  } catch {
    safeRemove(storage, ANALYSIS_DISMISSED_RUNS_STORAGE_KEY);
    return new Set();
  }
}

function writeDismissedRuns(storage: Storage | null, runIds: Set<string>): void {
  if (!storage) return;
  try {
    storage.setItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY, JSON.stringify([...runIds]));
  } catch {
    // Dismissal still applies to the mounted controller when storage is unavailable.
  }
}

function scopeStorageId(projectId: string, sourceRevision: ScriptRevision): string {
  return JSON.stringify([
    projectId,
    sourceRevision.revision_id,
    sourceRevision.source_sha256 ?? null
  ]);
}

function readSession(storage: Storage | null, scopeId: string): AnalysisRunSession | null {
  const sessions = readObject(storage, ANALYSIS_RUN_SESSIONS_STORAGE_KEY);
  const value = sessions[scopeId];
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as { runId?: unknown; draftId?: unknown };
  if (typeof candidate.runId !== "string" || typeof candidate.draftId !== "string") {
    delete sessions[scopeId];
    writeObject(storage, ANALYSIS_RUN_SESSIONS_STORAGE_KEY, sessions);
    return null;
  }
  return { runId: candidate.runId, draftId: candidate.draftId };
}

function writeSession(
  storage: Storage | null,
  scopeId: string,
  session: AnalysisRunSession
): void {
  const sessions = readObject(storage, ANALYSIS_RUN_SESSIONS_STORAGE_KEY);
  sessions[scopeId] = session;
  writeObject(storage, ANALYSIS_RUN_SESSIONS_STORAGE_KEY, sessions);
}

function removeSession(storage: Storage | null, scopeId: string): void {
  const sessions = readObject(storage, ANALYSIS_RUN_SESSIONS_STORAGE_KEY);
  if (!(scopeId in sessions)) return;
  delete sessions[scopeId];
  writeObject(storage, ANALYSIS_RUN_SESSIONS_STORAGE_KEY, sessions);
}

function readConfirmKey(storage: Storage | null, draftId: string): string | null {
  const keys = readObject(storage, ANALYSIS_CONFIRM_KEYS_STORAGE_KEY);
  return typeof keys[draftId] === "string" ? (keys[draftId] as string) : null;
}

function writeConfirmKey(storage: Storage | null, draftId: string, key: string): void {
  const keys = readObject(storage, ANALYSIS_CONFIRM_KEYS_STORAGE_KEY);
  keys[draftId] = key;
  writeObject(storage, ANALYSIS_CONFIRM_KEYS_STORAGE_KEY, keys);
}

function parseError(error: unknown): ParsedError {
  const errorRecord =
    error && typeof error === "object"
      ? (error as {
          message?: unknown;
          status?: unknown;
          code?: unknown;
          responseBody?: unknown;
        })
      : null;
  const fallbackMessage =
    typeof errorRecord?.message === "string" ? errorRecord.message : String(error);
  const fallbackStatus =
    typeof errorRecord?.status === "number" ? errorRecord.status : null;
  const fallbackCode = typeof errorRecord?.code === "string" ? errorRecord.code : null;

  const structuredBody =
    typeof errorRecord?.responseBody === "string" ? errorRecord.responseBody : fallbackMessage;

  try {
    const parsed: unknown = JSON.parse(structuredBody);
    if (parsed && typeof parsed === "object") {
      const container = parsed as {
        detail?: unknown;
        code?: unknown;
        message?: unknown;
        status?: unknown;
      };
      const detail = container.detail;
      if (detail && typeof detail === "object" && !Array.isArray(detail)) {
        const structured = detail as { code?: unknown; message?: unknown; http_status?: unknown };
        return {
          code: typeof structured.code === "string" ? structured.code : fallbackCode,
          message:
            typeof structured.message === "string" ? structured.message : fallbackMessage,
          status:
            typeof structured.http_status === "number"
              ? structured.http_status
              : fallbackStatus
        };
      }
      if (typeof detail === "string") {
        return { code: fallbackCode, message: detail, status: fallbackStatus };
      }
      return {
        code: typeof container.code === "string" ? container.code : fallbackCode,
        message:
          typeof container.message === "string" ? container.message : fallbackMessage,
        status: typeof container.status === "number" ? container.status : fallbackStatus
      };
    }
  } catch {
    // Plain text and already-normalized Error messages use the fallback below.
  }

  return { code: fallbackCode, message: fallbackMessage, status: fallbackStatus };
}

function controllerError(
  kind: AnalysisControllerError["kind"],
  error: unknown
): AnalysisControllerError {
  const parsed = parseError(error);
  return { kind, code: parsed.code, message: parsed.message };
}

function notFoundError(error: unknown): boolean {
  const parsed = parseError(error);
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

function isTerminal(status: AnalysisRunStatus): boolean {
  return status === "completed" || status === "failed" || status === "interrupted";
}

export function useAnalysisDraft(
  projectId: string,
  sourceRevision: ScriptRevision,
  options: UseAnalysisDraftOptions = {}
): UseAnalysisDraftResult {
  const api = options.api ?? defaultAnalysisDraftApi;
  const storage = options.storage === undefined ? defaultStorage() : options.storage;
  const pollIntervalMs = options.pollIntervalMs ?? defaultPollIntervalMs;
  const createIdempotencyKey = options.createIdempotencyKey ?? defaultIdempotencyKey;
  const sourceIdentity = `${sourceRevision.revision_id}\u0000${
    sourceRevision.source_sha256 ?? ""
  }`;
  const scopeId = scopeStorageId(projectId, sourceRevision);

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
    let visible = serverDraft;
    for (const batch of pendingBatchesRef.current) {
      visible = applyDraftOperations(visible, batch.operations);
    }
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
          const parsed = parseError(error);
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
    dismissedRunsRef.current = readDismissedRuns(storage);
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
        writeConfirmKey(storage, loadedDraft.id, loadedDraft.confirm_idempotency_key);
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
      setIsRunning(true);
      try {
        const created = await api.createAnalysisRun(projectId, sourceRevision.revision_id);
        if (!active()) return;
        writeSession(storage, scopeId, {
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
      removeSession(storage, scopeId);
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
      const session = readSession(storage, scopeId);
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
          let visible = authoritative;
          for (const batch of pendingBatchesRef.current) {
            visible = applyDraftOperations(visible, batch.operations);
          }
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
    writeDismissedRuns(storage, dismissed);
    displayedErrorRunIdRef.current = null;
    setAnalysisErrorState(null);
  }, [storage]);

  const retryAnalysis = useCallback((): void => {
    removeSession(storage, scopeId);
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
        readConfirmKey(storage, serverDraft.id) ??
        createIdempotencyKey();
      confirmKeysRef.current.set(serverDraft.id, idempotencyKey);
      writeConfirmKey(storage, serverDraft.id, idempotencyKey);
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
