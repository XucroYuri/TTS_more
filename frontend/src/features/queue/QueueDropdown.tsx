import { ListChecks, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { generationStatusKey, generationStatusTone, isTerminalGenerationStatus } from "../../lib/generationStatus";
import type { GenerationJob } from "../../types";

import "./queue-dropdown.css";

export interface QueueDropdownProps {
  jobs: GenerationJob[];
  currentProjectId?: string | null;
  lineLabels: Record<string, string>;
}

export function QueueDropdown({ jobs, currentProjectId, lineLabels }: QueueDropdownProps) {
  const { t } = useTranslation();
  const entries = jobs.slice().reverse().flatMap((job) => (
    job.items.map((item) => ({ job, item }))
  ));
  const activeCount = entries.filter(({ item }) => !isTerminalGenerationStatus(item.status)).length;

  return (
    <details className="line-queue-menu">
      <summary title={t("queue.readOnlyHint")}>
        {activeCount > 0 ? <Loader2 className="spin" size={14} /> : <ListChecks size={14} />}
        <span>{t("queue.title")}</span>
        <b>{activeCount || entries.length}</b>
      </summary>
      <div className="line-queue-popover" aria-label={t("queue.readOnlyHint")}>
        <div className="line-queue-heading">
          <strong>{t("queue.title")}</strong>
          <span>{t("queue.readOnlyHint")}</span>
        </div>
        {entries.length === 0 ? (
          <div className="line-queue-empty">{t("queue.noJobs")}</div>
        ) : (
          <div className="line-queue-list">
            {entries.map(({ job, item }) => {
              const lineKey = item.line_uid ?? item.line_id;
              const currentProjectLabel = job.project_id === currentProjectId ? lineLabels[lineKey] : undefined;
              const label = currentProjectLabel ?? `${job.project_id} · ${item.line_id}`;
              const progress = Math.round(Math.max(0, Math.min(1, item.progress)) * 100);
              return (
                <article className="line-queue-item" key={`${job.job_id}:${item.task_id}`}>
                  <div className="line-queue-item-head">
                    <strong title={label}>{label}</strong>
                    <span className={`status-pill tone-${generationStatusTone(item.status)}`}>{t(generationStatusKey(item.status))}</span>
                    <b>{progress}%</b>
                  </div>
                  <div className="line-queue-progress" aria-label={t("queue.progressLabel", { percent: progress })}>
                    <span style={{ width: `${progress}%` }} />
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </details>
  );
}
