import { useEffect, useState } from "react";

import type { AnnotationKind, SourceSpan } from "../../types";

export interface AnnotationPaneLabels {
  kindLabels: Record<AnnotationKind, string>;
  sourceRegionLabel: string;
  legendLabel: string;
  selectionDialogLabel: string;
  applySelection: string;
  closeSelection: string;
}

export const defaultAnnotationPaneLabels: AnnotationPaneLabels = {
  kindLabels: {
    speaker: "说话者",
    emotion_evidence: "情感证据",
    dialogue: "台词"
  },
  sourceRegionLabel: "剧本原文",
  legendLabel: "标注图例",
  selectionDialogLabel: "编辑原文标注",
  applySelection: "应用",
  closeSelection: "关闭"
};

export type AnnotationPaneLabelOverrides = Partial<Omit<AnnotationPaneLabels, "kindLabels">> & {
  kindLabels?: Partial<Record<AnnotationKind, string>>;
};

export function resolveAnnotationPaneLabels(
  overrides?: AnnotationPaneLabelOverrides
): AnnotationPaneLabels {
  return {
    ...defaultAnnotationPaneLabels,
    ...overrides,
    kindLabels: { ...defaultAnnotationPaneLabels.kindLabels, ...overrides?.kindLabels }
  };
}

export interface SelectionAnnotationMenuProps {
  span: SourceSpan;
  initialKinds?: AnnotationKind[];
  anchor?: { left: number; top: number } | null;
  onApply: (kinds: AnnotationKind[]) => void;
  onCancel: () => void;
  labels?: AnnotationPaneLabelOverrides;
}

const annotationKinds: AnnotationKind[] = ["speaker", "emotion_evidence", "dialogue"];

export function SelectionAnnotationMenu({
  span,
  initialKinds = [],
  anchor = null,
  onApply,
  onCancel,
  labels: labelOverrides
}: SelectionAnnotationMenuProps) {
  const labels = resolveAnnotationPaneLabels(labelOverrides);
  const [selectedKinds, setSelectedKinds] = useState<Set<AnnotationKind>>(
    () => new Set(initialKinds)
  );

  useEffect(() => {
    setSelectedKinds(new Set(initialKinds));
  }, [initialKinds]);

  const toggleKind = (kind: AnnotationKind) => {
    setSelectedKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  return (
    <div
      className="selection-annotation-menu"
      role="dialog"
      aria-label={labels.selectionDialogLabel}
      data-selection-start={span.start_utf16}
      data-selection-end={span.end_utf16}
      style={anchor ? { left: anchor.left, top: anchor.top } : undefined}
    >
      <div className="selection-annotation-menu__types">
        {annotationKinds.map((kind) => (
          <label
            key={kind}
            className={`selection-annotation-menu__type selection-annotation-menu__type--${kind}`}
          >
            <input
              type="checkbox"
              value={kind}
              checked={selectedKinds.has(kind)}
              onChange={() => toggleKind(kind)}
            />
            <span>{labels.kindLabels[kind]}</span>
          </label>
        ))}
      </div>
      <div className="selection-annotation-menu__actions">
        <button type="button" className="selection-annotation-menu__close" onClick={onCancel}>
          {labels.closeSelection}
        </button>
        <button
          type="button"
          className="selection-annotation-menu__apply"
          onClick={() => onApply(annotationKinds.filter((kind) => selectedKinds.has(kind)))}
        >
          {labels.applySelection}
        </button>
      </div>
    </div>
  );
}
