import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { CharacterCandidate, DraftOperation, SemanticUtterance } from "../../types";

export interface CharacterAliasEditorProps {
  characters: CharacterCandidate[];
  utterances?: SemanticUtterance[];
  onOperations: (operations: DraftOperation[]) => void;
  createCharacterId?: () => string;
  disabled?: boolean;
}

interface CharacterEdit {
  canonicalName: string;
  aliases: string;
}

let fallbackCharacterId = 0;

function defaultCreateCharacterId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return "character-" + globalThis.crypto.randomUUID();
  }
  fallbackCharacterId += 1;
  return "character-human-" + fallbackCharacterId;
}

function editsFor(characters: CharacterCandidate[]): Record<string, CharacterEdit> {
  return Object.fromEntries(
    characters.map((character) => [
      character.id,
      { canonicalName: character.canonical_name, aliases: character.aliases.join(", ") }
    ])
  );
}

function controlledAliases(value: string): string[] {
  return [...new Set(value.split(",").map((alias) => alias.trim()).filter(Boolean))];
}

export function CharacterAliasEditor({
  characters,
  onOperations,
  createCharacterId = defaultCreateCharacterId,
  disabled = false
}: CharacterAliasEditorProps) {
  const { t } = useTranslation();
  const [edits, setEdits] = useState<Record<string, CharacterEdit>>(() => editsFor(characters));
  const [newCharacterName, setNewCharacterName] = useState("");
  const [createOpen, setCreateOpen] = useState(false);

  useEffect(() => {
    setEdits(editsFor(characters));
  }, [characters]);

  const commitCharacter = (character: CharacterCandidate, edit: CharacterEdit) => {
    const canonicalName = edit.canonicalName.trim();
    if (!canonicalName) {
      setEdits((current) => ({
        ...current,
        [character.id]: {
          canonicalName: character.canonical_name,
          aliases: character.aliases.join(", ")
        }
      }));
      return;
    }
    const aliases = controlledAliases(edit.aliases);
    if (
      canonicalName === character.canonical_name &&
      aliases.length === character.aliases.length &&
      aliases.every((alias, index) => alias === character.aliases[index])
    ) {
      return;
    }
    onOperations([
      {
        op: "upsert_character",
        character: { ...character, canonical_name: canonicalName, aliases }
      }
    ]);
  };

  const createCharacter = () => {
    const canonicalName = newCharacterName.trim();
    if (!canonicalName) return;
    onOperations([
      {
        op: "upsert_character",
        character: {
          id: createCharacterId(),
          canonical_name: canonicalName,
          aliases: [],
          supporting_annotation_ids: [],
          project_character_id: null,
          confidence: null,
          status: "accepted",
          origin: "human"
        }
      }
    ]);
    setNewCharacterName("");
    setCreateOpen(false);
  };

  return (
    <section className="character-alias-editor" aria-labelledby="analysis-character-title">
      <div className="analysis-section-heading">
        <h2 id="analysis-character-title">{t("analysis.characters.title")}</h2>
        <button
          type="button"
          data-character-action="open-create"
          aria-expanded={createOpen}
          disabled={disabled}
          onClick={() => setCreateOpen((current) => !current)}
        >
          {t("analysis.characters.create")}
        </button>
        {createOpen ? (
          <div
            className="character-alias-editor__create-popover"
            role="dialog"
            aria-label={t("analysis.characters.create")}
          >
            <label>
              {t("analysis.characters.newName")}
              <input
                data-new-character-name
                value={newCharacterName}
                disabled={disabled}
                autoFocus
                onInput={(event) => setNewCharacterName(event.currentTarget.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") createCharacter();
                  if (event.key === "Escape") setCreateOpen(false);
                }}
              />
            </label>
            <div className="character-alias-editor__create-actions">
              <button type="button" onClick={() => setCreateOpen(false)}>
                {t("analysis.actions.cancel")}
              </button>
              <button
                type="button"
                data-character-action="create"
                disabled={disabled || !newCharacterName.trim()}
                onClick={createCharacter}
              >
                {t("analysis.characters.createHuman")}
              </button>
            </div>
          </div>
        ) : null}
      </div>

      {characters.length === 0 ? (
        <p className="analysis-empty-state">{t("analysis.characters.empty")}</p>
      ) : (
        <div className="character-alias-editor__cards">
          {characters.map((character) => {
            const edit = edits[character.id] ?? {
              canonicalName: character.canonical_name,
              aliases: character.aliases.join(", ")
            };
            return (
              <article
                key={character.id}
                className="character-alias-card"
                data-character-id={character.id}
              >
                <header className="character-alias-card__header">
                  <strong>{edit.canonicalName || character.canonical_name}</strong>
                  <div
                    className="analysis-review-actions"
                    aria-label={t("analysis.characters.reviewActions")}
                  >
                    {(["accepted", "pending", "rejected"] as const).map((status) => {
                      const action =
                        status === "accepted"
                          ? "accept"
                          : status === "rejected"
                            ? "reject"
                            : "restore";
                      return (
                        <button
                          key={status}
                          type="button"
                          data-character-action={action}
                          data-character-id={character.id}
                          disabled={disabled}
                          aria-pressed={character.status === status}
                          onClick={() =>
                            onOperations([
                              {
                                op: "set_character_status",
                                character_id: character.id,
                                status
                              }
                            ])
                          }
                        >
                          {t("analysis.actions." + action)}
                        </button>
                      );
                    })}
                  </div>
                </header>

                <label>
                  {t("analysis.characters.canonicalName")}
                  <input
                    data-character-name={character.id}
                    value={edit.canonicalName}
                    disabled={disabled}
                    onInput={(event) => {
                      const value = event.currentTarget.value;
                      setEdits((current) => ({
                        ...current,
                        [character.id]: { ...edit, canonicalName: value }
                      }));
                    }}
                    onBlur={() => commitCharacter(character, edit)}
                  />
                </label>
                <label>
                  {t("analysis.characters.aliases")}
                  <input
                    data-character-aliases={character.id}
                    value={edit.aliases}
                    disabled={disabled}
                    onInput={(event) => {
                      const value = event.currentTarget.value;
                      setEdits((current) => ({
                        ...current,
                        [character.id]: { ...edit, aliases: value }
                      }));
                    }}
                    onBlur={() => commitCharacter(character, edit)}
                  />
                </label>

                {character.aliases.length > 0 ? (
                  <div className="character-alias-card__split-list">
                    <span>{t("analysis.characters.splitAlias")}</span>
                    {character.aliases.map((alias) => (
                      <button
                        key={alias}
                        type="button"
                        data-split-alias={alias}
                        disabled={disabled}
                        onClick={() =>
                          onOperations([
                            {
                              op: "split_alias",
                              character_id: character.id,
                              alias,
                              character: {
                                id: createCharacterId(),
                                canonical_name: alias,
                                aliases: [],
                                supporting_annotation_ids: [],
                                project_character_id: null,
                                confidence: null,
                                status: "accepted",
                                origin: "human"
                              }
                            }
                          ])
                        }
                      >
                        {alias}
                      </button>
                    ))}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
