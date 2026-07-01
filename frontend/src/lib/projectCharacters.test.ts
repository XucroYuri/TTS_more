import { describe, expect, it } from "vitest";

import type { Character, ProjectCharacter, ScriptProject } from "../types";
import { freezeProjectCharacterLocally, projectCharacterRows, resolveProjectCharacters } from "./projectCharacters";

const library: Character[] = [
  {
    id: "xiao-pin",
    name: "小品",
    aliases: ["小品"],
    notes: "",
    default_engine: "gpt-sovits",
    default_profile: "xiao-pin-gpt",
    fallback_profiles: [],
    profiles: [
      {
        id: "xiao-pin-gpt",
        name: "小品 GPT",
        engine: "gpt-sovits",
        service_id: "local-gpt-sovits",
        fallback_services: [],
        bindings: [
          {
            binding_id: "xiao-pin-gpt-binding",
            provider_type: "gpt-sovits",
            service_id: "local-gpt-sovits",
            fallback_services: [],
            capabilities: ["trained_weights_voice"],
            config: { gpt_weights_path: "gpt-v1.ckpt" }
          }
        ],
        config: {}
      }
    ]
  }
];

const extendedNameLibrary: Character[] = [
  {
    id: "guangtou",
    name: "光头",
    aliases: ["光头胖子"],
    nicknames: ["小光"],
    match_names: ["光头TTS新-20260611"],
    notes: "",
    fallback_profiles: []
  },
  {
    id: "yanjing",
    name: "眼镜",
    aliases: ["眼镜哥"],
    nicknames: ["严镜"],
    match_names: ["TTS-大鹏眼镜"],
    notes: "",
    fallback_profiles: []
  }
];

const projectCharacters: ProjectCharacter[] = [
  { project_character_id: "role-1", name: "小品", library_character_id: "xiao-pin", mode: "reference" },
  {
    project_character_id: "role-2",
    name: "快照小品",
    library_character_id: "xiao-pin",
    mode: "snapshot",
    character_snapshot: {
      ...library[0],
      profiles: [
        {
          ...library[0].profiles![0],
          bindings: [
            {
              ...library[0].profiles![0].bindings![0],
              config: { gpt_weights_path: "gpt-frozen.ckpt" }
            }
          ]
        }
      ]
    }
  }
];

const project: ScriptProject = {
  title: "demo",
  default_language: "zh",
  project_characters: projectCharacters,
  lines: [
    { id: "l1", character_id: "role-1", text: "你好", note: "" },
    { id: "l2", character_id: "role-2", text: "再见", note: "" }
  ]
};

describe("project character helpers", () => {
  it("resolves reference and snapshot project roles into generation characters", () => {
    const resolved = resolveProjectCharacters(project, library);

    expect(resolved[0]).toMatchObject({ id: "role-1", name: "小品" });
    expect(resolved[0].profiles![0].bindings![0].config.gpt_weights_path).toBe("gpt-v1.ckpt");
    expect(resolved[1]).toMatchObject({ id: "role-2", name: "快照小品" });
    expect(resolved[1].profiles![0].bindings![0].config.gpt_weights_path).toBe("gpt-frozen.ckpt");
  });

  it("summarizes project role rows with mode, provider, and line count", () => {
    const rows = projectCharacterRows(project, library);

    expect(rows[0]).toMatchObject({ id: "role-1", name: "小品", mode: "reference", provider: "gpt-sovits", lineCount: 1 });
    expect(rows[1]).toMatchObject({ id: "role-2", name: "快照小品", mode: "snapshot", provider: "gpt-sovits", lineCount: 1 });
  });

  it("can create a local snapshot from a library reference for optimistic UI", () => {
    const frozen = freezeProjectCharacterLocally(projectCharacters[0], library);

    expect(frozen.mode).toBe("snapshot");
    expect(frozen.character_snapshot?.id).toBe("xiao-pin");
    expect(frozen.character_snapshot?.profiles?.[0].bindings?.[0].config.gpt_weights_path).toBe("gpt-v1.ckpt");
  });

  it("auto matches project roles with aliases, nicknames, and match names", () => {
    const nextProject: ScriptProject = {
      title: "demo",
      default_language: "zh",
      lines: [
        { id: "l1", character_id: "小光", text: "怎么爆炸了……", note: "" },
        { id: "l2", character_id: "严镜", text: "没有用！", note: "" }
      ]
    };

    const rows = projectCharacterRows(nextProject, extendedNameLibrary);

    expect(rows[0]).toMatchObject({ id: "小光", name: "光头", linked: true });
    expect(rows[1]).toMatchObject({ id: "严镜", name: "眼镜", linked: true });
  });
});
