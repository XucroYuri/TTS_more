import type { ScriptRevision } from "../../types";

export const ANALYSIS_DISMISSED_RUNS_STORAGE_KEY = "tts-more:analysis-dismissed-runs";
export const ANALYSIS_RUN_SESSIONS_STORAGE_KEY = "tts-more:analysis-run-sessions";
export const ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY = "tts-more:analysis-review-sessions";
export const ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY = "tts-more:active-analysis-scope";
export const ANALYSIS_CONFIRM_KEYS_STORAGE_KEY = "tts-more:analysis-confirm-keys";

export interface AnalysisRunSession {
  runId: string;
  draftId: string;
}

export interface ActiveAnalysisScope {
  projectId: string;
  revisionId: string;
  sourceSha256: string | null;
}

export function defaultAnalysisStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function safeRemove(storage: Storage | null, key: string): void {
  try { storage?.removeItem(key); } catch { /* Best-effort persistence. */ }
}

function readObject(storage: Storage | null, key: string): Record<string, unknown> {
  if (!storage) return {};
  try {
    const raw = storage.getItem(key);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("storage_shape_invalid");
    return parsed as Record<string, unknown>;
  } catch {
    safeRemove(storage, key);
    return {};
  }
}

function writeObject(storage: Storage | null, key: string, value: Record<string, unknown>): void {
  try { storage?.setItem(key, JSON.stringify(value)); } catch { /* Best-effort persistence. */ }
}

export function analysisScopeStorageId(projectId: string, sourceRevision: ScriptRevision): string {
  return JSON.stringify([projectId, sourceRevision.revision_id, sourceRevision.source_sha256 ?? null]);
}

export function activeAnalysisScopeForRevision(projectId: string, sourceRevision: ScriptRevision): ActiveAnalysisScope {
  return { projectId, revisionId: sourceRevision.revision_id, sourceSha256: sourceRevision.source_sha256 ?? null };
}

export function activeAnalysisScopeMatchesRevision(scope: ActiveAnalysisScope, projectId: string, sourceRevision: ScriptRevision): boolean {
  return scope.projectId === projectId
    && scope.revisionId === sourceRevision.revision_id
    && scope.sourceSha256 === (sourceRevision.source_sha256 ?? null);
}

export function readActiveAnalysisScope(storage: Storage | null = defaultAnalysisStorage()): ActiveAnalysisScope | null {
  const stored = readObject(storage, ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY);
  const projectId = stored.projectId;
  const revisionId = stored.revisionId;
  const sourceSha256 = stored.sourceSha256;
  if (Object.keys(stored).length === 0) return null;
  if (typeof projectId !== "string" || !projectId.trim() || typeof revisionId !== "string" || !revisionId.trim()
    || (sourceSha256 !== null && typeof sourceSha256 !== "string")) {
    safeRemove(storage, ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY);
    return null;
  }
  return { projectId, revisionId, sourceSha256 };
}

export function writeActiveAnalysisScope(projectId: string, sourceRevision: ScriptRevision, storage: Storage | null = defaultAnalysisStorage()): void {
  const scope = activeAnalysisScopeForRevision(projectId, sourceRevision);
  writeObject(storage, ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY, {
    projectId: scope.projectId,
    revisionId: scope.revisionId,
    sourceSha256: scope.sourceSha256
  });
}

export function clearActiveAnalysisScope(expected: ActiveAnalysisScope, storage: Storage | null = defaultAnalysisStorage()): void {
  const current = readActiveAnalysisScope(storage);
  if (current?.projectId === expected.projectId && current.revisionId === expected.revisionId && current.sourceSha256 === expected.sourceSha256) {
    safeRemove(storage, ACTIVE_ANALYSIS_SCOPE_STORAGE_KEY);
  }
}

export function readAnalysisRunSession(storage: Storage | null, scopeId: string, storageKey = ANALYSIS_RUN_SESSIONS_STORAGE_KEY): AnalysisRunSession | null {
  const sessions = readObject(storage, storageKey);
  const value = sessions[scopeId];
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  if (typeof candidate.runId !== "string" || typeof candidate.draftId !== "string") {
    delete sessions[scopeId];
    writeObject(storage, storageKey, sessions);
    return null;
  }
  return { runId: candidate.runId, draftId: candidate.draftId };
}

