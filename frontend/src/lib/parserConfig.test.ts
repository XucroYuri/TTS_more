import { describe, expect, it } from "vitest";

import { createDefaultParserProviderDraft, parserProviderKeyState, toParserProviderSavePayload, upsertKwjmParserProvider } from "./parserConfig";
import type { ParserProviderDraft } from "../types";

const provider: ParserProviderDraft = {
  name: "openai-main",
  base_url: "https://api.openai.com/v1",
  api_key_env: "OPENAI_API_KEY",
  model: "gpt-4o-mini",
  enabled: true,
  timeout_seconds: 30,
  priority: 10,
  key_configured: true,
  api_key: "",
};

describe("parser provider config helpers", () => {
  it("does not send an empty api key while saving parser providers", () => {
    const payload = toParserProviderSavePayload([provider]);

    expect(payload.providers[0]).not.toHaveProperty("api_key");
  });

  it("sends a trimmed api key when the user enters one", () => {
    const payload = toParserProviderSavePayload([{ ...provider, api_key: "  sk-test  " }]);

    expect(payload.providers[0]).toHaveProperty("api_key", "sk-test");
  });

  it("reports parser provider key state", () => {
    expect(parserProviderKeyState(provider)).toBe("configured");
    expect(parserProviderKeyState({ ...provider, key_configured: false })).toBe("missing");
  });

  it("creates new parser providers from a generic openai-compatible template", () => {
    expect(createDefaultParserProviderDraft(2)).toEqual({
      name: "",
      base_url: "https://api.openai.com/v1",
      api_key_env: "",
      model: "gpt-4o-mini",
      enabled: true,
      timeout_seconds: 45,
      priority: 102,
      key_configured: false,
      api_key: "",
    });
  });

  it("activates the kwjm preset with a trimmed api key while preserving other providers", () => {
    const existingKwjm: ParserProviderDraft = {
      name: "开物基模",
      base_url: "",
      api_key_env: "OLD_KWJM_KEY",
      model: "old-model",
      enabled: false,
      timeout_seconds: 10,
      priority: 99,
      key_configured: false,
      api_key: "",
    };

    const result = upsertKwjmParserProvider([provider, existingKwjm], "  kwjm-secret  ");

    expect(result[0]).toEqual(provider);
    expect(result[1]).toEqual({
      name: "开物基模",
      base_url: "https://kwjm.com",
      api_key_env: "KWJM_API_KEY",
      model: "gpt-5.5",
      enabled: true,
      timeout_seconds: 45,
      priority: 200,
      key_configured: false,
      api_key: "kwjm-secret",
    });
  });

  it("creates the kwjm preset when no existing provider is present", () => {
    const result = upsertKwjmParserProvider([provider], "kwjm-secret");

    expect(result).toHaveLength(2);
    expect(result[1]).toMatchObject({
      name: "开物基模",
      base_url: "https://kwjm.com",
      api_key_env: "KWJM_API_KEY",
      model: "gpt-5.5",
      enabled: true,
      timeout_seconds: 45,
      priority: 200,
      api_key: "kwjm-secret",
    });
  });
});
