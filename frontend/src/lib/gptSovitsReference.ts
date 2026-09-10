import type { Character, LogsReferenceAudioSample } from "../types";

export const CATALOG_STAGED_REFERENCE_OPTION = "__catalog_reference__";
const CATALOG_WEIGHT_OPTION_PREFIX = "__catalog_weight__:";

export function selectedDynamicWeightOption(
  config: Record<string, unknown>,
  kind: "gpt" | "sovits"
): { value: string; relativePath: string } {
  const directPath = stringValue(config[`${kind}_weights_path`]);
  if (directPath) return { value: directPath, relativePath: "" };
  const relativePath = stringValue(config[`${kind}_weights_relative_path`]);
  return relativePath
    ? { value: `${CATALOG_WEIGHT_OPTION_PREFIX}${kind}`, relativePath }
    : { value: "", relativePath: "" };
}

export function selectedLogsReferenceOptionValue(
  sample: LogsReferenceAudioSample | undefined,
  config: Record<string, unknown>
): string {
  if (sample) return sample.sample_id;
  return stringValue(config.ref_audio_path) ? CATALOG_STAGED_REFERENCE_OPTION : "";
}

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

export function referenceAudioSamplesForCharacter(
  character: Character | undefined,
  logsSamples: LogsReferenceAudioSample[],
  logsName = ""
): LogsReferenceAudioSample[] {
  if (!character) return [];
  const identities = [
    character.id,
    character.name,
    ...(character.aliases ?? []),
    ...(character.nicknames ?? []),
    ...(character.match_names ?? [])
  ].map(normalizeIdentity).filter(Boolean);
  const output: LogsReferenceAudioSample[] = [];
  const seenPaths = new Set<string>();
  let roleSampleIndex = 0;

  const append = (sample: LogsReferenceAudioSample) => {
    const pathKey = normalizePath(sample.path);
    if (!pathKey || seenPaths.has(pathKey)) return;
    seenPaths.add(pathKey);
    output.push(sample);
  };

  for (const group of character.reference_audio_groups ?? []) {
    for (const sample of group.samples ?? []) {
      append({
        sample_id: `role:${character.id}:${roleSampleIndex++}`,
        display_label: sample.text ? `${character.name} · ${sample.text}` : `${character.name} · ${fileName(sample.path)}`,
        path: sample.path,
        text: sample.text ?? "",
        text_source: sample.text_source ?? "none",
        character: character.name,
        prompt_lang: "zh",
        source: "role_library",
        logs_name: logsName || undefined,
      });
    }
    for (const path of [...(group.copied_paths ?? []), ...(group.paths ?? [])]) {
      if (!isAudioPath(path)) continue;
      append({
        sample_id: `role:${character.id}:${roleSampleIndex++}`,
        display_label: `${character.name} · ${fileName(path)}`,
        path,
        text: "",
        text_source: "none",
        character: character.name,
        prompt_lang: "zh",
        source: "role_library",
        logs_name: logsName || undefined,
      });
    }
  }

  const matchingLogsSamples = logsSamples.filter((sample) => {
    const sampleIdentity = normalizeIdentity(sample.character ?? "");
    return sampleIdentity
      ? identities.some((identity) => identitiesMatch(identity, sampleIdentity))
      : Boolean(logsName);
  });
  const logsSamplesToAppend = matchingLogsSamples.length > 0 || output.length > 0 || logsName
    ? matchingLogsSamples
    : logsSamples;
  for (const sample of logsSamplesToAppend) {
    append(sample);
  }
  return output;
}

function identitiesMatch(left: string, right: string): boolean {
  if (left === right) return true;
  return left.length >= 2 && right.length >= 2 && (left.includes(right) || right.includes(left));
}

function normalizeIdentity(value: string): string {
  return value.replace(/[\s_\-.]+/g, "").toLocaleLowerCase();
}

function normalizePath(value: string): string {
  return value.replaceAll("\\", "/").toLocaleLowerCase();
}

function fileName(value: string): string {
  return value.split(/[\\/]/).filter(Boolean).at(-1) ?? value;
}

function isAudioPath(value: string): boolean {
  return /\.(aac|flac|m4a|mp3|ogg|opus|wav|webm)$/i.test(value);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
