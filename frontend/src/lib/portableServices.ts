import { PortableApiError } from "../api";
export { validatePortableProxyUrl } from "./portableProxy";
import type {
  CatalogProvider,
  LocalPortableService,
  PortableImportApplyResponse,
  PortableImportPlanResponse,
  PortableImportPlanResult,
  PortableOperationEvent,
  PortableOperationLogsResponse,
  PortableOperationPhase,
  PortableOperationResponse,
  PortableServiceAction,
  WorkerHealth,
} from "../types";

export const PORTABLE_COMPONENTS = ["gpt-sovits", "indextts", "cosyvoice"] as const;
export const ACTIVE_PORTABLE_PHASES = new Set<PortableOperationPhase>([
  "not_initialized",
  "checking",
  "downloading",
  "installing",
  "validating",
  "starting",
]);
export const TERMINAL_PORTABLE_PHASES = new Set<PortableOperationPhase>([
  "ready",
  "stopped",
  "repairable",
  "blocked",
]);
export const PORTABLE_STOP_CONVERGENCE_TIMEOUT_MS = 2 * 60 * 1000;
export const PORTABLE_REPAIR_CONVERGENCE_TIMEOUT_MS = 6 * 60 * 60 * 1000;
const MAX_TERMINAL_LOG_EVENTS = 500;
const MAX_TERMINAL_LOG_PAGES = 6;
export type PortableUiPhase = PortableOperationPhase | "not_configured" | "stopping" | "repairing";
export const ACTIVE_PORTABLE_UI_PHASES = new Set<PortableUiPhase>([
  ...ACTIVE_PORTABLE_PHASES,
  "stopping",
  "repairing",
]);

export type PortableDisabledReason = "lan" | "external" | "incompatible" | null;

export interface PortableCardActions {
  browse: boolean;
  start: boolean;
  stop: boolean;
  repair: boolean;
  openFolder: boolean;
  openService: boolean;
  logs: boolean;
}

export interface PortableServiceCard {
  component: CatalogProvider;
  service: LocalPortableService | null;
  status: PortableUiPhase;
  actions: PortableCardActions;
  disabledReason: PortableDisabledReason;
}

export type PortableCardInput = Partial<LocalPortableService> & {
  component: CatalogProvider;
  status?: PortableOperationPhase | "not_configured";
};

type BusyPhases = Partial<Record<CatalogProvider, PortableUiPhase | undefined>>;

export type PortableRuntimeStatus = Pick<
  WorkerHealth,
  "service_id" | "ready" | "supervisor_state" | "can_start" | "state"
>;

export interface PortableServiceCardOptions {
  phases?: BusyPhases;
  runtimes?: PortableRuntimeStatus[];
}

export type PortableImportDisabledReason =
  | "busy"
  | "unconfigured"
  | "lan"
  | "external"
  | "incompatible"
  | "notInstalled"
  | "running"
  | "runtimeUnknown";

export interface PortableImportIdentity {
  serviceId: string;
  packageId: string;
  buildId: string;
  runtimeState: "stopped";
}

export interface PortableImportSummary {
  userFileCount: number;
  userBytes: number;
  reusableAssetCount: number;
  reusableAssetBytes: number;
  skippedAssetCount: number;
  alreadyPresentCount: number;
  assetNames: string[];
}

interface PortableImportPendingPlan {
  planId: string;
  planDigest: string;
  expiresAtMs: number;
  controlEpoch: number;
  attemptNonce: symbol;
  identity: PortableImportIdentity;
}

export type PortableImportState =
  | { phase: "idle"; notice?: "cancelled" }
  | { phase: "planning"; identity: PortableImportIdentity; attemptNonce: symbol; controlEpoch: number }
  | { phase: "awaiting-confirmation"; summary: PortableImportSummary; pending: PortableImportPendingPlan }
  | { phase: "applying"; summary: PortableImportSummary }
  | { phase: "success"; result: PortableImportApplyResponse }
  | { phase: "error"; errorKey: string }
  | { phase: "expired" };

