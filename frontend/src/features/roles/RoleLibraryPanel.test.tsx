import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { RoleLibraryPanel } from "./RoleLibraryPanel";

describe("RoleLibraryPanel", () => {
  it("keeps an explicit empty next action visible", () => {
    const markup = renderToStaticMarkup(createElement(RoleLibraryPanel, {
      empty: true,
      emptyLabel: "请先关联角色",
      children: null
    }));
    expect(markup).toContain("请先关联角色");
    expect(markup).toContain('aria-label="角色库"');
  });
});
