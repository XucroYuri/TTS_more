import type { Character, GenerationVersion, ProviderType, ReferenceAudioGroup, ScriptLine, WorkerHealth } from "../types";

export interface FallbackActionView {
  type: "start_service";
  serviceId: string;
  serviceName: string;
}

export type PreflightLineTone = "ok" | "warn" | "danger";
export type PreflightLoadTone = "ok" | "warn" | "neutral";

export type LineCardSecondaryBadge =
  | { kind: "latest_playable" }
  | { kind: "latest_failed" }
  | { kind: "version_count"; count: number }
  | { kind: "no_versions" };

export type InspectorPanelMode = "line_config" | "version_params";
export type InspectorSectionId = "config" | "reference" | "version" | "diagnostics";
export type InspectorDiagnosticsTone = "neutral" | "warn" | "danger";
export type InspectorDiagnosticsReason = "ready" | "not_loaded" | "signature_mismatch" | "error" | "manual";
export type LineFocusIntent = "card" | "checkbox" | "role";
export type GenerationMethodId = "gpt-sovits" | "indextts" | "cosyvoice" | "commercial";

export interface InspectorDiagnosticsInput {
  loaded?: boolean | null;
  loadedSignature?: string | null;
  expectedSignature?: string | null;
  lastError?: string | null;
  expanded?: boolean;
}

export interface InspectorDiagnosticsState {
  visible: boolean;
  expanded: boolean;
  tone: InspectorDiagnosticsTone;
  reason: InspectorDiagnosticsReason;
}

export interface HistoryPlayerSummary {
  versionId: string;
  playable: boolean;
  status: string;
  audioPath: string | null;
}

export interface LineFocusState {
  activeLineId: string | null;
  expandedLineId: string | null;
}

export interface GenerationMethodOption {
  id: GenerationMethodId;
  labelKey: string;
  hintKey: string;
  providers: ProviderType[];
}

export interface GenerationMethodRouteLabels {
  profileLabelKey: string;
  bindingLabelKey: string;
  serviceLabelKey: string;
}

export type ScriptConsoleBodyMode = "preview" | "edit";

type PreflightFallbackEntry = {
  fallback_action?: { type?: string; service_id?: string } | null;
  line_id?: string;
  line_uid?: string | null;
  status?: string;
  selected_service_id?: string | null;
  load_signature?: string | null;
  current_loaded_signature?: string | null;
  load_state?: string | null;
  load_match?: boolean | null;
  reason?: string | null;
};

export function preflightLineTone(item: PreflightFallbackEntry | undefined): PreflightLineTone | null {
  if (!item) return null;
  if (item.status === "ready") return "ok";
  if (item.status === "needs_user_action") return "warn";
  return "danger";
}

export function preflightLineLabelKey(item: PreflightFallbackEntry | undefined): string | null {
  if (!item) return null;
  if (item.status === "ready") return "preflight.ready";
  if (item.status === "needs_user_action") return "preflight.needsAction";
  return "preflight.blocked";
}

export function preflightLoadTone(item: PreflightFallbackEntry | undefined, loadedSignature?: string | null): PreflightLoadTone | null {
  if (!item || item.status !== "ready" || !item.load_signature) return null;
  if (item.load_state === "loaded" || item.load_match === true) return "ok";
  if (item.load_state === "switch_required") return "warn";
  const currentSignature = item.current_loaded_signature ?? loadedSignature;
  if (!currentSignature) return "neutral";
  return currentSignature === item.load_signature ? "ok" : "warn";
}

export function preflightLoadLabelKey(item: PreflightFallbackEntry | undefined, loadedSignature?: string | null): string | null {
  const tone = preflightLoadTone(item, loadedSignature);
  if (!tone) return null;
  if (item?.load_state === "not_loaded" || (!item?.current_loaded_signature && !loadedSignature)) return "preflight.notLoaded";
  if (tone === "ok") return "preflight.loaded";
  if (tone === "warn") return "preflight.switchNeeded";
  return "preflight.notLoaded";
}

export function lineFocusTransition(current: LineFocusState, lineId: string, intent: LineFocusIntent): LineFocusState {
  if (intent === "checkbox") return current;
  if (intent === "role") {
    return {
      activeLineId: lineId,
      expandedLineId: null
    };
  }
  return {
    activeLineId: lineId,
    expandedLineId: lineId
  };
}

export function generationMethodForProvider(provider: ProviderType): GenerationMethodId {
  if (provider === "gpt-sovits") return "gpt-sovits";
  if (provider === "indextts") return "indextts";
  if (provider === "cosyvoice") return "cosyvoice";
  return "commercial";
}

export function generationMethodOptions(): GenerationMethodOption[] {
  return [
    {
      id: "gpt-sovits",
      labelKey: "inspector.method.gpt",
      hintKey: "inspector.methodHint.gpt",
      providers: ["gpt-sovits"]
    },
    {
      id: "indextts",
      labelKey: "inspector.method.indextts",
      hintKey: "inspector.methodHint.indextts",
      providers: ["indextts"]
    },
    {
      id: "cosyvoice",
      labelKey: "inspector.method.cosyvoice",
      hintKey: "inspector.methodHint.cosyvoice",
      providers: ["cosyvoice"]
    },
    {
      id: "commercial",
      labelKey: "inspector.method.commercial",
      hintKey: "inspector.methodHint.commercial",
      providers: ["openai", "gemini", "xai", "volcengine", "generic-http", "vibevoice"]
    }
  ];
}

