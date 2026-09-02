import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { initI18n } from "../../i18n";
import type { VoiceCatalogPublicView } from "../../types";
import { VoiceAssetStatusPanel } from "./VoiceAssetStatusPanel";

initI18n();

const catalog: VoiceCatalogPublicView = {
  state: "partial",
  catalog_version: "catalog-v1",
  resources: [{
    resource_id: "role-jiujiu",
    character_id: "诸葛九九",
    character_aliases: ["九九"],
    reference_asset_ids: ["ref-1"],
    languages: ["zh"],
    generic_pool: false,
    confirmed: true,
    metadata_score: 5,
    engine_type: "gpt-sovits",
    state: "reload_required",
    service_id: "comfy-gpt",
    weight_artifact_ids: ["weight-1"],
    mapping_origin: "plugin",
    fingerprint: "resource-fp"
  }],
  references: [],
  diagnostics: [{ code: "voice_resource_reload_required", field_path: "resources.role-jiujiu" }],
  counts: { resources: 1, references: 0, weights: 2 }
};

describe("VoiceAssetStatusPanel", () => {
  const views: Array<{ root: ReturnType<typeof createRoot>; dom: JSDOM }> = [];
  afterEach(async () => {
    for (const view of views.splice(0)) await act(async () => view.root.unmount());
  });

  it("shows catalog counts and actionable resource state without exposing local paths", async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
    Object.assign(globalThis, {
      window: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      Node: dom.window.Node,
      Event: dom.window.Event,
      IS_REACT_ACT_ENVIRONMENT: true
    });
    const root = createRoot(dom.window.document.getElementById("root")!);
    views.push({ root, dom });
    const onSync = vi.fn();

    await act(async () => root.render(createElement(VoiceAssetStatusPanel, { catalog, syncing: false, onSync })));

    const text = dom.window.document.body.textContent ?? "";
    expect(text).toContain("1 个资源");
    expect(text).toContain("2 个权重");
    expect(text).toContain("需要重载");
    expect(text).toContain("voice_resource_reload_required");
    expect(text).not.toContain("E:\\");
    const button = dom.window.document.querySelector("button")!;
    await act(async () => button.click());
    expect(onSync).toHaveBeenCalledOnce();
  });
});
