import { useMemo } from "react";

import type { Character, ProjectCharacter } from "../../types";

export interface RoleLibraryControllerOptions {
  characters: Character[];
  projectCharacters: ProjectCharacter[];
  search: string;
  onSaveProjectCharacters: (characters: ProjectCharacter[]) => void;
}

export function useRoleLibraryController({
  characters,
  projectCharacters,
  search,
  onSaveProjectCharacters
}: RoleLibraryControllerOptions) {
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const filteredCharacters = useMemo(() => (
    normalizedSearch
      ? characters.filter((character) => [
        character.name,
        ...(character.aliases ?? []),
        ...(character.nicknames ?? []),
        ...(character.match_names ?? [])
      ].some((value) => value.toLocaleLowerCase().includes(normalizedSearch)))
      : characters
  ), [characters, normalizedSearch]);

  function mapProjectRole(projectRoleId: string, libraryCharacterId: string | null) {
    onSaveProjectCharacters(projectCharacters.map((projectCharacter) => (
      projectCharacter.project_character_id === projectRoleId
        ? {
          ...projectCharacter,
          library_character_id: libraryCharacterId,
          mode: "reference" as const,
          character_snapshot: null,
          project_binding: null,
          match_status: libraryCharacterId ? "matched" as const : "unmatched" as const
        }
        : projectCharacter
    )));
  }

  return { filteredCharacters, mapProjectRole };
}
