import type { ReactNode } from "react";

import "./role-library.css";

export interface RoleLibraryPanelProps {
  children: ReactNode;
  empty?: boolean;
  emptyLabel?: string;
}

export function RoleLibraryPanel({ children, empty = false, emptyLabel = "暂无角色" }: RoleLibraryPanelProps) {
  return (
    <div className="role-library-workbench" aria-label="角色库">
      {empty ? <div className="role-empty-config">{emptyLabel}</div> : children}
    </div>
  );
}
