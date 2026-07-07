import type { ProjectSummary, ScriptProject } from "../types";

export interface ProjectPreviewStats {
  lineCount: number;
  characterCount: number;
  scriptRevisionCount: number;
  parseRevisionCount: number;
  activeSourceMarkdown: string;
}

export function filterAndSortProjectSummaries(projects: ProjectSummary[], query: string): ProjectSummary[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  return [...projects]
    .filter((project) => {
      if (!normalizedQuery) return true;
      return `${project.title} ${project.project_id}`.toLocaleLowerCase().includes(normalizedQuery);
    })
    .sort((left, right) => {
      const rightTime = Date.parse(right.updated_at ?? "") || 0;
      const leftTime = Date.parse(left.updated_at ?? "") || 0;
      if (rightTime !== leftTime) return rightTime - leftTime;
      return (left.title || left.project_id).localeCompare(right.title || right.project_id);
    });
}

export function projectPreviewStats(project: ScriptProject | null | undefined): ProjectPreviewStats {
  const activeRevision = project?.script_revisions?.find((revision) => revision.revision_id === project.active_script_revision_id);
  const fallbackRevision = project?.script_revisions?.at(-1);
  return {
    lineCount: project?.lines.length ?? 0,
    characterCount: project?.project_characters?.length ?? 0,
    scriptRevisionCount: project?.script_revisions?.length ?? 0,
    parseRevisionCount: project?.parse_revisions?.length ?? 0,
    activeSourceMarkdown: activeRevision?.source_markdown ?? fallbackRevision?.source_markdown ?? ""
  };
}

export function nextProjectAfterDelete(projects: ProjectSummary[], deletedProjectId: string, currentProjectId: string | null): string | null {
  if (currentProjectId && currentProjectId !== deletedProjectId) return currentProjectId;
  const index = projects.findIndex((project) => project.project_id === deletedProjectId);
  const remaining = projects.filter((project) => project.project_id !== deletedProjectId);
  if (remaining.length === 0) return null;
  if (index < 0) return remaining[0].project_id;
  const nextIndex = Math.max(0, index - 1);
  return remaining[nextIndex].project_id;
}
