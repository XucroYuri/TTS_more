import type { AnnotationKind, SourceSpan } from "../../types";

export interface AnnotationPaneLabels {
  kindLabels: Record<AnnotationKind, string>;
  markLabels: Record<AnnotationKind, string>;
  markAriaLabels: Record<AnnotationKind, string>;
  sourceRegionLabel: string;
  legendLabel: string;
  selectionToolbarLabel: string;
  selectedTextPrefix: string;
  layeredDialogLabel: string;
  closeLayeredDialog: string;
  cancelSelection: string;
}

export const defaultAnnotationPaneLabels: AnnotationPaneLabels = {
  kindLabels: {
    speaker: "说话者 / Speaker",
    emotion_evidence: "情感证据 / Emotion evidence",
    dialogue: "台词 / Dialogue"
  },
  markLabels: {
    speaker: "标记为说话者",
    emotion_evidence: "标记为情感证据",
    dialogue: "标记为台词"
  },
  markAriaLabels: {
    speaker: "标记为说话者 / Mark as speaker",
    emotion_evidence: "标记为情感证据 / Mark as emotion evidence",
    dialogue: "标记为台词 / Mark as dialogue"
  },
  sourceRegionLabel: "剧本原文 / Script source",
  legendLabel: "标注图例 / Annotation legend",
  selectionToolbarLabel: "创建原文标注 / Create source annotation",
  selectedTextPrefix: "已选择 / Selected",
  layeredDialogLabel: "该片段的标注 / Annotations on this text",
  closeLayeredDialog: "关闭 / Close",
  cancelSelection: "取消 / Cancel"
};

export type AnnotationPaneLabelOverrides = Partial<
  Omit<AnnotationPaneLabels, "kindLabels" | "markLabels" | "markAriaLabels">
> & {
  kindLabels?: Partial<Record<AnnotationKind, string>>;
  markLabels?: Partial<Record<AnnotationKind, string>>;
  markAriaLabels?: Partial<Record<AnnotationKind, string>>;
};

export function resolveAnnotationPaneLabels(
  overrides?: AnnotationPaneLabelOverrides
): AnnotationPaneLabels {
  return {
    ...defaultAnnotationPaneLabels,
    ...overrides,
    kindLabels: { ...defaultAnnotationPaneLabels.kindLabels, ...overrides?.kindLabels },
    markLabels: { ...defaultAnnotationPaneLabels.markLabels, ...overrides?.markLabels },
    markAriaLabels: {
      ...defaultAnnotationPaneLabels.markAriaLabels,
      ...overrides?.markAriaLabels
    }
  };
}

export interface SelectionAnnotationMenuProps {
  span: SourceSpan;
  onSelectKind: (kind: AnnotationKind) => void;
  onCancel: () => void;
  labels?: AnnotationPaneLabelOverrides;
}

const annotationKinds: AnnotationKind[] = ["speaker", "emotion_evidence", "dialogue"];

export function SelectionAnnotationMenu({
  span,
  onSelectKind,
  onCancel,
  labels: labelOverrides
}: SelectionAnnotationMenuProps) {
  const labels = resolveAnnotationPaneLabels(labelOverrides);
  return (
    <div className="selection-annotation-menu" role="toolbar" aria-label={labels.selectionToolbarLabel}>
      <span className="selection-annotation-menu__selection">
        {labels.selectedTextPrefix}: “{span.text}”
      </span>
      <div className="selection-annotation-menu__actions">
        {annotationKinds.map((kind) => (
          <button
            key={kind}
            type="button"
            className={`selection-annotation-menu__button selection-annotation-menu__button--${kind}`}
            aria-label={labels.markAriaLabels[kind]}
            onClick={() => onSelectKind(kind)}
          >
            {labels.markLabels[kind]}
          </button>
        ))}
        <button type="button" className="selection-annotation-menu__cancel" onClick={onCancel}>
          {labels.cancelSelection}
        </button>
      </div>
    </div>
  );
}
