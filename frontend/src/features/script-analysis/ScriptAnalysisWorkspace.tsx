import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  AnalysisHistoryItem,
  AnalysisError,
  DraftOperation,
  ScriptProject,
  ScriptRevision,
  SemanticAnalysisDraft,
  SemanticUtterance,
  UnresolvedCandidate
} from "../../types";
import { AnalysisResultsPane, type AnalysisResultFilter } from "./AnalysisResultsPane";
import { AnalysisHistoryDropdown } from "./AnalysisHistoryDropdown";
import {
  AnalysisBlockingDialog,
  AnalysisLoadingState,
  AnalysisNotifications
} from "./AnalysisWorkspaceChrome";
import { CharacterAliasEditor } from "./CharacterAliasEditor";
import { SourceAnnotationPane, type AnnotationEdit } from "./SourceAnnotationPane";
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
  onDeleteAnalysisHistoryRequest?: (item: AnalysisHistoryItem) => Promise<boolean>;
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
  copyDiagnostics,
  onDeleteAnalysisHistoryRequest
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

  const revealElement = (element: HTMLElement | null | undefined) => {
    if (!element) return;
    element.scrollIntoView?.({ block: "center" });
    element.focus();
    element.classList.add("is-linked-focus");
    globalThis.setTimeout(() => element.classList.remove("is-linked-focus"), 1600);
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
      revealElement(card);
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
    revealElement(sourceSegment);
  };

  const handleApplyAnnotations = ({
    createdAnnotations,
    deletedAnnotationIds
  }: AnnotationEdit) => {
    const operations: DraftOperation[] = deletedAnnotationIds.map((annotationId) => ({
      op: "delete_annotation",
      annotation_id: annotationId
    }));
    createdAnnotations.forEach((annotation) => {
      operations.push({ op: "create_annotation", annotation });
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
    });
    if (operations.length > 0) queueOperations(operations);
  };

  const focusUnresolvedCandidate = (candidate: UnresolvedCandidate) => {
    const utteranceId = candidate.details.utterance_id;
    if (typeof utteranceId === "string") {
      const utterance = draft?.utterances.find((item) => item.id === utteranceId);
      if (utterance) focusSourceForUtterance(utterance);
      revealElement(
        workspaceRef.current?.querySelector<HTMLElement>(`[data-utterance-id="${utteranceId}"]`)
      );
      return;
    }
    const annotationId = candidate.details.annotation_id;
    if (typeof annotationId === "string") {
      focusResultForAnnotation(annotationId);
      return;
    }
    const rawStart = candidate.details.start_utf16 ?? candidate.details.start;
    if (typeof rawStart === "number") {
      revealElement(
        workspaceRef.current?.querySelector<HTMLElement>(
          `.source-annotation-pane__segment[data-start-utf16="${rawStart}"]`
        ) ?? workspaceRef.current?.querySelector<HTMLElement>(".source-annotation-pane__source")
      );
      return;
    }
    revealElement(
      workspaceRef.current?.querySelector<HTMLElement>(".source-annotation-pane__source")
    );
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
  const visibleWarnings = draft?.warnings ?? controller.run?.warnings ?? [];
  const annotationLabels = {
    kindLabels: {
      speaker: t("analysis.annotationKind.speaker"),
      emotion_evidence: t("analysis.annotationKind.emotionEvidence"),
      dialogue: t("analysis.annotationKind.dialogue")
    },
    sourceRegionLabel: t("analysis.source.regionLabel"),
    legendLabel: t("analysis.source.legendLabel"),
    selectionDialogLabel: t("analysis.source.selectionDialog"),
    applySelection: t("analysis.actions.apply"),
    closeSelection: t("analysis.actions.close")
  };

  return (
    <div className="script-analysis-workspace" ref={workspaceRef} data-testid="script-analysis-workspace">
      <header className="script-analysis-workspace__topbar">
        <div className="script-analysis-workspace__title">
          <div>
            <h1>{t("analysis.title")}</h1>
            <p>{t("analysis.subtitle")}</p>
          </div>
          <span className={`analysis-run-status analysis-run-status--${runStatusKey(controller)}`}>
            {t(`analysis.runStatus.${runStatusKey(controller)}`)}
          </span>
          {controller.run?.quality === "partial" ? (
            <span className="analysis-partial-status" data-analysis-quality="partial">
              {t("analysis.review.partialQuality")}
            </span>
          ) : null}
        </div>

        <div className="script-analysis-workspace__topbar-actions">
          {onDeleteAnalysisHistoryRequest ? (
            <AnalysisHistoryDropdown
              onDeleteRequest={onDeleteAnalysisHistoryRequest}
              onDeleted={(item) => {
                if (item.run_id === controller.run?.id) onCancel();
              }}
            />
          ) : null}
          <AnalysisNotifications
            warnings={visibleWarnings}
            warningSource={draft ? "draft" : "run"}
            unresolved={draft?.unresolved_candidates ?? []}
            disabled={!isEditable}
            onDismissWarning={(warningId) =>
              queueOperations([{ op: "dismiss_warning", warning_id: warningId }])
            }
            onLocateUnresolved={focusUnresolvedCandidate}
          />
          <span className="analysis-save-status" aria-live="polite">
            {controller.isSaving
              ? t("analysis.save.saving")
              : controller.pendingOperationBatches > 0
                ? t("analysis.save.pending", { count: controller.pendingOperationBatches })
                : t("analysis.save.saved")}
          </span>
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
        </div>
      </header>

      <main className="script-analysis-workspace__panes">
        <section className="script-analysis-workspace__source" aria-labelledby="analysis-source-title">
          <h2 id="analysis-source-title">
            {t("analysis.source.title")}
            {draft && controller.isReadOnly ? (
              <span className="analysis-save-status" role="status">
                {" · "}{t("analysis.source.confirmedReadOnly")}
              </span>
            ) : null}
          </h2>
          <SourceAnnotationPane
            sourceRevision={sourceRevision}
            annotations={draft?.annotations ?? []}
            disabled={!isEditable}
            onApplyAnnotations={handleApplyAnnotations}
            onSelectAnnotation={focusResultForAnnotation}
            labels={annotationLabels}
          />
        </section>

        <section className="script-analysis-workspace__review" aria-label={t("analysis.review.title")}>
          {!draft ? (
            controller.isRunning ? (
              <AnalysisLoadingState progress={controller.run?.progress ?? 0} />
            ) : (
              <p className="analysis-empty-state">{t("analysis.results.unavailable")}</p>
            )
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
                onOperations={queueOperations}
                createCharacterId={createCharacterId}
                disabled={!isEditable}
              />
            </>
          )}
        </section>
      </main>

      {controller.error ? (
        <AnalysisBlockingDialog
          kind="run"
          title={t("analysis.errors.runTitle")}
          description={t("analysis.errors.runDescription")}
          error={controller.error}
          onCopyDiagnostics={handleCopyDiagnostics}
          copyComplete={copyComplete}
          onDismiss={controller.dismissError}
          onRetry={controller.retryAnalysis}
        />
      ) : null}

      {controller.conflict ? (
        <AnalysisBlockingDialog
          kind="conflict"
          title={t("analysis.errors.conflictTitle")}
          description={t("analysis.errors.conflictGuidance")}
          onRetry={controller.retryPendingOperations}
        />
      ) : null}

      {controller.controllerError && !controller.conflict ? (
        <AnalysisBlockingDialog
          kind="controller"
          title={t("analysis.errors.controllerTitle")}
          description={t("analysis.errors.controllerDescription")}
          error={{
            code: controller.controllerError.code,
            http_status: controller.controllerError.httpStatus,
            stage: controller.controllerError.stage ?? controller.controllerError.kind,
            message: controller.controllerError.message,
            trace_id: null
          }}
          onRetry={
            controller.controllerError.kind === "patch"
              ? controller.retryPendingOperations
              : controller.controllerError.kind === "confirm"
                ? handleConfirm
              : controller.retryAnalysis
          }
        />
      ) : null}

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
