import { describe, expect, it } from "vitest";

import type { Character, ScriptLine } from "../types";
import { buildGenerationTask, lineEngine, lineProfile, lineServiceId } from "./routing";

const characters: Character[] = [
  {
    id: "alice",
    name: "Alice",
    aliases: [],
    notes: "",
    default_engine: "gpt-sovits",
    default_profile: "alice-gpt",
    fallback_profiles: [],
    profiles: [
      {
        id: "alice-gpt",
        name: "Alice GPT",
        engine: "gpt-sovits",
        service_id: "local-gpt-sovits",
        fallback_services: ["remote-gpt"],
        bindings: [
          {
            binding_id: "alice-gpt-binding",
            provider_type: "gpt-sovits",
            service_id: "local-gpt-sovits",
            fallback_services: ["remote-gpt"],
            capabilities: ["trained_weights_voice", "reference_audio_voice"],
            config: {
              ref_audio_path: "alice.wav",
              prompt_text: "reference prompt"
            }
          },
          {
            binding_id: "alice-openai",
            provider_type: "openai",
            service_id: "openai-tts",
            fallback_services: [],
            capabilities: ["commercial_voice", "style_instruction"],
            config: {
              voice: "alloy"
            }
          }
        ],
        config: {
          ref_audio_path: "alice.wav",
          prompt_text: "reference prompt"
        }
      }
    ]
  }
];

describe("routing helpers", () => {
  it("resolves engine, profile, service and parameters from a character profile", () => {
    const line: ScriptLine = { id: "l1", character_id: "alice", text: "hello", note: "" };

    expect(lineEngine(line, characters)).toBe("gpt-sovits");
    expect(lineProfile(line, characters)).toBe("alice-gpt");
    expect(lineServiceId(line, characters)).toBe("local-gpt-sovits");
    expect(buildGenerationTask(line, characters)).toMatchObject({
      engine: "gpt-sovits",
      profile: "alice-gpt",
      service_id: "local-gpt-sovits",
      provider_type: "gpt-sovits",
      binding_id: "alice-gpt-binding",
      required_capabilities: ["trained_weights_voice", "reference_audio_voice"],
      fallback_service_ids: ["remote-gpt"],
      parameters: {
        ref_audio_path: "alice.wav",
        prompt_text: "reference prompt"
      }
    });
  });

  it("lets line overrides win over character defaults", () => {
    const line: ScriptLine = {
      id: "l2",
      character_id: "alice",
      text: "hello",
      note: "",
      engine_override: "indextts",
      profile_override: "manual-profile",
      service_override: "remote-index"
    };

    expect(lineEngine(line, characters)).toBe("indextts");
    expect(lineProfile(line, characters)).toBe("manual-profile");
    expect(lineServiceId(line, characters)).toBe("remote-index");
  });

  it("maps commercial bindings to the commercial engine bucket", () => {
    const commercialCharacters: Character[] = [
      {
        id: "alice",
        name: "Alice",
        aliases: [],
        notes: "",
        default_engine: "gpt-sovits",
        default_profile: "alice-openai",
        fallback_profiles: [],
        profiles: [
          {
            id: "alice-openai",
            name: "Alice OpenAI",
            engine: "commercial",
            service_id: "openai-tts",
            fallback_services: [],
            bindings: [
              {
                binding_id: "alice-openai",
                provider_type: "openai",
                service_id: "openai-tts",
                fallback_services: [],
                capabilities: ["commercial_voice"],
                config: { voice: "alloy" }
              }
            ],
            config: {}
          }
        ]
      }
    ];

    const line: ScriptLine = { id: "l3", character_id: "alice", text: "hello", note: "" };

    expect(buildGenerationTask(line, commercialCharacters)).toMatchObject({
      engine: "commercial",
      provider_type: "openai",
      binding_id: "alice-openai"
    });
  });

  it("maps CosyVoice bindings to the CosyVoice engine bucket", () => {
    const cosyCharacters: Character[] = [
      {
        id: "alice",
        name: "Alice",
        aliases: [],
        notes: "",
        default_engine: "cosyvoice",
        default_profile: "alice-cosyvoice",
        fallback_profiles: [],
        profiles: [
          {
            id: "alice-cosyvoice",
            name: "Alice CosyVoice",
            engine: "cosyvoice",
            service_id: "cosyvoice-http",
            fallback_services: [],
            bindings: [
              {
                binding_id: "alice-cosyvoice",
                provider_type: "cosyvoice",
                service_id: "cosyvoice-http",
                fallback_services: [],
                capabilities: ["zero_shot_voice", "reference_audio_voice"],
                config: { mode: "zero_shot", prompt_audio_path: "alice.wav" }
              }
            ],
            config: {}
          }
        ]
      }
    ];

    const line: ScriptLine = { id: "l-cosy", character_id: "alice", text: "hello", note: "" };

    expect(buildGenerationTask(line, cosyCharacters)).toMatchObject({
      engine: "cosyvoice",
      provider_type: "cosyvoice",
      binding_id: "alice-cosyvoice",
      service_id: "cosyvoice-http"
    });
  });

  it("uses a line binding override when one profile has multiple bindings", () => {
    const line: ScriptLine = { id: "l4", character_id: "alice", text: "hello", note: "", binding_override: "alice-openai" };

    expect(buildGenerationTask(line, characters)).toMatchObject({
      service_id: "openai-tts",
      provider_type: "openai",
      binding_id: "alice-openai",
      required_capabilities: ["commercial_voice", "style_instruction"],
      parameters: { voice: "alloy" }
    });
  });

  it("derives the engine from the selected binding when a stale engine override disagrees", () => {
    const line: ScriptLine = {
      id: "l5",
      character_id: "alice",
      text: "hello",
      note: "",
      engine_override: "gpt-sovits",
      binding_override: "alice-openai"
    };

    expect(lineEngine(line, characters)).toBe("commercial");
    expect(buildGenerationTask(line, characters)).toMatchObject({
      engine: "commercial",
      provider_type: "openai",
      service_id: "openai-tts"
    });
  });

  it("uses a line temporary binding before character defaults", () => {
    const line: ScriptLine = {
      id: "l6",
      character_id: "alice",
      text: "temporary voice",
      note: "",
      temporary_binding: {
        binding_id: "line-temp-index",
        provider_type: "indextts",
        service_id: "remote-index",
        fallback_services: [],
        capabilities: ["reference_audio_voice", "emotion_text"],
        config: { voice: "tmp/ref.wav", emotion_mode: "emotion_text", emotion_text: "焦急" }
      }
    };

    expect(lineEngine(line, characters)).toBe("indextts");
    expect(lineServiceId(line, characters)).toBe("remote-index");
    expect(buildGenerationTask(line, characters)).toMatchObject({
      engine: "indextts",
      profile: "line-temp-index",
      service_id: "remote-index",
      provider_type: "indextts",
      binding_id: "line-temp-index",
      required_capabilities: ["reference_audio_voice", "emotion_text"],
      parameters: { voice: "tmp/ref.wav", emotion_text: "焦急" }
    });
  });

  it("rejects a generation task for an unmatched role without a binding", () => {
    const line: ScriptLine = { id: "l7", character_id: "guest", text: "hello", note: "" };

    expect(() => buildGenerationTask(line, [])).toThrow("needs a voice binding");
  });
});
