import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode
} from "react";

import type { AnnotationKind, ScriptRevision, SemanticAnnotation, SourceSpan } from "../../types";
import { splitAnnotatedText, type AnnotationTextSegment } from "./annotationView";
import { domRangeToSourceSpan, isUtf16Boundary } from "./selectionOffsets";
import {
  resolveAnnotationPaneLabels,
  SelectionAnnotationMenu,
  type AnnotationPaneLabelOverrides
} from "./SelectionAnnotationMenu";
import "./script-analysis.css";

export interface SourceAnnotationPaneProps {
  sourceRevision: ScriptRevision;
  annotations: SemanticAnnotation[];
  onCreateAnnotation: (annotation: SemanticAnnotation) => void;
  onSelectAnnotation: (annotationId: string) => void;
  createAnnotationId?: () => string;
  now?: () => string;
  labels?: AnnotationPaneLabelOverrides;
}

interface ActiveLayeredSegment {
  startUtf16: number;
  endUtf16: number;
  annotationIds: string[];
}

interface KeyboardCaret {
  anchorUtf16: number;
  focusUtf16: number;
}

interface DomBoundaryPoint {
  container: Node;
  offset: number;
}

let fallbackAnnotationSequence = 0;

function defaultCreateAnnotationId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return `annotation-${globalThis.crypto.randomUUID()}`;
  }
  fallbackAnnotationSequence += 1;
  return `annotation-${Date.now()}-${fallbackAnnotationSequence}`;
}

function annotationLayerText(text: string, kinds: AnnotationKind[]): ReactNode {
  let content: ReactNode = text;
  if (kinds.includes("speaker")) {
    content = <span className="annotation-layer annotation-layer--speaker">{content}</span>;
  }
  if (kinds.includes("emotion_evidence")) {
    content = (
      <span className="annotation-layer annotation-layer--emotion-evidence">{content}</span>
    );
  }
  return content;
}

function segmentClassName(segment: AnnotationTextSegment): string {
  const classes = ["source-annotation-pane__segment"];
  if (segment.annotationKinds.includes("dialogue")) {
    classes.push("source-annotation-pane__segment--dialogue");
  }
  if (segment.annotationIds.length > 1) classes.push("source-annotation-pane__segment--layered");
  return classes.join(" ");
}

function spanMatchesSourceRevision(span: SourceSpan, sourceRevision: ScriptRevision): boolean {
  const source = sourceRevision.source_markdown;
  const sourceSha256 = sourceRevision.source_sha256;
  return (
    Boolean(sourceSha256) &&
    span.source_revision_id === sourceRevision.revision_id &&
    span.source_sha256 === sourceSha256 &&
    Number.isInteger(span.start_utf16) &&
    Number.isInteger(span.end_utf16) &&
    span.start_utf16 >= 0 &&
    span.start_utf16 < span.end_utf16 &&
    span.end_utf16 <= source.length &&
    isUtf16Boundary(source, span.start_utf16) &&
    isUtf16Boundary(source, span.end_utf16) &&
    source.slice(span.start_utf16, span.end_utf16) === span.text
  );
}

function sourceOffsetToDomPoint(
  root: HTMLElement,
  source: string,
  sourceOffset: number
): DomBoundaryPoint | null {
  if (!isUtf16Boundary(source, sourceOffset)) return null;
  const showText = root.ownerDocument.defaultView?.NodeFilter.SHOW_TEXT ?? 4;
  const walker = root.ownerDocument.createTreeWalker(root, showText);
  const textNodes: Text[] = [];
  const sourceParts: string[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const textNode = node as Text;
    textNodes.push(textNode);
    sourceParts.push(textNode.data);
  }
  if (sourceParts.join("") !== source) return null;
  if (textNodes.length === 0) return sourceOffset === 0 ? { container: root, offset: 0 } : null;

  let cursor = 0;
  for (const textNode of textNodes) {
    const nextCursor = cursor + textNode.data.length;
    if (sourceOffset <= nextCursor) {
      return { container: textNode, offset: sourceOffset - cursor };
    }
    cursor = nextCursor;
  }
  return null;
}

