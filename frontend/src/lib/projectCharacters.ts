import type { Character, ProjectCharacter, ScriptProject } from "../types";

export interface ProjectCharacterRow {
  id: string;
  name: string;
  mode: ProjectCharacter["mode"];
  provider: string;
  profile: string;
  lineCount: number;
  linked: boolean;
}

export function resolveProjectCharacters(project: ScriptProject, library: Character[]): Character[] {
  const mappings = ensureProjectCharacters(project, library);
  return mappings.map((item) => {
    const source = item.mode === "snapshot" && item.character_snapshot
      ? item.character_snapshot
      : library.find((character) => character.id === item.library_character_id);
    if (!source) {
      return {
        id: item.project_character_id,
        name: item.name,
        aliases: [item.name],
        nicknames: [],
        match_names: [],
        notes: "",
        library_status: "draft",
        fallback_profiles: []
      };
    }
    return {
      ...cloneCharacter(source),
      id: item.project_character_id,
      name: item.name || source.name
    };
  });
}

export function projectCharacterRows(project: ScriptProject, library: Character[]): ProjectCharacterRow[] {
  const resolved = resolveProjectCharacters(project, library);
  const countByRole = new Map<string, number>();
  for (const line of project.lines) {
    countByRole.set(line.character_id, (countByRole.get(line.character_id) ?? 0) + 1);
  }
  const byId = new Map(resolved.map((character) => [character.id, character]));
  return ensureProjectCharacters(project, library).map((item) => {
    const character = byId.get(item.project_character_id);
    const profile = character?.profiles?.find((candidate) => candidate.id === character.default_profile) ?? character?.profiles?.[0];
    const binding = profile?.bindings?.[0];
    return {
      id: item.project_character_id,
      name: item.name,
      mode: item.mode,
      provider: binding?.provider_type ?? profile?.engine ?? character?.default_engine ?? "unassigned",
      profile: profile?.name ?? character?.default_profile ?? "unassigned",
      lineCount: countByRole.get(item.project_character_id) ?? 0,
      linked: Boolean(item.library_character_id || item.character_snapshot)
    };
  });
}

export function freezeProjectCharacterLocally(projectCharacter: ProjectCharacter, library: Character[]): ProjectCharacter {
  const source = projectCharacter.mode === "snapshot" && projectCharacter.character_snapshot
    ? projectCharacter.character_snapshot
    : library.find((character) => character.id === projectCharacter.library_character_id);
  return {
    ...projectCharacter,
    mode: "snapshot",
    character_snapshot: source ? cloneCharacter(source) : projectCharacter.character_snapshot ?? null
  };
}

export function ensureProjectCharacters(project: ScriptProject, library: Character[]): ProjectCharacter[] {
  if (project.project_characters?.length) return project.project_characters;
  const lookup = new Map<string, Character>();
  for (const character of library) {
    for (const value of matchValues(character)) {
      lookup.set(normalize(value), character);
    }
  }
  const seen = new Set<string>();
  const output: ProjectCharacter[] = [];
  for (const line of project.lines) {
    if (seen.has(line.character_id)) continue;
    seen.add(line.character_id);
    const match = lookup.get(normalize(line.character_id));
    output.push({
      project_character_id: line.character_id,
      name: match?.name ?? line.character_id,
      library_character_id: match?.id ?? null,
      mode: "reference",
      character_snapshot: null,
      match_confidence: match ? 1 : null,
      match_status: match ? "matched" : "unmatched"
    });
  }
  return output;
}

function cloneCharacter(character: Character): Character {
  return JSON.parse(JSON.stringify(character)) as Character;
}

function normalize(value: string): string {
  return value.replace(/\s+/g, "").toLocaleLowerCase();
}

function matchValues(character: Character): string[] {
  return Array.from(new Set([
    character.id,
    character.name,
    ...(character.aliases ?? []),
    ...(character.nicknames ?? []),
    ...(character.match_names ?? [])
  ]));
}