export function portableImportEligibility(
  service: LocalPortableService | null,
  runtime: PortableRuntimeStatus | undefined,
  busy = false,
): { allowed: boolean; reason: PortableImportDisabledReason | null } {
  if (busy) return { allowed: false, reason: "busy" };
  if (!service) return { allowed: false, reason: "unconfigured" };
  if (service.network_scope === "lan") return { allowed: false, reason: "lan" };
  if (!service.managed) {
    return {
      allowed: false,
      reason: service.mode === "external" || !service.package_root ? "external" : "incompatible",
    };
  }
  if (
    service.mode !== "local"
    || service.network_scope !== "localhost"
    || !service.service_id
    || !service.package_id
    || !service.package_root
    || !service.build_id
  ) {
    return { allowed: false, reason: "incompatible" };
  }
  if (service.setup_state !== "ready") return { allowed: false, reason: "notInstalled" };
  if (!runtime || runtime.service_id !== service.service_id || !runtime.supervisor_state) {
    return { allowed: false, reason: "runtimeUnknown" };
  }
  if (runtime.supervisor_state === "stopped") return { allowed: true, reason: null };
  if (runtime.supervisor_state === "running") return { allowed: false, reason: "running" };
  if (["starting", "stopping", "repairing"].includes(runtime.supervisor_state)) {
    return { allowed: false, reason: "busy" };
  }
  return { allowed: false, reason: "runtimeUnknown" };
}

export function portableImportIdentity(
  service: LocalPortableService | null,
  runtime: PortableRuntimeStatus | undefined,
): PortableImportIdentity | null {
  if (
    !service?.service_id
    || !service.package_id
    || !service.build_id
    || runtime?.service_id !== service.service_id
    || runtime.supervisor_state !== "stopped"
  ) return null;
  return {
    serviceId: service.service_id,
    packageId: service.package_id,
    buildId: service.build_id,
    runtimeState: "stopped",
  };
}

export function initialPortableImportState(): PortableImportState {
  return { phase: "idle" };
}

export function beginPortableImport(
  state: PortableImportState,
  identity: PortableImportIdentity,
): PortableImportState {
  return beginPortableImportAttempt(state, identity, Symbol("portable import attempt"), 0);
}

export function beginPortableImportAttempt(
  state: PortableImportState,
  identity: PortableImportIdentity,
  attemptNonce: symbol,
  controlEpoch: number,
): PortableImportState {
  if (portableImportLocksCard(state)) return state;
  return { phase: "planning", identity, attemptNonce, controlEpoch };
}

export function rebindPortableImportPlanningEpoch(
  state: PortableImportState,
  attemptNonce: symbol,
  controlEpoch: number,
): PortableImportState {
  if (state.phase !== "planning" || state.attemptNonce !== attemptNonce) return state;
  if (state.controlEpoch === controlEpoch) return state;
  return { ...state, controlEpoch };
}

export function receivePortableImportPlan(
  state: PortableImportState,
  response: PortableImportPlanResult,
  nowMs: number,
  controlEpoch: number,
): PortableImportState {
  if (state.phase !== "planning") return state;
  const rebound = rebindPortableImportPlanningEpoch(state, state.attemptNonce, controlEpoch);
  return receivePortableImportPlanAttempt(rebound, response, nowMs, controlEpoch, state.attemptNonce);
}

export function receivePortableImportPlanAttempt(
  state: PortableImportState,
  response: PortableImportPlanResult,
  nowMs: number,
  controlEpoch: number,
  attemptNonce: symbol,
): PortableImportState {
  if (
    state.phase !== "planning"
    || state.attemptNonce !== attemptNonce
    || state.controlEpoch !== controlEpoch
  ) return state;
  if ("status" in response) return { phase: "idle", notice: "cancelled" };
  if (response.expires_in_seconds <= 0) return { phase: "expired" };
  return {
    phase: "awaiting-confirmation",
    summary: safePortableImportSummary(response),
    pending: {
      planId: response.plan_id,
      planDigest: response.plan_digest,
      expiresAtMs: nowMs + response.expires_in_seconds * 1000,
      controlEpoch,
      attemptNonce,
      identity: state.identity,
    },
  };
}

export function consumePortableImportPlan(
  state: PortableImportState,
  nowMs: number,
  controlEpoch: number,
): {
  state: PortableImportState;
  request: { planId: string; planDigest: string; controlEpoch: number } | null;
} {
  if (state.phase !== "awaiting-confirmation") return { state, request: null };
  if (nowMs >= state.pending.expiresAtMs) return { state: { phase: "expired" }, request: null };
  if (controlEpoch !== state.pending.controlEpoch) {
    return {
      state: { phase: "error", errorKey: "portableServices.import.error.controlChanged" },
      request: null,
    };
  }
  return {
    state: { phase: "applying", summary: state.summary },
    request: {
      planId: state.pending.planId,
      planDigest: state.pending.planDigest,
      controlEpoch: state.pending.controlEpoch,
    },
  };
}

