import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Character, ProjectCharacter } from "../../types";
import { useRoleLibraryController } from "./useRoleLibraryController";

describe("useRoleLibraryController", () => {
  let root: ReturnType<typeof createRoot> | null = null;
  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
  });

  it("filters aliases and maps a project role through one injected save", async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>');
    Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
    const characters: Character[] = [{ id: "九九", name: "诸葛九九", aliases: ["小九"], nicknames: [], match_names: [], notes: "", tags: [], library_status: "confirmed", source_assets: {}, reference_audio_groups: [], profiles: [], fallback_profiles: [] }];
    const projectCharacters: ProjectCharacter[] = [{ project_character_id: "胶布", name: "胶布", library_character_id: null, mode: "reference" }];
    const onSaveCharacters = vi.fn();
    const onSaveProjectCharacters = vi.fn();
    let controller: ReturnType<typeof useRoleLibraryController> | null = null;
    function Harness() {
      controller = useRoleLibraryController({ characters, projectCharacters, search: "小九", onSaveCharacters, onSaveProjectCharacters });
      return null;
    }
    root = createRoot(dom.window.document.getElementById("root")!);
    await act(async () => root?.render(createElement(Harness)));

    expect(controller!.filteredCharacters.map((item) => item.id)).toEqual(["九九"]);
    controller!.mapProjectRole("胶布", "九九");
    expect(onSaveCharacters).toHaveBeenCalledWith([
      expect.objectContaining({
        id: "九九",
        aliases: ["小九", "胶布"]
      })
    ]);
    expect(onSaveProjectCharacters).toHaveBeenCalledWith([
      expect.objectContaining({
        project_character_id: "胶布",
        library_character_id: "九九",
        project_binding: null,
        match_status: "matched"
      })
    ]);
  });
});
