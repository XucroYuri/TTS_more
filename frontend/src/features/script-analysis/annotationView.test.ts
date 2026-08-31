import { describe, expect, it } from "vitest";

import type { AnnotationKind, SemanticAnnotation } from "../../types";
import { splitAnnotatedText } from "./annotationView";

function annotation(
  id: string,
  kind: AnnotationKind,
  source: string,
  start: number,
  end: number
): SemanticAnnotation {
  return {
    id,
    kind,
    span: {
      source_revision_id: "script-r007",
      start_utf16: start,
      end_utf16: end,
      text: source.slice(start, end),
      source_sha256: "sha256-exact-source"
    },
    origin: "ai",
    confidence: 0.9,
    status: "accepted",
    created_at: "2026-08-31T00:00:00.000Z",
    updated_at: "2026-08-31T00:00:00.000Z"
  };
}

describe("splitAnnotatedText", () => {
  it("uses exact sorted boundaries and preserves every overlapping identity and kind", () => {
    const source = "角色（惊喜）：台词";
    const speaker = annotation("speaker-1", "speaker", source, 0, 2);
    const emotion = annotation("emotion-1", "emotion_evidence", source, 3, 5);
    const dialogue = annotation("dialogue-1", "dialogue", source, 0, 9);

    const segments = splitAnnotatedText(source, [dialogue, emotion, speaker]);

    expect(segments).toEqual([
      {
        startUtf16: 0,
        endUtf16: 2,
        text: "角色",
        annotationIds: ["speaker-1", "dialogue-1"],
        annotationKinds: ["speaker", "dialogue"]
      },
      {
        startUtf16: 2,
        endUtf16: 3,
        text: "（",
        annotationIds: ["dialogue-1"],
        annotationKinds: ["dialogue"]
      },
      {
        startUtf16: 3,
        endUtf16: 5,
        text: "惊喜",
        annotationIds: ["emotion-1", "dialogue-1"],
        annotationKinds: ["emotion_evidence", "dialogue"]
      },
      {
        startUtf16: 5,
        endUtf16: 9,
        text: "）：台词",
        annotationIds: ["dialogue-1"],
        annotationKinds: ["dialogue"]
      }
    ]);
    expect(segments.map((segment) => segment.text).join("")).toBe(source);
  });

  it("preserves whitespace, CRLF, astral characters, and unannotated gaps exactly", () => {
    const source = " 甲😀\r\n  台词 ";
    const dialogue = annotation("dialogue-1", "dialogue", source, 8, 10);

    const segments = splitAnnotatedText(source, [dialogue]);

    expect(segments.map((segment) => segment.text).join("")).toBe(source);
    expect(segments).toEqual([
      {
        startUtf16: 0,
        endUtf16: 8,
        text: " 甲😀\r\n  ",
        annotationIds: [],
        annotationKinds: []
      },
      {
        startUtf16: 8,
        endUtf16: 10,
        text: "台词",
        annotationIds: ["dialogue-1"],
        annotationKinds: ["dialogue"]
      },
      {
        startUtf16: 10,
        endUtf16: 11,
        text: " ",
        annotationIds: [],
        annotationKinds: []
      }
    ]);
  });

  it("orders same-kind overlapping identities deterministically instead of deduplicating them", () => {
    const source = "台词";
    const second = annotation("dialogue-b", "dialogue", source, 0, 2);
    const first = annotation("dialogue-a", "dialogue", source, 0, 2);

    expect(splitAnnotatedText(source, [second, first])).toEqual([
      {
        startUtf16: 0,
        endUtf16: 2,
        text: source,
        annotationIds: ["dialogue-a", "dialogue-b"],
        annotationKinds: ["dialogue", "dialogue"]
      }
    ]);
  });

  it.each([
    ["start", 2, 4],
    ["end", 1, 2]
  ])("rejects an annotation %s boundary between an emoji surrogate pair", (_boundary, start, end) => {
    const source = "A😀B";
    const invalid = annotation("invalid", "dialogue", source, start, end);

    expect(() => splitAnnotatedText(source, [invalid])).toThrow("utf16_surrogate_split");
  });

  it("keeps a complete emoji inside one legal annotation segment", () => {
    const source = "A😀B";
    const emoji = annotation("emoji", "emotion_evidence", source, 1, 3);

    expect(splitAnnotatedText(source, [emoji])).toEqual([
      {
        startUtf16: 0,
        endUtf16: 1,
        text: "A",
        annotationIds: [],
        annotationKinds: []
      },
      {
        startUtf16: 1,
        endUtf16: 3,
        text: "😀",
        annotationIds: ["emoji"],
        annotationKinds: ["emotion_evidence"]
      },
      {
        startUtf16: 3,
        endUtf16: 4,
        text: "B",
        annotationIds: [],
        annotationKinds: []
      }
    ]);
  });

  it.each([
    ["negative start", -1, 1],
    ["past source end", 0, 3],
    ["empty interval", 1, 1],
    ["fractional coordinate", 0.5, 1]
  ])("rejects %s rather than silently dropping the annotation", (_case, start, end) => {
    const source = "台词";
    const invalid = annotation("invalid", "dialogue", source, start, end);

    expect(() => splitAnnotatedText(source, [invalid])).toThrow("annotation_span_invalid");
  });

  it("rejects a span whose captured text is not the exact source slice", () => {
    const source = "台词";
    const invalid = annotation("invalid", "dialogue", source, 0, 1);
    invalid.span.text = "词";

    expect(() => splitAnnotatedText(source, [invalid])).toThrow("annotation_text_mismatch");
  });
});