export function completePortableImport(
  state: PortableImportState,
  result: PortableImportApplyResponse,
): PortableImportState {
  if (state.phase !== "applying") return state;
  return { phase: "success", result };
}

export function failPortableImport(state: PortableImportState, errorKey: string): PortableImportState {
  if (state.phase === "idle" || state.phase === "success") return state;
  return { phase: "error", errorKey };
}

export function failPortableImportPlanAttempt(
  state: PortableImportState,
  attemptNonce: symbol,
  errorKey: string,
): PortableImportState {
  if (state.phase !== "planning" || state.attemptNonce !== attemptNonce) return state;
  return { phase: "error", errorKey };
}

function samePortableImportIdentity(
  expected: PortableImportIdentity,
  current: PortableImportIdentity | null,
): boolean {
  return Boolean(
    current
    && expected.serviceId === current.serviceId
    && expected.packageId === current.packageId
    && expected.buildId === current.buildId
    && current.runtimeState === "stopped"
  );
}

export function invalidatePortableImport(
  state: PortableImportState,
  current: PortableImportIdentity | null,
  controlEpoch?: number,
): PortableImportState {
  if (
    (state.phase === "planning" || state.phase === "awaiting-confirmation")
    && controlEpoch !== undefined
    && (state.phase === "planning" ? state.controlEpoch : state.pending.controlEpoch) !== controlEpoch
  ) {
    return { phase: "error", errorKey: "portableServices.import.error.controlChanged" };
  }
  const expected = state.phase === "planning"
    ? state.identity
    : state.phase === "awaiting-confirmation" ? state.pending.identity : null;
  if (!expected || samePortableImportIdentity(expected, current)) return state;
  return { phase: "error", errorKey: "portableServices.import.error.changed" };
}

export function expirePortableImportPlan(
  state: PortableImportState,
  attemptNonce: symbol,
  deadlineMs: number,
  expectedIdentity: PortableImportIdentity,
  nowMs: number,
): PortableImportState {
  if (
    state.phase !== "awaiting-confirmation"
    || state.pending.attemptNonce !== attemptNonce
    || state.pending.expiresAtMs !== deadlineMs
    || !samePortableImportIdentity(state.pending.identity, expectedIdentity)
    || nowMs < deadlineMs
  ) return state;
  return { phase: "expired" };
}

export function resetPortableImport(_state: PortableImportState): PortableImportState {
  return initialPortableImportState();
}

export function portableImportLocksCard(state: PortableImportState): boolean {
  return state.phase === "planning" || state.phase === "awaiting-confirmation" || state.phase === "applying";
}

export function shouldShowPortableLifecycleProgress(
  state: PortableImportState,
  progress: number | undefined,
): boolean {
  return typeof progress === "number" && !portableImportLocksCard(state);
}

const IMPORT_ERROR_KEYS: Record<string, string> = {
  LOCAL_CONTROL_IMPORT_PLAN_FAILED: "portableServices.import.error.planFailed",
  LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE: "portableServices.import.error.planUnavailable",
  LOCAL_CONTROL_IMPORT_BLOCKED: "portableServices.import.error.blocked",
  LOCAL_CONTROL_IMPORT_FAILED: "portableServices.import.error.failed",
};

export function portableImportErrorMessageKey(code: string | undefined): string {
  return (code && IMPORT_ERROR_KEYS[code]) || "portableServices.import.error.unknown";
}

export class PortableImportControlEpochError extends Error {
  constructor() {
    super("Portable import control epoch changed");
    this.name = "PortableImportControlEpochError";
  }
}

export async function withPortableImportControlEpoch<T>(
  acquireToken: (force: boolean) => Promise<string>,
  expectedEpoch: number,
  currentEpoch: () => number,
  run: (token: string) => Promise<T>,
): Promise<T> {
  if (currentEpoch() !== expectedEpoch) throw new PortableImportControlEpochError();
  const token = await acquireToken(false);
  if (currentEpoch() !== expectedEpoch) throw new PortableImportControlEpochError();
  return run(token);
}

export async function withPortableImportPlanControlEpoch<T>(
  acquireToken: (force: boolean) => Promise<string>,
  currentEpoch: () => number,
  run: (token: string, controlEpoch: number) => Promise<T>,
): Promise<{ value: T; controlEpoch: number }> {
  let usedEpoch: number | null = null;
  const value = await withControlTokenRetry(acquireToken, (token) => {
    usedEpoch = currentEpoch();
    return run(token, usedEpoch);
  });
  if (usedEpoch === null || currentEpoch() !== usedEpoch) throw new PortableImportControlEpochError();
  return { value, controlEpoch: usedEpoch };
}

