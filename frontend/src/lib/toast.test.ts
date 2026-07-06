import { describe, expect, it } from "vitest";
import { createToast, inferToastLevel, toastDuration, type ToastOptions } from "./toast";

describe("toast helpers", () => {
  it("creates a toast with incremental ids and default level", () => {
    const a = createToast("hello");
    const b = createToast("world");
    expect(a.id).toBeLessThan(b.id);
    expect(a.level).toBe("info");
    expect(a.message).toBe("hello");
  });

  it("respects an explicit level", () => {
    expect(createToast("oops", { level: "error" }).level).toBe("error");
    expect(createToast("careful", { level: "warning" }).level).toBe("warning");
  });

  it("infers error level from failure-related keys", () => {
    expect(inferToastLevel("notice.generationFailed")).toBe("error");
    expect(inferToastLevel("notice.preflightBlocked")).toBe("error");
    expect(inferToastLevel("empty.projectLoadFailed")).toBe("error");
  });

  it("infers success level from positive keys", () => {
    expect(inferToastLevel("notice.generated")).toBe("success");
    expect(inferToastLevel("notice.projectSaved")).toBe("success");
    expect(inferToastLevel("app.ready")).toBe("info");
  });

  it("infers warning level from risk/fallback keys", () => {
    expect(inferToastLevel("notice.preflightNeedsFallback")).toBe("warning");
    expect(inferToastLevel("confirm.revisionRisk")).toBe("warning");
  });

  it("returns the configured duration or the default", () => {
    expect(toastDuration()).toBe(5000);
    expect(toastDuration({ duration: 0 })).toBe(0);
    const opts: ToastOptions = { duration: 3000 };
    expect(toastDuration(opts)).toBe(3000);
  });
});
