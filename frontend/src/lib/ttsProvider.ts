import type { EngineName, ProviderType, WorkerHealth } from "../types";

export function engineProvider(provider: ProviderType, engine?: EngineName | unknown): ProviderType {
  if (provider !== "comfyui") return provider;
  if (engine === "gpt-sovits" || engine === "indextts" || engine === "cosyvoice") return engine;
  return provider;
}

export function serviceEngineProvider(service: WorkerHealth): ProviderType {
  return engineProvider(service.provider_type ?? service.engine as ProviderType, service.engine);
}

export function configForService(config: Record<string, unknown>, service?: WorkerHealth): Record<string, unknown> {
  if (service?.provider_type !== "comfyui") return config;
  const defaults = service.default_params ?? {};
  const changedResource = defaults.resource_id && defaults.resource_id !== config.resource_id;
  const next = { ...config };
  if (changedResource) {
    for (const key of ["gpt_weights_path", "sovits_weights_path", "gpt_weight_options", "sovits_weight_options", "logs_name", "logs_id", "ref_audio_path", "reference_audio", "prompt_text", "logs_reference_sample_id", "logs_reference_label", "logs_reference_service_id", "logs_reference_logs_name"]) delete next[key];
  }
  for (const key of ["resource_id", "gpt_weights_root", "sovits_weights_root", "logs_root"]) {
    if (defaults[key] !== undefined) next[key] = defaults[key];
  }
  return { ...next, engine: service.engine };
}
