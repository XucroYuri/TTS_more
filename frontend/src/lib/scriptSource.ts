import type { Character, ScriptLine, ScriptProject } from "../types";

export function projectToScriptSourceText(project: ScriptProject, characters: Character[]): string {
  return project.lines.map((line) => lineToScriptSourceText(project, characters, line)).join("\n");
}

function lineToScriptSourceText(project: ScriptProject, characters: Character[], line: ScriptLine): string {
  const name = characterDisplayName(project, characters, line.character_id);
  const note = line.note?.trim() ? `（${line.note.trim()}）` : "";
  return `${name}${note}: ${line.text}`;
}

function characterDisplayName(project: ScriptProject, characters: Character[], characterId: string): string {
  return (
    project.project_characters?.find((character) => character.project_character_id === characterId)?.name ||
    characters.find((character) => character.id === characterId)?.name ||
    characterId
  );
}