function safeRelativeAssetName(value: string): boolean {
  if (!value || value.includes("\\") || value.includes(":")) return false;
  const parts = value.split("/");
  return !value.startsWith("/")
    && parts.every((part) => part !== "" && part !== "." && part !== "..")
    && !Array.from(value).some((character) => {
      const code = character.charCodeAt(0);
      return code < 32 || code === 127;
    });
}

export function safePortableImportSummary(plan: PortableImportPlanResponse): PortableImportSummary {
  return {
    userFileCount: plan.user_file_count,
    userBytes: plan.user_bytes,
    reusableAssetCount: plan.reusable_assets.length,
    reusableAssetBytes: plan.reusable_asset_bytes,
    skippedAssetCount: plan.skipped_assets.length,
    alreadyPresentCount: plan.already_present.length,
    assetNames: plan.reusable_assets.filter(safeRelativeAssetName).slice(0, 20),
  };
}

function servicePhase(service: PortableCardInput): PortableOperationPhase | "not_configured" {
  if (service.status) return service.status;
  switch (service.setup_state) {
    case "ready":
      return "ready";
    case "env_missing":
    case "partial":
    case "repo_missing":
    case "repo_found":
      return "repairable";
    case "endpoint_unreachable":
      return "stopped";
    default:
      return "not_configured";
  }
}

function disabledReason(service: PortableCardInput): PortableDisabledReason {
  if (service.managed) return null;
  if (service.network_scope === "lan") return "lan";
  if (service.mode === "external" || !service.package_root) return "external";
  return "incompatible";
}

function noActions(browse = false): PortableCardActions {
  return {
    browse,
    start: false,
    stop: false,
    repair: false,
    openFolder: false,
    openService: false,
    logs: false,
  };
}

export function portableServiceCards(
  services: PortableCardInput[],
  options: PortableServiceCardOptions = {},
): PortableServiceCard[] {
  const busyPhases = options.phases ?? {};
  const runtimesById = new Map(
    (options.runtimes ?? [])
      .filter((runtime) => typeof runtime.service_id === "string")
      .map((runtime) => [runtime.service_id as string, runtime]),
  );
  return PORTABLE_COMPONENTS.map((component) => {
    const matches = services.filter((service) => service.component === component);
    const service = matches.find((item) => item.managed && item.package_root) ?? matches.find((item) => item.package_root) ?? matches[0];
    if (!service) {
      return { component, service: null, status: "not_configured", actions: noActions(true), disabledReason: null };
    }
    const reason = disabledReason(service);
    const busy = busyPhases[component];
    const local = Boolean(service.managed && service.package_root);
    const installed = service.setup_state === "ready" || service.status === "ready" || service.status === "stopped";
    const runtime = service.service_id ? runtimesById.get(service.service_id) : undefined;
    const running = runtime?.supervisor_state === "running";
    const serviceReady = running && runtime?.ready === true;
    const status = busy ?? (
      local && installed
        ? (runtime ? (serviceReady ? "ready" : running ? "checking" : "stopped") : "checking")
        : servicePhase(service)
    );
    if (reason || (busy && ACTIVE_PORTABLE_UI_PHASES.has(busy))) {
      return {
        component,
        service: service as LocalPortableService,
        status,
        actions: noActions(false),
        disabledReason: reason,
      };
    }
    const startable = Boolean(local && installed && runtime && !running && runtime.can_start !== false);
    return {
      component,
      service: service as LocalPortableService,
      status,
      disabledReason: null,
      actions: {
        browse: true,
        start: startable,
        stop: Boolean(local && running),
        repair: local,
        openFolder: local,
        openService: Boolean(local && serviceReady && service.base_url),
        logs: local,
      },
    };
  });
}

