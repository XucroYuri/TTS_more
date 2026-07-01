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

function versionGroupId(version: GenerationVersion): string {
  const metadata = version.metadata ?? {};
  return String(metadata.batch_id ?? metadata.job_id ?? version.version_id);
}
