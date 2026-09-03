import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { AnalysisError, AnalysisWarning, UnresolvedCandidate } from "../../types";

type NotificationPanel = "warnings" | "unresolved";

const warningTitleKeys: Record<string, string> = {
  emotion_evidence_mismatch: "analysis.diagnostics.emotionEvidenceMismatch",
  missing_quoted_dialogue: "analysis.diagnostics.missingQuotedDialogue",
  missing_quoted_dialogue_coverage: "analysis.diagnostics.missingQuotedDialogue"
};

const unresolvedTitleKeys: Record<string, string> = {
  speaker_unknown: "analysis.uncertainty.speakerUnknown",
  speaker_ambiguous: "analysis.uncertainty.speakerAmbiguous",
  dialogue_ambiguous: "analysis.uncertainty.dialogueAmbiguous",
  emotion_inferred: "analysis.uncertainty.emotionInferred",
  emotion_ambiguous: "analysis.uncertainty.emotionAmbiguous",
  source_anchor_ambiguous: "analysis.uncertainty.sourceAnchorAmbiguous"
};

export interface AnalysisNotificationsProps {
  warnings: AnalysisWarning[];
  warningSource: "run" | "draft";
  unresolved: UnresolvedCandidate[];
  disabled?: boolean;
  onDismissWarning: (warningId: string) => void;
  onLocateUnresolved: (candidate: UnresolvedCandidate) => void;
}

export function AnalysisNotifications({
  warnings,
  warningSource,
  unresolved,
  disabled = false,
  onDismissWarning,
  onLocateUnresolved
}: AnalysisNotificationsProps) {
  const { t } = useTranslation();
  const [openPanel, setOpenPanel] = useState<NotificationPanel | null>(null);
  const [openDiagnosticId, setOpenDiagnosticId] = useState<string | null>(null);
  if (warnings.length === 0 && unresolved.length === 0) return null;

  const togglePanel = (panel: NotificationPanel) => {
    setOpenDiagnosticId(null);
    setOpenPanel((current) => (current === panel ? null : panel));
  };

  return (
    <div className="analysis-notifications">
      <div className="analysis-notifications__buttons">
        {warnings.length > 0 ? (
          <button
            type="button"
            data-notification="warnings"
            aria-expanded={openPanel === "warnings"}
            onClick={() => togglePanel("warnings")}
          >
            {t("analysis.review.draftWarningsTitle")} <span>{warnings.length}</span>
          </button>
        ) : null}
        {unresolved.length > 0 ? (
          <button
            type="button"
            data-notification="unresolved"
            aria-expanded={openPanel === "unresolved"}
            onClick={() => togglePanel("unresolved")}
          >
            {t("analysis.review.unresolvedTitle")} <span>{unresolved.length}</span>
          </button>
        ) : null}
      </div>

      {openPanel === "warnings" ? (
        <section
          className="analysis-notifications__popover"
          aria-label={t("analysis.review.draftWarningsTitle")}
        >
          {warnings.map((warning) => (
            <article
              className="analysis-notification-item"
              data-warning-source={warningSource}
              key={warning.id}
            >
              <strong>
                {t(warningTitleKeys[warning.code] ?? "analysis.diagnostics.reviewRequired")}
              </strong>
              <div className="analysis-notification-item__actions">
                <button
                  type="button"
                  data-diagnostic-action="toggle"
                  aria-expanded={openDiagnosticId === warning.id}
                  onClick={() =>
                    setOpenDiagnosticId((current) => (current === warning.id ? null : warning.id))
                  }
                >
                  {t("analysis.actions.diagnostics")}
                </button>
                {warningSource === "draft" ? (
                  <button
                    type="button"
                    data-warning-action="dismiss"
                    disabled={disabled}
                    onClick={() => onDismissWarning(warning.id)}
                  >
                    {t("analysis.review.dismissWarning")}
                  </button>
                ) : null}
              </div>
              {openDiagnosticId === warning.id ? (
                <div className="analysis-notification-item__diagnostics">
                  <code>{warning.code}</code>
                  <p>{warning.message}</p>
                  <pre>{JSON.stringify(warning.details, null, 2)}</pre>
                </div>
              ) : null}
            </article>
          ))}
        </section>
      ) : null}

      {openPanel === "unresolved" ? (
        <section
          className="analysis-notifications__popover"
          data-unresolved-candidates
          aria-label={t("analysis.review.unresolvedTitle")}
        >
          {unresolved.map((candidate) => (
            <article className="analysis-notification-item" key={candidate.id}>
              <button
                type="button"
                className="analysis-notification-item__locate"
                data-unresolved-action="locate"
                onClick={() => onLocateUnresolved(candidate)}
              >
                {t(unresolvedTitleKeys[candidate.code] ?? "analysis.diagnostics.reviewRequired")}
              </button>
              <button
                type="button"
                data-diagnostic-action="toggle"
                aria-expanded={openDiagnosticId === candidate.id}
                onClick={() =>
                  setOpenDiagnosticId((current) =>
                    current === candidate.id ? null : candidate.id
                  )
                }
              >
                {t("analysis.actions.diagnostics")}
              </button>
              {openDiagnosticId === candidate.id ? (
                <div className="analysis-notification-item__diagnostics">
                  <code>{candidate.code}</code>
                  <p>{candidate.message}</p>
                  <pre>{JSON.stringify(candidate.details, null, 2)}</pre>
                </div>
              ) : null}
            </article>
          ))}
        </section>
      ) : null}
    </div>
  );
}

