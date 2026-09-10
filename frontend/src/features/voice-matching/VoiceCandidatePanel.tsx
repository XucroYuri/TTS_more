import { Loader2, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { VoiceCandidate, VoiceRecommendation, VoiceSelectionSnapshot } from "../../types";
import { voiceBlockerAction } from "./voiceReadiness";
import "./voice-matching.css";

interface VoiceCandidatePanelProps {
  recommendation: VoiceRecommendation | null;
  selection?: VoiceSelectionSnapshot | null;
  loading: boolean;
  error?: string | null;
  selectingCandidateId: string | null;
  referenceAudioUrl: (assetId: string) => string;
  onSelect: (candidateId: string) => void;
  onConfirmIdentity: (candidateId: string) => void;
  onClear: () => void;
  onSyncAssets?: () => void;
  onOpenServices?: () => void;
}

function candidateDetail(candidate: VoiceCandidate) {
  return candidate.score_breakdown;
}

export function VoiceCandidatePanel({
  recommendation,
  selection,
  loading,
  error,
  selectingCandidateId,
  referenceAudioUrl,
  onSelect,
  onConfirmIdentity,
  onClear,
  onSyncAssets,
  onOpenServices
}: VoiceCandidatePanelProps) {
  const { t } = useTranslation();
  const [rejectedCandidateIds, setRejectedCandidateIds] = useState<Set<string>>(new Set());
  useEffect(() => {
    setRejectedCandidateIds(new Set());
  }, [recommendation?.line_id, recommendation?.catalog_version]);
  const candidates = (recommendation?.candidates ?? [])
    .filter((candidate) => !rejectedCandidateIds.has(candidate.candidate_id))
    .slice(0, 3);
  const blocker = recommendation?.blockers[0] ?? null;
  const blockerAction = blocker ? voiceBlockerAction(blocker) : null;
  const blockerActionCallback = blockerAction === "sync-assets"
    ? onSyncAssets
    : blockerAction === "open-services"
      ? onOpenServices
      : undefined;

  return (
    <section className="voice-candidate-panel" aria-label={t("voiceMatching.candidateTitle")}>
      <div className="voice-panel-heading">
        <div>
          <span className="voice-panel-eyebrow"><Sparkles size={14} />{t("voiceMatching.candidateEyebrow")}</span>
          <h3>{t("voiceMatching.candidateTitle")}</h3>
        </div>
        {selection && (
          <button className="secondary-button compact-button" type="button" onClick={onClear} disabled={Boolean(selectingCandidateId)}>
            <Trash2 size={13} />{t("voiceMatching.clearSelection")}
          </button>
        )}
      </div>

      {loading && <div className="voice-panel-empty"><Loader2 className="spin" size={15} />{t("voiceMatching.loadingCandidates")}</div>}
      {!loading && error && <p className="voice-panel-error" role="alert">{error}</p>}
      {!loading && !error && blocker && (
        <div className="voice-candidate-blocker" role="status">
          <span>{t(`voiceMatching.blocker.${blocker}`, { defaultValue: t("voiceMatching.blocked", { reason: blocker }) })}</span>
          {blockerAction && blockerActionCallback && (
            <button className="secondary-button compact-button" type="button" onClick={blockerActionCallback}>
              {t(`voiceMatching.blockerAction.${blockerAction}`)}
            </button>
          )}
        </div>
      )}
      {!loading && !error && !blocker && candidates.length === 0 && <p className="voice-panel-empty">{t("voiceMatching.noCandidates")}</p>}

      {!loading && candidates.length > 0 && (
        <div className="voice-candidate-list">
          {candidates.map((candidate, index) => {
            const detail = candidateDetail(candidate);
            const selected = selection?.candidate_id === candidate.candidate_id;
            const selecting = selectingCandidateId === candidate.candidate_id;
            return (
              <article className={`voice-candidate-card ${selected ? "selected" : ""} ${candidate.requires_identity_confirmation ? "needs-confirmation" : ""}`} key={candidate.candidate_id}>
                <div className="voice-candidate-topline">
                  <div>
                    <span className="voice-candidate-rank">#{index + 1}</span>
                    <strong>{candidate.resource_id}</strong>
                  </div>
                  <strong className="voice-candidate-score">{candidate.score.toFixed(1)}</strong>
                </div>
                <div className="voice-score-grid">
                  <span>{t("voiceMatching.score.character")} {detail.character}</span>
                  <span>{t("voiceMatching.score.emotion")} {detail.emotion}</span>
                  <span>{t("voiceMatching.score.duration")} {detail.duration}</span>
                  <span>{t("voiceMatching.score.language")} {detail.language}</span>
                </div>
                <div className="voice-candidate-meta">
                  <span>{candidate.engine_type}</span>
                  <span>{candidate.speed_factor.toFixed(2)}×</span>
                  {candidate.target_duration_seconds != null && <span>{t("voiceMatching.seconds", { value: candidate.target_duration_seconds.toFixed(1) })}</span>}
                  {candidate.auto_fill_eligible && <span className="voice-auto-fill">{t("voiceMatching.autoFillEligible")}</span>}
                </div>
                {candidate.training_task && (
                  <div className="voice-dynamic-weight-summary">
                    <span>{t("voiceMatching.trainingTask")}</span>
                    <strong>{candidate.training_task}</strong>
                    {candidate.gpt_weight_artifact_id && candidate.sovits_weight_artifact_id && (
                      <span className="voice-dynamic-pair-ready">{t("voiceMatching.dynamicPairReady")}</span>
                    )}
                  </div>
                )}
                <audio className="voice-reference-audio" controls preload="none" src={referenceAudioUrl(candidate.reference_asset_id)} />
                {candidate.requires_identity_confirmation ? (
                  <div className="voice-identity-confirmation" role="group" aria-label={t("voiceMatching.fuzzyConfirmationQuestion")}>
                    <strong>{t("voiceMatching.fuzzyConfirmationQuestion")}</strong>
                    <div>
                      <button
                        className="primary-button compact-button"
                        type="button"
                        disabled={Boolean(selectingCandidateId) || candidate.blockers.length > 0}
                        onClick={() => onConfirmIdentity(candidate.candidate_id)}
                      >
                        {selecting && <Loader2 className="spin" size={13} />}
                        {t("voiceMatching.confirmIdentityAccurate")}
                      </button>
                      <button
                        className="secondary-button compact-button"
                        type="button"
                        disabled={Boolean(selectingCandidateId)}
                        onClick={() => setRejectedCandidateIds((current) => new Set([...current, candidate.candidate_id]))}
                      >
                        {t("voiceMatching.confirmIdentityInaccurate")}
                      </button>
                    </div>
                  </div>
                ) : !selected ? (
                  <button
                    className="primary-button compact-button"
                    type="button"
                    disabled={Boolean(selectingCandidateId) || candidate.blockers.length > 0}
                    onClick={() => onSelect(candidate.candidate_id)}
                  >
                    {selecting && <Loader2 className="spin" size={13} />}
                    {t("voiceMatching.selectCandidate")}
                  </button>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
