import type { LogsReferenceAudioSample, ReferenceAudioSample } from "../types";

export function gptReferenceAudioConfig(sample?: ReferenceAudioSample | null): Record<string, unknown> {
  return {
    reference_audio: null,
    ref_audio_path: sample?.path ?? null,
    prompt_text: sample?.text ?? null,
    logs_reference_sample_id: null,
    logs_reference_label: null,
    logs_reference_service_id: null,
    logs_reference_logs_name: null,
  };
}

export function applyLogsReferenceSampleToConfig(
  currentConfig: Record<string, unknown>,
  sample: LogsReferenceAudioSample,
  context: { serviceId?: string | null } = {}
): Record<string, unknown> {
  return {
    ...currentConfig,
    ref_audio_path: sample.path,
    reference_audio: null,
    prompt_text: sample.text || null,
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