export function writeAnalysisRunSession(storage: Storage | null, scopeId: string, session: AnalysisRunSession, storageKey = ANALYSIS_RUN_SESSIONS_STORAGE_KEY): void {
  const sessions = readObject(storage, storageKey);
  sessions[scopeId] = session;
  writeObject(storage, storageKey, sessions);
}

export function removeAnalysisRunSession(storage: Storage | null, scopeId: string, storageKey = ANALYSIS_RUN_SESSIONS_STORAGE_KEY): void {
  const sessions = readObject(storage, storageKey);
  if (!(scopeId in sessions)) return;
  delete sessions[scopeId];
  writeObject(storage, storageKey, sessions);
}

export function hasRestorableAnalysisSession(projectId: string, sourceRevision: ScriptRevision, storage: Storage | null = defaultAnalysisStorage()): boolean {
  return readAnalysisRunSession(storage, analysisScopeStorageId(projectId, sourceRevision)) !== null;
}

export function clearRestorableAnalysisSession(projectId: string, sourceRevision: ScriptRevision, storage: Storage | null = defaultAnalysisStorage()): void {
  removeAnalysisRunSession(storage, analysisScopeStorageId(projectId, sourceRevision));
}

export function hasReviewableAnalysisSession(projectId: string, sourceRevision: ScriptRevision, storage: Storage | null = defaultAnalysisStorage()): boolean {
  return readAnalysisRunSession(storage, analysisScopeStorageId(projectId, sourceRevision), ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY) !== null;
}

export function archiveRestorableAnalysisSession(projectId: string, sourceRevision: ScriptRevision, storage: Storage | null = defaultAnalysisStorage()): boolean {
  const scopeId = analysisScopeStorageId(projectId, sourceRevision);
  const session = readAnalysisRunSession(storage, scopeId);
  if (!session) return false;
  writeAnalysisRunSession(storage, scopeId, session, ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY);
  removeAnalysisRunSession(storage, scopeId);
  return true;
}

export function restoreReviewableAnalysisSession(projectId: string, sourceRevision: ScriptRevision, storage: Storage | null = defaultAnalysisStorage()): boolean {
  const scopeId = analysisScopeStorageId(projectId, sourceRevision);
  const session = readAnalysisRunSession(storage, scopeId, ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY);
  if (!session) return false;
  writeAnalysisRunSession(storage, scopeId, session);
  removeAnalysisRunSession(storage, scopeId, ANALYSIS_REVIEW_SESSIONS_STORAGE_KEY);
  return true;
}

export function readDismissedAnalysisRuns(storage: Storage | null): Set<string> {
  if (!storage) return new Set();
  try {
    const parsed: unknown = JSON.parse(storage.getItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY) ?? "[]");
    if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string")) throw new Error("dismissed_runs_invalid");
    return new Set(parsed);
  } catch {
    safeRemove(storage, ANALYSIS_DISMISSED_RUNS_STORAGE_KEY);
    return new Set();
  }
}

export function writeDismissedAnalysisRuns(storage: Storage | null, runIds: Set<string>): void {
  try { storage?.setItem(ANALYSIS_DISMISSED_RUNS_STORAGE_KEY, JSON.stringify([...runIds])); } catch { /* Best-effort persistence. */ }
}

export function readAnalysisConfirmKey(storage: Storage | null, draftId: string): string | null {
  const keys = readObject(storage, ANALYSIS_CONFIRM_KEYS_STORAGE_KEY);
  return typeof keys[draftId] === "string" ? keys[draftId] as string : null;
}

export function writeAnalysisConfirmKey(storage: Storage | null, draftId: string, key: string): void {
  const keys = readObject(storage, ANALYSIS_CONFIRM_KEYS_STORAGE_KEY);
  keys[draftId] = key;
  writeObject(storage, ANALYSIS_CONFIRM_KEYS_STORAGE_KEY, keys);
}
