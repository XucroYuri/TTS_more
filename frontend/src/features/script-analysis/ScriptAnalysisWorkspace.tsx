import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  AnalysisError,
  DraftOperation,
  ScriptProject,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticAnnotation,
  SemanticUtterance
} from "../../types";
import { AnalysisResultsPane, type AnalysisResultFilter } from "./AnalysisResultsPane";
import { CharacterAliasEditor } from "./CharacterAliasEditor";
import { SourceAnnotationPane } from "./SourceAnnotationPane";
import {
  useAnalysisDraft,
  type UseAnalysisDraftOptions,
  type UseAnalysisDraftResult
} from "./useAnalysisDraft";
import "./script-analysis.css";

export interface ScriptAnalysisWorkspaceProps {
  projectId: string;
  sourceRevision: ScriptRevision;
  onConfirmed: (project: ScriptProject) => void;
  onCancel: () => void;
  controllerOptions?: UseAnalysisDraftOptions;
  createCharacterId?: () => string;
  createUtteranceId?: () => string;
  copyDiagnostics?: (diagnostics: string) => void | Promise<void>;
}

export interface ConfirmableUtteranceSummary {
  importable: number;
  excluded: number;
}

export function summarizeConfirmableUtterances(
  draft: SemanticAnalysisDraft
): ConfirmableUtteranceSummary {
  const annotations = new Map(draft.annotations.map((annotation) => [annotation.id, annotation]));
  const characters = new Map(draft.characters.map((character) => [character.id, character]));
  const importable = draft.utterances.filter((utterance) => {
    if (utterance.status !== "accepted") return false;
    const dialogue = annotations.get(utterance.dialogue_annotation_id);
    if (!dialogue || dialogue.kind !== "dialogue" || dialogue.status !== "accepted") return false;
    if (!utterance.character_candidate_id) return false;
    return characters.get(utterance.character_candidate_id)?.status === "accepted";
  }).length;
  return { importable, excluded: draft.utterances.length - importable };
}

function diagnosticsFor(error: AnalysisError): string {
  return JSON.stringify(
    {
      code: error.code,
      http_status: error.http_status,
      stage: error.stage,
      message: error.message,
      retryable: error.retryable,
      run_id: error.run_id,
      trace_id: error.trace_id,
      occurred_at: error.occurred_at,
      details: error.details
    },
    null,
    2
  );
}

function runStatusKey(controller: UseAnalysisDraftResult): string {
  if (controller.run) return controller.run.status;
  return controller.isRunning ? "running" : "failed";
}

let fallbackUtteranceId = 0;

function defaultCreateUtteranceId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return `utterance-${globalThis.crypto.randomUUID()}`;
  }
  fallbackUtteranceId += 1;
  return `utterance-human-${fallbackUtteranceId}`;
}

