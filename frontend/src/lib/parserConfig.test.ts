import { describe, expect, it } from "vitest";

import { createDefaultParserProviderDraft, parserProviderKeyState, toParserProviderSavePayload } from "./parserConfig";
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

  it("creates new parser providers from the kwjm gpt-5.5 template", () => {
    expect(createDefaultParserProviderDraft(2)).toEqual({
      name: "开物基模",
      base_url: "",
      api_key_env: "KWJM_API_KEY",
      model: "gpt-5.5",
      enabled: true,
      timeout_seconds: 45,
      priority: 102,
      key_configured: false,
      api_key: "",
    });
  });
});