function setNativeSourceRange(
  root: HTMLElement,
  source: string,
  startUtf16: number,
  endUtf16: number
): Range | null {
  const start = sourceOffsetToDomPoint(root, source, startUtf16);
  const end = sourceOffsetToDomPoint(root, source, endUtf16);
  const selection = root.ownerDocument.defaultView?.getSelection();
  if (!start || !end || !selection) return null;

  const range = root.ownerDocument.createRange();
  range.setStart(start.container, start.offset);
  range.setEnd(end.container, end.offset);
  selection.removeAllRanges();
  selection.addRange(range);
  return range;
}

function nextCodePointBoundary(source: string, offset: number): number {
  if (offset >= source.length) return source.length;
  const codePoint = source.codePointAt(offset);
  return Math.min(source.length, offset + (codePoint !== undefined && codePoint > 0xffff ? 2 : 1));
}

function previousCodePointBoundary(source: string, offset: number): number {
  if (offset <= 0) return 0;
  const previous = offset - 1;
  if (
    previous > 0 &&
    source.charCodeAt(previous) >= 0xdc00 &&
    source.charCodeAt(previous) <= 0xdfff &&
    source.charCodeAt(previous - 1) >= 0xd800 &&
    source.charCodeAt(previous - 1) <= 0xdbff
  ) {
    return previous - 1;
  }
  return previous;
}

