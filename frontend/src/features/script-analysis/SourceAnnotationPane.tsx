import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";

import type { AnnotationKind, ScriptRevision, SemanticAnnotation, SourceSpan } from "../../types";
import { splitAnnotatedText, type AnnotationTextSegment } from "./annotationView";
import { domRangeToSourceSpan } from "./selectionOffsets";
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
  const labels = useMemo(() => resolveAnnotationPaneLabels(labelOverrides), [labelOverrides]);
  const segments = useMemo(
    () => splitAnnotatedText(sourceRevision.source_markdown, annotations),
    [annotations, sourceRevision.source_markdown]
  );
  const annotationsById = useMemo(
    () => new Map(annotations.map((annotation) => [annotation.id, annotation])),
    [annotations]
  );

  const captureSelection = useCallback(() => {
    const root = sourceRootRef.current;
    const selection = root?.ownerDocument.defaultView?.getSelection();
    if (!root || !selection || selection.rangeCount !== 1 || selection.isCollapsed) {
      setSelectedSpan(null);
      return;
    }
    try {
      setSelectedSpan(domRangeToSourceSpan(root, selection.getRangeAt(0), sourceRevision));
      setActiveLayeredSegment(null);
    } catch {
      setSelectedSpan(null);
    }
  }, [sourceRevision]);

  const createAnnotation = useCallback(
    (kind: AnnotationKind) => {
      if (!selectedSpan) return;
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
      sourceRootRef.current?.ownerDocument.defaultView?.getSelection()?.removeAllRanges();
      setSelectedSpan(null);
    },
    [createAnnotationId, now, onCreateAnnotation, selectedSpan]
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
        onMouseUp={captureSelection}
        onKeyUp={captureSelection}
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
          onCancel={() => setSelectedSpan(null)}
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
