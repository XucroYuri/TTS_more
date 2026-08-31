import { describe, expect, it } from "vitest";
// @ts-expect-error jsdom ships without declaration files in this project.
import { JSDOM } from "jsdom";

import type { ScriptRevision } from "../../types";
import { domRangeToOffsets, domRangeToSourceSpan } from "./selectionOffsets";

function sourceRevision(source: string): ScriptRevision {
  return {
    revision_id: "script-r007",
    source_markdown: source,
    source_sha256: "sha256-exact-source",
    created_at: "2026-08-31T00:00:00.000Z"
  };
}

function renderTextNodes(document: Document, sourceParts: string[]): HTMLElement {
  const root = document.createElement("div");
  for (const [index, part] of sourceParts.entries()) {
    const wrapper = document.createElement(index % 2 === 0 ? "span" : "button");
    wrapper.append(document.createTextNode(part));
    root.append(wrapper);
  }
  document.body.append(root);
  return root;
}

describe("domRangeToSourceSpan", () => {
  it("maps a cross-text-node range with an astral emoji to exact UTF-16 metadata", () => {
    const dom = new JSDOM("<p>outside-before</p>");
    const root = renderTextNodes(dom.window.document, ["甲😀", "台", "词"]);
    dom.window.document.body.append(dom.window.document.createTextNode("outside-after"));
    const first = root.firstElementChild!.firstChild!;
    const second = root.children[1].firstChild!;
    const range = dom.window.document.createRange();
    range.setStart(first, 1);
    range.setEnd(second, 1);

    expect(domRangeToOffsets(root, range)).toEqual({ startUtf16: 1, endUtf16: 4 });
    expect(domRangeToSourceSpan(root, range, sourceRevision("甲😀台词"))).toEqual({
      source_revision_id: "script-r007",
      start_utf16: 1,
      end_utf16: 4,
      text: "😀台",
      source_sha256: "sha256-exact-source"
    });
  });

  it("rejects a range whose endpoint leaves the source root", () => {
    const dom = new JSDOM();
    const root = renderTextNodes(dom.window.document, ["甲", "台词"]);
    const outside = dom.window.document.createTextNode("外部");
    dom.window.document.body.append(outside);
    const range = dom.window.document.createRange();
    range.setStart(root.firstElementChild!.firstChild!, 0);
    range.setEnd(outside, 1);

    expect(() => domRangeToSourceSpan(root, range, sourceRevision("甲台词"))).toThrow(
      "selection_outside_source"
    );
  });

  it("rejects a collapsed range", () => {
    const dom = new JSDOM();
    const root = renderTextNodes(dom.window.document, ["甲台词"]);
    const text = root.firstElementChild!.firstChild!;
    const range = dom.window.document.createRange();
    range.setStart(text, 1);
    range.collapse(true);

    expect(() => domRangeToSourceSpan(root, range, sourceRevision("甲台词"))).toThrow(
      "selection_collapsed"
    );
  });

  it("maps exact root, nested button, and nested span element boundaries", () => {
    const dom = new JSDOM();
    const document = dom.window.document;
    const root = document.createElement("div");
    const first = document.createElement("span");
    first.append(document.createTextNode("甲"));
    const nestedButton = document.createElement("button");
    const emoji = document.createElement("span");
    emoji.append(document.createTextNode("😀"));
    const dialogue = document.createElement("span");
    dialogue.append(document.createTextNode("台"));
    nestedButton.append(emoji, dialogue);
    const last = document.createElement("span");
    last.append(document.createTextNode("词"));
    root.append(first, nestedButton, last);
    document.body.append(root);

    const rootBoundaryRange = document.createRange();
    rootBoundaryRange.setStart(root, 1);
    rootBoundaryRange.setEnd(root, 2);
    expect(domRangeToOffsets(root, rootBoundaryRange)).toEqual({ startUtf16: 1, endUtf16: 4 });
    expect(domRangeToSourceSpan(root, rootBoundaryRange, sourceRevision("甲😀台词")).text).toBe(
      "😀台"
    );

    const buttonBoundaryRange = document.createRange();
    buttonBoundaryRange.setStart(nestedButton, 0);
    buttonBoundaryRange.setEnd(nestedButton, 2);
    expect(domRangeToOffsets(root, buttonBoundaryRange)).toEqual({ startUtf16: 1, endUtf16: 4 });

    const spanBoundaryRange = document.createRange();
    spanBoundaryRange.setStart(emoji, 0);
    spanBoundaryRange.setEnd(emoji, 1);
    expect(domRangeToOffsets(root, spanBoundaryRange)).toEqual({ startUtf16: 1, endUtf16: 3 });
  });

  it("rejects an element boundary with an illegal child offset", () => {
    const dom = new JSDOM();
    const root = renderTextNodes(dom.window.document, ["甲", "台词"]);
    const endText = root.lastElementChild!.firstChild!;
    const invalidRange = {
      startContainer: root,
      startOffset: root.childNodes.length + 1,
      endContainer: endText,
      endOffset: 1,
      collapsed: false,
      toString: () => "甲台"
    } as unknown as Range;

    expect(() => domRangeToOffsets(root, invalidRange)).toThrow("selection_unmappable");
  });

  it.each([
    ["start", 2, 4],
    ["end", 1, 2]
  ])("rejects a %s boundary between an emoji surrogate pair", (_boundary, start, end) => {
    const dom = new JSDOM();
    const root = renderTextNodes(dom.window.document, ["A😀B"]);
    const text = root.firstElementChild!.firstChild!;
    const range = dom.window.document.createRange();
    range.setStart(text, start);
    range.setEnd(text, end);

    expect(() => domRangeToSourceSpan(root, range, sourceRevision("A😀B"))).toThrow(
      "utf16_surrogate_split"
    );
  });

  it("accepts source boundaries around a complete emoji", () => {
    const dom = new JSDOM();
    const root = renderTextNodes(dom.window.document, ["A😀B"]);
    const text = root.firstElementChild!.firstChild!;
    const range = dom.window.document.createRange();
    range.setStart(text, 1);
    range.setEnd(text, 3);

    expect(domRangeToSourceSpan(root, range, sourceRevision("A😀B"))).toMatchObject({
      start_utf16: 1,
      end_utf16: 3,
      text: "😀"
    });
  });

  it("rejects a root whose text is not the immutable revision source", () => {
    const dom = new JSDOM();
    const root = renderTextNodes(dom.window.document, ["甲台词！"]);
    const text = root.firstElementChild!.firstChild!;
    const range = dom.window.document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, text.textContent!.length);

    expect(() => domRangeToSourceSpan(root, range, sourceRevision("甲台词"))).toThrow(
      "source_revision_mismatch"
    );
  });

  it("rejects a revision without its source hash", () => {
    const dom = new JSDOM();
    const root = renderTextNodes(dom.window.document, ["甲"]);
    const text = root.firstElementChild!.firstChild!;
    const range = dom.window.document.createRange();
    range.selectNodeContents(text);
    const revision = sourceRevision("甲");
    revision.source_sha256 = null;

    expect(() => domRangeToSourceSpan(root, range, revision)).toThrow("source_revision_hash_missing");
  });
});
