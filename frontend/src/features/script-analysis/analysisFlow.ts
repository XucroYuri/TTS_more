import { createElement, Fragment, type ReactNode } from "react";

import { createScriptRevision } from "../../api";
import type { AnalysisHistoryItem, ScriptProject, ScriptRevision } from "../../types";
import { readScriptFile, type ScriptFileInput } from "./fileInput";
import { ScriptAnalysisWorkspace } from "./ScriptAnalysisWorkspace";
import { activeAnalysisScopeForRevision, type ActiveAnalysisScope } from "./analysisSessionStorage";
import type { UseAnalysisDraftOptions } from "./useAnalysisDraft";

export type WorkspaceStage = "tts" | "analysis";

export function shouldAutosaveWorkspace(stage: WorkspaceStage): boolean {
  return stage === "tts";
}

export function reviewConfirmedAnalysis(projectId: string, sourceRevision: ScriptRevision): ActiveAnalysisScope {
  return activeAnalysisScopeForRevision(projectId, sourceRevision);
}

export function activeScriptSourceText(
  project: ScriptProject,
  fallback?: ScriptRevision
): string | null {
  return project.script_revisions?.find(
    (revision) => revision.revision_id === project.active_script_revision_id
  )?.source_markdown ?? fallback?.source_markdown ?? null;
}

export interface AnalysisSourceFileMetadata {
  sourceText: string;
  filename: string;
  mediaType: string;
  warning?: ScriptFileInput["warning"];
}

export type AnalysisScriptFileOutcome =
  | { status: "loaded"; source: string; metadata: AnalysisSourceFileMetadata }
  | { status: "error"; source: string; errorCode: "unsupported_script_file" | "script_file_read_failed" }
  | { status: "stale"; source: string };

export async function readAnalysisScriptFile(
  file: File,
  currentSource: string,
  isCurrent: () => boolean,
  readFile: (file: File) => Promise<ScriptFileInput> = readScriptFile
): Promise<AnalysisScriptFileOutcome> {
  try {
    const result = await readFile(file);
    if (!isCurrent()) return { status: "stale", source: currentSource };
    return {
      status: "loaded",
      source: result.text,
      metadata: {
        sourceText: result.text,
        filename: result.filename,
        mediaType: result.mediaType,
        warning: result.warning
      }
    };
  } catch (error) {
    if (!isCurrent()) return { status: "stale", source: currentSource };
    return {
      status: "error",
      source: currentSource,
      errorCode: error instanceof Error && error.message === "unsupported_script_file"
        ? "unsupported_script_file"
        : "script_file_read_failed"
    };
  }
}

interface ScriptRevisionPayload {
  project: ScriptProject;
  script_revision: ScriptRevision;
}

export interface BeginAnalysisSourceRevisionOptions {
  projectId: string;
  source: string;
  summary: string;
  metadata?: AnalysisSourceFileMetadata;
  isCurrent: () => boolean;
  onCreated?: (
    payload: ScriptRevisionPayload
  ) => ScriptRevisionPayload | Promise<ScriptRevisionPayload>;
  onReady: (payload: ScriptRevisionPayload) => void | Promise<void>;
  createRevision?: (
    projectId: string,
    source: string,
    summary: string,
    metadata?: { source_filename?: string | null; source_media_type?: string | null }
  ) => Promise<ScriptRevisionPayload>;
}

export async function beginAnalysisSourceRevision(
  options: BeginAnalysisSourceRevisionOptions
): Promise<"started" | "stale"> {
  if (!options.source.trim()) throw new Error("script_source_required");
  const metadata = options.metadata?.sourceText === options.source
    ? {
        source_filename: options.metadata.filename,
        source_media_type: options.metadata.mediaType
      }
    : undefined;
  const createdPayload = await (options.createRevision ?? createScriptRevision)(
    options.projectId,
    options.source,
    options.summary,
    metadata
  );
  const payload = options.onCreated
    ? await options.onCreated(createdPayload)
    : createdPayload;
  if (!options.isCurrent()) return "stale";
  await options.onReady(payload);
  return "started";
}

export interface ConfirmedAnalysisHandoff {
  currentProjectId: string;
  project: ScriptProject;
  activeLineId: string;
  expandedLineId: null;
  selectedLineIds: string[];
  selectedHistoryVersions: Record<string, string>;
  versionDrafts: Record<string, never>;
  lineTextDrafts: Record<string, string>;
  managedProjectId: string;
  managedProject: ScriptProject;
  managerTitleDraft: string;
  managerSourceDraft: string;
}

export function buildConfirmedAnalysisHandoff(
  projectId: string,
  project: ScriptProject,
  sourceRevision: ScriptRevision
): ConfirmedAnalysisHandoff {
  const activeSource = activeScriptSourceText(project, sourceRevision) ?? sourceRevision.source_markdown;
  return {
    currentProjectId: projectId,
    project,
    activeLineId: project.lines[0]?.id ?? "",
    expandedLineId: null,
    selectedLineIds: [],
    selectedHistoryVersions: {},
    versionDrafts: {},
    lineTextDrafts: {},
    managedProjectId: projectId,
    managedProject: project,
    managerTitleDraft: project.title,
    managerSourceDraft: activeSource
  };
}

export interface AnalysisStageGateProps {
  stage: WorkspaceStage;
  projectId: string | null;
  sourceRevision: ScriptRevision | null;
  onConfirmed: (project: ScriptProject) => void;
  onCancel: () => void;
  controllerOptions?: UseAnalysisDraftOptions;
  onDeleteAnalysisHistoryRequest?: (item: AnalysisHistoryItem) => Promise<boolean>;
  ttsWorkbench: ReactNode;
}

export function AnalysisStageGate({
  stage,
  projectId,
  sourceRevision,
  onConfirmed,
  onCancel,
  controllerOptions,
  onDeleteAnalysisHistoryRequest,
  ttsWorkbench
}: AnalysisStageGateProps) {
  if (stage === "analysis" && projectId && sourceRevision) {
    return createElement(ScriptAnalysisWorkspace, {
      key: `${projectId}:${sourceRevision.revision_id}:${sourceRevision.source_sha256 ?? ""}`,
      projectId,
      sourceRevision,
      onConfirmed,
      onCancel,
      controllerOptions,
      onDeleteAnalysisHistoryRequest
    });
  }
  return createElement(Fragment, null, ttsWorkbench);
}
