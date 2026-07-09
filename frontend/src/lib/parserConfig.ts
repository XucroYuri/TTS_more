import type { ParserProviderDraft, ParserProvidersSavePayload } from "../types";

export type ParserProviderKeyState = "configured" | "missing";

export const KWJM_BASE_URL = "https://kwjm.com";
export const KWJM_BASE_URL_PLACEHOLDER = KWJM_BASE_URL;
export const KWJM_API_KEY_ENV = "KWJM_API_KEY";
export const KWJM_MODEL = "gpt-5.5";
export const KWJM_PROVIDER_NAME = "开物基模";

export function parserProviderKeyState(provider: Pick<ParserProviderDraft, "key_configured">): ParserProviderKeyState {
  return provider.key_configured ? "configured" : "missing";
}

export function createDefaultParserProviderDraft(index = 0): ParserProviderDraft {
  // Seed the "Add provider" button with a generic OpenAI-compatible template.
  // (开物基模/KWJM is no longer the primary default — it ships last in the
  // backend preset list as a project-specific fallback.)
  return {
    name: "",
    adapter: "openai-compatible",
    base_url: "https://api.openai.com/v1",
    api_key_env: "",
    model: "gpt-5.5",
    enabled: true,
    timeout_seconds: 45,
    priority: index > 0 ? 100 + index : 10,
    key_configured: false,
    api_key: "",
  };
}

export function upsertKwjmParserProvider(providers: ParserProviderDraft[], apiKey: string): ParserProviderDraft[] {
  const trimmedKey = apiKey.trim();
  const kwjmDraft: ParserProviderDraft = {
    name: KWJM_PROVIDER_NAME,
    adapter: "openai-compatible",
    base_url: KWJM_BASE_URL,
    api_key_env: KWJM_API_KEY_ENV,
    model: KWJM_MODEL,
    enabled: true,
    timeout_seconds: 45,
    priority: 200,
    key_configured: false,
    api_key: "",
  };
  let updatedKwjm = false;
  const nextProviders = providers.map((provider) => {
    if (updatedKwjm || !isKwjmProvider(provider)) return provider;
    updatedKwjm = true;
    return {
      ...kwjmDraft,
      key_configured: provider.key_configured,
      api_key: trimmedKey,
    };
  });
  if (!updatedKwjm) {
    nextProviders.push({ ...kwjmDraft, api_key: trimmedKey });
  }
  return nextProviders;
}

export function toParserProviderSavePayload(providers: ParserProviderDraft[]): ParserProvidersSavePayload {
  return {
    providers: providers.map(({ key_configured: _keyConfigured, api_key, ...provider }) => {
      const trimmedKey = api_key?.trim();
      return trimmedKey ? { ...provider, api_key: trimmedKey } : provider;
    }),
  };
}

function isKwjmProvider(provider: Pick<ParserProviderDraft, "name" | "api_key_env">): boolean {
  return provider.name.trim() === KWJM_PROVIDER_NAME || provider.api_key_env.trim() === KWJM_API_KEY_ENV;
}
