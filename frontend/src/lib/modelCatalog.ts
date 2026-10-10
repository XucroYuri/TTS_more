import type { LogsReferenceAudioSample, ReferenceAudioSample, RoleLibraryCandidate, VoiceBinding, WorkerHealth } from "../types";

export function gptSovitsProjectBindingFromModel(
  projectCharacterId: string,
  model: RoleLibraryCandidate,
  sample?: LogsReferenceAudioSample | ReferenceAudioSample | null,
  service?: WorkerHealth
): VoiceBinding {
  // A legacy remote sample may be selected explicitly without duration metadata;
  // automatic defaults require a verified, annotated 3–10 second recording.
  const selectedSample = sample
    ? (isSelectableGptReferenceSample(sample) ? sample : null)
    : firstReferenceSampleFromModel(model);
  const logsName = model.logs_name || model.name || model.id;
  return {
    binding_id: `${projectCharacterId}-project-gpt`,
    provider_type: service?.provider_type === "comfyui" ? "comfyui" : "gpt-sovits",
    service_id: model.service_id ?? null,
    fallback_services: [],
    capabilities: ["trained_weights_voice", "reference_audio_voice"],
    config: compactConfig({
      engine: "gpt-sovits",
      logs_id: model.logs_id,
      logs_name: logsName,
      gpt_model_catalog_id: model.id,
      gpt_model_version: model.model_version ?? null,
      path_service_id: model.service_id ?? undefined,
      character_filter: logsName,
      gpt_weight_options: model.gpt_weights ?? [],
      sovits_weight_options: model.sovits_weights ?? [],
      gpt_weights_path: model.recommended_gpt_weights_path ?? null,
      sovits_weights_path: model.recommended_sovits_weights_path ?? null,
      ref_audio_path: selectedSample?.path ?? null,
      prompt_text: selectedSample?.text ?? null,
      prompt_lang: samplePromptLang(selectedSample) || "zh",
      logs_reference_sample_id: logsSampleId(selectedSample) ?? null,
      logs_reference_label: logsSampleLabel(selectedSample) ?? null,
      logs_reference_service_id: model.service_id ?? null,
      logs_reference_logs_name: logsName
    }),
  };
}

export function firstReferenceSampleFromModel(model: RoleLibraryCandidate): ReferenceAudioSample | null {
  for (const group of model.reference_audio_groups ?? []) {
    const sample = group.samples?.find(isUsableGptReferenceSample);
    if (sample) return sample;
  }
  return null;
}

type GptReferenceSample = LogsReferenceAudioSample | ReferenceAudioSample;

export function isUsableGptReferenceSample(sample: GptReferenceSample): boolean {
  return Boolean(sample.path && sample.text?.trim()
    && typeof sample.duration_seconds === "number"
    && Number.isFinite(sample.duration_seconds)
    && sample.duration_seconds >= 3 && sample.duration_seconds <= 10);
}

export function isSelectableGptReferenceSample(sample: GptReferenceSample): boolean {
  return Boolean(sample.path && sample.text?.trim()
    && (sample.duration_seconds == null || isUsableGptReferenceSample(sample)));
}

export function gptSovitsExperimentConfig(logsName: string, model?: RoleLibraryCandidate): Record<string, unknown> {
  // Binding patches are merged into the current line. Explicit nulls prevent
  // an incomplete/new experiment from inheriting the previous reference.
  const cleared = {
    logs_id: null, logs_name: logsName,
    gpt_model_catalog_id: null, gpt_model_version: null,
    gpt_weights_path: null, sovits_weights_path: null,
    gpt_weight_options: [], sovits_weight_options: [],
    gpt_checkpoint: null, sovits_checkpoint: null,
    gpt_weight: null, sovits_weight: null, gpt_weights: null, sovits_weights: null,
    reference_audio: null, ref_audio_path: null, prompt_audio_path: null,
    prompt_text: null, logs_reference_sample_id: null, logs_reference_label: null,
    logs_reference_service_id: null, logs_reference_logs_name: logsName,
  };
  if (!model) return cleared;
  const next = { ...cleared, ...gptSovitsProjectBindingFromModel("line", model).config };
  if (!model.recommended_gpt_weights_path || !model.recommended_sovits_weights_path) {
    // A logs-only candidate must not synthesize through the previously selected
    // service's default model and masquerade as a different trained experiment.
    next.ref_audio_path = null;
    next.prompt_text = null;
    next.logs_reference_sample_id = null;
    next.logs_reference_label = null;
  }
  return next;
}

export function gptSovitsExperimentOptions(models: RoleLibraryCandidate[]): Array<{ value: string; model: RoleLibraryCandidate }> {
  const labels = models.map((model) => {
    const name = model.logs_name || model.name || model.id;
    if (model.model_version) return `${name} [${model.model_version}]`;
    const ambiguous = models.filter((item) => (item.logs_name || item.name || item.id) === name).length > 1;
    return ambiguous ? `${name} · ${model.id}` : name;
  });
  return models.map((model, index) => ({
    model,
    value: labels.filter((label) => label === labels[index]).length > 1 ? `${labels[index]} · ${model.id}` : labels[index],
  }));
}

export function resolveGptSovitsExperiment(value: string, models: RoleLibraryCandidate[]): RoleLibraryCandidate | undefined {
  const exactOption = gptSovitsExperimentOptions(models).find((option) => option.value === value);
  if (exactOption) return exactOption.model;
  const matches = models.filter((model) => (model.logs_name || model.name || model.id) === value);
  // Raw logs names remain usable for legacy/manual input only when unambiguous.
  return matches.length === 1 ? matches[0] : undefined;
}

export function gptSovitsExperimentInputValue(config: Record<string, unknown>, models: RoleLibraryCandidate[]): string {
  const options = gptSovitsExperimentOptions(models);
  const selected = options.find((option) => option.model.id === config.gpt_model_catalog_id);
  if (selected) return selected.value;
  const logsName = typeof config.logs_name === "string" ? config.logs_name : "";
  const matches = options.filter((option) => (option.model.logs_name || option.model.name || option.model.id) === logsName);
  if (matches.length === 1) return matches[0].value;
  // Existing bindings predate catalog IDs. Two independent checkpoint paths
  // can still identify their version without changing the persisted logs name.
  const byWeights = matches.filter(({model}) => {
    const gptPaths = [model.recommended_gpt_weights_path, ...(model.gpt_weights ?? []).map((item) => item.path)];
    const sovitsPaths = [model.recommended_sovits_weights_path, ...(model.sovits_weights ?? []).map((item) => item.path)];
    return typeof config.gpt_weights_path === "string" && typeof config.sovits_weights_path === "string"
      && gptPaths.includes(config.gpt_weights_path) && sovitsPaths.includes(config.sovits_weights_path);
  });
  return byWeights.length === 1 ? byWeights[0].value : logsName;
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
