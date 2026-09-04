import type { ReactNode } from "react";

import "./workbench-shell.css";

export interface WorkbenchWarning {
  id: string;
  title: string;
  content: ReactNode;
}

export interface WorkbenchShellProps {
  stage: "analysis" | "tts";
  sidebar: ReactNode;
  topbar?: ReactNode;
  warnings?: WorkbenchWarning[];
  overlays?: ReactNode;
  onStageChange?: (stage: "analysis" | "tts") => void;
  children: ReactNode;
}

export function WorkbenchShell({
  stage,
  sidebar,
  topbar,
  warnings = [],
  overlays,
  onStageChange,
  children
}: WorkbenchShellProps) {
  return (
    <div className={`app-shell ${stage === "analysis" ? "app-shell-analysis" : ""}`}>
      <aside className="sidebar">{sidebar}</aside>
      <main className={`workspace ${stage === "analysis" ? "workspace-analysis" : ""}`}>
        {(topbar || onStageChange || warnings.length > 0) && (
          <header className="workbench-shell-header">
            {onStageChange && (
              <nav className="workbench-stage-nav" aria-label="工作台阶段">
                <button type="button" aria-current={stage === "analysis" ? "step" : undefined} onClick={() => onStageChange("analysis")}>剧本分析</button>
                <button type="button" aria-current={stage === "tts" ? "step" : undefined} onClick={() => onStageChange("tts")}>配音工作台</button>
              </nav>
            )}
            {topbar}
            {warnings.length > 0 && (
              <aside className="workbench-notifications" aria-label="通知与待处理事项">
                {warnings.map((warning) => (
                  <details key={warning.id}>
                    <summary>{warning.title}</summary>
                    {warning.content}
                  </details>
                ))}
              </aside>
            )}
          </header>
        )}
        {children}
      </main>
      {overlays}
    </div>
  );
}
