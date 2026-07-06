export type ToastLevel = "info" | "success" | "warning" | "error";

export interface Toast {
  id: number;
  level: ToastLevel;
  message: string;
  createdAt: number;
}

export interface ToastOptions {
  level?: ToastLevel;
  /** Auto-dismiss after this many milliseconds. 0 keeps it until manually removed. */
  duration?: number;
}

const DEFAULT_DURATION_MS = 5000;

let nextToastId = 1;

export function createToast(message: string, options: ToastOptions = {}): Toast {
  return {
    id: nextToastId++,
    level: options.level ?? "info",
    message,
    createdAt: Date.now(),
  };
}

export function toastDuration(options: ToastOptions = {}): number {
  return options.duration ?? DEFAULT_DURATION_MS;
}

/**
 * Infer a toast level from an i18n key fragment.
 * Keys containing "fail", "error", "blocked" map to error; "warning"/"risk" to warning;
 * "saved"/"ready"/"generated"/"success" to success; otherwise info.
 */
export function inferToastLevel(keyOrMessage: string): ToastLevel {
  const lower = keyOrMessage.toLowerCase();
  if (/(failed|failure|error|blocked|invalid|missing|loadfailed)/.test(lower)) return "error";
  if (/(warn|risk|confirm|fallback|busy|needsaction)/.test(lower)) return "warning";
  if (/(saved|generated|success|complete|loaded|applied|copied|created)/.test(lower)) return "success";
  return "info";
}