export function SourceAnnotationPane({
  sourceRevision,
  annotations,
  onCreateAnnotation,
  onSelectAnnotation,
  createAnnotationId = defaultCreateAnnotationId,
  now = () => new Date().toISOString(),
  labels: labelOverrides
}: SourceAnnotationPaneProps) {
  const sourceRootRef = useRef<HTMLDivElement>(null);
  const [selectedSpan, setSelectedSpan] = useState<SourceSpan | null>(null);
  const [activeLayeredSegment, setActiveLayeredSegment] =
    useState<ActiveLayeredSegment | null>(null);
  const keyboardCaretRef = useRef<KeyboardCaret>({ anchorUtf16: 0, focusUtf16: 0 });
  const previousSourceIdentityRef = useRef({
    revisionId: sourceRevision.revision_id,
    sourceSha256: sourceRevision.source_sha256,
    source: sourceRevision.source_markdown
  });
  const labels = useMemo(() => resolveAnnotationPaneLabels(labelOverrides), [labelOverrides]);
  const groundedAnnotations = useMemo(
    () =>
      annotations.filter((annotation) =>
        spanMatchesSourceRevision(annotation.span, sourceRevision)
      ),
    [
      annotations,
      sourceRevision.revision_id,
      sourceRevision.source_markdown,
      sourceRevision.source_sha256
    ]
  );
  const segments = useMemo(
    () => splitAnnotatedText(sourceRevision.source_markdown, groundedAnnotations),
    [groundedAnnotations, sourceRevision.source_markdown]
  );
  const annotationsById = useMemo(
    () => new Map(groundedAnnotations.map((annotation) => [annotation.id, annotation])),
    [groundedAnnotations]
  );

  const clearNativeSelection = useCallback(() => {
    sourceRootRef.current?.ownerDocument.defaultView?.getSelection()?.removeAllRanges();
  }, []);

  const clearSelectedSpan = useCallback(() => {
    clearNativeSelection();
    keyboardCaretRef.current = {
      anchorUtf16: keyboardCaretRef.current.focusUtf16,
      focusUtf16: keyboardCaretRef.current.focusUtf16
    };
    setSelectedSpan(null);
  }, [clearNativeSelection]);

  useLayoutEffect(() => {
    const previous = previousSourceIdentityRef.current;
    const identityChanged =
      previous.revisionId !== sourceRevision.revision_id ||
      previous.sourceSha256 !== sourceRevision.source_sha256 ||
      previous.source !== sourceRevision.source_markdown;
    previousSourceIdentityRef.current = {
      revisionId: sourceRevision.revision_id,
      sourceSha256: sourceRevision.source_sha256,
      source: sourceRevision.source_markdown
    };
    if (!identityChanged) return;
    clearNativeSelection();
    keyboardCaretRef.current = { anchorUtf16: 0, focusUtf16: 0 };
    setSelectedSpan(null);
    setActiveLayeredSegment(null);
  }, [
    clearNativeSelection,
    sourceRevision.revision_id,
    sourceRevision.source_markdown,
    sourceRevision.source_sha256
  ]);

  const captureSelection = useCallback(() => {
    const root = sourceRootRef.current;
    const selection = root?.ownerDocument.defaultView?.getSelection();
    if (!root || !selection || selection.rangeCount !== 1 || selection.isCollapsed) {
      setSelectedSpan(null);
      return;
    }
    try {
      const span = domRangeToSourceSpan(root, selection.getRangeAt(0), sourceRevision);
      keyboardCaretRef.current = {
        anchorUtf16: span.start_utf16,
        focusUtf16: span.end_utf16
      };
      setSelectedSpan(span);
      setActiveLayeredSegment(null);
    } catch {
      setSelectedSpan(null);
    }
  }, [sourceRevision]);

  const initializeKeyboardCaret = useCallback(
    (event: ReactFocusEvent<HTMLDivElement>) => {
      if (event.target !== event.currentTarget) return;
      const root = sourceRootRef.current;
      if (!root) return;
      keyboardCaretRef.current = { anchorUtf16: 0, focusUtf16: 0 };
      setNativeSourceRange(root, sourceRevision.source_markdown, 0, 0);
      setSelectedSpan(null);
      setActiveLayeredSegment(null);
    },
    [sourceRevision.source_markdown]
  );

  const handleKeyboardSelection = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.target !== event.currentTarget) return;
      if (!["Home", "End", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
      const root = sourceRootRef.current;
      if (!root) return;
      event.preventDefault();

      const source = sourceRevision.source_markdown;
      const caret = keyboardCaretRef.current;
      let focusUtf16 = caret.focusUtf16;
      if (event.key === "Home") focusUtf16 = 0;
      if (event.key === "End") focusUtf16 = source.length;
      if (event.key === "ArrowLeft") {
        focusUtf16 =
          !event.shiftKey && caret.anchorUtf16 !== caret.focusUtf16
            ? Math.min(caret.anchorUtf16, caret.focusUtf16)
            : previousCodePointBoundary(source, caret.focusUtf16);
      }
      if (event.key === "ArrowRight") {
        focusUtf16 =
          !event.shiftKey && caret.anchorUtf16 !== caret.focusUtf16
            ? Math.max(caret.anchorUtf16, caret.focusUtf16)
            : nextCodePointBoundary(source, caret.focusUtf16);
      }

      const anchorUtf16 = event.shiftKey ? caret.anchorUtf16 : focusUtf16;
      keyboardCaretRef.current = { anchorUtf16, focusUtf16 };
      const startUtf16 = Math.min(anchorUtf16, focusUtf16);
      const endUtf16 = Math.max(anchorUtf16, focusUtf16);
      const range = setNativeSourceRange(root, source, startUtf16, endUtf16);
      setActiveLayeredSegment(null);
      if (!range || startUtf16 === endUtf16) {
        setSelectedSpan(null);
        return;
      }
      try {
        setSelectedSpan(domRangeToSourceSpan(root, range, sourceRevision));
      } catch {
        clearSelectedSpan();
      }
    },
    [clearSelectedSpan, sourceRevision]
  );

  const createAnnotation = useCallback(
    (kind: AnnotationKind) => {
      if (!selectedSpan) return;
      if (!spanMatchesSourceRevision(selectedSpan, sourceRevision)) {
        clearSelectedSpan();
        return;
      }
      const timestamp = now();
      onCreateAnnotation({
        id: createAnnotationId(),
        kind,
        span: selectedSpan,
        origin: "human",
        confidence: null,
        status: "accepted",
        created_at: timestamp,
        updated_at: timestamp
      });
      clearSelectedSpan();
    },
    [clearSelectedSpan, createAnnotationId, now, onCreateAnnotation, selectedSpan, sourceRevision]
  );

  const openSegment = useCallback(
    (segment: AnnotationTextSegment) => {
      setSelectedSpan(null);
      if (segment.annotationIds.length === 1) {
        onSelectAnnotation(segment.annotationIds[0]);
        setActiveLayeredSegment(null);
        return;
      }
      setActiveLayeredSegment({
        startUtf16: segment.startUtf16,
        endUtf16: segment.endUtf16,
        annotationIds: segment.annotationIds
      });
    },
    [onSelectAnnotation]
  );

  return (
    <section className="source-annotation-pane">
      <ul className="source-annotation-pane__legend" aria-label={labels.legendLabel}>
        {(Object.keys(labels.kindLabels) as AnnotationKind[]).map((kind) => (
          <li key={kind} className="source-annotation-pane__legend-item">
            <span
              className={`source-annotation-pane__legend-mark source-annotation-pane__legend-mark--${kind}`}
              aria-hidden="true"
            >
              Aa
            </span>
            <span>{labels.kindLabels[kind]}</span>
          </li>
        ))}
      </ul>

      <div
        ref={sourceRootRef}
        className="source-annotation-pane__source"
        role="region"
        aria-label={labels.sourceRegionLabel}
        aria-readonly="true"
        tabIndex={0}
        style={{ whiteSpace: "pre-wrap" }}
        onFocus={initializeKeyboardCaret}
        onKeyDown={handleKeyboardSelection}
        onMouseUp={captureSelection}
      >
        {segments.map((segment) => {
          const key = `${segment.startUtf16}:${segment.endUtf16}`;
          if (segment.annotationIds.length === 0) return <span key={key}>{segment.text}</span>;
          const kindLabels = [...new Set(segment.annotationKinds)].map(
            (kind) => labels.kindLabels[kind]
          );
          return (
            <button
              key={key}
              type="button"
              className={segmentClassName(segment)}
              data-annotation-count={segment.annotationIds.length}
              data-start-utf16={segment.startUtf16}
              data-end-utf16={segment.endUtf16}
              aria-label={`${segment.text}: ${kindLabels.join(", ")}`}
              onClick={() => openSegment(segment)}
            >
              {annotationLayerText(segment.text, segment.annotationKinds)}
            </button>
          );
        })}
      </div>

      {selectedSpan ? (
        <SelectionAnnotationMenu
          span={selectedSpan}
          labels={labelOverrides}
          onSelectKind={createAnnotation}
          onCancel={clearSelectedSpan}
        />
      ) : null}

      {activeLayeredSegment ? (
        <div
          className="source-annotation-pane__layered-dialog"
          role="dialog"
          aria-label={labels.layeredDialogLabel}
        >
          <div className="source-annotation-pane__layered-list">
            {activeLayeredSegment.annotationIds.map((annotationId) => {
              const annotation = annotationsById.get(annotationId);
              if (!annotation) return null;
              return (
                <button
                  key={annotation.id}
                  type="button"
                  className="source-annotation-pane__layered-choice"
                  onClick={() => {
                    onSelectAnnotation(annotation.id);
                    setActiveLayeredSegment(null);
                  }}
                >
                  {labels.kindLabels[annotation.kind]} · {annotation.id}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            className="source-annotation-pane__layered-close"
            onClick={() => setActiveLayeredSegment(null)}
          >
            {labels.closeLayeredDialog}
          </button>
        </div>
      ) : null}
    </section>
  );
}