const KNOWN_ERROR_KEYS: Record<string, string> = {
  ALL_LOCKED_SOURCES_EXHAUSTED: "portableServices.error.allSourcesExhausted",
  CUDA_PROBE_FAILED: "portableServices.error.cudaProbeFailed",
  DISK_SPACE_INSUFFICIENT: "portableServices.error.diskSpaceInsufficient",
  DOWNLOAD_NETWORK_INTERRUPTED: "portableServices.error.downloadInterrupted",
  LOCAL_CONTROL_FORBIDDEN: "portableServices.error.controlExpired",
  LOCAL_CONTROL_IDENTITY_MISMATCH: "portableServices.error.identityMismatch",
  LOCAL_CONTROL_INVALID_PACKAGE: "portableServices.error.invalidPackage",
  LOCAL_CONTROL_NOT_MANAGEABLE: "portableServices.error.notManageable",
  LOCAL_CONTROL_STATUS_TIMEOUT: "portableServices.error.statusTimeout",
  PORT_IN_USE: "portableServices.error.portInUse",
};

export function portablePhaseLabelKey(phase: PortableUiPhase): string {
  if (phase === "not_configured") return "portableServices.phase.notConfigured";
  const camel = phase.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
  return `portableServices.phase.${camel}`;
}

export function portableErrorMessageKey(code: string | undefined): string {
  return (code && KNOWN_ERROR_KEYS[code]) || "portableServices.error.unknown";
}

export function shouldRevealManualProxy(code: string | undefined): boolean {
  return code === "ALL_LOCKED_SOURCES_EXHAUSTED";
}

export function portablePhaseAfterAction(
  action: PortableServiceAction,
  response: { status: string; operation_id?: string },
): PortableUiPhase | null {
  if (action === "stop") return "stopping";
  if (action === "repair") return "repairing";
  if (action !== "start") return null;
  const phase = response.status as PortableOperationPhase;
  if (ACTIVE_PORTABLE_PHASES.has(phase) || TERMINAL_PORTABLE_PHASES.has(phase)) return phase;
  return "starting";
}

export async function withControlTokenRetry<T>(
  acquireToken: (force: boolean) => Promise<string>,
  run: (token: string) => Promise<T>,
): Promise<T> {
  const token = await acquireToken(false);
  try {
    return await run(token);
  } catch (error) {
    if (!(error instanceof PortableApiError) || error.status !== 403) throw error;
  }
  return run(await acquireToken(true));
}

export interface PortablePollSnapshot {
  phase: PortableOperationPhase;
  operation: PortableOperationResponse;
  events: PortableOperationEvent[];
  nextSeq: number;
}

export interface PortableOperationPoller {
  start(): void;
  resume(): void;
  stop(): void;
}

export interface PortableOperationPollerOptions {
  pollStatus(signal: AbortSignal): Promise<PortableOperationResponse>;
  pollLogs(afterSeq: number, signal: AbortSignal): Promise<PortableOperationLogsResponse>;
  onSnapshot(snapshot: PortablePollSnapshot): void;
  onTerminal(snapshot: PortablePollSnapshot): void | Promise<void>;
  onError?(error: unknown): void;
  isHidden?(): boolean;
  schedule?(callback: () => void, delay: number): ReturnType<typeof setTimeout> | number;
  clearSchedule?(handle: ReturnType<typeof setTimeout> | number): void;
}

const MAX_VISIBLE_PORTABLE_EVENTS = 200;

export function mergePortableEvents(
  current: PortableOperationEvent[],
  incoming: PortableOperationEvent[],
): PortableOperationEvent[] {
  if (incoming.length === 0) return current;
  const events = new Map(current.map((event) => [event.seq, event]));
  for (const event of incoming) events.set(event.seq, event);
  return [...events.values()]
    .sort((left, right) => left.seq - right.seq)
    .slice(-MAX_VISIBLE_PORTABLE_EVENTS);
}

