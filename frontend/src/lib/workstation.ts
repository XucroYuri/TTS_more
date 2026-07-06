import type { GenerationManifest, LineHistory, ParserProviderConfig, RuntimeMode, ScriptLine, VoiceCandidates, WorkerHealth } from "../types";

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

export type ServiceTopbarHealthItemId = "local" | "paid" | "parser" | "resources";

export interface ServiceTopbarHealthItem {
  id: ServiceTopbarHealthItemId;
  labelKey: string;
  tone: ServiceTone;
  value: string;
}

export const coreLocalProviders = new Set(["gpt-sovits", "indextts", "cosyvoice"]);

export interface CoreProviderCoverage {
  provider: string;
  services: WorkerHealth[];
  operational: boolean;
}

export function coreProviderCoverage(services: WorkerHealth[]): CoreProviderCoverage[] {
  return Array.from(coreLocalProviders).map((provider) => {
    const providerServices = services.filter((service) => service.enabled !== false && (service.provider_type ?? service.engine) === provider);
    return {
      provider,
      services: providerServices,
      operational: providerServices.some(isServiceOperational)
    };
  });
}

export function isServiceOperational(service: WorkerHealth): boolean {
  if (service.enabled === false) return false;
  if (service.base_url?.startsWith("mock://")) return false;
  if (!service.base_url) return false;
  if (service.state && !["ready", "running"].includes(service.state)) return false;
  if (service.severity === "danger") return false;
  if (service.supervisor?.manageable && !service.supervisor.running) return false;
  if (service.capabilities?.includes("paid_provider") && service.key_configured === false) return false;
  return Boolean(service.ready);
}

export function isServiceRoutable(service: WorkerHealth): boolean {
  if (service.enabled === false) return false;
  if (service.base_url?.startsWith("mock://")) return false;
  if (!service.base_url) return false;
  if (["not_configured", "repo_missing", "env_missing", "endpoint_unreachable"].includes(service.setup_state ?? "")) return false;
  if (service.state && !["ready", "running", "partial"].includes(service.state)) return false;
  if (service.severity === "danger") return false;
  if (service.supervisor?.manageable && !service.supervisor.running) return false;
  if (service.capabilities?.includes("paid_provider") && service.key_configured === false) return false;
  return Boolean(service.ready || service.state === "partial");
}

export function routableProviderServices(services: WorkerHealth[], provider: string): WorkerHealth[] {
  return services.filter((service) => service.provider_type === provider && Boolean(service.service_id) && isServiceRoutable(service));
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

  const coverage = coreProviderCoverage(services);
  const missingProvider = coverage.find((item) => !item.operational);
  if (missingProvider) {
    const firstService = missingProvider.services[0];
    if (firstService) {
      return { disabled: true, reasonKey: "validation.reason.serviceNotReady", serviceId: firstService.service_id ?? firstService.engine };
    }
    return { disabled: true, reasonKey: "validation.reason.serviceMissing", serviceId: missingProvider.provider };
  }
  if (!candidates?.ready) return { disabled: true, reasonKey: "validation.reason.resourcesNotReady" };
  return { disabled: false, reasonKey: null };
}

export function filterScriptLines(lines: ScriptLine[], manifest: GenerationManifest, filters: LineFilters): ScriptLine[] {
  const normalizedSearch = filters.search?.trim().toLocaleLowerCase();
  return lines.filter((line) => {
    if (filters.characterId && filters.characterId !== "all" && line.character_id !== filters.characterId) return false;
    if (filters.provider && filters.provider !== "all" && (filters.providerForLine?.(line) ?? inferLineProvider(line)) !== filters.provider) return false;
    if (filters.status && filters.status !== "all" && lineStatus(line, manifest) !== filters.status) return false;
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

export function lineHistoryForLine(manifest: GenerationManifest, line: Pick<ScriptLine, "id" | "line_uid">): LineHistory | undefined {
  if (line.line_uid && manifest.lines[line.line_uid]) return manifest.lines[line.line_uid];
  return manifest.lines[line.id];
}

export function lineStatus(line: Pick<ScriptLine, "id" | "line_uid"> | string, manifest: GenerationManifest): LineStatusFilter {
  const history = typeof line === "string" ? manifest.lines[line] : lineHistoryForLine(manifest, line);
  const latest = history?.versions.at(-1);
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
  const localCoverage = coreProviderCoverage(services);
  const paidServices = services.filter((service) => service.capabilities?.includes("paid_provider"));
  const localReady = localCoverage.filter((item) => item.operational).length;
  const paidReady = paidServices.filter(isServiceOperational).length;
  const parserReady = parserProviders.filter((provider) => provider.enabled && provider.key_configured).length;
  const resourcesReady = Boolean(candidates?.ready);
  const local = { ready: localReady, total: localCoverage.length, tone: groupTone(localReady, localCoverage.length) };
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

export function serviceTopbarHealthItems(summary: ServiceTopbarSummary): ServiceTopbarHealthItem[] {
  return [
    { id: "local", labelKey: "services.localShort", tone: summary.local.tone, value: `${summary.local.ready}/${summary.local.total}` },
    { id: "paid", labelKey: "services.apiShort", tone: summary.paid.tone, value: `${summary.paid.ready}/${summary.paid.total}` },
    { id: "parser", labelKey: "services.parserShort", tone: summary.parser.tone, value: `${summary.parser.ready}/${summary.parser.total}` },
    { id: "resources", labelKey: "services.resourcesShort", tone: summary.resources.tone, value: "" }
  ];
}

function inferLineProvider(line: ScriptLine): string {
  const raw = `${line.engine_override ?? ""} ${line.binding_override ?? ""} ${line.profile_override ?? ""}`.toLowerCase();
  if (raw.includes("index")) return "indextts";
  if (raw.includes("cosy")) return "cosyvoice";
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
