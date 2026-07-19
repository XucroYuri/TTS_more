import { createElement, type ComponentType } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi } from "vitest";

import * as panelModule from "../components/LocalPortableServicesPanel";
import { PortableApiError } from "../api";
import { resources } from "../i18n";
import type {
  LocalPortableService,
  PortableImportApplyResponse,
  PortableImportPlanResponse,
} from "../types";
import * as portableModule from "./portableServices";

type ImportIdentity = {
  serviceId: string;
  packageId: string;
  buildId: string;
  runtimeState: "stopped";
};

type ImportState = {
  phase: "idle" | "planning" | "awaiting-confirmation" | "applying" | "success" | "error" | "expired";
  notice?: "cancelled";
  summary?: {
    userFileCount: number;
    userBytes: number;
    reusableAssetCount: number;
    reusableAssetBytes: number;
    skippedAssetCount: number;
    alreadyPresentCount: number;
    assetNames: string[];
  };
  pending?: { planId: string; planDigest: string; expiresAtMs: number; controlEpoch: number; identity: ImportIdentity };
  errorKey?: string;
  result?: PortableImportApplyResponse;
};

type ImportHelpers = {
  portableImportEligibility?: (
    service: LocalPortableService | null,
    runtime: { service_id?: string; ready: boolean; supervisor_state?: string } | undefined,
    busy?: boolean,
  ) => { allowed: boolean; reason: string | null };
  portableImportIdentity?: (
    service: LocalPortableService | null,
    runtime: { service_id?: string; ready: boolean; supervisor_state?: string } | undefined,
  ) => ImportIdentity | null;
  initialPortableImportState?: () => ImportState;
  beginPortableImport?: (state: ImportState, identity: ImportIdentity) => ImportState;
  receivePortableImportPlan?: (
    state: ImportState,
    response: PortableImportPlanResponse | { status: "cancelled" },
    nowMs: number,
    controlEpoch: number,
  ) => ImportState;
  consumePortableImportPlan?: (
    state: ImportState,
    nowMs: number,
    controlEpoch: number,
  ) => {
    state: ImportState;
    request: { planId: string; planDigest: string; controlEpoch: number } | null;
  };
  completePortableImport?: (state: ImportState, result: PortableImportApplyResponse) => ImportState;
  failPortableImport?: (state: ImportState, errorKey: string) => ImportState;
  invalidatePortableImport?: (state: ImportState, identity: ImportIdentity | null) => ImportState;
  resetPortableImport?: (state: ImportState) => ImportState;
  portableImportLocksCard?: (state: ImportState) => boolean;
  portableImportErrorMessageKey?: (code: string | undefined) => string;
  safePortableImportSummary?: (plan: PortableImportPlanResponse) => NonNullable<ImportState["summary"]>;
  withPortableImportControlEpoch?: <T>(
    acquireToken: (force: boolean) => Promise<string>,
    expectedEpoch: number,
    currentEpoch: () => number,
    run: (token: string) => Promise<T>,
  ) => Promise<T>;
  withPortableImportPlanControlEpoch?: <T>(
    acquireToken: (force: boolean) => Promise<string>,
    currentEpoch: () => number,
    run: (token: string) => Promise<T>,
  ) => Promise<{ value: T; controlEpoch: number }>;
};

const helpers = portableModule as ImportHelpers;

const service: LocalPortableService = {
  service_id: "portable-gpt",
  component: "gpt-sovits",
  package_id: "gpt-main",
  display_name: "GPT-SoVITS",
  base_url: "http://127.0.0.1:9880",
  mode: "local",
  network_scope: "localhost",
  managed: true,
  setup_state: "ready",
  package_root: "D:/Portable/GPT-SoVITS",
  build_id: "build-2",
  port_override: null,
};

const stoppedRuntime = {
  service_id: "portable-gpt",
  ready: false,
  supervisor_state: "stopped",
};

