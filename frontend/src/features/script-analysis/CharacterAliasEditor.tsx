import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { CharacterCandidate, DraftOperation, SemanticUtterance } from "../../types";

export interface CharacterAliasEditorProps {
  characters: CharacterCandidate[];
  utterances: SemanticUtterance[];
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
    return `character-${globalThis.crypto.randomUUID()}`;
  }
  fallbackCharacterId += 1;
  return `character-human-${fallbackCharacterId}`;
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

export function reassignUtterance(
  utterance: SemanticUtterance,
  characterCandidateId: string | null
): SemanticUtterance {
  if (characterCandidateId) {
    return {
      ...utterance,
      character_candidate_id: characterCandidateId,
      uncertainty_codes: utterance.uncertainty_codes.filter(
        (code) => code !== "speaker_unknown" && code !== "speaker_ambiguous"
      )
    };
  }
  return {
    ...utterance,
    character_candidate_id: null,
    status: utterance.status === "accepted" ? "pending" : utterance.status,
    uncertainty_codes: [...new Set([...utterance.uncertainty_codes, "speaker_unknown" as const])]
  };
}

export function CharacterAliasEditor({
  characters,
  utterances,
  onOperations,
  createCharacterId = defaultCreateCharacterId,
  disabled = false
}: CharacterAliasEditorProps) {
  const { t } = useTranslation();
  const [edits, setEdits] = useState<Record<string, CharacterEdit>>(() => editsFor(characters));
  const [newCharacterName, setNewCharacterName] = useState("");
  const [mergeTarget, setMergeTarget] = useState(
    characters.find((character) => character.status === "accepted")?.id ?? ""
  );
  const [mergeSource, setMergeSource] = useState(characters[1]?.id ?? "");

  const acceptedCharacters = useMemo(
    () => characters.filter((character) => character.status === "accepted"),
    [characters]
  );

  useEffect(() => {
    setEdits(editsFor(characters));
    setMergeTarget((current) =>
      acceptedCharacters.some((candidate) => candidate.id === current)
        ? current
        : acceptedCharacters[0]?.id ?? ""
    );
    setMergeSource((current) =>
      characters.some((candidate) => candidate.id === current)
        ? current
        : characters.find((candidate) => candidate.id !== characters[0]?.id)?.id ?? ""
    );
  }, [acceptedCharacters, characters]);

  return (
    <section className="character-alias-editor" aria-labelledby="analysis-character-title">
      <h2 id="analysis-character-title">{t("analysis.characters.title")}</h2>
      <div className="character-alias-editor__create">
        <label>
          {t("analysis.characters.newName")}
          <input
            data-new-character-name
            value={newCharacterName}
            disabled={disabled}
            onInput={(event) => setNewCharacterName(event.currentTarget.value)}
          />
        </label>
        <button
          type="button"
          data-character-action="create"
          disabled={disabled || !newCharacterName.trim()}
          onClick={() => {
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
          }}
        >
          {t("analysis.characters.createHuman")}
        </button>
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
              <article key={character.id} className="character-alias-card" data-character-id={character.id}>
                <div className="character-alias-card__status-row">
                  <span className={`analysis-status analysis-status--${character.status}`}>
                    {t(`analysis.status.${character.status}`)}
                  </span>
                  <div className="analysis-review-actions">
                    <button
                      type="button"
                      data-character-action="accept"
                      data-character-id={character.id}
                      disabled={disabled}
                      onClick={() =>
                        onOperations([
                          { op: "set_character_status", character_id: character.id, status: "accepted" }
                        ])
                      }
                    >
                      {t("analysis.actions.accept")}
                    </button>
                    <button
                      type="button"
                      data-character-action="reject"
                      data-character-id={character.id}
                      disabled={disabled}
                      onClick={() =>
                        onOperations([
                          { op: "set_character_status", character_id: character.id, status: "rejected" }
                        ])
                      }
                    >
                      {t("analysis.actions.reject")}
                    </button>
                    <button
                      type="button"
                      data-character-action="restore"
                      data-character-id={character.id}
                      disabled={disabled}
                      onClick={() =>
                        onOperations([
                          { op: "set_character_status", character_id: character.id, status: "pending" }
                        ])
                      }
                    >
                      {t("analysis.actions.restore")}
                    </button>
                  </div>
                </div>

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
                  />
                </label>
                <button
                  type="button"
                  data-character-action="save"
                  data-character-id={character.id}
                  disabled={disabled || !edit.canonicalName.trim()}
                  onClick={() =>
                    onOperations([
                      {
                        op: "upsert_character",
                        character: {
                          ...character,
                          canonical_name: edit.canonicalName.trim(),
                          aliases: controlledAliases(edit.aliases)
                        }
                      }
                    ])
                  }
                >
                  {t("analysis.actions.save")}
                </button>
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

      {characters.length > 1 ? (
        <div className="character-alias-editor__merge">
          <label>
            {t("analysis.characters.mergeTarget")}
            <select
              data-merge-target
              value={mergeTarget}
              disabled={disabled}
              onChange={(event) => setMergeTarget(event.target.value)}
            >
              {acceptedCharacters.map((character) => (
                <option key={character.id} value={character.id}>{character.canonical_name}</option>
              ))}
            </select>
          </label>
          <label>
            {t("analysis.characters.mergeSource")}
            <select
              data-merge-source
              value={mergeSource}
              disabled={disabled}
              onChange={(event) => setMergeSource(event.target.value)}
            >
              {characters.map((character) => (
                <option key={character.id} value={character.id}>{character.canonical_name}</option>
              ))}
            </select>
          </label>
          <button
            type="button"
            data-character-action="merge"
            disabled={disabled || !mergeTarget || !mergeSource || mergeTarget === mergeSource}
            onClick={() =>
              onOperations([
                {
                  op: "merge_characters",
                  target_character_id: mergeTarget,
                  source_character_ids: [mergeSource]
                }
              ])
            }
          >
            {t("analysis.actions.merge")}
          </button>
        </div>
      ) : null}

      {utterances.length > 0 ? (
        <div className="character-alias-editor__assignments">
          <h3>{t("analysis.characters.assignments")}</h3>
          {utterances.map((utterance) => (
            <label key={utterance.id}>
              {utterance.id}
              <select
                data-utterance-character={utterance.id}
                value={utterance.character_candidate_id ?? ""}
                disabled={disabled}
                onChange={(event) =>
                  onOperations([
                    {
                      op: "update_utterance",
                      utterance_id: utterance.id,
                      utterance: reassignUtterance(utterance, event.target.value || null)
                    }
                  ])
                }
              >
                <option value="">{t("analysis.results.unassigned")}</option>
                {acceptedCharacters.map((character) => (
                  <option key={character.id} value={character.id}>{character.canonical_name}</option>
                ))}
              </select>
            </label>
          ))}
        </div>
      ) : null}
    </section>
  );
}
