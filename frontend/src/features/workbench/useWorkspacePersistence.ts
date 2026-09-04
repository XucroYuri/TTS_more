import { useCallback, useEffect, useRef } from "react";

import type { Character, ScriptProject } from "../../types";

export interface WorkspaceSnapshot {
  projectId: string;
  project: ScriptProject;
  characters: Character[];
}

interface PendingWorkspaceSnapshot {
  snapshot: WorkspaceSnapshot;
  key: string;
  authorityEpoch: number;
  timerId: number | null;
}

export interface UseWorkspacePersistenceOptions {
  persist: (snapshot: WorkspaceSnapshot) => Promise<void>;
  debounceMs?: number;
  onSaving?: () => void;
  onSaved?: (snapshot: WorkspaceSnapshot) => void;
  onError?: (error: unknown, snapshot: WorkspaceSnapshot) => void;
}

export interface WorkspacePersistenceController {
  hydrate: (snapshot: WorkspaceSnapshot) => void;
  markAuthoritative: (snapshot: WorkspaceSnapshot) => void;
  schedule: (snapshot: WorkspaceSnapshot) => boolean;
  flush: (projectId: string) => Promise<WorkspaceSnapshot | null>;
  cancel: (projectId: string) => void;
  pending: (projectId: string) => WorkspaceSnapshot | null;
}

export function workspaceSnapshotKey(snapshot: WorkspaceSnapshot): string {
  return JSON.stringify({
    projectId: snapshot.projectId,
    project: snapshot.project,
    characters: snapshot.characters
  });
}

export function useWorkspacePersistence(
  options: UseWorkspacePersistenceOptions
): WorkspacePersistenceController {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const baselineByProjectRef = useRef(new Map<string, string>());
  const pendingByProjectRef = useRef(new Map<string, PendingWorkspaceSnapshot>());
  const authorityEpochByProjectRef = useRef(new Map<string, number>());
  const saveChainRef = useRef(Promise.resolve());

  const clearPending = useCallback((projectId: string) => {
    const pending = pendingByProjectRef.current.get(projectId);
    if (pending?.timerId != null) window.clearTimeout(pending.timerId);
    pendingByProjectRef.current.delete(projectId);
  }, []);

  const nextAuthorityEpoch = useCallback((projectId: string) => {
    const next = (authorityEpochByProjectRef.current.get(projectId) ?? 0) + 1;
    authorityEpochByProjectRef.current.set(projectId, next);
    return next;
  }, []);

  const markAuthoritative = useCallback((snapshot: WorkspaceSnapshot) => {
    clearPending(snapshot.projectId);
    nextAuthorityEpoch(snapshot.projectId);
    baselineByProjectRef.current.set(snapshot.projectId, workspaceSnapshotKey(snapshot));
  }, [clearPending, nextAuthorityEpoch]);

  const flush = useCallback(async (projectId: string) => {
    const pending = pendingByProjectRef.current.get(projectId);
    if (!pending) return null;
    if (pending.timerId != null) window.clearTimeout(pending.timerId);
    pendingByProjectRef.current.delete(projectId);
    if (
      pending.authorityEpoch
      !== (authorityEpochByProjectRef.current.get(projectId) ?? 0)
    ) return null;
    let saved: WorkspaceSnapshot | null = null;
    saveChainRef.current = saveChainRef.current.then(async () => {
      if (
        pending.authorityEpoch
        !== (authorityEpochByProjectRef.current.get(projectId) ?? 0)
      ) return;
      optionsRef.current.onSaving?.();
      try {
        await optionsRef.current.persist(pending.snapshot);
      } catch (error) {
        if (
          pending.authorityEpoch
          === (authorityEpochByProjectRef.current.get(projectId) ?? 0)
        ) {
          pendingByProjectRef.current.set(projectId, {
            ...pending,
            timerId: null
          });
          optionsRef.current.onError?.(error, pending.snapshot);
        }
        return;
      }
      if (
        pending.authorityEpoch
        !== (authorityEpochByProjectRef.current.get(projectId) ?? 0)
      ) return;
      baselineByProjectRef.current.set(projectId, pending.key);
      saved = pending.snapshot;
      optionsRef.current.onSaved?.(pending.snapshot);
    });
    await saveChainRef.current;
    return saved;
  }, []);

  const schedule = useCallback((snapshot: WorkspaceSnapshot) => {
    const key = workspaceSnapshotKey(snapshot);
    if (baselineByProjectRef.current.get(snapshot.projectId) === key) {
      clearPending(snapshot.projectId);
      return false;
    }
    clearPending(snapshot.projectId);
    const authorityEpoch = authorityEpochByProjectRef.current.get(snapshot.projectId) ?? 0;
    const pending: PendingWorkspaceSnapshot = {
      snapshot,
      key,
      authorityEpoch,
      timerId: null
    };
    pending.timerId = window.setTimeout(() => {
      if (pendingByProjectRef.current.get(snapshot.projectId) !== pending) return;
      pending.timerId = null;
      void flush(snapshot.projectId);
    }, optionsRef.current.debounceMs ?? 700);
    pendingByProjectRef.current.set(snapshot.projectId, pending);
    return true;
  }, [clearPending, flush]);

  const cancel = useCallback((projectId: string) => {
    clearPending(projectId);
    nextAuthorityEpoch(projectId);
  }, [clearPending, nextAuthorityEpoch]);

  const pending = useCallback((projectId: string) => (
    pendingByProjectRef.current.get(projectId)?.snapshot ?? null
  ), []);

  useEffect(() => () => {
    for (const projectId of pendingByProjectRef.current.keys()) clearPending(projectId);
  }, [clearPending]);

  return {
    hydrate: markAuthoritative,
    markAuthoritative,
    schedule,
    flush,
    cancel,
    pending
  };
}
