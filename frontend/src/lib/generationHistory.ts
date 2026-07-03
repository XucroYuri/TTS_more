import type { GenerationStatus, GenerationVersion, ProviderType } from "../types";

export interface GenerationVersionGroup {
  groupId: string;
  label: string;
  versions: GenerationVersion[];
  latestStatus: GenerationStatus;
}

export interface InspectorVersionDraft {
  provider_type: ProviderType | null;
  service_id?: string | null;
  profile: string;
  binding_id?: string | null;
  parameters: Record<string, unknown>;
}

export interface GenerationFailureView {
  labelKey: string;
  detail: string;
}

export interface GenerationVersionTags {
  service: string;
  config: string;
  verification: "verified" | "assumed" | "legacy";
}

export function groupGenerationVersions(versions: GenerationVersion[]): GenerationVersionGroup[] {
  const groups = new Map<string, GenerationVersionGroup>();
  versions.forEach((version) => {
    const groupId = versionGroupId(version);
    const group = groups.get(groupId);
    if (group) {
      group.versions.push(version);
      group.latestStatus = version.status;
      return;
    }
    groups.set(groupId, {
      groupId,
      label: groupId,
      versions: [version],
      latestStatus: version.status
    });
  });
  return Array.from(groups.values());
}

export function newestPlayableVersion(versions: GenerationVersion[]): GenerationVersion | undefined {
  return versions.filter((version) => version.status === "completed" && Boolean(version.audio_path)).at(-1);
}

export function versionToInspectorDraft(version: GenerationVersion): InspectorVersionDraft {
  return {
    provider_type: version.provider_type ?? null,
    service_id: version.service_id,
    profile: version.profile,
    binding_id: version.binding_id,
    parameters: { ...(version.parameters ?? {}) }
  };
}

export function generationFailureView(version: GenerationVersion): GenerationFailureView {
  const stage = String(version.metadata?.failure_stage ?? "");
  const stageKeys: Record<string, string> = {
    routing: "history.failure.routing",
    loading: "history.failure.loading",
    synthesis: "history.failure.synthesis",
    finalizing: "history.failure.finalizing"
  };
  return {
    labelKey: stageKeys[stage] ?? "history.failure.generic",
    detail: version.error ?? ""
  };
}

export function generationVersionTags(version: GenerationVersion, serviceName?: string): GenerationVersionTags {
  const parameters = version.parameters ?? {};
  const service = serviceName || version.service_id || version.engine;
  const parts = [
    stringParam(parameters.logs_name),
    version.profile,
    version.binding_id,
  ].filter(Boolean);
  return {
    service,
    config: Array.from(new Set(parts)).join(" · ") || version.profile,
    verification: version.verified_load_signature ? "verified" : version.requested_load_signature ? "assumed" : "legacy"
  };
}

function versionGroupId(version: GenerationVersion): string {
  const metadata = version.metadata ?? {};
  return String(metadata.batch_id ?? metadata.job_id ?? version.version_id);
}

function stringParam(value: unknown): string {
  return typeof value === "string" ? value : "";
}
