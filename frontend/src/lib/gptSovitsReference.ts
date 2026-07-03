import type { LogsReferenceAudioSample } from "../types";

export function applyLogsReferenceSampleToConfig(
  currentConfig: Record<string, unknown>,
  sample: LogsReferenceAudioSample,
  context: { serviceId?: string | null } = {}
): Record<string, unknown> {
  return {
    ...currentConfig,
    ref_audio_path: sample.path,
    prompt_text: sample.text || currentConfig.prompt_text,
    prompt_lang: sample.prompt_lang || currentConfig.prompt_lang || "zh",
    logs_reference_sample_id: sample.sample_id,
    logs_reference_label: sample.display_label,
    logs_reference_service_id: context.serviceId || undefined,
    logs_reference_logs_name: sample.logs_name || currentConfig.logs_name,
  };
}

export function selectedLogsReferenceSample(
  samples: LogsReferenceAudioSample[],
  config: Record<string, unknown>,
  context: { serviceId?: string | null } = {}
): LogsReferenceAudioSample | undefined {
  const sampleServiceId = stringValue(config.logs_reference_service_id);
  if (sampleServiceId && context.serviceId && sampleServiceId !== context.serviceId) return undefined;
  const sampleId = stringValue(config.logs_reference_sample_id);
  const refPath = stringValue(config.ref_audio_path);
  return samples.find((sample) => sample.sample_id === sampleId) ?? samples.find((sample) => sample.path === refPath);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
