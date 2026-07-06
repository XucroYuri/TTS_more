import type { ParserProviderDraft, ParserProvidersSavePayload } from "../types";

export type ParserProviderKeyState = "configured" | "missing";

export const KWJM_BASE_URL_PLACEHOLDER = "https://your-domain.com/v1";
export const KWJM_API_KEY_ENV = "KWJM_API_KEY";
export const KWJM_MODEL = "gpt-5.5";
export const KWJM_PROVIDER_NAME = "开物基模";

export function parserProviderKeyState(provider: Pick<ParserProviderDraft, "key_configured">): ParserProviderKeyState {
  return provider.key_configured ? "configured" : "missing";
}

export function createDefaultParserProviderDraft(index: number): ParserProviderDraft {
  return {
    name: KWJM_PROVIDER_NAME,
    base_url: "",
    api_key_env: KWJM_API_KEY_ENV,
    model: KWJM_MODEL,
    enabled: true,
    timeout_seconds: 45,
    priority: 100 + index,
    key_configured: false,
    api_key: "",
  };
}

export function toParserProviderSavePayload(providers: ParserProviderDraft[]): ParserProvidersSavePayload {
  return {
    providers: providers.map(({ key_configured: _keyConfigured, api_key, ...provider }) => {
      const trimmedKey = api_key?.trim();
      return trimmedKey ? { ...provider, api_key: trimmedKey } : provider;
    }),
  };
}
