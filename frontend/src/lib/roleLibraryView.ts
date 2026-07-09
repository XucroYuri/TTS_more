import type { Character, CharacterReferenceAudioGroup, ReferenceAudioSample, VoiceBinding, WorkerHealth } from "../types";

export type RoleLibraryServiceState = "ready" | "partial" | "blocked" | "disabled";

export interface RoleLibraryServiceOption {
  service: WorkerHealth;
  serviceId: string;
  label: string;
  providerType: string;
  apiContract: string;
  baseUrl: string | null;
  enabled: boolean;
  ready: boolean;
  state: RoleLibraryServiceState;
  supportsModelCatalog: boolean;
}

export interface RoleLibraryBindingRow {
  binding: VoiceBinding;
  bindingId: string;
  profileId: string;
  profileName: string;
  providerType: string;
  serviceId: string | null;
  serviceLabel: string;
  complete: boolean;
  missing: string[];
}

export type RoleLibraryDetailSelection =
  | { kind: "model"; modelId: string }
  | { kind: "candidate"; candidateId: string }
  | { kind: "library-character"; characterId: string }
  | { kind: "empty" };

export interface RoleLibraryDetailSelectionInput {
  selectedCharacterId?: string | null;
  selectedCandidateId?: string | null;
  selectedModelId?: string | null;
  filteredCharacters: Character[];
}

export interface RoleLibraryReferencePreviewSample extends ReferenceAudioSample {
  group: string;
}

export interface RoleLibraryReferencePreviewState {
  visibleSamples: RoleLibraryReferencePreviewSample[];
  hiddenSampleCount: number;
  hasOverflow: boolean;
}

export function roleLibraryServiceOptions(services: WorkerHealth[]): RoleLibraryServiceOption[] {
  return services
    .filter((service) => service.service_kind !== "llm-parser" && Boolean(service.service_id))
    .map((service) => {
      const serviceId = service.service_id ?? "";
      const providerType = service.provider_type ?? service.engine;
      const apiContract = service.api_contract ?? service.engine;
      return {
        service,
        serviceId,
        label: service.display_name || serviceId,
        providerType,
        apiContract,
        baseUrl: service.base_url ?? null,
        enabled: service.enabled !== false,
        ready: Boolean(service.ready),
        state: roleLibraryServiceState(service),
        supportsModelCatalog: supportsModelCatalog(service),
      };
    });
}

export function roleLibraryDetailSelection(input: RoleLibraryDetailSelectionInput): RoleLibraryDetailSelection {
  if (input.selectedModelId) return { kind: "model", modelId: input.selectedModelId };
  if (input.selectedCandidateId) return { kind: "candidate", candidateId: input.selectedCandidateId };
  if (input.selectedCharacterId && input.filteredCharacters.some((character) => character.id === input.selectedCharacterId)) {
    return { kind: "library-character", characterId: input.selectedCharacterId };
  }
  return { kind: "empty" };
}

export function roleLibraryReferencePreview(
  groups: CharacterReferenceAudioGroup[],
  maxVisible = 4
): RoleLibraryReferencePreviewState {
  const samples = groups.flatMap((group) => (group.samples ?? []).map((sample) => ({ ...sample, group: group.name })));
  const visibleSamples = samples.slice(0, maxVisible);
  const hiddenSampleCount = Math.max(0, samples.length - visibleSamples.length);
  return {
    visibleSamples,
    hiddenSampleCount,
    hasOverflow: hiddenSampleCount > 0
  };
}

export function catalogServiceOptions(services: WorkerHealth[]): RoleLibraryServiceOption[] {
  return roleLibraryServiceOptions(services).filter((option) => option.enabled && option.supportsModelCatalog);
}

export function selectedCatalogServiceId(selectedServiceId: string | null | undefined, options: RoleLibraryServiceOption[]): string | null {
  if (!selectedServiceId) return null;
  return options.some((option) => option.serviceId === selectedServiceId) ? selectedServiceId : null;
}

export function roleLibraryBindingRows(character: Character, services: WorkerHealth[]): RoleLibraryBindingRow[] {
  const serviceOptions = roleLibraryServiceOptions(services);
  const serviceById = new Map(serviceOptions.map((option) => [option.serviceId, option]));
  return (character.profiles ?? []).flatMap((profile) =>
    (profile.bindings ?? []).map((binding) => {
      const serviceId = binding.service_id ?? profile.service_id ?? null;
      const service = serviceId ? serviceById.get(serviceId) : undefined;
      const completeness = bindingCompleteness(binding);
      return {
        binding,
        bindingId: binding.binding_id,
        profileId: profile.id,
        profileName: profile.name,
        providerType: binding.provider_type,
        serviceId,
        serviceLabel: service?.label ?? serviceId ?? "",
        complete: completeness.complete,
        missing: completeness.missing,
      };
    })
  );
}

export function bindingCompleteness(binding: VoiceBinding): { complete: boolean; missing: string[] } {
  const config = binding.config ?? {};
  const missing: string[] = [];
  if (binding.provider_type === "gpt-sovits") {
    if (!config.logs_name) missing.push("logs");
    if (!config.gpt_weights_path) missing.push("GPT");
    if (!config.sovits_weights_path) missing.push("SoVITS");
    if (!config.ref_audio_path) missing.push("ref");
    if (!config.prompt_text) missing.push("prompt");
  } else if (binding.provider_type === "indextts") {
    if (!config.voice && !config.ref_audio_path && !config.reference_audio) missing.push("voice");
  } else if (!config.voice && !config.voice_id && !config.model) {
    missing.push("voice");
  }
  return { complete: missing.length === 0, missing };
}

function roleLibraryServiceState(service: WorkerHealth): RoleLibraryServiceState {
  if (service.enabled === false) return "disabled";
  if (service.state === "blocked" || service.severity === "danger") return "blocked";
  if (service.state === "partial" || service.severity === "attention") return "partial";
  if (service.ready || service.state === "ready" || service.state === "running") return "ready";
  return "partial";
}

function supportsModelCatalog(service: WorkerHealth): boolean {
  const provider = service.provider_type ?? service.engine;
  const capabilities = service.capabilities ?? [];
  if (provider !== "gpt-sovits") return false;
  return (
    service.api_contract === "gradio-gpt-sovits-webui" ||
    service.api_contract === "gpt-sovits-api-v2" ||
    capabilities.includes("gpt-sovits-api-v2") ||
    capabilities.includes("model_catalog") ||
    capabilities.includes("gradio_webui")
  );
}
