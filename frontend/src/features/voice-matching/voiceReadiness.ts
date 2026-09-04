import type { VoiceCatalogPublicView } from "../../types";

export type VoiceBlockerAction =
  | "sync-assets"
  | "open-services"
  | null;

export function voiceCatalogReady(
  catalog: Pick<VoiceCatalogPublicView, "state" | "resources"> | null
): boolean {
  return Boolean(catalog?.resources.some((resource) => resource.state === "ready"));
}

export function voiceBlockerAction(code: string): VoiceBlockerAction {
  const actions: Record<string, Exclude<VoiceBlockerAction, null>> = {
    voice_assets_unavailable: "sync-assets",
    service_offline: "open-services"
  };
  return actions[code] ?? null;
}
