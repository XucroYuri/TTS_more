import { memo, useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import {
  applyLocalPortableImport,
  fetchLocalControlToken,
  fetchLocalPortableServices,
  fetchPortableActionStatus,
  fetchPortableOperation,
  fetchPortableOperationLogs,
  fetchServicesStatus,
  PortableApiError,
  planLocalPortableImport,
  portableServiceAction,
  registerLocalPortableService,
  selectLocalPortableFolder,
} from "../api";
import {
  ACTIVE_PORTABLE_UI_PHASES,
  beginPortableImportAttempt,
  completePortableImport,
  consumePortableImportPlan,
  createPortableActionConvergencePoller,
  createPortableOperationPoller,
  failPortableImport,
  failPortableImportPlanAttempt,
  expirePortableImportPlan,
  initialPortableImportState,
  invalidatePortableImport,
  mergePortableEvents,
  portableErrorMessageKey,
  portableImportEligibility,
  portableImportErrorMessageKey,
  portableImportIdentity,
  portableImportLocksCard,
  portablePhaseAfterAction,
  portablePhaseLabelKey,
  portableServiceCards,
  PortableImportControlEpochError,
  PORTABLE_REPAIR_CONVERGENCE_TIMEOUT_MS,
  PORTABLE_STOP_CONVERGENCE_TIMEOUT_MS,
  receivePortableImportPlanAttempt,
  rebindPortableImportPlanningEpoch,
  resetPortableImport,
  shouldRevealManualProxy,
  shouldShowPortableLifecycleProgress,
  validatePortableProxyUrl,
  withControlTokenRetry,
  withPortableImportControlEpoch,
  withPortableImportPlanControlEpoch,
  type PortableServiceCard,
  type PortableCardActions,
  type PortableImportState,
  type PortableRuntimeStatus,
  type PortableUiPhase,
} from "../lib/portableServices";
import type {
  LocalPortableService,
  PortableOperationEvent,
  PortableServiceAction,
} from "../types";

const COMPONENT_NAMES = {
  "gpt-sovits": "GPT-SoVITS",
  indextts: "IndexTTS",
  cosyvoice: "CosyVoice",
} as const;
type ControlRunner = <T>(run: (token: string) => Promise<T>) => Promise<T>;
type ImportControlRunner = <T>(
  expectedEpoch: number,
  run: (token: string) => Promise<T>,
) => Promise<T>;
type ImportPlanControlRunner = <T>(
  run: (token: string, controlEpoch: number) => Promise<T>,
) => Promise<{ value: T; controlEpoch: number }>;

interface PortableWorkbenchSnapshot {
  services: LocalPortableService[];
  runtimes: PortableRuntimeStatus[];
}

export interface LocalPortableServicesPanelProps {
  initialServices?: LocalPortableService[];
  onServicesStatusRefresh: () => Promise<void>;
}

interface LocalPortableServiceCardProps {
  card: PortableServiceCard;
  runtimeStatuses: PortableRuntimeStatus[];
  runWithControl: ControlRunner;
  runWithImportControl: ImportControlRunner;
  runWithImportPlanControl: ImportPlanControlRunner;
  controlEpoch: number;
  getControlEpoch: () => number;
  onReload: (signal?: AbortSignal) => Promise<PortableWorkbenchSnapshot>;
  onServicesStatusRefresh: () => Promise<void>;
  onLiveMessage: (message: string) => void;
}

function errorView(error: unknown): { code: string | undefined; detail: string } {
  if (error instanceof PortableApiError) return { code: error.code, detail: error.message };
  if (error instanceof Error && error.name !== "AbortError") return { code: undefined, detail: error.message };
  return { code: undefined, detail: "" };
}

function portableImportFailureKey(error: unknown): string {
  if (
    error instanceof PortableImportControlEpochError
    || (error instanceof PortableApiError && error.status === 403)
  ) return "portableServices.import.error.controlChanged";
  return portableImportErrorMessageKey(error instanceof PortableApiError ? error.code : undefined);
}

export interface PortableImportInlineProps {
  state: PortableImportState;
  onConfirm: () => void;
  onCancel: () => void;
  onRetry?: () => void;
  retryDisabled?: boolean;
  retryDescribedBy?: string;
  confirmButtonRef?: RefObject<HTMLButtonElement | null>;
  retryButtonRef?: RefObject<HTMLButtonElement | null>;
}

export function PortableImportInline({
  state,
  onConfirm,
  onCancel,
  onRetry,
  retryDisabled = false,
  retryDescribedBy,
  confirmButtonRef,
  retryButtonRef,
}: PortableImportInlineProps) {
  const { i18n, t } = useTranslation();
  const number = useMemo(() => new Intl.NumberFormat(i18n.language), [i18n.language]);
  if (state.phase === "idle") {
    return state.notice === "cancelled"
      ? <p className="portable-import-status" role="status">{t("portableServices.import.cancelled")}</p>
      : null;
  }
  if (state.phase === "planning") {
    return <p className="portable-import-status" role="status">{t("portableServices.import.planning")}</p>;
  }
  if (state.phase === "applying") {
    return <p className="portable-import-status" role="status">{t("portableServices.import.applying")}</p>;
  }
  if (state.phase === "success") {
    return (
      <p className="portable-import-status portable-import-success" role="status">
        {t("portableServices.import.success", {
          copied: number.format(state.result.copied_user_files),
          reused: number.format(state.result.reused_assets.length),
          skipped: number.format(state.result.skipped_assets.length),
          present: number.format(state.result.already_present.length),
        })}
      </p>
    );
  }
  if (state.phase === "error" || state.phase === "expired") {
    const messageKey = state.phase === "expired"
      ? "portableServices.import.error.planUnavailable"
      : state.errorKey;
    return (
      <div className="portable-import-result">
        <p className="portable-import-error" role="alert">{t(messageKey)}</p>
        <button
          ref={retryButtonRef}
          type="button"
          onClick={onRetry ?? onCancel}
          disabled={retryDisabled}
          aria-describedby={retryDisabled ? retryDescribedBy : undefined}
        >
          {t("portableServices.import.retry")}
        </button>
      </div>
    );
  }
  const summary = state.summary;
  return (
    <section className="portable-import-confirmation" aria-label={t("portableServices.import.action")}>
      <h4 className="portable-import-ready" role="status" tabIndex={-1}>{t("portableServices.import.ready")}</h4>
      <p className="portable-import-preserved">{t("portableServices.import.preservedStopped")}</p>
      <dl className="portable-import-summary">
        <div><dt>{t("portableServices.import.userFiles", { count: number.format(summary.userFileCount), bytes: number.format(summary.userBytes) })}</dt></div>
        <div><dt>{t("portableServices.import.reusableAssets", { count: number.format(summary.reusableAssetCount), bytes: number.format(summary.reusableAssetBytes) })}</dt></div>
        <div><dt>{t("portableServices.import.skippedAssets", { count: number.format(summary.skippedAssetCount) })}</dt></div>
        <div><dt>{t("portableServices.import.alreadyPresent", { count: number.format(summary.alreadyPresentCount) })}</dt></div>
      </dl>
      {summary.assetNames.length ? (
        <details className="portable-import-assets">
          <summary>{t("portableServices.import.assetDetails")}</summary>
          <ul>{summary.assetNames.map((name) => <li key={name}>{name}</li>)}</ul>
        </details>
      ) : null}
      <div className="portable-import-confirm-actions">
        <button ref={confirmButtonRef} type="button" className="primary" onClick={onConfirm}>{t("portableServices.import.confirm")}</button>
        <button type="button" onClick={onCancel}>{t("portableServices.import.cancel")}</button>
      </div>
    </section>
  );
}

export interface PortableMutableControlsProps {
  actions: Pick<PortableCardActions, "browse" | "start" | "stop" | "repair" | "openFolder">;
  locked: boolean;
  lifecycleDisabled: boolean;
  onBrowse: () => void;
  onAction: (action: PortableServiceAction) => void;
}

export function PortableMutableControls({
  actions,
  locked,
  lifecycleDisabled,
  onBrowse,
  onAction,
}: PortableMutableControlsProps) {
  const { t } = useTranslation();
  return (
    <>
      <button type="button" onClick={onBrowse} disabled={locked || !actions.browse}>{t("portableServices.action.browse")}</button>
      <button type="button" onClick={() => onAction("start")} disabled={locked || lifecycleDisabled || !actions.start}>{t("portableServices.action.start")}</button>
      <button type="button" onClick={() => onAction("stop")} disabled={locked || lifecycleDisabled || !actions.stop}>{t("portableServices.action.stop")}</button>
      <button type="button" onClick={() => onAction("repair")} disabled={locked || lifecycleDisabled || !actions.repair}>{t("portableServices.action.repair")}</button>
      <button type="button" onClick={() => onAction("open-folder")} disabled={locked || lifecycleDisabled || !actions.openFolder}>{t("portableServices.action.openFolder")}</button>
    </>
  );
}

const LocalPortableServiceCard = memo(function LocalPortableServiceCard({
  card,
  runtimeStatuses,
  runWithControl,
  runWithImportControl,
  runWithImportPlanControl,
  controlEpoch,
  getControlEpoch,
  onReload,
  onServicesStatusRefresh,
  onLiveMessage,
}: LocalPortableServiceCardProps) {
  const { t } = useTranslation();
  const [pendingAction, setPendingAction] = useState<PortableServiceAction | "browse" | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [operationPhase, setOperationPhase] = useState<PortableUiPhase | null>(null);
  const [convergence, setConvergence] = useState<{
    action: "stop" | "repair";
    actionId: string | null;
    terminalFromResponse: boolean;
  } | null>(null);
  const [events, setEvents] = useState<PortableOperationEvent[]>([]);
  const [logsOpen, setLogsOpen] = useState(false);
  const [errorCode, setErrorCode] = useState<string | undefined>();
  const [errorDetail, setErrorDetail] = useState("");
  const [proxyUrl, setProxyUrl] = useState("");
  const [importState, setImportState] = useState<PortableImportState>(initialPortableImportState);
  const importStateRef = useRef<PortableImportState>(importState);
  const planningAttemptRef = useRef<{ nonce: symbol; controller: AbortController } | null>(null);
  const importTriggerRef = useRef<HTMLButtonElement>(null);
  const importConfirmRef = useRef<HTMLButtonElement>(null);
  const restoreImportFocusRef = useRef(false);
  const announcedReadyNonceRef = useRef<symbol | null>(null);
  const cursorRef = useRef(0);
  const effectivePhase = operationPhase ?? card.status;
  const effectiveCard = useMemo(() => {
    if (!operationPhase || !card.service) return card;
    return portableServiceCards([card.service], {
      phases: { [card.component]: operationPhase },
      runtimes: runtimeStatuses,
    })
      .find((item) => item.component === card.component) ?? card;
  }, [card, operationPhase, runtimeStatuses]);
  const operationBusy = operationPhase ? ACTIVE_PORTABLE_UI_PHASES.has(operationPhase) : false;
  const lifecycleBusy = pendingAction !== null || operationBusy;
  const importLocked = portableImportLocksCard(importState);
  const controlsBusy = lifecycleBusy || importLocked;
  const importRuntime = card.service?.service_id
    ? runtimeStatuses.find((runtime) => runtime.service_id === card.service?.service_id)
    : undefined;
  const importIdentity = portableImportIdentity(card.service, importRuntime);
  const importIdentityRef = useRef(importIdentity);
  importIdentityRef.current = importIdentity;
  const importEligibility = portableImportEligibility(card.service, importRuntime, lifecycleBusy);
  const lastProgress = [...events].reverse().find((event) => typeof event.percent === "number")?.percent;
  const lastEvent = events.at(-1);
  const reasonId = `portable-disabled-${card.component}`;
  const statusId = `portable-status-${card.component}`;
  const importReasonId = `portable-import-disabled-${card.component}`;

  const updateImportState = useCallback((next: PortableImportState) => {
    importStateRef.current = next;
    setImportState(next);
  }, []);

  const abortPlanningAttempt = useCallback((expectedNonce?: symbol) => {
    const active = planningAttemptRef.current;
    if (!active || (expectedNonce && active.nonce !== expectedNonce)) return;
    planningAttemptRef.current = null;
    active.controller.abort();
  }, []);

  useEffect(() => () => abortPlanningAttempt(), [abortPlanningAttempt]);

  useEffect(() => {
    const current = importStateRef.current;
    const next = invalidatePortableImport(current, importIdentity, controlEpoch);
    if (next !== current) {
      if (current.phase === "planning") abortPlanningAttempt(current.attemptNonce);
      restoreImportFocusRef.current = true;
      updateImportState(next);
    }
  }, [abortPlanningAttempt, controlEpoch, importIdentity?.buildId, importIdentity?.packageId, importIdentity?.serviceId, updateImportState]);

  useEffect(() => {
    if (importState.phase === "awaiting-confirmation") {
      if (announcedReadyNonceRef.current !== importState.pending.attemptNonce) {
        announcedReadyNonceRef.current = importState.pending.attemptNonce;
        importConfirmRef.current?.focus();
        onLiveMessage(t("portableServices.import.ready"));
      }
      return;
    }
    if (
      restoreImportFocusRef.current
      && (importState.phase === "idle" || importState.phase === "error" || importState.phase === "expired")
      && importTriggerRef.current
    ) {
      importTriggerRef.current.focus();
      restoreImportFocusRef.current = false;
    }
  }, [importState, onLiveMessage, t]);

  useEffect(() => {
    if (importState.phase !== "awaiting-confirmation") return;
    const { attemptNonce, expiresAtMs, identity } = importState.pending;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const checkDeadline = () => {
      const current = importStateRef.current;
      const next = expirePortableImportPlan(current, attemptNonce, expiresAtMs, identity, Date.now());
      if (next !== current) {
        restoreImportFocusRef.current = true;
        updateImportState(next);
        onLiveMessage(t("portableServices.import.error.planUnavailable"));
        return;
      }
      if (
        current.phase === "awaiting-confirmation"
        && current.pending.attemptNonce === attemptNonce
        && current.pending.expiresAtMs === expiresAtMs
      ) {
        timer = setTimeout(checkDeadline, Math.max(1, expiresAtMs - Date.now()));
      }
    };
    timer = setTimeout(checkDeadline, Math.max(1, expiresAtMs - Date.now()));
    return () => {
      if (timer !== null) clearTimeout(timer);
    };
  }, [importState, onLiveMessage, t, updateImportState]);

  useEffect(() => {
    if (!operationId && !convergence) setOperationPhase(null);
  }, [card.status, convergence, operationId]);

  useEffect(() => {
    if (!operationId) return;
    const poller = createPortableOperationPoller({
      pollStatus: (signal) => runWithControl((token) => fetchPortableOperation(card.component, operationId, token, signal)),
      pollLogs: (afterSeq, signal) => runWithControl((token) => fetchPortableOperationLogs(card.component, operationId, token, afterSeq, 100, signal)),
      onSnapshot: (snapshot) => {
        cursorRef.current = snapshot.nextSeq;
        setOperationPhase(snapshot.phase);
        setEvents((current) => mergePortableEvents(current, snapshot.events));
        const latestError = [...snapshot.events].reverse().find((event) => event.error_code);
        if (latestError?.error_code) {
          setErrorCode(latestError.error_code);
          setErrorDetail(latestError.message);
        }
      },
      onTerminal: async (snapshot) => {
        onLiveMessage(t(portablePhaseLabelKey(snapshot.phase)));
        await Promise.all([onReload(), onServicesStatusRefresh()]);
      },
      onError: (error) => {
        const view = errorView(error);
        setOperationPhase("blocked");
        setErrorCode(view.code);
        setErrorDetail(view.detail);
      },
      isHidden: () => document.visibilityState === "hidden",
    });
    const handleVisibility = () => {
      if (document.visibilityState === "visible") poller.resume();
    };
    document.addEventListener("visibilitychange", handleVisibility);
    poller.start();
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
      poller.stop();
    };
  }, [card.component, onLiveMessage, onReload, onServicesStatusRefresh, operationId, runWithControl, t]);

  useEffect(() => {
    if (!convergence) return;
    const serviceId = card.service?.service_id;
    const poller = createPortableActionConvergencePoller({
      check: async (signal) => {
        let terminal = convergence.terminalFromResponse;
        if (convergence.actionId) {
          const actionState = await runWithControl((token) => fetchPortableActionStatus(
            card.component,
            convergence.actionId as string,
            token,
            signal,
          ));
          setOperationPhase(actionState.status === "stopping" || actionState.status === "repairing"
            ? actionState.status
            : convergence.action === "stop" ? "stopping" : "repairing");
          terminal = convergence.action === "stop"
            ? actionState.status === "stopped"
            : actionState.status === "completed";
        }
        if (!terminal) return false;
        const snapshot = await onReload(signal);
        if (convergence.action === "repair") {
          const local = snapshot.services.find((service) => service.service_id === serviceId);
          return local?.setup_state === "ready";
        }
        const runtime = snapshot.runtimes.find((service) => service.service_id === serviceId);
        return Boolean(runtime && runtime.supervisor_state === "stopped" && runtime.ready !== true);
      },
      onSettled: async () => {
        setConvergence(null);
        setPendingAction(null);
        setOperationPhase(null);
        if (convergence.action === "repair") setProxyUrl("");
        onLiveMessage(t(`portableServices.action.${convergence.action}`));
        await onServicesStatusRefresh();
      },
      onTimeout: () => {
        setConvergence(null);
        setPendingAction(null);
        setOperationPhase(null);
        setErrorCode("LOCAL_CONTROL_STATUS_TIMEOUT");
        setErrorDetail(t("portableServices.error.statusTimeout"));
        onLiveMessage(t("portableServices.error.statusTimeout"));
      },
      onError: (error) => {
        const view = errorView(error);
        setConvergence(null);
        setPendingAction(null);
        setOperationPhase(null);
        setErrorCode(view.code);
        setErrorDetail(view.detail);
        onLiveMessage(t(portableErrorMessageKey(view.code)));
      },
      timeoutMs: convergence.action === "repair"
        ? PORTABLE_REPAIR_CONVERGENCE_TIMEOUT_MS
        : PORTABLE_STOP_CONVERGENCE_TIMEOUT_MS,
    });
    poller.start();
    return () => poller.stop();
  }, [card.component, card.service?.service_id, convergence, onLiveMessage, onReload, onServicesStatusRefresh, runWithControl, t]);

  const chooseFolder = useCallback(async () => {
    if (controlsBusy || !card.actions.browse) return;
    setPendingAction("browse");
    setErrorCode(undefined);
    setErrorDetail("");
    try {
      const selection = await runWithControl((token) => selectLocalPortableFolder(card.component, token));
      if (selection.status === "cancelled") {
        onLiveMessage(t("portableServices.selectionCancelled"));
        return;
      }
      await runWithControl((token) => registerLocalPortableService({
        component: card.component,
        package_id: selection.package.package_id,
        path: selection.package.package_root,
        ...(card.service?.port_override == null ? {} : { port_override: card.service.port_override }),
      }, token));
      setOperationId(null);
      setOperationPhase(null);
      cursorRef.current = 0;
      setEvents([]);
      onLiveMessage(t("portableServices.selected", { component: COMPONENT_NAMES[card.component] }));
      await Promise.all([onReload(), onServicesStatusRefresh()]);
    } catch (error) {
      const view = errorView(error);
      setErrorCode(view.code);
      setErrorDetail(view.detail);
      onLiveMessage(t(portableErrorMessageKey(view.code)));
    } finally {
      setPendingAction(null);
    }
  }, [card.actions.browse, card.component, card.service?.port_override, controlsBusy, onLiveMessage, onReload, onServicesStatusRefresh, runWithControl, t]);

  const chooseImportSource = useCallback(async () => {
    if (!importEligibility.allowed || !importIdentity || portableImportLocksCard(importStateRef.current)) return;
    abortPlanningAttempt();
    const attemptNonce = Symbol(`portable import ${card.component}`);
    const controller = new AbortController();
    planningAttemptRef.current = { nonce: attemptNonce, controller };
    const planning = beginPortableImportAttempt(
      importStateRef.current,
      importIdentity,
      attemptNonce,
      getControlEpoch(),
    );
    updateImportState(planning);
    try {
      const planned = await runWithImportPlanControl((token, requestEpoch) => {
        const current = importStateRef.current;
        const rebound = rebindPortableImportPlanningEpoch(current, attemptNonce, requestEpoch);
        if (rebound !== current) updateImportState(rebound);
        return planLocalPortableImport(card.component, token, controller.signal);
      });
      if (
        controller.signal.aborted
        || planningAttemptRef.current?.nonce !== attemptNonce
        || importStateRef.current.phase !== "planning"
        || importStateRef.current.attemptNonce !== attemptNonce
      ) return;
      planningAttemptRef.current = null;
      const response = planned.value;
      const epoch = planned.controlEpoch;
      let next = receivePortableImportPlanAttempt(
        importStateRef.current,
        response,
        Date.now(),
        epoch,
        attemptNonce,
      );
      next = invalidatePortableImport(next, importIdentityRef.current, epoch);
      if ("status" in response || next.phase === "error") restoreImportFocusRef.current = true;
      updateImportState(next);
      if ("status" in response) {
        onLiveMessage(t("portableServices.import.cancelled"));
      } else if (next.phase === "error") {
        onLiveMessage(t(next.errorKey));
      }
    } catch (error) {
      if (controller.signal.aborted || planningAttemptRef.current?.nonce !== attemptNonce) return;
      planningAttemptRef.current = null;
      const errorKey = portableImportFailureKey(error);
      const next = failPortableImportPlanAttempt(importStateRef.current, attemptNonce, errorKey);
      if (next !== importStateRef.current) {
        restoreImportFocusRef.current = true;
        updateImportState(next);
        onLiveMessage(t(errorKey));
      }
    }
  }, [abortPlanningAttempt, card.component, getControlEpoch, importEligibility.allowed, importIdentity, onLiveMessage, runWithImportPlanControl, t, updateImportState]);

  const confirmImport = useCallback(async () => {
    const consumed = consumePortableImportPlan(importStateRef.current, Date.now(), getControlEpoch());
    if (!consumed.request && (consumed.state.phase === "expired" || consumed.state.phase === "error")) {
      restoreImportFocusRef.current = true;
    }
    updateImportState(consumed.state);
    if (!consumed.request) {
      if (consumed.state.phase === "expired") {
        onLiveMessage(t("portableServices.import.error.planUnavailable"));
      } else if (consumed.state.phase === "error") {
        onLiveMessage(t(consumed.state.errorKey));
      }
      return;
    }
    let result;
    try {
      result = await runWithImportControl(
        consumed.request.controlEpoch,
        (token) => applyLocalPortableImport(
          card.component,
          consumed.request?.planId as string,
          consumed.request?.planDigest as string,
          token,
        ),
      );
    } catch (error) {
      const errorKey = portableImportFailureKey(error);
      restoreImportFocusRef.current = true;
      updateImportState(failPortableImport(importStateRef.current, errorKey));
      onLiveMessage(t(errorKey));
      return;
    }
    updateImportState(completePortableImport(importStateRef.current, result));
    onLiveMessage(t("portableServices.import.success", {
      copied: result.copied_user_files,
      reused: result.reused_assets.length,
      skipped: result.skipped_assets.length,
      present: result.already_present.length,
    }));
    await Promise.allSettled([onReload(), onServicesStatusRefresh()]);
  }, [card.component, getControlEpoch, onLiveMessage, onReload, onServicesStatusRefresh, runWithImportControl, t, updateImportState]);

  const cancelImport = useCallback(() => {
    abortPlanningAttempt();
    restoreImportFocusRef.current = true;
    updateImportState(resetPortableImport(importStateRef.current));
    onLiveMessage(t("portableServices.import.cancelled"));
  }, [abortPlanningAttempt, onLiveMessage, t, updateImportState]);

  const runAction = useCallback(async (action: PortableServiceAction) => {
    if (controlsBusy) return;
    const manualProxy = action === "repair" && shouldRevealManualProxy(errorCode)
      ? proxyUrl
      : undefined;
    if (manualProxy && !validatePortableProxyUrl(manualProxy)) {
      setErrorDetail(t("portableServices.manualProxyInvalid"));
      onLiveMessage(t("portableServices.manualProxyInvalid"));
      return;
    }
    setPendingAction(action);
    setErrorCode(undefined);
    setErrorDetail("");
    let keepBusy = false;
    try {
      const response = await runWithControl((token) => portableServiceAction(
        card.component,
        action,
        token,
        manualProxy ? { proxy_url: manualProxy } : {},
      ));
      if (action === "start") {
        abortPlanningAttempt();
        updateImportState(resetPortableImport(importStateRef.current));
      }
      const nextPhase = portablePhaseAfterAction(action, response);
      if (action === "start" && response.operation_id) {
        cursorRef.current = 0;
        setEvents([]);
        setLogsOpen(true);
        setOperationId(response.operation_id);
        setOperationPhase(nextPhase);
      } else if (action === "stop" || action === "repair") {
        const terminalFromResponse = action === "stop"
          ? response.status === "stopped"
          : response.status === "completed";
        if (!terminalFromResponse && !response.action_id) {
          throw new PortableApiError(
            502,
            "LOCAL_CONTROL_INVALID_RESPONSE",
            "Portable action response did not include an action_id",
          );
        }
        setOperationPhase(nextPhase);
        setConvergence({
          action,
          actionId: response.action_id ?? null,
          terminalFromResponse,
        });
        keepBusy = true;
      } else {
        if (action === "start") setOperationId(null);
        setOperationPhase(nextPhase);
        onLiveMessage(t(`portableServices.action.${action === "open-folder" ? "openFolder" : action}`));
        await Promise.all([onReload(), onServicesStatusRefresh()]);
      }
    } catch (error) {
      const view = errorView(error);
      setErrorCode(view.code);
      setErrorDetail(view.detail);
      onLiveMessage(t(portableErrorMessageKey(view.code)));
    } finally {
      if (!keepBusy) setPendingAction(null);
    }
  }, [abortPlanningAttempt, card.component, controlsBusy, errorCode, onLiveMessage, onReload, onServicesStatusRefresh, proxyUrl, runWithControl, t, updateImportState]);

  const toggleLogs = useCallback(async () => {
    const nextOpen = !logsOpen;
    setLogsOpen(nextOpen);
    if (!nextOpen || !operationId) return;
    try {
      const page = await runWithControl((token) => fetchPortableOperationLogs(card.component, operationId, token, cursorRef.current, 100));
      cursorRef.current = page.next_seq;
      setEvents((current) => mergePortableEvents(current, page.events));
    } catch (error) {
      const view = errorView(error);
      setErrorCode(view.code);
      setErrorDetail(view.detail);
    }
  }, [card.component, logsOpen, operationId, runWithControl]);

  const cardActions = effectiveCard.actions;
  const phaseLabel = t(portablePhaseLabelKey(effectivePhase));
  const showImportAction = importState.phase === "idle" || importState.phase === "success";
  const showImportAvailability = showImportAction || importState.phase === "error" || importState.phase === "expired";
  const visibleProgress = shouldShowPortableLifecycleProgress(importState, lastProgress) ? lastProgress : undefined;

  return (
    <article className={`local-portable-card phase-${effectivePhase}`} data-portable-component={card.component}>
      <header className="local-portable-card-head">
        <div>
          <h3>{COMPONENT_NAMES[card.component]}</h3>
          <p id={statusId}>{phaseLabel}</p>
        </div>
        <span className="portable-status-badge" data-phase={effectivePhase}>{phaseLabel}</span>
      </header>

      <dl className="portable-service-facts">
        <div>
          <dt>{t("portableServices.path")}</dt>
          <dd title={card.service?.package_root ?? undefined}>{card.service?.package_root ?? t("portableServices.notConfiguredPath")}</dd>
        </div>
        {card.service?.base_url ? (
          <div>
            <dt>{t("portableServices.endpoint")}</dt>
            <dd title={card.service.base_url}>{card.service.base_url}</dd>
          </div>
        ) : null}
      </dl>

      {card.disabledReason ? (
        <p className="portable-disabled-reason" id={reasonId}>{t(`portableServices.disabled.${card.disabledReason}`)}</p>
      ) : controlsBusy ? (
        <p className="portable-busy-reason" id={reasonId}>{t("portableServices.disabled.busy")}</p>
      ) : null}

      {typeof visibleProgress === "number" ? (
        <div className="portable-progress-wrap">
          <progress max={100} value={visibleProgress} aria-label={t("portableServices.progress", { percent: Math.round(visibleProgress) })} />
          <span>{t("portableServices.progress", { percent: Math.round(visibleProgress) })}</span>
        </div>
      ) : null}

      <div className="portable-card-actions" aria-describedby={(card.disabledReason || controlsBusy) ? reasonId : undefined}>
        <PortableMutableControls
          actions={cardActions}
          locked={controlsBusy}
          lifecycleDisabled={card.disabledReason !== null}
          onBrowse={() => void chooseFolder()}
          onAction={(action) => void runAction(action)}
        />
        {showImportAction ? (
          <button
            ref={importTriggerRef}
            type="button"
            className="portable-import-action"
            onClick={() => void chooseImportSource()}
            disabled={!importEligibility.allowed}
            aria-describedby={!importEligibility.allowed ? importReasonId : undefined}
          >
            {t("portableServices.import.action")}
          </button>
        ) : null}
        {card.service?.base_url && cardActions.openService ? (
          <a href={card.service.base_url} target="_blank" rel="noreferrer">{t("portableServices.action.openService")}</a>
        ) : (
          <button type="button" disabled>{t("portableServices.action.openService")}</button>
        )}
        <button type="button" onClick={() => void toggleLogs()} disabled={!operationId || !cardActions.logs} aria-expanded={logsOpen}>
          {t(logsOpen ? "portableServices.action.closeLogs" : "portableServices.action.logs")}
        </button>
      </div>

      {showImportAvailability && !importEligibility.allowed && importEligibility.reason ? (
        <p className="portable-import-disabled" id={importReasonId}>
          {t(`portableServices.import.disabled.${importEligibility.reason}`)}
        </p>
      ) : null}

      <PortableImportInline
        state={importState}
        onConfirm={() => void confirmImport()}
        onCancel={cancelImport}
        onRetry={() => void chooseImportSource()}
        retryDisabled={!importEligibility.allowed}
        retryDescribedBy={importReasonId}
        confirmButtonRef={importConfirmRef}
        retryButtonRef={importTriggerRef}
      />

      {lastEvent ? <p className="portable-next-action"><strong>{t("portableServices.nextAction")}:</strong> {lastEvent.message}</p> : null}

      {(errorCode || errorDetail || operationId || card.service?.build_id) ? (
        <details className="portable-technical-details">
          <summary>{t("portableServices.operationDetails")}</summary>
          <dl>
            {errorCode ? <div><dt>error code</dt><dd>{errorCode}</dd></div> : null}
            {operationId ? <div><dt>operation</dt><dd>{operationId}</dd></div> : null}
            {card.service?.build_id ? <div><dt>build</dt><dd>{card.service.build_id}</dd></div> : null}
          </dl>
          {errorDetail ? <p>{errorDetail}</p> : null}
        </details>
      ) : null}

      {errorCode ? <p className="portable-error" role="alert">{t(portableErrorMessageKey(errorCode))}</p> : null}
      {shouldRevealManualProxy(errorCode) ? (
        <div className="portable-proxy-guidance">
          <p>{t("portableServices.manualProxyGuidance")}</p>
          <label htmlFor={`portable-proxy-${card.component}`}>{t("portableServices.manualProxyLabel")}</label>
          <input
            id={`portable-proxy-${card.component}`}
            type="password"
            value={proxyUrl}
            onChange={(event) => setProxyUrl(event.target.value)}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            placeholder="http://127.0.0.1:10808"
          />
        </div>
      ) : null}

      {logsOpen ? (
        <section className="portable-event-log" aria-labelledby={`portable-logs-${card.component}`}>
          <h4 id={`portable-logs-${card.component}`}>{t("portableServices.logsTitle")}</h4>
          {events.length ? (
            <ol>
              {events.map((event) => (
                <li key={event.seq}>
                  <span>{event.seq}</span>
                  <strong>{t(portablePhaseLabelKey(event.phase))}</strong>
                  <p>{event.message}</p>
                </li>
              ))}
            </ol>
          ) : <p>{t("portableServices.noLogs")}</p>}
        </section>
      ) : null}
    </article>
  );
});

