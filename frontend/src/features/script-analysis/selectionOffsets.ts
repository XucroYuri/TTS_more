import type { ScriptRevision, SourceSpan } from "../../types";

export interface SourceOffsets {
  startUtf16: number;
  endUtf16: number;
}

interface TextNodeIndex {
  node: Text;
  startUtf16: number;
}

function textNodeIndex(root: HTMLElement): { nodes: TextNodeIndex[]; source: string } {
  const showText = root.ownerDocument.defaultView?.NodeFilter.SHOW_TEXT ?? 4;
  const walker = root.ownerDocument.createTreeWalker(root, showText);
  const nodes: TextNodeIndex[] = [];
  const sourceParts: string[] = [];
  let startUtf16 = 0;

  for (let current = walker.nextNode(); current; current = walker.nextNode()) {
    const node = current as Text;
    nodes.push({ node, startUtf16 });
    sourceParts.push(node.data);
    startUtf16 += node.data.length;
  }

  return { nodes, source: sourceParts.join("") };
}

function isTextNode(node: Node): node is Text {
  return node.nodeType === 3;
}

function boundaryOffset(
  indexes: TextNodeIndex[],
  container: Node,
  localOffset: number
): number | null {
  if (!isTextNode(container) || !Number.isInteger(localOffset)) return null;
  const index = indexes.find((candidate) => candidate.node === container);
  if (!index || localOffset < 0 || localOffset > container.data.length) return null;
  return index.startUtf16 + localOffset;
}

/** Converts an exact, non-empty DOM text range below root into UTF-16 offsets. */
export function domRangeToOffsets(root: HTMLElement, range: Range): SourceOffsets {
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) {
    throw new RangeError("selection_outside_source");
  }
  if (range.collapsed) throw new RangeError("selection_collapsed");

  const { nodes, source } = textNodeIndex(root);
  const startUtf16 = boundaryOffset(nodes, range.startContainer, range.startOffset);
  const endUtf16 = boundaryOffset(nodes, range.endContainer, range.endOffset);
  if (startUtf16 === null || endUtf16 === null) {
    throw new RangeError("selection_unmappable");
  }
  if (startUtf16 >= endUtf16) throw new RangeError("selection_unmappable");

  const selectedSource = source.slice(startUtf16, endUtf16);
  if (range.toString() !== selectedSource) throw new RangeError("selection_unmappable");
  return { startUtf16, endUtf16 };
}

/** Grounds a DOM range in one immutable ScriptRevision without normalizing its text. */
export function domRangeToSourceSpan(
  root: HTMLElement,
  range: Range,
  sourceRevision: ScriptRevision
): SourceSpan {
  if (!sourceRevision.source_sha256) throw new Error("source_revision_hash_missing");

  const { source } = textNodeIndex(root);
  if (source !== sourceRevision.source_markdown) throw new Error("source_revision_mismatch");

  const { startUtf16, endUtf16 } = domRangeToOffsets(root, range);
  const text = source.slice(startUtf16, endUtf16);
  if (!text || text !== range.toString()) throw new RangeError("selection_unmappable");

  return {
    source_revision_id: sourceRevision.revision_id,
    start_utf16: startUtf16,
    end_utf16: endUtf16,
    text,
    source_sha256: sourceRevision.source_sha256
  };
}
