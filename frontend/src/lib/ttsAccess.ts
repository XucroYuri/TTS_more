import type { CatalogProvider, OpenSourceTTSConfigureRequest, SourceProfile, WorkerHealth } from "../types";

export function gradioContractForProvider(provider: CatalogProvider): string {
  return {
    "gpt-sovits": "gradio-gpt-sovits-webui",
    indextts: "gradio-indextts2-webui",
    cosyvoice: "gradio-cosyvoice-webui",
  }[provider];
}

export function sourceProfileForEndpointUrl(rawUrl: string): Exclude<SourceProfile, "local_repo" | "api_placeholder"> {
  let host = "";
  try {
    host = new URL(rawUrl).hostname.toLowerCase();
  } catch {
    host = rawUrl.split(":")[0]?.trim().toLowerCase() ?? "";
  }
  if (host === "localhost" || host === "127.0.0.1" || host === "::1") return "local_endpoint";
  if (isPrivateIpv4(host) || host.endsWith(".local") || (host && !host.includes("."))) return "lan_endpoint";
  return "cloud_endpoint";
}

export function networkScopeForSourceProfile(sourceProfile: SourceProfile): WorkerHealth["network_scope"] {
  if (sourceProfile === "local_endpoint") return "localhost";
  if (sourceProfile === "lan_endpoint") return "lan";
  if (sourceProfile === "api_placeholder") return "commercial";
  return "public";
}

export function buildGradioEndpointRequest(options: {
  provider_type: CatalogProvider;
  display_name?: string | null;
  base_url: string;
  resource_group: string;
  capacity: number;
  enabled: boolean;
  service_id?: string | null;
}): OpenSourceTTSConfigureRequest {
  const sourceProfile = sourceProfileForEndpointUrl(options.base_url);
  return {
    provider_type: options.provider_type,
    service_id: options.service_id ?? null,
    display_name: options.display_name || null,
    source_profile: sourceProfile,
    repo_path: null,
    base_url: options.base_url,
    api_contract: gradioContractForProvider(options.provider_type),
    network_scope: networkScopeForSourceProfile(sourceProfile),
    managed: false,
    enabled: options.enabled,
    resource_group: options.resource_group,
    capacity: options.capacity,
    start_command: [],
    start_cwd: null,
  };
}

function isPrivateIpv4(host: string): boolean {
  const parts = host.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [first, second] = parts;
  return first === 10 || (first === 172 && second >= 16 && second <= 31) || (first === 192 && second === 168);
}