export interface AnalysisBlockingDialogProps {
  kind: "run" | "conflict" | "controller";
  title: string;
  description: string;
  error?: AnalysisError | null;
  retryLabel?: string;
  onRetry?: () => void;
  onDismiss?: () => void;
  onCopyDiagnostics?: () => void;
  copyComplete?: boolean;
}

export function AnalysisBlockingDialog({
  kind,
  title,
  description,
  error,
  retryLabel,
  onRetry,
  onDismiss,
  onCopyDiagnostics,
  copyComplete = false
}: AnalysisBlockingDialogProps) {
  const { t } = useTranslation();
  const [detailsOpen, setDetailsOpen] = useState(false);

  return (
    <div className="analysis-error-backdrop">
      <section
        className="analysis-error-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={"analysis-error-title-" + kind}
        data-analysis-error={kind}
      >
        <h2 id={"analysis-error-title-" + kind}>{title}</h2>
        <p>{description}</p>

        {error && detailsOpen ? (
          <dl className="analysis-error-dialog__diagnostics">
            <div><dt>{t("analysis.errors.code")}</dt><dd>{error.code}</dd></div>
            <div><dt>{t("analysis.errors.httpStatus")}</dt><dd>{error.http_status}</dd></div>
            <div><dt>{t("analysis.errors.stage")}</dt><dd>{error.stage}</dd></div>
            <div><dt>{t("analysis.errors.message")}</dt><dd>{error.message}</dd></div>
            <div>
              <dt>{t("analysis.errors.traceId")}</dt>
              <dd>{error.trace_id ?? t("analysis.common.none")}</dd>
            </div>
          </dl>
        ) : null}

        <div className="analysis-error-dialog__actions">
          {error ? (
            <button
              type="button"
              data-error-action="details"
              aria-expanded={detailsOpen}
              onClick={() => setDetailsOpen((current) => !current)}
            >
              {t("analysis.actions.diagnostics")}
            </button>
          ) : null}
          {error && onCopyDiagnostics && detailsOpen ? (
            <button type="button" data-error-action="copy" onClick={onCopyDiagnostics}>
              {copyComplete ? t("analysis.errors.copied") : t("analysis.errors.copyDiagnostics")}
            </button>
          ) : null}
          {onDismiss ? (
            <button type="button" data-error-action="dismiss" onClick={onDismiss}>
              {t("analysis.errors.dismiss")}
            </button>
          ) : null}
          {onRetry ? (
            <button type="button" data-error-action="retry" onClick={onRetry}>
              {retryLabel ?? t("analysis.actions.retry")}
            </button>
          ) : null}
        </div>
      </section>
    </div>
  );
}

export function AnalysisLoadingState({ progress }: { progress: number }) {
  const { t } = useTranslation();
  const percentage = Math.max(0, Math.min(100, Math.round(progress * 100)));
  return (
    <section className="analysis-loading-state" aria-label={t("analysis.results.waiting")}>
      <div className="analysis-loading-state__heading">
        <span>{t("analysis.runStatus.running")}</span>
        <strong>{percentage}%</strong>
      </div>
      <div
        className="analysis-loading-state__progress"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percentage}
      >
        <span style={{ width: percentage + "%" }} />
      </div>
      <div className="analysis-results-list" aria-hidden="true">
        {Array.from({ length: 6 }, (_, index) => (
          <div className="analysis-result-skeleton" key={index}>
            <span />
            <span />
            <span />
          </div>
        ))}
      </div>
    </section>
  );
}
