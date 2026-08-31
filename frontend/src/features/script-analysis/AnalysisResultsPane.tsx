import { useMemo } from "react";
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
    ? [annotation.span.start_utf16, annotation.span.end_utf16, utterance.id] as const
    : [Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER, utterance.id] as const;
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

export function AnalysisResultsPane({
  draft,
  filter,
  onFilterChange,
  onOperations,
  onSelectUtterance,
  disabled = false
}: AnalysisResultsPaneProps) {
  const { t } = useTranslation();
  const utterances = useMemo(() => orderedAnalysisUtterances(draft, filter), [draft, filter]);

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
              {t(`analysis.filters.${value === "low" ? "lowConfidence" : value}`)}
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
            const speakerAnnotation = draft.annotations.find(
              (item) => item.id === utterance.speaker_annotation_id
            );
            const character = draft.characters.find(
              (item) => item.id === utterance.character_candidate_id
            );
            const emotionEvidence = utterance.emotion_evidence_annotation_ids
              .map((id) => draft.annotations.find((item) => item.id === id)?.span.text)
              .filter((value): value is string => Boolean(value));

            return (
              <article
                key={utterance.id}
                className="analysis-result-card"
                data-utterance-id={utterance.id}
                tabIndex={-1}
                onClick={() => onSelectUtterance(utterance)}
              >
                <header className="analysis-result-card__header">
                  <strong>{character?.canonical_name ?? speakerAnnotation?.span.text ?? t("analysis.results.unassigned")}</strong>
                  <span className={`analysis-status analysis-status--${utterance.status}`}>
                    {t(`analysis.status.${utterance.status}`)}
                  </span>
                </header>
                <blockquote className="analysis-result-card__dialogue">
                  {dialogue?.span.text ?? t("analysis.results.missingDialogue")}
                </blockquote>
                <dl className="analysis-result-card__details">
                  <div>
                    <dt>{t("analysis.fields.speaker")}</dt>
                    <dd>{speakerAnnotation?.span.text ?? character?.canonical_name ?? t("analysis.results.unassigned")}</dd>
                  </div>
                  <div>
                    <dt>{t("analysis.fields.emotionEvidence")}</dt>
                    <dd>{emotionEvidence.join("、") || t("analysis.common.none")}</dd>
                  </div>
                  <div>
                    <dt>{t("analysis.fields.emotion")}</dt>
                    <dd>
                      {utterance.normalized_emotion ?? t("analysis.common.none")}
                      {utterance.custom_emotion ? ` · ${utterance.custom_emotion}` : ""}
                      {utterance.emotion_intensity === null ? "" : ` · ${utterance.emotion_intensity}`}
                      {utterance.emotion_origin === "inferred" ? (
                        <span className="analysis-badge analysis-badge--inferred">
                          {t("analysis.badges.inferred")}
                        </span>
                      ) : null}
                    </dd>
                  </div>
                  <div>
                    <dt>{t("analysis.fields.confidence")}</dt>
                    <dd>{Math.round(utterance.confidence * 100)}%</dd>
                  </div>
                  <div>
                    <dt>{t("analysis.fields.uncertainty")}</dt>
                    <dd>{utterance.uncertainty_codes.join(", ") || t("analysis.common.none")}</dd>
                  </div>
                </dl>
                <div className="analysis-review-actions" aria-label={t("analysis.results.reviewActions")}>
                  {(["accepted", "pending", "rejected"] as const).map((status) => (
                    <button
                      key={status}
                      type="button"
                      data-utterance-action={status === "accepted" ? "accept" : status === "rejected" ? "reject" : "pending"}
                      data-utterance-id={utterance.id}
                      disabled={disabled}
                      aria-pressed={utterance.status === status}
                      onClick={(event) => {
                        event.stopPropagation();
                        onOperations([
                          { op: "set_utterance_status", utterance_id: utterance.id, status }
                        ]);
                      }}
                    >
                      {t(`analysis.actions.${status === "accepted" ? "accept" : status === "rejected" ? "reject" : "restore"}`)}
                    </button>
                  ))}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
