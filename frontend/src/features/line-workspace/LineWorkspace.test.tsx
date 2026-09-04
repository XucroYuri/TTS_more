import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LineWorkspace } from "./LineWorkspace";

describe("LineWorkspace", () => {
  it("keeps the line list and matching inspector as one workspace boundary", () => {
    const markup = renderToStaticMarkup(createElement(LineWorkspace, {
      lineList: createElement("p", null, "台词列表"),
      inspector: createElement("aside", { className: "inspector-binding" }, "声音设置")
    }));
    expect(markup).toContain("台词列表");
    expect(markup).toContain("声音设置");
    expect(markup).toContain("inspector-binding");
  });
});