export function createPortableOperationPoller(options: PortableOperationPollerOptions): PortableOperationPoller {
  const schedule = options.schedule ?? ((callback, delay) => setTimeout(callback, delay));
  const clearSchedule = options.clearSchedule ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  let stopped = false;
  let inFlight = false;
  let timer: ReturnType<typeof setTimeout> | number | undefined;
  let controller: AbortController | undefined;
  let nextSeq = 0;

  const cancelTimer = () => {
    if (timer !== undefined) clearSchedule(timer);
    timer = undefined;
  };

  const queueNext = () => {
    if (stopped) return;
    cancelTimer();
    timer = schedule(() => {
      timer = undefined;
      void tick();
    }, options.isHidden?.() ? 2000 : 500);
  };

  const tick = async () => {
    if (stopped || inFlight) return;
    inFlight = true;
    controller = new AbortController();
    try {
      const requestCursor = nextSeq;
      const [operation, firstLogs] = await Promise.all([
        options.pollStatus(controller.signal),
        options.pollLogs(nextSeq, controller.signal),
      ]);
      if (stopped) return;
      const phase = operation.operation?.status ?? operation.status;
      const events = [...firstLogs.events];
      let page = firstLogs;
      let pageCount = 1;
      if (page.events.length > 0 && page.next_seq <= requestCursor) {
        throw new Error("Portable operation log cursor did not advance");
      }
      if (events.length > MAX_TERMINAL_LOG_EVENTS) {
        throw new Error("Portable operation log exceeds the bounded event limit");
      }
      nextSeq = Math.max(nextSeq, page.next_seq);
      if (TERMINAL_PORTABLE_PHASES.has(phase)) {
        while (page.events.length > 0) {
          if (pageCount >= MAX_TERMINAL_LOG_PAGES) {
            throw new Error("Portable operation log exceeds the bounded page limit");
          }
          const cursor = nextSeq;
          page = await options.pollLogs(cursor, controller.signal);
          pageCount += 1;
          if (stopped) return;
          if (page.events.length > 0 && page.next_seq <= cursor) {
            throw new Error("Portable operation log cursor did not advance");
          }
          events.push(...page.events);
          if (events.length > MAX_TERMINAL_LOG_EVENTS) {
            throw new Error("Portable operation log exceeds the bounded event limit");
          }
          nextSeq = Math.max(nextSeq, page.next_seq);
        }
      }
      const snapshot = { phase, operation, events, nextSeq };
      options.onSnapshot(snapshot);
      if (TERMINAL_PORTABLE_PHASES.has(phase)) {
        stopped = true;
        await options.onTerminal(snapshot);
      }
    } catch (error) {
      if (!stopped && !(error instanceof DOMException && error.name === "AbortError")) {
        stopped = true;
        options.onError?.(error);
      }
    } finally {
      inFlight = false;
      controller = undefined;
      if (!stopped) queueNext();
    }
  };

  return {
    start() {
      if (stopped) return;
      void tick();
    },
    resume() {
      if (stopped) return;
      cancelTimer();
      if (!inFlight) void tick();
    },
    stop() {
      stopped = true;
      cancelTimer();
      controller?.abort();
    },
  };
}

export interface PortableActionConvergencePoller {
  start(): void;
  stop(): void;
}

export interface PortableActionConvergencePollerOptions {
  check(signal: AbortSignal): Promise<boolean>;
  onSettled(): void | Promise<void>;
  onTimeout(): void | Promise<void>;
  onError?(error: unknown): void;
  intervalMs?: number;
  timeoutMs?: number;
  now?(): number;
  schedule?(callback: () => void, delay: number): ReturnType<typeof setTimeout> | number;
  clearSchedule?(handle: ReturnType<typeof setTimeout> | number): void;
}

export function createPortableActionConvergencePoller(
  options: PortableActionConvergencePollerOptions,
): PortableActionConvergencePoller {
  const schedule = options.schedule ?? ((callback, delay) => setTimeout(callback, delay));
  const clearSchedule = options.clearSchedule ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  const now = options.now ?? Date.now;
  const intervalMs = options.intervalMs ?? 500;
  const timeoutMs = options.timeoutMs ?? PORTABLE_STOP_CONVERGENCE_TIMEOUT_MS;
  const startedAt = now();
  let stopped = false;
  let inFlight = false;
  let timer: ReturnType<typeof setTimeout> | number | undefined;
  let controller: AbortController | undefined;

  const stop = () => {
    stopped = true;
    if (timer !== undefined) clearSchedule(timer);
    timer = undefined;
    controller?.abort();
  };

  const queue = () => {
    if (stopped) return;
    timer = schedule(() => {
      timer = undefined;
      void tick();
    }, intervalMs);
  };

  const tick = async () => {
    if (stopped || inFlight) return;
    if (now() - startedAt >= timeoutMs) {
      stop();
      await options.onTimeout();
      return;
    }
    inFlight = true;
    controller = new AbortController();
    try {
      const settled = await options.check(controller.signal);
      if (stopped) return;
      if (settled) {
        stop();
        await options.onSettled();
      } else {
        queue();
      }
    } catch (error) {
      if (!stopped && !(error instanceof DOMException && error.name === "AbortError")) {
        stop();
        options.onError?.(error);
      }
    } finally {
      inFlight = false;
      controller = undefined;
    }
  };

  return {
    start() {
      if (!stopped) void tick();
    },
    stop,
  };
}
