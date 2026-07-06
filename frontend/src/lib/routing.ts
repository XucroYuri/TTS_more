import type { Character, EngineName, GenerationTask, ScriptLine, VoiceBinding, VoiceProfile } from "../types";

export function lineEngine(line: ScriptLine, characters: Character[]): EngineName {
  const binding = lineBinding(line, characters);
  if (binding) return engineForProvider(binding.provider_type);
  const character = findCharacter(line, characters);
  const profile = findProfile(line, characters);
  return line.engine_override ?? profile?.engine ?? character?.default_engine ?? "gpt-sovits";
}

export function lineProfile(line: ScriptLine, characters: Character[]): string {
  if (line.temporary_binding) return line.temporary_binding.binding_id;
  const character = findCharacter(line, characters);
  return line.profile_override ?? character?.default_profile ?? "default";
}

export function lineServiceId(line: ScriptLine, characters: Character[]): string | null {
  const binding = lineBinding(line, characters);
  const profile = findProfile(line, characters);
  return line.service_override ?? binding?.service_id ?? profile?.service_id ?? null;
}

export function lineFallbackServiceIds(line: ScriptLine, characters: Character[]): string[] {
  const binding = lineBinding(line, characters);
  return binding?.fallback_services ?? findProfile(line, characters)?.fallback_services ?? [];
}

function lineParameters(line: ScriptLine, characters: Character[]): Record<string, unknown> {
  const binding = lineBinding(line, characters);
  return binding?.config ?? findProfile(line, characters)?.config ?? {};
}

export function buildGenerationTask(line: ScriptLine, characters: Character[]): GenerationTask {
  const binding = lineBinding(line, characters);
  if (!binding) {
    throw new Error(`line ${line.id} character ${line.character_id} needs a voice binding before generation`);
  }
  return {
    line,
    engine: lineEngine(line, characters),
    profile: lineProfile(line, characters),
    service_id: lineServiceId(line, characters),
    fallback_service_ids: lineFallbackServiceIds(line, characters),
    provider_type: binding?.provider_type ?? null,
    binding_id: binding?.binding_id ?? null,
    required_capabilities: binding?.capabilities ?? [],
    parameters: lineParameters(line, characters)
  };
}

export function lineBinding(line: ScriptLine, characters: Character[]): VoiceBinding | undefined {
  if (line.temporary_binding) return line.temporary_binding;
  const profile = findProfile(line, characters);
  if (!profile?.bindings?.length) return undefined;
  if (line.binding_override) {
    return profile.bindings.find((binding) => binding.binding_id === line.binding_override) ?? profile.bindings[0];
  }
  return profile.bindings[0];
}

function findCharacter(line: ScriptLine, characters: Character[]): Character | undefined {
  return characters.find((item) => item.id === line.character_id);
}

function findProfile(line: ScriptLine, characters: Character[]): VoiceProfile | undefined {
  const character = findCharacter(line, characters);
  const profileId = line.profile_override ?? character?.default_profile;
  return character?.profiles?.find((profile) => profile.id === profileId);
}

function engineForProvider(provider: VoiceBinding["provider_type"]): EngineName {
  if (provider === "gpt-sovits" || provider === "indextts" || provider === "cosyvoice" || provider === "vibevoice") return provider;
  return "commercial";
}
