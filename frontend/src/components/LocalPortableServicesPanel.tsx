import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  fetchLocalControlToken,
  fetchLocalPortableServices,
  fetchPortableActionStatus,
  fetchPortableOperation,
  fetchPortableOperationLogs,
  fetchServicesStatus,
  PortableApiError,
  portableServiceAction,
  registerLocalPortableService,
  selectLocalPortableFolder,
} from "../api";
import {
  ACTIVE_PORTABLE_UI_PHASES,
  createPortableActionConvergencePoller,
  createPortableOperationPoller,
  mergePortableEvents,
  portableErrorMessageKey,
  portablePhaseAfterAction,
  portablePhaseLabelKey,
  portableServiceCards,
  PORTABLE_REPAIR_CONVERGENCE_TIMEOUT_MS,
  PORTABLE_STOP_CONVERGENCE_TIMEOUT_MS,
  shouldRevealManualProxy,
  validatePortableProxyUrl,
  withControlTokenRetry,
  type PortableServiceCard,
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
  onReload: (signal?: AbortSignal) => Promise<PortableWorkbenchSnapshot>;
  onServicesStatusRefresh: () => Promise<void>;
  onLiveMessage: (message: string) => void;
}

function errorView(error: unknown): { code: string | undefined; detail: string } {
  if (error instanceof PortableApiError) return { code: error.code, detail: error.message };
  if (error instanceof Error && error.name !== "AbortError") return { code: undefined, detail: error.message };
  return { code: undefined, detail: "" };
}

const LocalPortableServiceCard = memo(function LocalPortableServiceCard({
  card,
  runtimeStatuses,
  runWithControl,
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
  const controlsBusy = pendingAction !== null || operationBusy;
  const lastProgress = [...events].reverse().find((event) => typeof event.percent === "number")?.percent;
  const lastEvent = events.at(-1);
  const reasonId = `portable-disabled-${card.component}`;
  const statusId = `portable-status-${card.component}`;

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
  }, [card.component, controlsBusy, errorCode, onLiveMessage, onReload, onServicesStatusRefresh, proxyUrl, runWithControl, t]);

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
  const disableLifecycle = controlsBusy || card.disabledReason !== null;
  const phaseLabel = t(portablePhaseLabelKey(effectivePhase));

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

      {typeof lastProgress === "number" ? (
        <div className="portable-progress-wrap">
          <progress max={100} value={lastProgress} aria-label={t("portableServices.progress", { percent: Math.round(lastProgress) })} />
          <span>{t("portableServices.progress", { percent: Math.round(lastProgress) })}</span>
        </div>
      ) : null}

      <div className="portable-card-actions" aria-describedby={(card.disabledReason || controlsBusy) ? reasonId : undefined}>
        <button type="button" onClick={() => void chooseFolder()} disabled={controlsBusy || !cardActions.browse}>{t("portableServices.action.browse")}</button>
        <button type="button" onClick={() => void runAction("start")} disabled={disableLifecycle || !cardActions.start}>{t("portableServices.action.start")}</button>
        <button type="button" onClick={() => void runAction("stop")} disabled={disableLifecycle || !cardActions.stop}>{t("portableServices.action.stop")}</button>
        <button type="button" onClick={() => void runAction("repair")} disabled={disableLifecycle || !cardActions.repair}>{t("portableServices.action.repair")}</button>
        <button type="button" onClick={() => void runAction("open-folder")} disabled={disableLifecycle || !cardActions.openFolder}>{t("portableServices.action.openFolder")}</button>
        {card.service?.base_url && cardActions.openService ? (
          <a href={card.service.base_url} target="_blank" rel="noreferrer">{t("portableServices.action.openService")}</a>
        ) : (
          <button type="button" disabled>{t("portableServices.action.openService")}</button>
        )}
        <button type="button" onClick={() => void toggleLogs()} disabled={!operationId || !cardActions.logs} aria-expanded={logsOpen}>
          {t(logsOpen ? "portableServices.action.closeLogs" : "portableServices.action.logs")}
        </button>
      </div>

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
  const tokenRef = useRef<string | null>(null);
  const tokenPromiseRef = useRef<Promise<string> | null>(null);
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
            onReload={refreshSnapshot}
            onServicesStatusRefresh={refreshServicesStatus}
            onLiveMessage={setLiveMessage}
          />
        ))}
      </div>
    </section>
  );
}
