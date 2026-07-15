import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  fetchLocalControlToken,
  fetchLocalPortableServices,
  fetchPortableOperation,
  fetchPortableOperationLogs,
  PortableApiError,
  portableServiceAction,
  registerLocalPortableService,
  selectLocalPortableFolder,
} from "../api";
import {
  ACTIVE_PORTABLE_PHASES,
  createPortableOperationPoller,
  portableErrorMessageKey,
  portablePhaseAfterAction,
  portablePhaseLabelKey,
  portableServiceCards,
  shouldRevealManualProxy,
  withControlTokenRetry,
  type PortableServiceCard,
} from "../lib/portableServices";
import type {
  LocalPortableService,
  PortableOperationEvent,
  PortableOperationPhase,
  PortableServiceAction,
} from "../types";

const COMPONENT_NAMES = {
  "gpt-sovits": "GPT-SoVITS",
  indextts: "IndexTTS",
  cosyvoice: "CosyVoice",
} as const;
const MAX_VISIBLE_EVENTS = 200;

type ControlRunner = <T>(run: (token: string) => Promise<T>) => Promise<T>;

export interface LocalPortableServicesPanelProps {
  initialServices?: LocalPortableService[];
  onServicesStatusRefresh: () => Promise<void>;
}

interface LocalPortableServiceCardProps {
  card: PortableServiceCard;
  runWithControl: ControlRunner;
  onReload: () => Promise<void>;
  onServicesStatusRefresh: () => Promise<void>;
  onLiveMessage: (message: string) => void;
}

function mergeEvents(current: PortableOperationEvent[], incoming: PortableOperationEvent[]): PortableOperationEvent[] {
  if (incoming.length === 0) return current;
  const events = new Map(current.map((event) => [event.seq, event]));
  for (const event of incoming) events.set(event.seq, event);
  return [...events.values()].sort((left, right) => left.seq - right.seq).slice(-MAX_VISIBLE_EVENTS);
}

function errorView(error: unknown): { code: string | undefined; detail: string } {
  if (error instanceof PortableApiError) return { code: error.code, detail: error.message };
  if (error instanceof Error && error.name !== "AbortError") return { code: undefined, detail: error.message };
  return { code: undefined, detail: "" };
}

const LocalPortableServiceCard = memo(function LocalPortableServiceCard({
  card,
  runWithControl,
  onReload,
  onServicesStatusRefresh,
  onLiveMessage,
}: LocalPortableServiceCardProps) {
  const { t } = useTranslation();
  const [pendingAction, setPendingAction] = useState<PortableServiceAction | "browse" | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [operationPhase, setOperationPhase] = useState<PortableOperationPhase | null>(null);
  const [events, setEvents] = useState<PortableOperationEvent[]>([]);
  const [logsOpen, setLogsOpen] = useState(false);
  const [errorCode, setErrorCode] = useState<string | undefined>();
  const [errorDetail, setErrorDetail] = useState("");
  const cursorRef = useRef(0);
  const effectivePhase = operationPhase ?? card.status;
  const effectiveCard = useMemo(() => {
    if (!operationPhase || !card.service) return card;
    return portableServiceCards([card.service], { [card.component]: operationPhase })
      .find((item) => item.component === card.component) ?? card;
  }, [card, operationPhase]);
  const operationBusy = operationPhase ? ACTIVE_PORTABLE_PHASES.has(operationPhase) : false;
  const controlsBusy = pendingAction !== null || operationBusy;
  const lastProgress = [...events].reverse().find((event) => typeof event.percent === "number")?.percent;
  const lastEvent = events.at(-1);
  const reasonId = `portable-disabled-${card.component}`;
  const statusId = `portable-status-${card.component}`;

  useEffect(() => {
    if (!operationId) setOperationPhase(null);
  }, [card.status, operationId]);

  useEffect(() => {
    if (!operationId) return;
    const poller = createPortableOperationPoller({
      pollStatus: (signal) => runWithControl((token) => fetchPortableOperation(card.component, operationId, token, signal)),
      pollLogs: (afterSeq, signal) => runWithControl((token) => fetchPortableOperationLogs(card.component, operationId, token, afterSeq, 100, signal)),
      onSnapshot: (snapshot) => {
        cursorRef.current = snapshot.nextSeq;
        setOperationPhase(snapshot.phase);
        setEvents((current) => mergeEvents(current, snapshot.events));
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
  }, [card.actions.browse, card.component, controlsBusy, onLiveMessage, onReload, onServicesStatusRefresh, runWithControl, t]);

  const runAction = useCallback(async (action: PortableServiceAction) => {
    if (controlsBusy) return;
    setPendingAction(action);
    setErrorCode(undefined);
    setErrorDetail("");
    try {
      const response = await runWithControl((token) => portableServiceAction(card.component, action, token));
      const nextPhase = portablePhaseAfterAction(action, response);
      if (action === "start" && response.operation_id) {
        cursorRef.current = 0;
        setEvents([]);
        setLogsOpen(true);
        setOperationId(response.operation_id);
        setOperationPhase(nextPhase);
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
      setPendingAction(null);
    }
  }, [card.component, controlsBusy, onLiveMessage, onReload, onServicesStatusRefresh, runWithControl, t]);

  const toggleLogs = useCallback(async () => {
    const nextOpen = !logsOpen;
    setLogsOpen(nextOpen);
    if (!nextOpen || !operationId) return;
    try {
      const page = await runWithControl((token) => fetchPortableOperationLogs(card.component, operationId, token, cursorRef.current, 100));
      cursorRef.current = page.next_seq;
      setEvents((current) => mergeEvents(current, page.events));
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
      {shouldRevealManualProxy(errorCode) ? <p className="portable-proxy-guidance">{t("portableServices.manualProxyGuidance")}</p> : null}

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
  const [loading, setLoading] = useState(initialServices.length === 0);
  const [loadError, setLoadError] = useState("");
  const [liveMessage, setLiveMessage] = useState("");
  const tokenRef = useRef<string | null>(null);
  const tokenPromiseRef = useRef<Promise<string> | null>(null);
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

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await runWithControl((token) => fetchLocalPortableServices(token));
      setServices(payload.services);
      setLoadError("");
    } catch (error) {
      const view = errorView(error);
      setLoadError(t(portableErrorMessageKey(view.code)));
    } finally {
      setLoading(false);
    }
  }, [runWithControl, t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const cards = useMemo(() => portableServiceCards(services), [services]);

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
            runWithControl={runWithControl}
            onReload={reload}
            onServicesStatusRefresh={refreshServicesStatus}
            onLiveMessage={setLiveMessage}
          />
        ))}
      </div>
    </section>
  );
}
