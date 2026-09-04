import { describe, expect, it } from "vitest";

import type { VoiceCatalogPublicView, VoiceResourceRecord } from "../../types";
import { voiceBlockerAction, voiceCatalogReady } from "./voiceReadiness";

const readyResource: VoiceResourceRecord = {
  resource_id: "voice-ready",
  character_id: "九九",
  character_aliases: [],
  reference_asset_ids: ["ref-1"],
  languages: ["zh"],
  generic_pool: false,
  confirmed: true,
  metadata_score: 5,
  engine_type: "gpt-sovits",
  state: "ready",
  service_id: "comfyui",
  weight_artifact_ids: [],
  mapping_origin: "plugin",
  fingerprint: "ready"
};

function catalog(resources: VoiceResourceRecord[]): VoiceCatalogPublicView {
  return {
    state: "partial",
    catalog_version: "catalog-v1",
    resources,
    references: [],
    diagnostics: [],
    counts: { resources: resources.length, references: 0, weights: 0 }
  };
}

describe("voice readiness", () => {
  it("uses ready resources instead of the aggregate catalog state", () => {
    expect(voiceCatalogReady(catalog([readyResource]))).toBe(true);
    expect(
      voiceCatalogReady(catalog([{ ...readyResource, state: "unavailable" }]))
    ).toBe(false);
    expect(voiceCatalogReady(null)).toBe(false);
  });

  it("maps blocker codes to one concrete recovery action", () => {
    expect(voiceBlockerAction("no_role_mapping")).toBeNull();
    expect(voiceBlockerAction("no_eligible_voice_candidate")).toBeNull();
    expect(voiceBlockerAction("voice_assets_unavailable")).toBe("sync-assets");
    expect(voiceBlockerAction("service_offline")).toBe("open-services");
    expect(voiceBlockerAction("unknown")).toBeNull();
  });
});