export function LocalPortableServicesPanel({ initialServices = [], onServicesStatusRefresh }: LocalPortableServicesPanelProps) {
  const { t } = useTranslation();
  const [services, setServices] = useState<LocalPortableService[]>(initialServices);
  const [runtimeStatuses, setRuntimeStatuses] = useState<PortableRuntimeStatus[]>([]);
  const [loading, setLoading] = useState(initialServices.length === 0);
  const [loadError, setLoadError] = useState("");
  const [liveMessage, setLiveMessage] = useState("");
  const [controlEpoch, setControlEpoch] = useState(0);
  const tokenRef = useRef<string | null>(null);
  const tokenPromiseRef = useRef<Promise<string> | null>(null);
  const tokenEpochRef = useRef(0);
  const snapshotGenerationRef = useRef(0);
  const statusRefreshRef = useRef(onServicesStatusRefresh);

  useEffect(() => {
    statusRefreshRef.current = onServicesStatusRefresh;
  }, [onServicesStatusRefresh]);

  const refreshServicesStatus = useCallback(() => statusRefreshRef.current(), []);

  const acquireToken = useCallback(async (force: boolean): Promise<string> => {
    if (force) tokenRef.current = null;
    if (tokenRef.current) return tokenRef.current;
    if (!tokenPromiseRef.current) {
      tokenPromiseRef.current = fetchLocalControlToken()
        .then((token) => {
          tokenRef.current = token;
          tokenEpochRef.current += 1;
          setControlEpoch(tokenEpochRef.current);
          return token;
        })
        .finally(() => {
          tokenPromiseRef.current = null;
        });
    }
    return tokenPromiseRef.current;
  }, []);

  const runWithControl = useCallback(async function runWithMemoryToken<T>(run: (token: string) => Promise<T>): Promise<T> {
    return withControlTokenRetry(acquireToken, run);
  }, [acquireToken]);

  const getControlEpoch = useCallback(() => tokenEpochRef.current, []);

  const runWithImportControl = useCallback(async function runWithBoundMemoryToken<T>(
    expectedEpoch: number,
    run: (token: string) => Promise<T>,
  ): Promise<T> {
    return withPortableImportControlEpoch(acquireToken, expectedEpoch, getControlEpoch, run);
  }, [acquireToken, getControlEpoch]);

  const runWithImportPlanControl = useCallback(async function runWithPlanMemoryToken<T>(
    run: (token: string, controlEpoch: number) => Promise<T>,
  ): Promise<{ value: T; controlEpoch: number }> {
    return withPortableImportPlanControlEpoch(acquireToken, getControlEpoch, run);
  }, [acquireToken, getControlEpoch]);

  const refreshSnapshot = useCallback(async (signal?: AbortSignal): Promise<PortableWorkbenchSnapshot> => {
    const generation = ++snapshotGenerationRef.current;
    const [local, status] = await Promise.all([
      runWithControl((token) => fetchLocalPortableServices(token, signal)),
      fetchServicesStatus(signal),
    ]);
    const snapshot = { services: local.services, runtimes: status.services };
    if (generation === snapshotGenerationRef.current) {
      setServices(snapshot.services);
      setRuntimeStatuses(snapshot.runtimes);
      setLoadError("");
    }
    return snapshot;
  }, [runWithControl]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      await refreshSnapshot();
    } catch (error) {
      const view = errorView(error);
      setLoadError(t(portableErrorMessageKey(view.code)));
    } finally {
      setLoading(false);
    }
  }, [refreshSnapshot, t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const cards = useMemo(
    () => portableServiceCards(services, { runtimes: runtimeStatuses }),
    [runtimeStatuses, services],
  );

  return (
    <section className="local-portable-services-panel" aria-labelledby="local-portable-services-title">
      <header className="local-portable-services-head">
        <div>
          <h2 id="local-portable-services-title">{t("portableServices.title")}</h2>
          <p>{t("portableServices.description")}</p>
        </div>
        <button type="button" onClick={() => void reload()} disabled={loading}>{t("actions.refresh")}</button>
      </header>
      <div className="portable-panel-live" aria-live="polite" aria-atomic="true">
        {liveMessage || (loading ? t("portableServices.loading") : loadError)}
      </div>
      <div className="local-portable-services-grid">
        {cards.map((card) => (
          <LocalPortableServiceCard
            key={card.component}
            card={card}
            runtimeStatuses={runtimeStatuses}
            runWithControl={runWithControl}
            runWithImportControl={runWithImportControl}
            runWithImportPlanControl={runWithImportPlanControl}
            controlEpoch={controlEpoch}
            getControlEpoch={getControlEpoch}
            onReload={refreshSnapshot}
            onServicesStatusRefresh={refreshServicesStatus}
            onLiveMessage={setLiveMessage}
          />
        ))}
      </div>
    </section>
  );
}
