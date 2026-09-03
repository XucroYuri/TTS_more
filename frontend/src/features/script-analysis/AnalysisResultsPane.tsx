import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { DraftOperation, SemanticAnalysisDraft, SemanticUtterance } from "../../types";

export type AnalysisResultFilter = "all" | "pending" | "low";

export interface AnalysisResultsPaneProps {
  draft: SemanticAnalysisDraft;
  filter: AnalysisResultFilter;
  onFilterChange: (filter: AnalysisResultFilter) => void;
  onOperations: (operations: DraftOperation[]) => void;
  onSelectUtterance: (utterance: SemanticUtterance) => void;
  disabled?: boolean;
}

function sourceOrder(draft: SemanticAnalysisDraft, utterance: SemanticUtterance) {
  const annotation = draft.annotations.find(
    (candidate) => candidate.id === utterance.dialogue_annotation_id
  );
  return annotation
    ? ([annotation.span.start_utf16, annotation.span.end_utf16, utterance.id] as const)
    : ([Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER, utterance.id] as const);
}

export function orderedAnalysisUtterances(
  draft: SemanticAnalysisDraft,
  filter: AnalysisResultFilter
): SemanticUtterance[] {
  return [...draft.utterances]
    .filter((utterance) => {
      if (filter === "pending") return utterance.status === "pending";
      if (filter === "low") return utterance.confidence < 0.8;
      return true;
    })
    .sort((left, right) => {
      const leftOrder = sourceOrder(draft, left);
      const rightOrder = sourceOrder(draft, right);
      return (
        leftOrder[0] - rightOrder[0] ||
        leftOrder[1] - rightOrder[1] ||
        leftOrder[2].localeCompare(rightOrder[2])
      );
    });
}

const resolvedAcceptanceUncertainty = new Set([
  "speaker_unknown",
  "speaker_ambiguous",
  "dialogue_ambiguous"
]);

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

export function utteranceStatusOperations(
  draft: SemanticAnalysisDraft,
  utterance: SemanticUtterance,
  status: SemanticUtterance["status"]
): DraftOperation[] {
  const dialogue = draft.annotations.find(
    (annotation) => annotation.id === utterance.dialogue_annotation_id
  );
  if (!dialogue) return [];

  if (status === "accepted") {
    const character = draft.characters.find(
      (candidate) => candidate.id === utterance.character_candidate_id
    );
    if (!character || character.status === "rejected") return [];
    const operations: DraftOperation[] = [];
    if (dialogue.status !== "accepted") {
      operations.push({
        op: "set_annotation_status",
        annotation_id: dialogue.id,
        status: "accepted"
      });
    }
    if (character.status === "pending") {
      operations.push({
        op: "set_character_status",
        character_id: character.id,
        status: "accepted"
      });
    }
    const uncertaintyCodes = utterance.uncertainty_codes.filter(
      (code) => !resolvedAcceptanceUncertainty.has(code)
    );
    if (uncertaintyCodes.length !== utterance.uncertainty_codes.length) {
      operations.push({
        op: "update_utterance",
        utterance_id: utterance.id,
        utterance: { ...utterance, uncertainty_codes: uncertaintyCodes }
      });
    }
    operations.push({
      op: "set_utterance_status",
      utterance_id: utterance.id,
      status: "accepted"
    });
    return operations;
  }

  return [
    {
      op: "set_annotation_status",
      annotation_id: dialogue.id,
      status
    },
    {
      op: "set_utterance_status",
      utterance_id: utterance.id,
      status
    }
  ];
}

function reviewAction(status: SemanticUtterance["status"]) {
  if (status === "accepted") return "accept";
  if (status === "rejected") return "reject";
  return "pending";
}