export function generationMethodRouteLabels(methodId: GenerationMethodId): GenerationMethodRouteLabels {
  if (methodId === "gpt-sovits") {
    return {
      profileLabelKey: "inspector.gptRolePreset",
      bindingLabelKey: "inspector.gptVoiceBinding",
      serviceLabelKey: "inspector.gptService"
    };
  }
  if (methodId === "indextts") {
    return {
      profileLabelKey: "inspector.indexRolePreset",
      bindingLabelKey: "inspector.indexVoiceBinding",
      serviceLabelKey: "inspector.indexService"
    };
  }
  if (methodId === "cosyvoice") {
    return {
      profileLabelKey: "inspector.cosyVoicePreset",
      bindingLabelKey: "inspector.cosyVoiceBinding",
      serviceLabelKey: "inspector.cosyVoiceService"
    };
  }
  return {
    profileLabelKey: "inspector.commercialVoicePreset",
    bindingLabelKey: "inspector.commercialVoiceBinding",
    serviceLabelKey: "inspector.commercialEndpoint"
  };
}

export function scriptConsoleBodyMode(isEditing: boolean): ScriptConsoleBodyMode {
  return isEditing ? "edit" : "preview";
}

export function roleAccentClass(index: number): string {
  return `role-accent-${Math.abs(index) % 8}`;
}

export function shouldRequestRevisionConfirmation(scriptRevisionCount = 0, parseRevisionCount = 0): boolean {
  return scriptRevisionCount > 1 || parseRevisionCount > 1;
}

export function lineCardSecondaryBadges(latestVersion: GenerationVersion | undefined, versionCount: number): LineCardSecondaryBadge[] {
  if (versionCount <= 0 || !latestVersion) return [{ kind: "no_versions" }];
  const badges: LineCardSecondaryBadge[] = [];
  if (latestVersion.status === "completed" && latestVersion.audio_path) {
    badges.push({ kind: "latest_playable" });
  } else if (latestVersion.status === "failed") {
    badges.push({ kind: "latest_failed" });
  }
  badges.push({ kind: "version_count", count: versionCount });
  return badges;
}

export function inspectorPanelMode(selectedVersionId?: string | null): InspectorPanelMode {
  return selectedVersionId ? "version_params" : "line_config";
}

export function inspectorSections(mode: InspectorPanelMode): InspectorSectionId[] {
  const base: InspectorSectionId[] = ["config", "reference"];
  return mode === "version_params" ? ["version", ...base] : base;
}

export function inspectorVersionContextVisible(mode: InspectorPanelMode, selectedVersionId?: string | null): boolean {
  return mode === "version_params" && Boolean(selectedVersionId);
}

export function inspectorDiagnosticsState(input: InspectorDiagnosticsInput): InspectorDiagnosticsState {
  const loadedSignature = input.loadedSignature ?? "";
  const expectedSignature = input.expectedSignature ?? "";
  const hasError = Boolean(input.lastError);
  const hasMismatch = Boolean(loadedSignature && expectedSignature && loadedSignature !== expectedSignature);
  const isNotLoaded = Boolean(expectedSignature && !input.loaded);

  if (hasError) {
    return { visible: true, expanded: true, tone: "danger", reason: "error" };
  }
  if (hasMismatch) {
    return { visible: true, expanded: Boolean(input.expanded), tone: "warn", reason: "signature_mismatch" };
  }
  if (isNotLoaded) {
    return { visible: true, expanded: Boolean(input.expanded), tone: "warn", reason: "not_loaded" };
  }
  if (input.expanded) {
    return { visible: true, expanded: true, tone: "neutral", reason: "manual" };
  }
  return { visible: false, expanded: false, tone: "neutral", reason: "ready" };
}

export function historyPlayerSummary(version: GenerationVersion): HistoryPlayerSummary {
  return {
    versionId: version.version_id,
    playable: version.status === "completed" && Boolean(version.audio_path),
    status: version.status,
    audioPath: version.audio_path ?? null
  };
}

export function trustedBackupReferenceGroups(line: ScriptLine | undefined, characters: Character[]): ReferenceAudioGroup[] {
  if (!line || line.temporary_binding) return [];
  const character = characters.find((item) => item.id === line.character_id);
  return (character?.reference_audio_groups ?? [])
    .map((group) => {
      const samples = Array.from(new Set([
        ...(group.samples ?? []).map((sample) => sample.path),
        ...(group.paths ?? []),
        ...(group.copied_paths ?? [])
      ].filter(Boolean)));
      return {
        id: group.id,
        name: group.name,
        path: samples[0] ?? "",
        audio_count: samples.length,
        samples
      };
    })
    .filter((group) => group.samples.length > 0)
    .slice(0, 8);
}

export function inspectorBackupReferenceVisible(_provider: ProviderType, _trustedGroupCount: number): boolean {
  return false;
}

export function preflightFallbackAction(
  item: PreflightFallbackEntry | { items?: PreflightFallbackEntry[] },
  services: WorkerHealth[]
): FallbackActionView | null {
  const target = Array.isArray((item as { items?: PreflightFallbackEntry[] }).items)
    ? (item as { items?: PreflightFallbackEntry[] }).items?.find((entry) => entry.fallback_action?.type === "start_service")
    : item as PreflightFallbackEntry;
  const action = target?.fallback_action;
  if (action?.type !== "start_service" || !action.service_id) return null;
  const service = services.find((candidate) => candidate.service_id === action.service_id);
  return {
    type: "start_service",
    serviceId: action.service_id,
    serviceName: service?.display_name ?? action.service_id
  };
}
