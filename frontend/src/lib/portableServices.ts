import { PortableApiError } from "../api";
import type {
  CatalogProvider,
  LocalPortableService,
  PortableOperationEvent,
  PortableOperationLogsResponse,
  PortableOperationPhase,
  PortableOperationResponse,
  PortableServiceAction,
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
  status: PortableOperationPhase | "not_configured";
  actions: PortableCardActions;
  disabledReason: PortableDisabledReason;
}

export type PortableCardInput = Partial<LocalPortableService> & {
  component: CatalogProvider;
  status?: PortableOperationPhase | "not_configured";
};

type BusyPhases = Partial<Record<CatalogProvider, PortableOperationPhase | undefined>>;

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
  busyPhases: BusyPhases = {},
): PortableServiceCard[] {
  return PORTABLE_COMPONENTS.map((component) => {
    const matches = services.filter((service) => service.component === component);
    const service = matches.find((item) => item.managed && item.package_root) ?? matches.find((item) => item.package_root) ?? matches[0];
    if (!service) {
      return { component, service: null, status: "not_configured", actions: noActions(true), disabledReason: null };
    }
    const reason = disabledReason(service);
    const busy = busyPhases[component];
    const status = busy ?? servicePhase(service);
    if (reason || (busy && ACTIVE_PORTABLE_PHASES.has(busy))) {
      return {
        component,
        service: service as LocalPortableService,
        status,
        actions: noActions(false),
        disabledReason: reason,
      };
    }
    const local = Boolean(service.managed && service.package_root);
    return {
      component,
      service: service as LocalPortableService,
      status,
      disabledReason: null,
      actions: {
        browse: true,
        start: local,
        stop: local && status === "ready",
        repair: local,
        openFolder: local,
        openService: local && Boolean(service.base_url),
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
  PORT_IN_USE: "portableServices.error.portInUse",
};

export function portablePhaseLabelKey(phase: PortableOperationPhase | "not_configured"): string {
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
): PortableOperationPhase | null {
  if (action === "stop") return "stopped";
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
      const [operation, logs] = await Promise.all([
        options.pollStatus(controller.signal),
        options.pollLogs(nextSeq, controller.signal),
      ]);
      if (stopped) return;
      nextSeq = Math.max(nextSeq, logs.next_seq);
      const phase = operation.operation?.status ?? operation.status;
      const snapshot = { phase, operation, events: logs.events, nextSeq };
      options.onSnapshot(snapshot);
      if (TERMINAL_PORTABLE_PHASES.has(phase)) {
        stopped = true;
        await options.onTerminal(snapshot);
      }
    } catch (error) {
      if (!stopped && !(error instanceof DOMException && error.name === "AbortError")) {
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
