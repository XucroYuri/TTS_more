import { describe, expect, it } from "vitest";

import type { Character, ProjectCharacter, ScriptProject } from "../types";
import { freezeProjectCharacterLocally, projectCharacterRows, resolveProjectCharacters } from "./projectCharacters";

const library: Character[] = [
  {
    id: "xiao-pin",
    name: "小品",
    avatar_path: "data/character_avatars/xiao-pin.png",
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
    id: "hero",
    name: "主角",
    aliases: ["英雄队长"],
    nicknames: ["队长"],
    match_names: ["demo-hero-logs"],
    notes: "",
    fallback_profiles: []
  },
  {
    id: "mentor",
    name: "导师",
    aliases: ["顾问"],
    nicknames: ["顾问"],
    match_names: ["demo-mentor-logs"],
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

    expect(rows[0]).toMatchObject({ id: "role-1", name: "小品", mode: "reference", provider: "gpt-sovits", lineCount: 1, avatarPath: "data/character_avatars/xiao-pin.png", avatarFallback: "小" });
    expect(rows[1]).toMatchObject({ id: "role-2", name: "快照小品", mode: "snapshot", provider: "gpt-sovits", lineCount: 1, avatarPath: "data/character_avatars/xiao-pin.png", avatarFallback: "快" });
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
        { id: "l1", character_id: "队长", text: "我们必须出发。", note: "" },
        { id: "l2", character_id: "顾问", text: "保持阵型。", note: "" }
      ]
    };

    const rows = projectCharacterRows(nextProject, extendedNameLibrary);

    expect(rows[0]).toMatchObject({ id: "队长", name: "主角", linked: true });
    expect(rows[1]).toMatchObject({ id: "顾问", name: "导师", linked: true });
  });
});
