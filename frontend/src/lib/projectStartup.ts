import type { GenerationManifest, ProjectSummary, ScriptProject } from "../types";

export const startupProjectStorageKey = "tts-more.currentProjectId";

export function createEmptyProject(): ScriptProject {
  return {
    title: "",
    default_language: "zh",
    project_characters: [],
    active_script_revision_id: null,
    active_parse_revision_id: null,
    script_revisions: [],
    parse_revisions: [],
    lines: []
  };
}

export function createEmptyManifest(projectId: string | null): GenerationManifest {
  return { project_id: projectId ?? "", lines: {} };
}

export function selectStartupProjectId(projects: ProjectSummary[], preferredProjectId: string | null): string | null {
  if (projects.length === 0) return null;
  if (preferredProjectId && projects.some((project) => project.project_id === preferredProjectId)) return preferredProjectId;
  return projects[0].project_id;
}

export function readStoredProjectId(): string | null {
  try {
    return window.localStorage.getItem(startupProjectStorageKey);
  } catch {
    return null;
  }
}

export function writeStoredProjectId(projectId: string | null): void {
  try {
    if (projectId) {
      window.localStorage.setItem(startupProjectStorageKey, projectId);
    } else {
      window.localStorage.removeItem(startupProjectStorageKey);
    }
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
}