const plan: PortableImportPlanResponse = {
  plan_id: "SECRET_PLAN_ID",
  plan_digest: "SECRET_PLAN_DIGEST",
  expires_in_seconds: 1,
  user_file_count: 3,
  user_bytes: 2048,
  reusable_assets: [
    ...Array.from({ length: 22 }, (_, index) => `models/model-${index}.bin`),
    "C:/Users/private/model.bin",
  ],
  reusable_asset_bytes: 4096,
  skipped_assets: ["models/skipped.bin"],
  already_present: ["data/user/existing.json"],
  old_package_preserved: true,
};

const applied: PortableImportApplyResponse = {
  copied_user_files: 3,
  reused_assets: ["models/model-0.bin"],
  skipped_assets: ["models/skipped.bin"],
  already_present: ["data/user/existing.json"],
};

function requireHelpers() {
  expect(helpers.portableImportEligibility).toBeTypeOf("function");
  expect(helpers.portableImportIdentity).toBeTypeOf("function");
  expect(helpers.initialPortableImportState).toBeTypeOf("function");
  expect(helpers.beginPortableImport).toBeTypeOf("function");
  expect(helpers.receivePortableImportPlan).toBeTypeOf("function");
  expect(helpers.consumePortableImportPlan).toBeTypeOf("function");
  expect(helpers.completePortableImport).toBeTypeOf("function");
  expect(helpers.failPortableImport).toBeTypeOf("function");
  expect(helpers.invalidatePortableImport).toBeTypeOf("function");
  expect(helpers.resetPortableImport).toBeTypeOf("function");
  expect(helpers.portableImportLocksCard).toBeTypeOf("function");
  expect(helpers.portableImportErrorMessageKey).toBeTypeOf("function");
  expect(helpers.safePortableImportSummary).toBeTypeOf("function");
  expect(helpers.withPortableImportControlEpoch).toBeTypeOf("function");
  expect(helpers.withPortableImportPlanControlEpoch).toBeTypeOf("function");
  return helpers as Required<ImportHelpers>;
}

describe("portable import eligibility", () => {
  it("allows only an installed managed localhost worker whose runtime is strictly stopped", () => {
    const { portableImportEligibility } = requireHelpers();
    expect(portableImportEligibility(service, stoppedRuntime)).toEqual({ allowed: true, reason: null });
    expect(portableImportEligibility(service, stoppedRuntime, true)).toEqual({ allowed: false, reason: "busy" });
    expect(portableImportEligibility(service, { ...stoppedRuntime, supervisor_state: "running", ready: true })).toEqual({
      allowed: false,
      reason: "running",
    });
    expect(portableImportEligibility(service, { ...stoppedRuntime, supervisor_state: "starting" })).toEqual({
      allowed: false,
      reason: "busy",
    });
    expect(portableImportEligibility(service, { ...stoppedRuntime, supervisor_state: "stopping" })).toEqual({
      allowed: false,
      reason: "busy",
    });
    expect(portableImportEligibility(service, undefined)).toEqual({ allowed: false, reason: "runtimeUnknown" });
    expect(portableImportEligibility(null, undefined)).toEqual({ allowed: false, reason: "unconfigured" });
    expect(portableImportEligibility({ ...service, managed: false, mode: "external", network_scope: "lan" }, undefined)).toEqual({
      allowed: false,
      reason: "lan",
    });
    expect(portableImportEligibility({ ...service, managed: false, mode: "external" }, undefined)).toEqual({
      allowed: false,
      reason: "external",
    });
    expect(portableImportEligibility({ ...service, managed: false }, undefined)).toEqual({
      allowed: false,
      reason: "incompatible",
    });
    expect(portableImportEligibility({ ...service, setup_state: "partial" }, stoppedRuntime)).toEqual({
      allowed: false,
      reason: "notInstalled",
    });
  });
});

