import { useMemo } from "react";

import type { Character, ProjectCharacter } from "../../types";

export interface RoleLibraryControllerOptions {
  characters: Character[];
  projectCharacters: ProjectCharacter[];
  search: string;
  onSaveCharacters: (characters: Character[]) => void;
  onSaveProjectCharacters: (characters: ProjectCharacter[]) => void;
}

export function useRoleLibraryController({
  characters,
  projectCharacters,
  search,
  onSaveCharacters,
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
    const projectRole = projectCharacters.find((item) => item.project_character_id === projectRoleId);
    const roleName = projectRole?.name.trim() ?? "";
    const roleKey = roleName.toLocaleLowerCase();
    const targetCharacter = libraryCharacterId
      ? characters.find((character) => character.id === libraryCharacterId)
      : undefined;
    if (targetCharacter && roleKey) {
      const targetMatchValues = [
        targetCharacter.name,
        ...(targetCharacter.aliases ?? []),
        ...(targetCharacter.nicknames ?? []),
        ...(targetCharacter.match_names ?? [])
      ];
      const needsAlias = !targetMatchValues.some((value) => value.trim().toLocaleLowerCase() === roleKey);
      const updatedAt = new Date().toISOString();
      let removedConflictingAlias = false;
      const remainingCharacters = characters
        .filter((character) => character.id !== targetCharacter.id)
        .map((character) => {
          const aliases = (character.aliases ?? []).filter((alias) => alias.trim().toLocaleLowerCase() !== roleKey);
          if (aliases.length === (character.aliases ?? []).length) return character;
          removedConflictingAlias = true;
          return { ...character, aliases, updated_at: updatedAt };
        });
      if (needsAlias || removedConflictingAlias) {
        onSaveCharacters([
          ...remainingCharacters,
          {
            ...targetCharacter,
            aliases: needsAlias
              ? [...(targetCharacter.aliases ?? []), roleName]
              : targetCharacter.aliases,
            updated_at: updatedAt
          }
        ]);
      }
    }
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
