import { describe, expect, it } from "vitest";

import type { Character, WorkerHealth } from "../types";
import {
  bindingCompleteness,
  catalogServiceOptions,
  roleLibraryBindingRows,
  roleLibraryDetailSelection,
  roleLibraryReferencePreview,
  roleLibraryServiceOptions,
  selectedCatalogServiceId,
} from "./roleLibraryView";

const services: WorkerHealth[] = [
  {
    service_id: "local-gpt",
    display_name: "Local GPT",
    engine: "gpt-sovits",
    provider_type: "gpt-sovits",
    api_contract: "gradio-gpt-sovits-webui",
    ready: true,
    base_url: "http://127.0.0.1:9872",
  },
  {
    service_id: "remote-index",
    display_name: "Remote Index",
    engine: "indextts",
    provider_type: "indextts",
    api_contract: "gradio-indextts2-webui",
    ready: true,
    base_url: "http://192.168.2.10:7860",
  },
  {
    service_id: "cosy-disabled",
    display_name: "Cosy Disabled",
    engine: "cosyvoice",
    provider_type: "cosyvoice",
    api_contract: "gradio-cosyvoice-webui",
    ready: false,
    enabled: false,
  },
  {
    service_id: "parser",
    display_name: "Parser",
    engine: "commercial",
    provider_type: "openai",
    ready: true,
    service_kind: "llm-parser",
  },
];

describe("role library view helpers", () => {
  it("derives TTS service options from the access configuration without hiding non-GPT providers", () => {
    const options = roleLibraryServiceOptions(services);

    expect(options.map((option) => option.serviceId)).toEqual(["local-gpt", "remote-index", "cosy-disabled"]);
    expect(options.find((option) => option.serviceId === "local-gpt")).toMatchObject({
      providerType: "gpt-sovits",
      apiContract: "gradio-gpt-sovits-webui",
      supportsModelCatalog: true,
      state: "ready",
    });
    expect(options.find((option) => option.serviceId === "remote-index")).toMatchObject({
      providerType: "indextts",
      supportsModelCatalog: false,
      state: "ready",
    });
    expect(options.find((option) => option.serviceId === "cosy-disabled")).toMatchObject({
      providerType: "cosyvoice",
      state: "disabled",
    });
  });

  it("uses only configured model-catalog services for catalog scans", () => {
    const options = catalogServiceOptions(services);

    expect(options.map((option) => option.serviceId)).toEqual(["local-gpt"]);
    expect(selectedCatalogServiceId("remote-index", options)).toBeNull();
    expect(selectedCatalogServiceId("local-gpt", options)).toBe("local-gpt");
  });

  it("summarizes every provider binding on a role instead of only GPT-SoVITS", () => {
    const character: Character = {
      id: "hero",
      name: "Hero",
      aliases: [],
      notes: "",
      fallback_profiles: [],
      profiles: [
        {
          id: "hero-gpt",
          name: "Hero GPT",
          engine: "gpt-sovits",
          service_id: "local-gpt",
          fallback_services: [],
          config: {},
          bindings: [
            {
              binding_id: "hero-gpt-binding",
              provider_type: "gpt-sovits",
              service_id: "local-gpt",
              fallback_services: [],
              capabilities: ["trained_weights_voice", "reference_audio_voice"],
              config: {
                logs_name: "hero-logs",
                gpt_weights_path: "GPT_weights/hero.ckpt",
                sovits_weights_path: "SoVITS_weights/hero.pth",
                ref_audio_path: "hero.wav",
                prompt_text: "走吧",
              },
            },
          ],
        },
        {
          id: "hero-index",
          name: "Hero Index",
          engine: "indextts",
          service_id: "remote-index",
          fallback_services: [],
          config: {},
          bindings: [
            {
              binding_id: "hero-index-binding",
              provider_type: "indextts",
              service_id: "remote-index",
              fallback_services: [],
              capabilities: ["reference_audio_voice"],
              config: {},
            },
          ],
        },
      ],
    };

    const rows = roleLibraryBindingRows(character, services);

    expect(rows.map((row) => row.providerType)).toEqual(["gpt-sovits", "indextts"]);
    expect(rows[0]).toMatchObject({ bindingId: "hero-gpt-binding", serviceLabel: "Local GPT", complete: true });
    expect(rows[1]).toMatchObject({ bindingId: "hero-index-binding", serviceLabel: "Remote Index", complete: false });
    expect(bindingCompleteness(rows[1].binding).missing).toEqual(["voice"]);
  });

  it("does not auto-open a global character detail when no explicit selection exists", () => {
    const characters: Character[] = [
      { id: "hero", name: "Hero", aliases: [], notes: "", fallback_profiles: [] },
      { id: "mentor", name: "Mentor", aliases: [], notes: "", fallback_profiles: [] },
    ];

    expect(roleLibraryDetailSelection({
      selectedCharacterId: null,
      filteredCharacters: characters,
      selectedCandidateId: null,
      selectedModelId: null
    })).toEqual({ kind: "empty" });

    expect(roleLibraryDetailSelection({
      selectedCharacterId: "mentor",
      filteredCharacters: characters,
      selectedCandidateId: null,
      selectedModelId: null
    })).toEqual({ kind: "library-character", characterId: "mentor" });
  });

  it("shows only a short reference preview by default", () => {
    const preview = roleLibraryReferencePreview([
      {
        id: "core",
        name: "Core samples",
        paths: [],
        samples: Array.from({ length: 7 }, (_, index) => ({ path: `refs/sample-${index + 1}.wav` }))
      }
    ]);

    expect(preview.visibleSamples.map((sample) => sample.path)).toEqual([
      "refs/sample-1.wav",
      "refs/sample-2.wav",
      "refs/sample-3.wav",
      "refs/sample-4.wav",
    ]);
    expect(preview.hiddenSampleCount).toBe(3);
    expect(preview.hasOverflow).toBe(true);
  });
});