describe("portable import state machine", () => {
  it("handles picker cancellation, explicit confirmation, single-use apply, failure, and a fresh plan", () => {
    const api = requireHelpers();
    const identity = api.portableImportIdentity(service, stoppedRuntime);
    expect(identity).not.toBeNull();
    if (!identity) return;

    const idle = api.initialPortableImportState();
    const planning = api.beginPortableImport(idle, identity);
    expect(planning.phase).toBe("planning");
    const cancelled = api.receivePortableImportPlan(planning, { status: "cancelled" }, 1000, 7);
    expect(cancelled).toMatchObject({ phase: "idle", notice: "cancelled" });

    const awaiting = api.receivePortableImportPlan(api.beginPortableImport(cancelled, identity), plan, 1000, 7);
    expect(awaiting.phase).toBe("awaiting-confirmation");
    expect(awaiting.pending?.controlEpoch).toBe(7);
    const first = api.consumePortableImportPlan(awaiting, 1999, 7);
    expect(first.request).toEqual({
      planId: "SECRET_PLAN_ID",
      planDigest: "SECRET_PLAN_DIGEST",
      controlEpoch: 7,
    });
    expect(first.state.phase).toBe("applying");
    expect(first.state.pending).toBeUndefined();
    const duplicate = api.consumePortableImportPlan(first.state, 1999, 7);
    expect(duplicate.request).toBeNull();

    const failed = api.failPortableImport(first.state, "portableServices.import.error.failed");
    expect(failed).toMatchObject({ phase: "error", errorKey: "portableServices.import.error.failed" });
    expect(api.consumePortableImportPlan(failed, 1999, 7).request).toBeNull();
    expect(api.beginPortableImport(failed, identity).phase).toBe("planning");
    expect(api.completePortableImport(first.state, applied)).toMatchObject({ phase: "success", result: applied });
  });

  it("expires exactly at the deadline and discards plans after identity, runtime, token drift, and Start", async () => {
    const api = requireHelpers();
    const identity = api.portableImportIdentity(service, stoppedRuntime);
    expect(identity).not.toBeNull();
    if (!identity) return;

    const makeAwaiting = () => api.receivePortableImportPlan(
      api.beginPortableImport(api.initialPortableImportState(), identity),
      plan,
      1000,
      7,
    );
    expect(api.consumePortableImportPlan(makeAwaiting(), 1999, 7).request).not.toBeNull();
    const expired = api.consumePortableImportPlan(makeAwaiting(), 2000, 7);
    expect(expired.request).toBeNull();
    expect(expired.state.phase).toBe("expired");

    const changedEpoch = api.consumePortableImportPlan(makeAwaiting(), 1500, 8);
    expect(changedEpoch.request).toBeNull();
    expect(changedEpoch.state).toMatchObject({
      phase: "error",
      errorKey: "portableServices.import.error.controlChanged",
    });
    expect(changedEpoch.state.pending).toBeUndefined();

    for (const changed of [
      { ...identity, serviceId: "changed-service" },
      { ...identity, packageId: "changed-package" },
      { ...identity, buildId: "changed-build" },
      null,
    ]) {
      expect(api.invalidatePortableImport(makeAwaiting(), changed).phase).toBe("error");
    }
    expect(api.resetPortableImport(makeAwaiting())).toEqual({ phase: "idle" });
    expect(api.portableImportLocksCard(makeAwaiting())).toBe(true);
    expect(api.portableImportLocksCard({ phase: "applying" })).toBe(true);
    expect(api.portableImportLocksCard({ phase: "success", result: applied })).toBe(false);

    const run = vi.fn().mockRejectedValue(new PortableApiError(
      403,
      "LOCAL_CONTROL_FORBIDDEN",
      "token changed at C:/Users/private",
    ));
    const acquireToken = vi.fn().mockResolvedValue("epoch-seven-token");
    await expect(api.withPortableImportControlEpoch(acquireToken, 7, () => 7, run)).rejects.toMatchObject({
      status: 403,
      code: "LOCAL_CONTROL_FORBIDDEN",
    });
    expect(acquireToken).toHaveBeenCalledTimes(1);
    expect(acquireToken).toHaveBeenCalledWith(false);
    expect(run).toHaveBeenCalledTimes(1);

    const driftedAcquire = vi.fn().mockResolvedValue("new-token");
    const neverRun = vi.fn();
    await expect(api.withPortableImportControlEpoch(driftedAcquire, 7, () => 8, neverRun)).rejects.toMatchObject({
      name: "PortableImportControlEpochError",
    });
    expect(driftedAcquire).not.toHaveBeenCalled();
    expect(neverRun).not.toHaveBeenCalled();

    let planEpoch = 0;
    const acquirePlanToken = vi.fn(async (force: boolean) => {
      planEpoch = force ? 2 : 1;
      return force ? "fresh-plan-token" : "stale-plan-token";
    });
    const runPlan = vi.fn(async (token: string) => {
      if (token === "stale-plan-token") throw new PortableApiError(403, "LOCAL_CONTROL_FORBIDDEN", "expired");
      return { status: "cancelled" as const };
    });
    await expect(api.withPortableImportPlanControlEpoch(acquirePlanToken, () => planEpoch, runPlan)).resolves.toEqual({
      value: { status: "cancelled" },
      controlEpoch: 2,
    });
    expect(runPlan).toHaveBeenCalledTimes(2);

    planEpoch = 3;
    await expect(api.withPortableImportPlanControlEpoch(
      async () => "epoch-three-token",
      () => planEpoch,
      async () => {
        planEpoch = 4;
        return { status: "cancelled" as const };
      },
    )).rejects.toMatchObject({ name: "PortableImportControlEpochError" });
  });
});

