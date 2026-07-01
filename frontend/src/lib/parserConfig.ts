import type { ParserProviderDraft, ParserProvidersSavePayload } from "../types";

export type ParserProviderKeyState = "configured" | "missing";

export function parserProviderKeyState(provider: Pick<ParserProviderDraft, "key_configured">): ParserProviderKeyState {
  return provider.key_configured ? "configured" : "missing";
}

export function toParserProviderSavePayload(providers: ParserProviderDraft[]): ParserProvidersSavePayload {
  return {
    providers: providers.map(({ key_configured: _keyConfigured, api_key, ...provider }) => {
      const trimmedKey = api_key?.trim();
      return trimmedKey ? { ...provider, api_key: trimmedKey } : provider;
    }),
  };
}
