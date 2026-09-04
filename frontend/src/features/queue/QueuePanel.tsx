import type { GenerationJob } from "../../types";
import { generationStatusKey, generationStatusTone, type GenerationStatusTone } from "../../lib/generationStatus";
import { useTranslation } from "react-i18next";

import "./queue-panel.css";

export interface QueuePanelProps {
  jobs: GenerationJob[];
  activeJob: GenerationJob | null;
  queued: number;
  running: number;
  completed: number;
  failed: number;
  cancelled: number;
  total: number;
  processed: number;
  progressPercent: number;
  statusLabel: string;
  statusTone: GenerationStatusTone;
  externalStatusLabel?: (status: string) => string;
  onCancel?: () => void;
  onRetry?: () => void;
}

export function QueuePanel(props: QueuePanelProps) {
  const { t } = useTranslation();
  const hasWork = props.total > 0 || props.jobs.length > 0;
  return (
    <section className={`queue-status-card ${hasWork ? "has-work" : "is-empty"}`} aria-label={t("queue.title")}>
      <div className="queue-status-head">
        <div>
          <strong>{t("queue.title")}</strong>
          <span>{hasWork ? t("queue.processedRatio", { processed: props.processed, total: props.total }) : t("queue.noJobs")}</span>
        </div>
        <span className={`status-pill tone-${props.statusTone}`}>{props.statusLabel}</span>
      </div>
      {hasWork && (
        <>
          <div className="queue-progress-row">
            <strong>{props.progressPercent}%</strong>
            <div className="queue-dispatch-bar" aria-label={t("queue.progressLabel", { percent: props.progressPercent })}>
              <span style={{ width: `${props.progressPercent}%` }} />
            </div>
          </div>
          <div className="queue-count-strip" aria-label={t("queue.countSummary")}>
            <span><strong>{props.queued}</strong>{t("filters.queued")}</span>
            <span><strong>{props.running}</strong>{t("filters.running")}</span>
            <span><strong>{props.completed}</strong>{t("status.completed")}</span>
            <span><strong>{props.failed}</strong>{t("status.failed")}</span>
            <span><strong>{props.cancelled}</strong>{t("status.cancelled")}</span>
          </div>
        </>
      )}
      {props.jobs.length > 0 && (
        <details className="queue-job-details" open={Boolean(props.activeJob)}>
          <summary><span>{t("queue.recentJobs")}</span><small>{t("queue.itemCount", { count: props.jobs.length })}</small></summary>
          <div className="queue-job-list">
            {props.jobs.slice(0, 5).map((job) => {
              const promptItem = job.items.find((item) => item.external_status || item.external_job_id);
              return (
                <article className={`queue-job-card state-${generationStatusTone(job.status)}`} key={job.job_id}>
                  <div><strong>{job.job_id}</strong><span>{t("queue.itemCount", { count: job.items.length })}</span></div>
                  <div className="queue-job-meta">
                    <span className={`status-pill tone-${generationStatusTone(job.status)}`}>{t(generationStatusKey(job.status))}</span>
                    {promptItem?.external_status && (
                      <span className="line-meta-chip neutral" title={promptItem.external_job_id ?? ""}>
                        {t("queue.promptStatus", { status: props.externalStatusLabel?.(promptItem.external_status) ?? promptItem.external_status })}
                      </span>
                    )}
                    <span>{Math.round(Math.max(0, Math.min(1, job.progress)) * 100)}%</span>
                  </div>
                </article>
              );
            })}
          </div>
        </details>
      )}
      {(props.onCancel || props.onRetry) && (
        <div className="queue-panel-actions">
          {props.onCancel && <button type="button" className="secondary-button compact-button" onClick={props.onCancel}>{t("actions.cancel")}</button>}
          {props.onRetry && <button type="button" className="primary-button compact-button" onClick={props.onRetry}>{t("actions.retry")}</button>}
        </div>
      )}
    </section>
  );
}