describe("portable import safe presentation", () => {
  it("exposes counts and bytes, limits safe relative asset names to 20, and never includes secrets or absolute paths", () => {
    const { safePortableImportSummary } = requireHelpers();
    const summary = safePortableImportSummary(plan);
    expect(summary).toMatchObject({
      userFileCount: 3,
      userBytes: 2048,
      reusableAssetCount: 23,
      reusableAssetBytes: 4096,
      skippedAssetCount: 1,
      alreadyPresentCount: 1,
    });
    expect(summary.assetNames).toHaveLength(20);
    expect(summary.assetNames.every((name) => name.startsWith("models/"))).toBe(true);
    expect(JSON.stringify(summary)).not.toContain("SECRET_PLAN");
    expect(JSON.stringify(summary)).not.toContain("C:/Users/private");
  });

  it("maps import failures to fixed copy keys without returning backend detail", () => {
    const { portableImportErrorMessageKey } = requireHelpers();
    const backend = new PortableApiError(
      409,
      "LOCAL_CONTROL_IMPORT_BLOCKED",
      "private failure at C:/Users/private/package",
    );
    expect(portableImportErrorMessageKey(backend.code)).toBe("portableServices.import.error.blocked");
    expect(portableImportErrorMessageKey("LOCAL_CONTROL_IMPORT_PLAN_FAILED")).toBe("portableServices.import.error.planFailed");
    expect(portableImportErrorMessageKey("LOCAL_CONTROL_IMPORT_PLAN_UNAVAILABLE")).toBe("portableServices.import.error.planUnavailable");
    expect(portableImportErrorMessageKey("LOCAL_CONTROL_IMPORT_FAILED")).toBe("portableServices.import.error.failed");
    const fallback = portableImportErrorMessageKey("PRIVATE_NEW_CODE");
    expect(fallback).toBe("portableServices.import.error.unknown");
    expect(fallback).not.toContain(backend.message);
    expect(fallback).not.toContain("C:/Users/private");
  });

  it("renders bilingual inline confirmation and retry copy with status semantics but no plan secret", async () => {
    const api = requireHelpers();
    type InlineProps = {
      state: ImportState;
      onConfirm: () => void;
      onCancel: () => void;
    };
    const Inline = (panelModule as { PortableImportInline?: ComponentType<InlineProps> }).PortableImportInline;
    expect(Inline).toBeTypeOf("function");
    if (!Inline) return;
    const identity = api.portableImportIdentity(service, stoppedRuntime);
    expect(identity).not.toBeNull();
    if (!identity) return;
    const awaiting = api.receivePortableImportPlan(
      api.beginPortableImport(api.initialPortableImportState(), identity),
      plan,
      1000,
      7,
    );

    for (const language of ["zh-CN", "en-US"] as const) {
      const instance = createInstance();
      await instance.init({ lng: language, resources: { [language]: { translation: resources[language] } } });
      const render = (state: ImportState) => renderToStaticMarkup(createElement(
        I18nextProvider,
        { i18n: instance },
        createElement(Inline, { state, onConfirm: () => undefined, onCancel: () => undefined }),
      ));
      const confirmation = render(awaiting);
      const planning = render({ phase: "planning" });
      const applying = render({ phase: "applying" });
      const error = render({ phase: "error", errorKey: "portableServices.import.error.failed" });

      expect(confirmation).toContain("<details");
      expect(confirmation).not.toContain("SECRET_PLAN_ID");
      expect(confirmation).not.toContain("SECRET_PLAN_DIGEST");
      expect(confirmation).not.toContain("C:/Users/private");
      expect(planning).toContain("role=\"status\"");
      expect(applying).toContain("role=\"status\"");
      if (language === "zh-CN") {
        expect(confirmation).toContain("旧版本便携包将保留，绝不会删除或修改");
        expect(confirmation).toContain("服务必须保持已停止");
        expect(confirmation).toContain("确认并导入");
        expect(error).toContain("重新选择旧版本");
      } else {
        expect(confirmation).toContain("previous package is preserved and will never be deleted or modified");
        expect(confirmation).toContain("worker must remain stopped");
        expect(confirmation).toContain("Confirm import");
        expect(error).toContain("Choose previous version again");
      }
    }
  });

  it("structurally disables every state-changing card control during confirmation and apply", async () => {
    const api = requireHelpers();
    type MutableControlsProps = {
      actions: { browse: boolean; start: boolean; stop: boolean; repair: boolean; openFolder: boolean };
      locked: boolean;
      lifecycleDisabled: boolean;
      onBrowse: () => void;
      onAction: (action: "start" | "stop" | "repair" | "open-folder") => void;
    };
    const Controls = (panelModule as { PortableMutableControls?: ComponentType<MutableControlsProps> }).PortableMutableControls;
    expect(Controls).toBeTypeOf("function");
    if (!Controls) return;
    const instance = createInstance();
    await instance.init({ lng: "en-US", resources: { "en-US": { translation: resources["en-US"] } } });
    const render = (locked: boolean) => renderToStaticMarkup(createElement(
      I18nextProvider,
      { i18n: instance },
      createElement(Controls, {
        actions: { browse: true, start: true, stop: true, repair: true, openFolder: true },
        locked,
        lifecycleDisabled: false,
        onBrowse: () => undefined,
        onAction: () => undefined,
      }),
    ));
    const identity = api.portableImportIdentity(service, stoppedRuntime);
    expect(identity).not.toBeNull();
    if (!identity) return;
    const awaiting = api.receivePortableImportPlan(
      api.beginPortableImport(api.initialPortableImportState(), identity),
      plan,
      1000,
      7,
    );
    for (const state of [awaiting, { phase: "applying" } as ImportState]) {
      const html = render(api.portableImportLocksCard(state));
      expect(html.match(/<button/g)).toHaveLength(5);
      expect(html.match(/ disabled=""/g)).toHaveLength(5);
    }
  });
});
