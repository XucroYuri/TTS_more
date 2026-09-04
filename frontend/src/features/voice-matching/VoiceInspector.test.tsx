import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { VoiceInspector } from "./VoiceInspector";

describe("VoiceInspector", () => {
  it("exposes an accessible inspector without owning network behavior", () => {
    const markup = renderToStaticMarkup(createElement(VoiceInspector, {
      mode: "recommendation",
      children: createElement("button", null, "选择声音")
    }));
    expect(markup).toContain('aria-label="声音检查区"');
    expect(markup).toContain("inspector-recommendation");
    expect(markup).toContain("选择声音");
  });
});