export function ScriptAnalysisWorkspace({
  projectId,
  sourceRevision,
  onConfirmed,
  onCancel,
  controllerOptions,
  createCharacterId,
  createUtteranceId = defaultCreateUtteranceId,
  copyDiagnostics
}: ScriptAnalysisWorkspaceProps) {
  const { t, i18n } = useTranslation();
  const controller = useAnalysisDraft(projectId, sourceRevision, controllerOptions);
  const [filter, setFilter] = useState<AnalysisResultFilter>("all");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submittingConfirmation, setSubmittingConfirmation] = useState(false);
  const [copyComplete, setCopyComplete] = useState(false);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const confirmationRef = useRef<Promise<unknown> | null>(null);
  const mountedRef = useRef(true);
  const draft = controller.draft;
  const summary = useMemo(
    () => (draft ? summarizeConfirmableUtterances(draft) : { importable: 0, excluded: 0 }),
    [draft]
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const queueOperations = (operations: DraftOperation[]) => {
    controller.queueOperations(operations);
  };

  const focusResultForAnnotation = (annotationId: string) => {
    if (!draft) return;
    const utterance = draft.utterances.find(
      (candidate) =>
        candidate.dialogue_annotation_id === annotationId ||
        candidate.speaker_annotation_id === annotationId ||
        candidate.emotion_evidence_annotation_ids.includes(annotationId)
    );
    if (!utterance) return;
    setFilter("all");
    const focus = () => {
      const card = workspaceRef.current?.querySelector<HTMLElement>(
        `[data-utterance-id="${utterance.id}"]`
      );
      card?.scrollIntoView?.({ block: "center" });
      card?.focus();
    };
    focus();
    if (!workspaceRef.current?.querySelector(`[data-utterance-id="${utterance.id}"]`)) {
      globalThis.setTimeout(focus, 0);
    }
  };

  const focusSourceForUtterance = (utterance: SemanticUtterance) => {
    if (!draft) return;
    const dialogue = draft.annotations.find(
      (annotation) => annotation.id === utterance.dialogue_annotation_id
    );
    if (!dialogue) return;
    const sourceSegment = workspaceRef.current?.querySelector<HTMLElement>(
      `.source-annotation-pane__segment[data-start-utf16="${dialogue.span.start_utf16}"]`
    );
    sourceSegment?.scrollIntoView?.({ block: "center" });
    sourceSegment?.focus();
  };

  const handleCreateAnnotation = (annotation: SemanticAnnotation) => {
    const operations: DraftOperation[] = [{ op: "create_annotation", annotation }];
    if (annotation.kind === "dialogue") {
      operations.push({
        op: "create_utterance",
        utterance: {
          id: createUtteranceId(),
          dialogue_annotation_id: annotation.id,
          speaker_annotation_id: null,
          character_candidate_id: null,
          emotion_evidence_annotation_ids: [],
          normalized_emotion: null,
          custom_emotion: null,
          emotion_intensity: null,
          emotion_origin: "none",
          language: i18n.resolvedLanguage ?? i18n.language ?? "zh-CN",
          confidence: 1,
          uncertainty_codes: ["speaker_unknown"],
          status: "pending"
        }
      });
    }
    queueOperations(operations);
  };

  const handleCopyDiagnostics = () => {
    if (!controller.error) return;
    const diagnostics = diagnosticsFor(controller.error);
    const action = copyDiagnostics
      ? copyDiagnostics(diagnostics)
      : globalThis.navigator?.clipboard?.writeText
        ? globalThis.navigator.clipboard.writeText(diagnostics)
        : undefined;
    Promise.resolve(action).then(() => setCopyComplete(true));
  };

  const handleConfirm = () => {
    if (confirmationRef.current) return;
    setSubmittingConfirmation(true);
    const confirmation = controller.confirm();
    confirmationRef.current = confirmation;
    void confirmation
      .then((result) => {
        if (!mountedRef.current) return;
        onConfirmed(result.project);
        setConfirmOpen(false);
      })
      .catch(() => undefined)
      .finally(() => {
        if (confirmationRef.current === confirmation) confirmationRef.current = null;
        if (mountedRef.current) setSubmittingConfirmation(false);
      });
  };

  const isEditable = Boolean(draft) && !controller.isRunning && !controller.isReadOnly;

  return (
    <div className="script-analysis-workspace" ref={workspaceRef} data-testid="script-analysis-workspace">
      <header className="script-analysis-workspace__header">
        <div>
          <h1>{t("analysis.title")}</h1>
          <p>{t("analysis.subtitle")}</p>
        </div>
        <span className={`analysis-run-status analysis-run-status--${runStatusKey(controller)}`}>
          {t(`analysis.runStatus.${runStatusKey(controller)}`)}
        </span>
      </header>

      {controller.error ? (
        <section className="analysis-error-panel" role="alert" data-analysis-error="run">
          <h2>{t("analysis.errors.runTitle")}</h2>
          <dl>
            <div><dt>{t("analysis.errors.code")}</dt><dd>{controller.error.code}</dd></div>
            <div><dt>{t("analysis.errors.httpStatus")}</dt><dd>{controller.error.http_status}</dd></div>
            <div><dt>{t("analysis.errors.stage")}</dt><dd>{controller.error.stage}</dd></div>
            <div><dt>{t("analysis.errors.message")}</dt><dd>{controller.error.message}</dd></div>
            <div><dt>{t("analysis.errors.traceId")}</dt><dd>{controller.error.trace_id ?? t("analysis.common.none")}</dd></div>
          </dl>
          <div className="analysis-error-panel__actions">
            <button type="button" data-error-action="copy" onClick={handleCopyDiagnostics}>
              {copyComplete ? t("analysis.errors.copied") : t("analysis.errors.copyDiagnostics")}
            </button>
            <button type="button" data-error-action="dismiss" onClick={controller.dismissError}>
              {t("analysis.errors.dismiss")}
            </button>
          </div>
        </section>
      ) : null}

      {controller.conflict ? (
        <section className="analysis-error-panel analysis-error-panel--conflict" role="alert" data-analysis-error="conflict">
          <h2>{t("analysis.errors.conflictTitle")}</h2>
          <p>{controller.conflict.message}</p>
          <p>{t("analysis.errors.conflictGuidance")}</p>
          <button type="button" data-error-action="retry" onClick={controller.retryPendingOperations}>
            {t("analysis.actions.retry")}
          </button>
        </section>
      ) : null}

      {controller.controllerError && !controller.conflict ? (
        <section className="analysis-error-panel" role="alert" data-analysis-error="controller">
          <h2>{t("analysis.errors.controllerTitle")}</h2>
          <p>{controller.controllerError.code ? `${controller.controllerError.code}: ` : ""}{controller.controllerError.message}</p>
          <button
            type="button"
            data-error-action="retry"
            onClick={
              controller.controllerError.kind === "patch"
                ? controller.retryPendingOperations
                : controller.retryAnalysis
            }
          >
            {t("analysis.actions.retry")}
          </button>
        </section>
      ) : null}

      <main className="script-analysis-workspace__panes">
        <section className="script-analysis-workspace__source" aria-labelledby="analysis-source-title">
          <h2 id="analysis-source-title">{t("analysis.source.title")}</h2>
          <SourceAnnotationPane
            sourceRevision={sourceRevision}
            annotations={draft?.annotations ?? []}
            onCreateAnnotation={handleCreateAnnotation}
            onSelectAnnotation={focusResultForAnnotation}
          />
        </section>

        <section className="script-analysis-workspace__review" aria-label={t("analysis.review.title")}>
          {controller.run?.quality === "partial" ? (
            <p className="analysis-review-notice" data-analysis-quality="partial">
              {t("analysis.review.partialQuality")}
            </p>
          ) : null}

          {controller.run?.warnings.length ? (
            <section className="analysis-review-messages" aria-label={t("analysis.review.runWarningsTitle")}>
              <h2>{t("analysis.review.runWarningsTitle")}</h2>
              {controller.run.warnings.map((warning) => (
                <article
                  className="analysis-review-message"
                  data-warning-source="run"
                  key={warning.id}
                >
                  <strong>{warning.code}</strong>
                  <p>{warning.message}</p>
                </article>
              ))}
            </section>
          ) : null}

          {draft?.warnings.length ? (
            <section className="analysis-review-messages" aria-label={t("analysis.review.draftWarningsTitle")}>
              <h2>{t("analysis.review.draftWarningsTitle")}</h2>
              {draft.warnings.map((warning) => (
                <article
                  className="analysis-review-message"
                  data-warning-source="draft"
                  key={warning.id}
                >
                  <div>
                    <strong>{warning.code}</strong>
                    <p>{warning.message}</p>
                  </div>
                  <button
                    type="button"
                    data-warning-action="dismiss"
                    disabled={!isEditable}
                    onClick={() => queueOperations([{ op: "dismiss_warning", warning_id: warning.id }])}
                  >
                    {t("analysis.review.dismissWarning")}
                  </button>
                </article>
              ))}
            </section>
          ) : null}

          {draft?.unresolved_candidates.length ? (
            <section className="analysis-review-messages" data-unresolved-candidates>
              <h2>{t("analysis.review.unresolvedTitle")}</h2>
              {draft.unresolved_candidates.map((candidate) => (
                <article className="analysis-review-message" key={candidate.id}>
                  <strong>{candidate.code}</strong>
                  <p>{candidate.message}</p>
                </article>
              ))}
            </section>
          ) : null}

          {!draft ? (
            <p className="analysis-empty-state">
              {controller.isRunning ? t("analysis.results.waiting") : t("analysis.results.unavailable")}
            </p>
          ) : (
            <>
              <AnalysisResultsPane
                draft={draft}
                filter={filter}
                onFilterChange={setFilter}
                onOperations={queueOperations}
                onSelectUtterance={focusSourceForUtterance}
                disabled={!isEditable}
              />
              <CharacterAliasEditor
                characters={draft.characters}
                utterances={draft.utterances}
                onOperations={queueOperations}
                createCharacterId={createCharacterId}
                disabled={!isEditable}
              />
            </>
          )}
        </section>
      </main>

      <footer className="script-analysis-workspace__actions">
        <span aria-live="polite">
          {controller.isSaving
            ? t("analysis.save.saving")
            : controller.pendingOperationBatches > 0
              ? t("analysis.save.pending", { count: controller.pendingOperationBatches })
              : t("analysis.save.saved")}
        </span>
        <button type="button" data-action="cancel-workspace" onClick={onCancel}>
          {t("analysis.actions.cancel")}
        </button>
        <button
          type="button"
          data-action="confirm-open"
          disabled={!isEditable}
          onClick={() => setConfirmOpen(true)}
        >
          {t("analysis.confirm.open")}
        </button>
        {draft?.confirmed_revision_id ? (
          <button
            type="button"
            data-action="confirm-recover"
            disabled={submittingConfirmation || controller.isConfirming}
            onClick={handleConfirm}
          >
            {t("analysis.confirm.recover")}
          </button>
        ) : null}
      </footer>

      {confirmOpen && draft ? (
        <div className="analysis-confirm-backdrop">
          <section className="analysis-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="analysis-confirm-title">
            <h2 id="analysis-confirm-title">{t("analysis.confirm.title")}</h2>
            <p>{t("analysis.confirm.importCount", { count: summary.importable })}</p>
            <p>{t("analysis.confirm.excludedCount", { count: summary.excluded })}</p>
            <p>{t("analysis.confirm.authoritative")}</p>
            <div className="analysis-confirm-dialog__actions">
              <button
                type="button"
                data-action="confirm-cancel"
                disabled={submittingConfirmation}
                onClick={() => setConfirmOpen(false)}
              >
                {t("analysis.actions.cancel")}
              </button>
              <button
                type="button"
                data-action="confirm-submit"
                disabled={submittingConfirmation || controller.isConfirming}
                onClick={handleConfirm}
              >
                {submittingConfirmation ? t("analysis.confirm.confirming") : t("analysis.confirm.submit")}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
