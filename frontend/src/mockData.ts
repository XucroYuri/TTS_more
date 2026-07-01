import type { Character, GenerationManifest, ScriptProject, WorkerHealth } from "./types";

export const initialCharacters: Character[] = [
  {
    id: "xiao-mei",
    name: "小美",
    aliases: ["小美", "女主"],
    notes: "清亮、紧张时语速略快",
    default_engine: "gpt-sovits",
    default_profile: "xiao-mei-proplus",
    fallback_profiles: ["xiao-mei-index-emo", "en-Emma_woman"],
    profiles: [
      {
        id: "xiao-mei-proplus",
        name: "小美 ProPlus",
        engine: "gpt-sovits",
        service_id: "local-gpt-sovits",
        fallback_services: [],
        bindings: [
          {
            binding_id: "xiao-mei-proplus-gpt",
            provider_type: "gpt-sovits",
            service_id: "local-gpt-sovits",
            fallback_services: [],
            capabilities: ["trained_weights_voice", "reference_audio_voice"],
            config: { prompt_lang: "zh" }
          }
        ],
        config: { prompt_lang: "zh" }
      },
      {
        id: "xiao-mei-index-emo",
        name: "小美 IndexTTS emotion",
        engine: "indextts",
        service_id: "local-indextts",
        fallback_services: [],
        bindings: [
          {
            binding_id: "xiao-mei-index-emo-binding",
            provider_type: "indextts",
            service_id: "local-indextts",
            fallback_services: [],
            capabilities: ["reference_audio_voice", "emotion_text"],
            config: {}
          }
        ],
        config: {}
      },
      {
        id: "xiao-mei-openai",
        name: "小美 OpenAI",
        engine: "commercial",
        service_id: "openai-tts",
        fallback_services: ["gemini-tts", "xai-tts", "volcengine-tts"],
        bindings: [
          {
            binding_id: "xiao-mei-openai-binding",
            provider_type: "openai",
            service_id: "openai-tts",
            fallback_services: ["gemini-tts", "xai-tts", "volcengine-tts"],
            capabilities: ["commercial_voice", "style_instruction"],
            config: { voice: "alloy", instructions: "年轻女性，清亮，紧张时语速略快。" }
          }
        ],
        config: {}
      }
    ]
  },
  {
    id: "wang-qiang",
    name: "王强",
    aliases: ["王强", "男主"],
    notes: "低沉、克制、情绪爆发需要 IndexTTS",
    default_engine: "indextts",
    default_profile: "wang-qiang-emo",
    fallback_profiles: ["wang-qiang-proplus", "en-Carter_man"],
    profiles: [
      {
        id: "wang-qiang-emo",
        name: "王强 IndexTTS emotion",
        engine: "indextts",
        service_id: "local-indextts",
        fallback_services: [],
        bindings: [
          {
            binding_id: "wang-qiang-index-emo-binding",
            provider_type: "indextts",
            service_id: "local-indextts",
            fallback_services: [],
            capabilities: ["reference_audio_voice", "emotion_text"],
            config: {}
          }
        ],
        config: {}
      },
      {
        id: "wang-qiang-openai",
        name: "王强 OpenAI",
        engine: "commercial",
        service_id: "openai-tts",
        fallback_services: ["gemini-tts", "xai-tts", "volcengine-tts"],
        bindings: [
          {
            binding_id: "wang-qiang-openai-binding",
            provider_type: "openai",
            service_id: "openai-tts",
            fallback_services: ["gemini-tts", "xai-tts", "volcengine-tts"],
            capabilities: ["commercial_voice", "style_instruction"],
            config: { voice: "onyx", instructions: "低沉、克制，情绪爆发时保持清晰咬字。" }
          }
        ],
        config: {}
      }
    ]
  },
  {
    id: "pang-bai",
    name: "旁白",
    aliases: ["旁白", "Narrator"],
    notes: "稳定、干净、段落感强",
    default_engine: "gpt-sovits",
    default_profile: "narrator-proplus",
    fallback_profiles: ["narrator-index", "narrator-openai"],
    profiles: [
      {
        id: "narrator-proplus",
        name: "旁白 ProPlus",
        engine: "gpt-sovits",
        service_id: "local-gpt-sovits",
        fallback_services: [],
        bindings: [
          {
            binding_id: "narrator-proplus-gpt",
            provider_type: "gpt-sovits",
            service_id: "local-gpt-sovits",
            fallback_services: [],
            capabilities: ["trained_weights_voice", "reference_audio_voice"],
            config: { prompt_lang: "zh" }
          }
        ],
        config: { prompt_lang: "zh" }
      },
      {
        id: "narrator-index",
        name: "旁白 IndexTTS",
        engine: "indextts",
        service_id: "local-indextts",
        fallback_services: [],
        bindings: [
          {
            binding_id: "narrator-index-binding",
            provider_type: "indextts",
            service_id: "local-indextts",
            fallback_services: [],
            capabilities: ["reference_audio_voice", "emotion_text"],
            config: { emotion_mode: "same_as_voice" }
          }
        ],
        config: { emotion_mode: "same_as_voice" }
      },
      {
        id: "narrator-openai",
        name: "旁白 OpenAI",
        engine: "commercial",
        service_id: "openai-tts",
        fallback_services: ["gemini-tts", "xai-tts", "volcengine-tts"],
        bindings: [
          {
            binding_id: "narrator-openai-binding",
            provider_type: "openai",
            service_id: "openai-tts",
            fallback_services: ["gemini-tts", "xai-tts", "volcengine-tts"],
            capabilities: ["commercial_voice", "style_instruction"],
            config: { voice: "verse", instructions: "稳定、干净、段落感强的中文旁白。" }
          }
        ],
        config: {}
      }
    ]
  }
];

