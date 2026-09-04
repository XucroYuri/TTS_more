import type { ReactNode } from "react";

export interface VoiceInspectorProps {
  children: ReactNode;
  mode: string;
}

export function VoiceInspector({ children, mode }: VoiceInspectorProps) {
  return (
    <aside className={`inspector inspector-${mode}`} aria-label="声音检查区">
      {children}
    </aside>
  );
}
