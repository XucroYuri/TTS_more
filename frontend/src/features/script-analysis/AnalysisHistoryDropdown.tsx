import { ChevronDown, History, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { deleteAnalysisRun, fetchAnalysisHistory } from "../../api";
import type { AnalysisHistoryItem, AnalysisRunStatus } from "../../types";
import { defaultAnalysisStorage, removeDeletedAnalysisSession } from "./analysisSessionStorage";

interface AnalysisHistoryDropdownProps {
  onOpen?: () => void;
  onDeleteRequest: (item: AnalysisHistoryItem) => Promise<boolean>;
  onDeleted?: (item: AnalysisHistoryItem) => void;
}

const activeStatuses = new Set<AnalysisRunStatus>(["queued", "running"]);

function statusTone(status: AnalysisRunStatus): string {
  if (status === "completed") return "success";
  if (status === "failed") return "danger";
  if (status === "running") return "running";
  if (status === "queued") return "queued";
  return "neutral";
}

function formatAnalysisTime(value: string, language: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language.startsWith("en") ? "en-US" : "zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(date);
}

export function AnalysisHistoryDropdown({ onOpen, onDeleteRequest, onDeleted }: AnalysisHistoryDropdownProps) {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AnalysisHistoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  const loadHistory = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setError("");
    try {
      const response = await fetchAnalysisHistory();
      setItems(response.runs);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("analysisHistory.loadFailed"));
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, [open]);

  useEffect(() => {
    if (!open || !items.some((item) => activeStatuses.has(item.status))) return;
    const timer = window.setInterval(() => void loadHistory(false), 2_000);
    return () => window.clearInterval(timer);
  }, [items, loadHistory, open]);

  async function toggleOpen() {
    const nextOpen = !open;
    setOpen(nextOpen);
    if (!nextOpen) return;
    onOpen?.();
    await loadHistory();
  }

  async function removeItem(item: AnalysisHistoryItem) {
    if (!await onDeleteRequest(item)) return;
    setDeletingRunId(item.run_id);
    setError("");
    try {
      const deleted = await deleteAnalysisRun(item.run_id);
      removeDeletedAnalysisSession(deleted.deleted_run_id, deleted.deleted_draft_id, defaultAnalysisStorage());
      setItems((current) => current.filter((candidate) => candidate.run_id !== item.run_id));
      onDeleted?.(item);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("analysisHistory.deleteFailed"));
    } finally {
      setDeletingRunId(null);
    }
  }

  return (
    <div className="topbar-menu-wrap analysis-history-wrap" ref={wrapperRef}>
      <button
        aria-expanded={open}
        className={`topbar-action-button menu-trigger ${open ? "active" : ""}`}
        data-action="analysis-history"
        onClick={() => void toggleOpen()}
        type="button"
      >
        <History size={15} />
        <span className="menu-trigger-label">{t("topbar.analysisHistory")}</span>
        <ChevronDown className={open ? "rotate-180" : ""} size={13} />
      </button>
      {open && (
        <section aria-label={t("analysisHistory.title")} className="analysis-history-popover">
          <header className="analysis-history-header">
            <div>
              <strong>{t("analysisHistory.title")}</strong>
              <span>{t("analysisHistory.count", { count: items.length })}</span>
            </div>
            <button
              aria-label={t("actions.refresh")}
              className="icon-button small"
              disabled={loading}
              onClick={() => void loadHistory()}
              title={t("actions.refresh")}
              type="button"
            >
              {loading ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
            </button>
          </header>
          <div className="analysis-history-columns" aria-hidden="true">
            <span>{t("analysisHistory.scriptName")}</span>
            <span>{t("analysisHistory.analysisTime")}</span>
            <span>{t("analysisHistory.status")}</span>
            <span>{t("analysisHistory.actions")}</span>
          </div>
          {error && <p className="analysis-history-error" role="alert">{error}</p>}
          {loading && items.length === 0 ? (
            <div className="analysis-history-empty"><Loader2 className="spin" size={18} /> {t("analysisHistory.loading")}</div>
          ) : items.length === 0 ? (
            <div className="analysis-history-empty">{t("analysisHistory.empty")}</div>
          ) : (
            <div className="analysis-history-list">
              {items.map((item) => {
                const deleting = deletingRunId === item.run_id;
                return (
                  <article className="analysis-history-row" key={item.run_id}>
                    <div className="analysis-history-script" title={item.project_title}>
                      <strong>{item.project_title}</strong>
                      <span>{item.source_revision_id}</span>
                    </div>
                    <time dateTime={item.created_at}>{formatAnalysisTime(item.created_at, i18n.language)}</time>
                    <span className={`analysis-history-status tone-${statusTone(item.status)}`}>
                      {t(`analysisHistory.statuses.${item.status}`)}
                      {activeStatuses.has(item.status) && <small>{Math.round(item.progress * 100)}%</small>}
                    </span>
                    <button
                      aria-label={t("analysisHistory.deleteItem", { title: item.project_title })}
                      className="icon-button small danger"
                      disabled={deleting}
                      onClick={() => void removeItem(item)}
                      title={t("analysisHistory.delete")}
                      type="button"
                    >
                      {deleting ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />}
                    </button>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
