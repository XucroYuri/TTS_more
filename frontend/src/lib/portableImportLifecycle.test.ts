import { describe, expect, it } from "vitest";

import type { PortableImportPlanResponse } from "../types";
import * as portableModule from "./portableServices";

const identity = {
  serviceId: "portable-gpt",
  packageId: "gpt-main",
  buildId: "build-2",
  runtimeState: "stopped" as const,
};
type PortableIdentity = typeof identity;

const plan: PortableImportPlanResponse = {
  plan_id: "SECRET_PLAN_ID",
  plan_digest: "SECRET_PLAN_DIGEST",
  expires_in_seconds: 2,
  user_file_count: 1,
  user_bytes: 10,
  reusable_assets: ["models/base.bin"],
  reusable_asset_bytes: 20,
  skipped_assets: [],
  already_present: [],
  old_package_preserved: true,
};

type AttemptApi = {
  beginPortableImportAttempt?: (state: unknown, identity: PortableIdentity, nonce: symbol, controlEpoch: number) => any;
  rebindPortableImportPlanningEpoch?: (state: unknown, nonce: symbol, controlEpoch: number) => any;
  receivePortableImportPlanAttempt?: (
    state: unknown,
    response: PortableImportPlanResponse | { status: "cancelled" },
    nowMs: number,
    controlEpoch: number,
    nonce: symbol,
  ) => any;
  failPortableImportPlanAttempt?: (state: unknown, nonce: symbol, errorKey: string) => any;
  expirePortableImportPlan?: (
    state: unknown,
    nonce: symbol,
    deadlineMs: number,
    expectedIdentity: PortableIdentity,
    nowMs: number,
  ) => any;
  shouldShowPortableLifecycleProgress?: (state: unknown, progress: number | undefined) => boolean;
};

const attempts = portableModule as AttemptApi;

function requireAttemptApi(): Required<AttemptApi> {
  expect(attempts.beginPortableImportAttempt).toBeTypeOf("function");
  expect(attempts.rebindPortableImportPlanningEpoch).toBeTypeOf("function");
  expect(attempts.receivePortableImportPlanAttempt).toBeTypeOf("function");
  expect(attempts.failPortableImportPlanAttempt).toBeTypeOf("function");
  expect(attempts.expirePortableImportPlan).toBeTypeOf("function");
  expect(attempts.shouldShowPortableLifecycleProgress).toBeTypeOf("function");
  return attempts as Required<AttemptApi>;
}

describe("portable import attempt ownership", () => {
  it("accepts only the current nonce and token epoch for planning success, cancellation, and failure", () => {
    const api = requireAttemptApi();
    const firstNonce = Symbol("first import attempt");
    const secondNonce = Symbol("second import attempt");
    const first = api.beginPortableImportAttempt({ phase: "idle" }, identity, firstNonce, 7);
    expect(first).toMatchObject({ phase: "planning", attemptNonce: firstNonce, controlEpoch: 7 });

    const rebound = api.rebindPortableImportPlanningEpoch(first, firstNonce, 8);
    expect(rebound).toMatchObject({ phase: "planning", attemptNonce: firstNonce, controlEpoch: 8 });
    expect(api.rebindPortableImportPlanningEpoch(rebound, secondNonce, 9)).toBe(rebound);

    const second = api.beginPortableImportAttempt({ phase: "error", errorKey: "old" }, identity, secondNonce, 8);
    expect(api.receivePortableImportPlanAttempt(second, plan, 1000, 7, firstNonce)).toBe(second);
    expect(api.receivePortableImportPlanAttempt(second, { status: "cancelled" }, 1000, 7, firstNonce)).toBe(second);
    expect(api.failPortableImportPlanAttempt(second, firstNonce, "portableServices.import.error.planFailed")).toBe(second);

    const awaiting = api.receivePortableImportPlanAttempt(second, plan, 1000, 8, secondNonce);
    expect(awaiting).toMatchObject({
      phase: "awaiting-confirmation",
      pending: { attemptNonce: secondNonce, expiresAtMs: 3000, controlEpoch: 8, identity },
    });
    expect(api.receivePortableImportPlanAttempt(second, { status: "cancelled" }, 1000, 8, secondNonce)).toMatchObject({
      phase: "idle",
      notice: "cancelled",
    });
    expect(api.failPortableImportPlanAttempt(second, secondNonce, "portableServices.import.error.planFailed")).toMatchObject({
      phase: "error",
      errorKey: "portableServices.import.error.planFailed",
    });
  });

  it("expires only the matching pending identity/deadline and removes its sensitive plan immediately", () => {
    const api = requireAttemptApi();
    const nonce = Symbol("expiring import attempt");
    const planning = api.beginPortableImportAttempt({ phase: "idle" }, identity, nonce, 7);
    const awaiting = api.receivePortableImportPlanAttempt(planning, plan, 1000, 7, nonce);

    expect(api.expirePortableImportPlan(awaiting, Symbol("stale"), 3000, identity, 3000)).toBe(awaiting);
    expect(api.expirePortableImportPlan(awaiting, nonce, 2999, identity, 3000)).toBe(awaiting);
    expect(api.expirePortableImportPlan(awaiting, nonce, 3000, { ...identity, buildId: "changed" }, 3000)).toBe(awaiting);
    expect(api.expirePortableImportPlan(awaiting, nonce, 3000, identity, 2999)).toBe(awaiting);

    const expired = api.expirePortableImportPlan(awaiting, nonce, 3000, identity, 3000);
    expect(expired).toEqual({ phase: "expired" });
    expect(JSON.stringify(expired)).not.toContain("SECRET_PLAN_ID");
    expect(JSON.stringify(expired)).not.toContain("SECRET_PLAN_DIGEST");
  });

  it("hides stale lifecycle percentages throughout import-owned phases", () => {
    const { shouldShowPortableLifecycleProgress } = requireAttemptApi();
    expect(shouldShowPortableLifecycleProgress({ phase: "idle" }, 42)).toBe(true);
    expect(shouldShowPortableLifecycleProgress({ phase: "success", result: {} }, 42)).toBe(true);
    expect(shouldShowPortableLifecycleProgress({ phase: "planning" }, 42)).toBe(false);
    expect(shouldShowPortableLifecycleProgress({ phase: "awaiting-confirmation" }, 42)).toBe(false);
    expect(shouldShowPortableLifecycleProgress({ phase: "applying" }, 42)).toBe(false);
    expect(shouldShowPortableLifecycleProgress({ phase: "idle" }, undefined)).toBe(false);
  });
});
