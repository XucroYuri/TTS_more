import type {
  CharacterCandidate,
  DraftOperation,
  SemanticAnalysisDraft,
  SemanticAnnotation,
  SemanticUtterance
} from "../../types";

function cloneAnnotation(item: SemanticAnnotation): SemanticAnnotation {
  return { ...item, span: { ...item.span } };
}

function cloneCharacter(item: CharacterCandidate): CharacterCandidate {
  return { ...item, aliases: [...item.aliases], supporting_annotation_ids: [...item.supporting_annotation_ids] };
}

function cloneUtterance(item: SemanticUtterance): SemanticUtterance {
  return {
    ...item,
    emotion_evidence_annotation_ids: [...item.emotion_evidence_annotation_ids],
    uncertainty_codes: [...item.uncertainty_codes]
  };
}

function operationError(message: string): Error {
  return new Error(`analysis_operation_invalid:${message}`);
}

function findIndex<T extends { id: string }>(items: T[], id: string): number {
  const index = items.findIndex((item) => item.id === id);
  if (index < 0) throw operationError(`missing:${id}`);
  return index;
}

function ensureMissing<T extends { id: string }>(items: T[], id: string): void {
  if (items.some((item) => item.id === id)) throw operationError(`duplicate:${id}`);
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)];
}

export function applyDraftOperations(sourceDraft: SemanticAnalysisDraft, operations: DraftOperation[]): SemanticAnalysisDraft {
  const next: SemanticAnalysisDraft = {
    ...sourceDraft,
    annotations: sourceDraft.annotations.map(cloneAnnotation),
    characters: sourceDraft.characters.map(cloneCharacter),
    utterances: sourceDraft.utterances.map(cloneUtterance),
    unresolved_candidates: sourceDraft.unresolved_candidates.map((item) => ({ ...item, details: { ...item.details } })),
    warnings: sourceDraft.warnings.map((item) => ({ ...item, details: { ...item.details } }))
  };

  for (const operation of operations) {
    switch (operation.op) {
      case "create_annotation":
        ensureMissing(next.annotations, operation.annotation.id);
        next.annotations.push(cloneAnnotation(operation.annotation));
        break;
      case "replace_annotation":
        if (operation.annotation_id !== operation.annotation.id) throw operationError("annotation_id_mismatch");
        next.annotations[findIndex(next.annotations, operation.annotation_id)] = cloneAnnotation(operation.annotation);
        break;
      case "delete_annotation": {
        const index = findIndex(next.annotations, operation.annotation_id);
        const [removed] = next.annotations.splice(index, 1);
        if (removed.kind === "dialogue") {
          next.utterances = next.utterances.filter((item) => item.dialogue_annotation_id !== removed.id);
        } else if (removed.kind === "speaker") {
          next.utterances = next.utterances.map((item) => item.speaker_annotation_id === removed.id ? { ...item, speaker_annotation_id: null } : item);
          next.characters = next.characters.map((item) => ({ ...item, supporting_annotation_ids: item.supporting_annotation_ids.filter((id) => id !== removed.id) }));
        } else {
          next.utterances = next.utterances.map((item) => ({ ...item, emotion_evidence_annotation_ids: item.emotion_evidence_annotation_ids.filter((id) => id !== removed.id) }));
        }
        break;
      }
      case "set_annotation_status": {
        const index = findIndex(next.annotations, operation.annotation_id);
        const current = next.annotations[index];
        next.annotations[index] = { ...current, status: operation.status };
        if (current.kind === "dialogue" && operation.status === "rejected") {
          next.utterances = next.utterances.map((item) => item.dialogue_annotation_id === current.id ? { ...item, status: "rejected" } : item);
        } else if (current.kind === "dialogue" && operation.status === "pending") {
          next.utterances = next.utterances.map((item) => item.dialogue_annotation_id === current.id && item.status === "accepted" ? { ...item, status: "pending" } : item);
        }
        break;
      }
      case "upsert_character": {
        const index = next.characters.findIndex((item) => item.id === operation.character.id);
        if (index < 0) next.characters.push(cloneCharacter(operation.character));
        else next.characters[index] = cloneCharacter(operation.character);
        break;
      }
      case "set_character_status": {
        const index = findIndex(next.characters, operation.character_id);
        next.characters[index] = { ...next.characters[index], status: operation.status };
        if (operation.status === "rejected") {
          next.utterances = next.utterances.map((item) => item.character_candidate_id === operation.character_id ? { ...item, status: "rejected" } : item);
        } else if (operation.status === "pending") {
          next.utterances = next.utterances.map((item) => item.character_candidate_id === operation.character_id && item.status === "accepted" ? { ...item, status: "pending" } : item);
        }
        break;
      }
      case "merge_characters": {
        if (operation.source_character_ids.includes(operation.target_character_id)
          || new Set(operation.source_character_ids).size !== operation.source_character_ids.length) {
          throw operationError("character_merge_invalid");
        }
        const targetIndex = findIndex(next.characters, operation.target_character_id);
        const sources = operation.source_character_ids.map((id) => next.characters[findIndex(next.characters, id)]);
        const target = next.characters[targetIndex];
        next.characters[targetIndex] = {
          ...target,
          aliases: uniqueStrings([...target.aliases, ...sources.flatMap((source) => source.aliases)]),
          supporting_annotation_ids: uniqueStrings([
            ...target.supporting_annotation_ids,
            ...sources.flatMap((source) => source.supporting_annotation_ids)
          ])
        };
        const sourceIds = new Set(operation.source_character_ids);
        next.characters = next.characters.filter((item) => !sourceIds.has(item.id));
        next.utterances = next.utterances.map((item) => item.character_candidate_id && sourceIds.has(item.character_candidate_id)
          ? { ...item, character_candidate_id: operation.target_character_id }
          : item);
        break;
      }
      case "split_alias": {
        const sourceIndex = findIndex(next.characters, operation.character_id);
        const source = next.characters[sourceIndex];
        if (!source.aliases.includes(operation.alias)) throw operationError("character_alias_missing");
        ensureMissing(next.characters, operation.character.id);
        next.characters[sourceIndex] = { ...source, aliases: source.aliases.filter((alias) => alias !== operation.alias) };
        next.characters.push(cloneCharacter(operation.character));
        break;
      }
      case "create_utterance":
        ensureMissing(next.utterances, operation.utterance.id);
        next.utterances.push(cloneUtterance(operation.utterance));
        break;
      case "update_utterance":
        if (operation.utterance_id !== operation.utterance.id) throw operationError("utterance_id_mismatch");
        next.utterances[findIndex(next.utterances, operation.utterance_id)] = cloneUtterance(operation.utterance);
        break;
      case "delete_utterance":
        findIndex(next.utterances, operation.utterance_id);
        next.utterances = next.utterances.filter((item) => item.id !== operation.utterance_id);
        break;
      case "set_utterance_status": {
        const index = findIndex(next.utterances, operation.utterance_id);
        next.utterances[index] = { ...next.utterances[index], status: operation.status };
        break;
      }
      case "dismiss_warning":
        findIndex(next.warnings, operation.warning_id);
        next.warnings = next.warnings.filter((item) => item.id !== operation.warning_id);
        break;
    }
  }
  return next;
}

export function applyQueuedDraftOperations(
  serverDraft: SemanticAnalysisDraft,
  batches: readonly { operations: DraftOperation[] }[]
): SemanticAnalysisDraft {
  return batches.reduce((visible, batch) => applyDraftOperations(visible, batch.operations), serverDraft);
}
