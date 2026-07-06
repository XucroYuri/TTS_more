import type { Character, ScriptLine, ScriptProject } from "../types";
import { formatScriptNote } from "./lineNote";

export function projectToScriptSourceText(project: ScriptProject, characters: Character[]): string {
  if (project.lines.length === 0) {
    const activeRevision = project.script_revisions?.find((revision) => revision.revision_id === project.active_script_revision_id);
    if (activeRevision?.source_markdown) return activeRevision.source_markdown;
  }
  return project.lines.map((line) => lineToScriptSourceText(project, characters, line)).join("\n");
}

function lineToScriptSourceText(project: ScriptProject, characters: Character[], line: ScriptLine): string {
  const name = characterDisplayName(project, characters, line.character_id);
  const note = formatScriptNote(line.note);
  return `${name}${note}: ${line.text}`;
}

function characterDisplayName(project: ScriptProject, characters: Character[], characterId: string): string {
  return (
    project.project_characters?.find((character) => character.project_character_id === characterId)?.name ||
    characters.find((character) => character.id === characterId)?.name ||
    characterId
  );
}
