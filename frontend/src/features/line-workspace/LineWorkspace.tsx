import type { ReactNode } from "react";

export interface LineWorkspaceProps {
  lineList: ReactNode;
  inspector: ReactNode;
}

export function LineWorkspace({ lineList, inspector }: LineWorkspaceProps) {
  return (
    <section className="workbench-grid" aria-label="台词工作区">
      <div className="lines-panel">{lineList}</div>
      {inspector}
    </section>
  );
}
