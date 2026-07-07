import type { LogsReferenceAudioSample, ReferenceAudioSample, RoleLibraryCandidate, VoiceBinding } from "../types";

export function gptSovitsProjectBindingFromModel(
  projectCharacterId: string,
  model: RoleLibraryCandidate,
  sample?: LogsReferenceAudioSample | ReferenceAudioSample | null
): VoiceBinding {
  const selectedSample = sample ?? firstReferenceSampleFromModel(model);
  const logsName = model.logs_name || model.name || model.id;
  return {
    binding_id: `${projectCharacterId}-project-gpt`,
    provider_type: "gpt-sovits",
    service_id: model.service_id ?? null,
    fallback_services: [],
    capabilities: ["trained_weights_voice", "reference_audio_voice"],
    config: compactConfig({
      logs_id: model.logs_id,
      logs_name: logsName,
      path_service_id: model.service_id ?? undefined,
      character_filter: logsName,
      gpt_weight_options: model.gpt_weights ?? [],
      sovits_weight_options: model.sovits_weights ?? [],
      gpt_weights_path: model.recommended_gpt_weights_path,
      sovits_weights_path: model.recommended_sovits_weights_path,
      ref_audio_path: selectedSample?.path ?? model.recommended_ref_audio_path,
      prompt_text: selectedSample?.text,
      prompt_lang: samplePromptLang(selectedSample) || "zh",
      logs_reference_sample_id: logsSampleId(selectedSample),
      logs_reference_label: logsSampleLabel(selectedSample),
      logs_reference_service_id: model.service_id ?? undefined,
      logs_reference_logs_name: logsName
    })
  };
}

export function firstReferenceSampleFromModel(model: RoleLibraryCandidate): ReferenceAudioSample | null {
  for (const group of model.reference_audio_groups ?? []) {
    const sample = group.samples?.[0];
    if (sample) return sample;
  }
  return null;
}

function samplePromptLang(sample?: LogsReferenceAudioSample | ReferenceAudioSample | null): string {
  if (!sample) return "";
  if ("prompt_lang" in sample && typeof sample.prompt_lang === "string") return sample.prompt_lang;
  return "";
}

function logsSampleId(sample?: LogsReferenceAudioSample | ReferenceAudioSample | null): string | undefined {
  return sample && "sample_id" in sample ? sample.sample_id : undefined;
}

function logsSampleLabel(sample?: LogsReferenceAudioSample | ReferenceAudioSample | null): string | undefined {
  return sample && "display_label" in sample ? sample.display_label : undefined;
}

function compactConfig(config: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(config).filter(([, value]) => value !== undefined && value !== ""));
}