export function AnalysisResultsPane({
  draft,
  filter,
  onFilterChange,
  onOperations,
  onSelectUtterance,
  disabled = false
}: AnalysisResultsPaneProps) {
  const { t } = useTranslation();
  const [openDetailsId, setOpenDetailsId] = useState<string | null>(null);
  const utterances = useMemo(() => orderedAnalysisUtterances(draft, filter), [draft, filter]);
  const acceptedCharacters = useMemo(
    () => draft.characters.filter((character) => character.status === "accepted"),
    [draft.characters]
  );
  const filterCounts = {
    all: draft.utterances.length,
    pending: draft.utterances.filter((utterance) => utterance.status === "pending").length,
    low: draft.utterances.filter((utterance) => utterance.confidence < 0.8).length
  };

  return (
    <section className="analysis-results-pane" aria-labelledby="analysis-results-title">
      <div className="analysis-section-heading">
        <h2 id="analysis-results-title">{t("analysis.results.title")}</h2>
        <div className="analysis-filter-bar" aria-label={t("analysis.filters.label")}>
          {(["all", "pending", "low"] as const).map((value) => (
            <button
              key={value}
              type="button"
              data-filter={value}
              aria-pressed={filter === value}
              onClick={() => onFilterChange(value)}
            >
              {t(`analysis.filters.${value === "low" ? "lowConfidence" : value}`)}{" "}
              <span>{filterCounts[value]}</span>
            </button>
          ))}
        </div>
      </div>

      {utterances.length === 0 ? (
        <p className="analysis-empty-state">{t("analysis.results.empty")}</p>
      ) : (
        <div className="analysis-results-list">
          {utterances.map((utterance) => {
            const dialogue = draft.annotations.find(
              (item) => item.id === utterance.dialogue_annotation_id
            );
            const character = draft.characters.find(
              (item) => item.id === utterance.character_candidate_id
            );
            const emotionEvidence = utterance.emotion_evidence_annotation_ids
              .map((id) => draft.annotations.find((item) => item.id === id)?.span.text)
              .filter((value): value is string => Boolean(value));
            const canAccept = Boolean(dialogue && character && character.status !== "rejected");
            const emotion = utterance.normalized_emotion
              ? t(`analysis.emotion.${utterance.normalized_emotion}`)
              : t("analysis.common.none");
            const detailsOpen = openDetailsId === utterance.id;

            return (
              <article
                key={utterance.id}
                className="analysis-result-card"
                data-utterance-id={utterance.id}
                tabIndex={-1}
                onClick={() => onSelectUtterance(utterance)}
              >
                <header className="analysis-result-card__header">
                  <label className="analysis-result-card__speaker">
                    <span className="analysis-result-card__field-label">
                      {t("analysis.fields.speaker")}
                    </span>
                    <select
                      data-utterance-character={utterance.id}
                      value={
                        acceptedCharacters.some(
                          (candidate) => candidate.id === utterance.character_candidate_id
                        )
                          ? utterance.character_candidate_id ?? ""
                          : ""
                      }
                      disabled={disabled}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => {
                        event.stopPropagation();
                        onOperations([
                          {
                            op: "update_utterance",
                            utterance_id: utterance.id,
                            utterance: reassignUtterance(utterance, event.target.value || null)
                          }
                        ]);
                      }}
                    >
                      <option value="">{t("analysis.results.unassigned")}</option>
                      {acceptedCharacters.map((candidate) => (
                        <option key={candidate.id} value={candidate.id}>
                          {candidate.canonical_name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div
                    className="analysis-review-actions"
                    aria-label={t("analysis.results.reviewActions")}
                  >
                    {(["accepted", "pending", "rejected"] as const).map((status) => {
                      const action = reviewAction(status);
                      return (
                        <button
                          key={status}
                          type="button"
                          data-utterance-action={action}
                          data-utterance-id={utterance.id}
                          disabled={disabled || (status === "accepted" && !canAccept)}
                          aria-pressed={utterance.status === status}
                          onClick={(event) => {
                            event.stopPropagation();
                            const operations = utteranceStatusOperations(draft, utterance, status);
                            if (operations.length > 0) onOperations(operations);
                          }}
                        >
                          {t(
                            `analysis.actions.${
                              status === "accepted"
                                ? "accept"
                                : status === "rejected"
                                  ? "reject"
                                  : "restore"
                            }`
                          )}
                        </button>
                      );
                    })}
                  </div>
                </header>

                <blockquote className="analysis-result-card__dialogue">
                  {dialogue?.span.text ?? t("analysis.results.missingDialogue")}
                </blockquote>

                <div className="analysis-result-card__summary">
                  <span>
                    <span className="analysis-result-card__field-label">
                      {t("analysis.fields.emotion")}
                    </span>
                    <strong>{emotion}</strong>
                    {utterance.custom_emotion ? ` · ${utterance.custom_emotion}` : ""}
                    {utterance.emotion_origin === "inferred" ? (
                      <span className="analysis-badge analysis-badge--inferred">
                        {t("analysis.badges.inferred")}
                      </span>
                    ) : null}
                  </span>
                  <span>
                    <span className="analysis-result-card__field-label">
                      {t("analysis.fields.confidence")}
                    </span>
                    <strong>{Math.round(utterance.confidence * 100)}%</strong>
                  </span>
                  <button
                    type="button"
                    data-utterance-action="details"
                    aria-expanded={detailsOpen}
                    onClick={(event) => {
                      event.stopPropagation();
                      setOpenDetailsId(detailsOpen ? null : utterance.id);
                    }}
                  >
                    {t("analysis.actions.details")}
                  </button>
                </div>

                {detailsOpen ? (
                  <aside
                    className="analysis-result-card__popover"
                    aria-label={t("analysis.results.detailsTitle")}
                    onClick={(event) => event.stopPropagation()}
                  >
                    <dl>
                      <div>
                        <dt>{t("analysis.fields.emotionEvidence")}</dt>
                        <dd>{emotionEvidence.join("、") || t("analysis.common.none")}</dd>
                      </div>
                      <div>
                        <dt>{t("analysis.fields.emotionIntensity")}</dt>
                        <dd>
                          {utterance.emotion_intensity === null
                            ? t("analysis.common.none")
                            : utterance.emotion_intensity}
                        </dd>
                      </div>
                      <div>
                        <dt>{t("analysis.fields.uncertainty")}</dt>
                        <dd>
                          {utterance.uncertainty_codes.join(", ") || t("analysis.common.none")}
                        </dd>
                      </div>
                    </dl>
                  </aside>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
