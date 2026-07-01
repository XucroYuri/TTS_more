import { describe, expect, it } from "vitest";

import { parserProviderKeyState, toParserProviderSavePayload } from "./parserConfig";
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
});
