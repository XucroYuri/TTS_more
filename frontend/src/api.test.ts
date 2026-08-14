import { beforeEach, describe, expect, it } from "vitest";

import { getApiToken, setApiToken } from "./api";

describe("api token storage", () => {
  // The token lives in a module-level variable, so tests share state.
  // Reset before each test to keep assertions deterministic.
  beforeEach(() => {
    setApiToken("");
  });

  it("returns empty string when no token is set", () => {
    expect(getApiToken()).toBe("");
  });

  it("stores and retrieves a token", () => {
    setApiToken("secret-abc");
    expect(getApiToken()).toBe("secret-abc");
  });

  it("clears the token when given an empty string", () => {
    setApiToken("secret-abc");
    setApiToken("");
    expect(getApiToken()).toBe("");
  });
});
