import type { GenerationManifest, ParserProviderConfig, RuntimeMode, ScriptLine, VoiceCandidates, WorkerHealth } from "../types";

export type LineStatusFilter = "all" | "not-generated" | "queued" | "loading" | "running" | "finalizing" | "completed" | "failed" | "cancelled";

export interface LineFilters {
  characterId?: string;
  provider?: string;
  status?: LineStatusFilter;
  search?: string;
  providerForLine?: (line: ScriptLine) => string;
}

export interface ValidationRunState {
  disabled: boolean;
  reasonKey: string | null;
  serviceId?: string;
}

export type ServiceTone = "ready" | "attention" | "offline";

export interface ServiceTopbarSummary {
  local: { ready: number; total: number; tone: ServiceTone };
  paid: { ready: number; total: number; tone: ServiceTone };
  parser: { ready: number; total: number; tone: ServiceTone };
  resources: { ready: boolean; tone: ServiceTone };
  overallTone: ServiceTone;
}

export const coreLocalProviders = new Set(["gpt-sovits", "indextts"]);

export function isServiceOperational(service: WorkerHealth): boolean {
  if (service.enabled === false) return false;
  if (service.base_url?.startsWith("mock://")) return false;
  if (!service.base_url) return false;
  if (service.supervisor?.manageable && !service.supervisor.running) return false;
  if (service.capabilities?.includes("paid_provider") && service.key_configured === false) return false;
  return Boolean(service.ready);
}

export function validationRunState(
  runtime: Pick<RuntimeMode, "service_mode"> | null,
  services: WorkerHealth[],
  candidates: Pick<VoiceCandidates, "ready"> | null,
  _manifest: GenerationManifest | null,
  isValidating: boolean,
  isGenerating: boolean
): ValidationRunState {
  if (isValidating) return { disabled: true, reasonKey: "validation.reason.validating" };
  if (isGenerating) return { disabled: true, reasonKey: "validation.reason.generating" };
  if (runtime?.service_mode !== "real") return { disabled: true, reasonKey: "validation.reason.mockMode" };

  const localServices = services.filter((service) => service.enabled !== false && coreLocalProviders.has(service.provider_type ?? service.engine));
  const missingService = localServices.find((service) => !isServiceOperational(service));
  if (missingService) {
    return { disabled: true, reasonKey: "validation.reason.serviceNotReady", serviceId: missingService.service_id ?? missingService.engine };
  }
  if (localServices.length < coreLocalProviders.size) return { disabled: true, reasonKey: "validation.reason.serviceMissing" };
  if (!candidates?.ready) return { disabled: true, reasonKey: "validation.reason.resourcesNotReady" };
  return { disabled: false, reasonKey: null };
}

export function filterScriptLines(lines: ScriptLine[], manifest: GenerationManifest, filters: LineFilters): ScriptLine[] {
  const normalizedSearch = filters.search?.trim().toLocaleLowerCase();
  return lines.filter((line) => {
    if (filters.characterId && filters.characterId !== "all" && line.character_id !== filters.characterId) return false;
    if (filters.provider && filters.provider !== "all" && (filters.providerForLine?.(line) ?? inferLineProvider(line)) !== filters.provider) return false;
    if (filters.status && filters.status !== "all" && lineStatus(line.id, manifest) !== filters.status) return false;
    if (normalizedSearch) {
      const haystack = `${line.id} ${line.character_id} ${line.note} ${line.text}`.toLocaleLowerCase();
      if (!haystack.includes(normalizedSearch)) return false;
    }
    return true;
  });
}

export function toggleLineSelection(selected: string[], lineId: string): string[] {
  return selected.includes(lineId) ? selected.filter((id) => id !== lineId) : [...selected, lineId];
}

export function lineStatus(lineId: string, manifest: GenerationManifest): LineStatusFilter {
  const latest = manifest.lines[lineId]?.versions.at(-1);
  return latest?.status ?? "not-generated";
}

export function standardProjectName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "";
  const isAsciiSlug = /^[a-z0-9]+(?:[-_ ][a-z0-9]+)*$/.test(trimmed);
  if (!isAsciiSlug) return trimmed;
  return trimmed
    .split(/[-_ ]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function serviceTopbarSummary(
  services: WorkerHealth[],
  candidates: Pick<VoiceCandidates, "ready"> | null,
  parserProviders: Array<Pick<ParserProviderConfig, "enabled" | "key_configured">> = []
): ServiceTopbarSummary {
  const localServices = services.filter((service) => service.enabled !== false && coreLocalProviders.has(service.provider_type ?? service.engine));
  const paidServices = services.filter((service) => service.capabilities?.includes("paid_provider"));
  const localReady = localServices.filter(isServiceOperational).length;
  const paidReady = paidServices.filter(isServiceOperational).length;
  const parserReady = parserProviders.filter((provider) => provider.enabled && provider.key_configured).length;
  const resourcesReady = Boolean(candidates?.ready);
  const local = { ready: localReady, total: localServices.length, tone: groupTone(localReady, localServices.length) };
  const paid = { ready: paidReady, total: paidServices.length, tone: groupTone(paidReady, paidServices.length) };
  const parser = { ready: parserReady, total: parserProviders.length, tone: groupTone(parserReady, parserProviders.length) };
  const resources = { ready: resourcesReady, tone: resourcesReady ? "ready" as const : "attention" as const };
  const paidNeedsAttention = paid.total > 0 && paid.tone !== "ready";
  const parserNeedsAttention = parser.total > 0 && parser.tone !== "ready";
  const overallTone = local.tone === "offline"
    ? "offline"
    : local.tone === "attention" || paidNeedsAttention || parserNeedsAttention || resources.tone === "attention"
      ? "attention"
      : "ready";
  return { local, paid, parser, resources, overallTone };
}

function inferLineProvider(line: ScriptLine): string {
  const raw = `${line.engine_override ?? ""} ${line.binding_override ?? ""} ${line.profile_override ?? ""}`.toLowerCase();
  if (raw.includes("index")) return "indextts";
  if (raw.includes("vibe")) return "vibevoice";
  if (raw.includes("openai")) return "openai";
  if (raw.includes("gemini")) return "gemini";
  if (raw.includes("xai") || raw.includes("grok")) return "xai";
  if (raw.includes("volc")) return "volcengine";
  if (raw.includes("gpt")) return "gpt-sovits";
  return line.engine_override ?? "gpt-sovits";
}

function groupTone(ready: number, total: number): ServiceTone {
  if (total === 0) return "offline";
  if (ready === total) return "ready";
  return ready > 0 ? "attention" : "offline";
}