export const initialProject: ScriptProject = {
  title: "demo-script",
  default_language: "zh",
  lines: [
    { id: "l0001", character_id: "pang-bai", note: "冷静", text: "雨停的时候，城市像刚从一场梦里醒来。", language: "zh" },
    { id: "l0002", character_id: "xiao-mei", note: "压低声音", text: "你终于来了，我以为你不会再出现。", language: "zh" },
    { id: "l0003", character_id: "wang-qiang", note: "克制怒意", text: "我答应过的事，从来不会忘。", language: "zh" },
    { id: "l0004", character_id: "xiao-mei", note: "急促", text: "那就别再浪费时间，跟我走。", language: "zh", engine_override: "indextts", profile_override: "xiao-mei-index-emo" }
  ]
};

export const initialManifest: GenerationManifest = {
  project_id: "demo",
  lines: {
    l0001: {
      line_id: "l0001",
      versions: [
        {
          version_id: "v001",
          engine: "vibevoice",
          profile: "en-Carter_man",
          status: "completed",
          audio_path: "data/projects/demo/audio/vibevoice/en-Carter_man/l0001_v001.wav",
          created_at: "2026-06-30T09:00:00Z"
        }
      ]
    },
    l0003: {
      line_id: "l0003",
      versions: [
        {
          version_id: "v001",
          engine: "indextts",
          profile: "wang-qiang-emo",
          status: "failed",
          error: "voice reference audio does not exist",
          created_at: "2026-06-30T09:02:00Z"
        }
      ]
    }
  }
};

export const fallbackWorkers: WorkerHealth[] = [
  { service_id: "local-gpt-sovits", engine: "gpt-sovits", ready: true, mode: "mock", resource_group: "local-gpu-0", capabilities: ["tts"] },
  { service_id: "local-indextts", engine: "indextts", ready: true, mode: "mock", resource_group: "local-gpu-0", capabilities: ["tts"] },
  { service_id: "openai-tts", engine: "commercial", provider_type: "openai", ready: false, mode: "external", resource_group: "paid-openai", priority: 80, capabilities: ["tts", "commercial_voice", "paid_provider"], health: { status: "needs key" } }
];
