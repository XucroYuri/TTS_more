import type { AnnotationKind, SemanticAnnotation } from "../../types";
import { isUtf16Boundary } from "./selectionOffsets";

export interface AnnotationTextSegment {
  startUtf16: number;
  endUtf16: number;
  text: string;
  annotationIds: string[];
  annotationKinds: AnnotationKind[];
}

const annotationKindOrder: Record<AnnotationKind, number> = {
  speaker: 0,
  emotion_evidence: 1,
  dialogue: 2
};

function validateAnnotation(source: string, annotation: SemanticAnnotation): void {
  const { start_utf16: start, end_utf16: end, text } = annotation.span;
  if (
    !Number.isInteger(start) ||
    !Number.isInteger(end) ||
    start < 0 ||
    end > source.length ||
    start >= end
  ) {
    throw new RangeError("annotation_span_invalid");
  }
  if (!isUtf16Boundary(source, start) || !isUtf16Boundary(source, end)) {
    throw new RangeError("utf16_surrogate_split");
  }
  if (source.slice(start, end) !== text) throw new RangeError("annotation_text_mismatch");
}

function compareAnnotations(left: SemanticAnnotation, right: SemanticAnnotation): number {
  const kindDifference = annotationKindOrder[left.kind] - annotationKindOrder[right.kind];
  return kindDifference || left.id.localeCompare(right.id);
}

/** Splits source only at annotation boundaries and retains every covering layer. */
export function splitAnnotatedText(
  source: string,
  annotations: SemanticAnnotation[]
): AnnotationTextSegment[] {
  for (const annotation of annotations) validateAnnotation(source, annotation);

  const boundaries = new Set<number>([0, source.length]);
  for (const annotation of annotations) {
    boundaries.add(annotation.span.start_utf16);
    boundaries.add(annotation.span.end_utf16);
  }
  const sortedBoundaries = [...boundaries].sort((left, right) => left - right);
  const segments: AnnotationTextSegment[] = [];

  for (let index = 0; index < sortedBoundaries.length - 1; index += 1) {
    const startUtf16 = sortedBoundaries[index];
    const endUtf16 = sortedBoundaries[index + 1];
    if (startUtf16 === endUtf16) continue;

    const covering = annotations
      .filter(
        (annotation) =>
          annotation.span.start_utf16 <= startUtf16 && annotation.span.end_utf16 >= endUtf16
      )
      .sort(compareAnnotations);
    segments.push({
      startUtf16,
      endUtf16,
      text: source.slice(startUtf16, endUtf16),
      annotationIds: covering.map((annotation) => annotation.id),
      annotationKinds: covering.map((annotation) => annotation.kind)
    });
  }

  return segments;
}
