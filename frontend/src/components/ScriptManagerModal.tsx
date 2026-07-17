import { Edit3, FileText, FolderOpen, List, Loader2, Plus, Save, Search, Trash2, Wand2, X } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { filterAndSortProjectSummaries, projectPreviewStats } from "../lib/scriptManagement";
import type { ProjectSummary, ScriptProject } from "../types";

interface ScriptManagerModalProps {
  open: boolean;
  variant?: "modal" | "inline";
  projects: ProjectSummary[];
  currentProjectId: string | null;
  selectedProjectId: string | null;
  selectedProject: ScriptProject | null;
  isSelectedProjectLoading: boolean;
  searchText: string;
  titleDraft: string;
  sourceDraft: string;
  newScriptTitle: string;
  newScriptSource: string;
  isCreatingScript: boolean;
  isSavingScript: boolean;
  isParsingScript: boolean;
  deletingProjectId: string | null;
  onClose: () => void;
  onSearchTextChange: (value: string) => void;
  onSelectProject: (projectId: string) => void;
  onOpenProject: (projectId: string) => void;
  onTitleDraftChange: (value: string) => void;
  onSourceDraftChange: (value: string) => void;
  onNewScriptTitleChange: (value: string) => void;
  onNewScriptSourceChange: (value: string) => void;
  onCreateScript: () => void;
  onRenameScript: () => void;
  onSaveRevision: () => void;
  onParseRevision: () => void;
  onDeleteScript: () => void;
}

type InlineDrawerTab = "list" | "edit";

export function ScriptManagerModal({
  open,
  variant = "modal",
  projects,
  currentProjectId,
  selectedProjectId,
  selectedProject,
  isSelectedProjectLoading,
  searchText,
  titleDraft,
  sourceDraft,
  newScriptTitle,
  newScriptSource,
  isCreatingScript,
  isSavingScript,
  isParsingScript,
  deletingProjectId,
  onClose,
  onSearchTextChange,
  onSelectProject,
  onOpenProject,
  onTitleDraftChange,
  onSourceDraftChange,
  onNewScriptTitleChange,
  onNewScriptSourceChange,
  onCreateScript,
  onRenameScript,
  onSaveRevision,
  onParseRevision,
  onDeleteScript
}: ScriptManagerModalProps) {
  const { t } = useTranslation();
  const drawerBaseId = useId();
  const [inlineDrawerTab, setInlineDrawerTab] = useState<InlineDrawerTab>("list");
  const visibleProjects = useMemo(() => filterAndSortProjectSummaries(projects, searchText), [projects, searchText]);
  const selectedSummary = projects.find((project) => project.project_id === selectedProjectId) ?? null;
  const stats = projectPreviewStats(selectedProject);
  const lineCount = selectedProject ? stats.lineCount : selectedSummary?.line_count ?? 0;
  const characterCount = selectedProject ? stats.characterCount : selectedSummary?.character_count ?? 0;
  const scriptRevisionCount = selectedProject ? stats.scriptRevisionCount : selectedSummary?.script_revision_count ?? 0;
  const parseRevisionCount = selectedProject ? stats.parseRevisionCount : selectedSummary?.parse_revision_count ?? 0;
  const selectedTitle = titleDraft || selectedProject?.title || selectedSummary?.title || selectedProjectId || "";
  const selectedIsCurrent = Boolean(selectedProjectId && selectedProjectId === currentProjectId);
  const busy = isCreatingScript || isSavingScript || isParsingScript || Boolean(deletingProjectId);
  const isInline = variant === "inline";

  if (!open) return null;

  if (isInline) {
    const editingExistingScript = Boolean(selectedProjectId);
    const inlineDrawerTabs: Array<{ id: InlineDrawerTab; label: string; icon: ReactNode }> = [
      { id: "list", label: t("script.drawer.list"), icon: <List size={14} /> },
      { id: "edit", label: t("script.drawer.edit"), icon: <Edit3 size={14} /> }
    ];
    const handleInlineProjectSelect = (projectId: string) => {
      onSelectProject(projectId);
      setInlineDrawerTab("edit");
    };
    const handleInlineTabKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
      const tabOrder: InlineDrawerTab[] = ["list", "edit"];
      const activeIndex = tabOrder.indexOf(inlineDrawerTab);
      let nextTab: InlineDrawerTab | null = null;

      if (event.key === "ArrowLeft") nextTab = tabOrder[Math.max(0, activeIndex - 1)];
      if (event.key === "ArrowRight") nextTab = tabOrder[Math.min(tabOrder.length - 1, activeIndex + 1)];
      if (event.key === "Home") nextTab = tabOrder[0];
      if (event.key === "End") nextTab = tabOrder[tabOrder.length - 1];
      if (!nextTab || nextTab === inlineDrawerTab) return;

      event.preventDefault();
      setInlineDrawerTab(nextTab);
      event.currentTarget.querySelector<HTMLButtonElement>(`[data-drawer-tab="${nextTab}"]`)?.focus();
    };

    return (
      <section className="script-manager-modal script-manager-inline" role="region" aria-labelledby="script-manager-title">
        <header className="script-manager-head">
          <div>
            <strong id="script-manager-title">{t("script.workspaceTitle")}</strong>
            <span>{selectedTitle ? t("script.workspaceActive", { title: selectedTitle }) : t("script.workspaceHint")}</span>
          </div>
        </header>

        <div className="script-manager-inline-tabs" role="tablist" aria-label={t("script.workspaceTitle")} onKeyDown={handleInlineTabKeyDown}>
          {inlineDrawerTabs.map((tab) => (
            <button
              className={`script-manager-inline-tab ${inlineDrawerTab === tab.id ? "active" : ""}`}
              data-drawer-tab={tab.id}
              id={`${drawerBaseId}-${tab.id}-tab`}
              key={tab.id}
              onClick={() => setInlineDrawerTab(tab.id)}
              role="tab"
              type="button"
              aria-controls={`${drawerBaseId}-${tab.id}-panel`}
              aria-selected={inlineDrawerTab === tab.id}
              tabIndex={inlineDrawerTab === tab.id ? 0 : -1}
            >
              {tab.icon}
              <span>{tab.label}</span>
            </button>
          ))}
        </div>

        <div className="script-manager-body">
          <div className="script-manager-inline-track" data-active-drawer={inlineDrawerTab}>
            <aside
              className="script-manager-list-panel script-manager-drawer-panel"
              id={`${drawerBaseId}-list-panel`}
              role="tabpanel"
              aria-labelledby={`${drawerBaseId}-list-tab`}
              aria-hidden={inlineDrawerTab !== "list"}
              inert={inlineDrawerTab !== "list" ? true : undefined}
            >
              <label className="script-manager-search">
                <Search size={14} />
                <input value={searchText} onChange={(event) => onSearchTextChange(event.target.value)} placeholder={t("script.searchScripts")} />
              </label>
              <div className="script-manager-list" role="listbox" aria-label={t("script.existingScripts")}>
                {visibleProjects.map((project) => (
                  <button
                    className={`script-manager-row ${project.project_id === selectedProjectId ? "active" : ""}`}
                    key={project.project_id}
                    onClick={() => handleInlineProjectSelect(project.project_id)}
                    role="option"
                    type="button"
                    aria-selected={project.project_id === selectedProjectId}
                  >
                    <span>
                      <strong>{project.title || project.project_id}</strong>
                      {project.project_id === currentProjectId && <small>{t("app.currentProject")}</small>}
                    </span>
                    <em>{t("script.projectRowMeta", { lines: project.line_count, revisions: project.parse_revision_count ?? 0 })}</em>
                  </button>
                ))}
                {visibleProjects.length === 0 && (
                  <div className="empty-row project-empty-state">
                    <strong>{t("empty.noProjects")}</strong>
                    <span>{t("script.noProjectMatches")}</span>
                  </div>
                )}
              </div>
            </aside>

            <section
              className="script-manager-action-panel script-manager-drawer-panel"
              id={`${drawerBaseId}-edit-panel`}
              role="tabpanel"
              aria-labelledby={`${drawerBaseId}-edit-tab`}
              aria-hidden={inlineDrawerTab !== "edit"}
              inert={inlineDrawerTab !== "edit" ? true : undefined}
            >
              <label>
                <span>{t("script.newScriptTitle")}</span>
                <input
                  value={editingExistingScript ? titleDraft : newScriptTitle}
                  onChange={(event) => editingExistingScript ? onTitleDraftChange(event.target.value) : onNewScriptTitleChange(event.target.value)}
                  disabled={editingExistingScript && isSelectedProjectLoading}
                  placeholder={t("script.newScriptTitlePlaceholder")}
                />
              </label>
              <label className="script-manager-source-field">
                <span>{t("script.currentSource")}</span>
                <textarea
                  className="script-manager-source-editor"
                  value={editingExistingScript ? sourceDraft : newScriptSource}
                  onChange={(event) => editingExistingScript ? onSourceDraftChange(event.target.value) : onNewScriptSourceChange(event.target.value)}
                  disabled={editingExistingScript && isSelectedProjectLoading}
                  placeholder={editingExistingScript ? t("script.emptySourcePreview") : t("script.newScriptSourcePlaceholder")}
                />
              </label>
              <div className="script-manager-inline-actions">
                {editingExistingScript ? (
                  <>
                    {selectedProjectId && !selectedIsCurrent && (
                      <button className="secondary-button" type="button" onClick={() => onOpenProject(selectedProjectId)} disabled={busy}>
                        <FolderOpen size={13} /> {t("script.openScript")}
                      </button>
                    )}
                    <button className="secondary-button" type="button" onClick={onSaveRevision} disabled={!selectedProjectId || isSelectedProjectLoading || busy}>
                      {isSavingScript ? <Loader2 className="spin" size={14} /> : <Save size={14} />} {t("script.saveRevision")}
                    </button>
                    <button className="primary-button" type="button" onClick={onParseRevision} disabled={!selectedProjectId || isSelectedProjectLoading || busy}>
                      {isParsingScript ? <Loader2 className="spin" size={14} /> : <Wand2 size={14} />} {t("script.parseRevision")}
                    </button>
                    <button className="secondary-button danger-button" type="button" onClick={onDeleteScript} disabled={!selectedProjectId || isSelectedProjectLoading || busy}>
                      {deletingProjectId ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />} {t("script.deleteScript")}
                    </button>
                  </>
                ) : (
                  <button className="primary-button" type="button" onClick={onCreateScript} disabled={busy}>
                    {isCreatingScript ? <Loader2 className="spin" size={15} /> : <Plus size={15} />} {t("script.createScript")}
                  </button>
                )}
              </div>
            </section>
          </div>
        </div>
      </section>
    );
  }

  const content = (
      <section className="script-manager-modal" role="dialog" aria-modal aria-labelledby="script-manager-title">
        <header className="script-manager-head">
          <div>
            <strong id="script-manager-title">{t("script.managerTitle")}</strong>
            <span>{t("script.managerHint")}</span>
          </div>
          <button className="icon-button small" type="button" onClick={onClose} title={t("actions.close")}><X size={14} /></button>
        </header>

        <div className="script-manager-body">
          <aside className="script-manager-list-panel">
            <label className="script-manager-search">
              <Search size={14} />
              <input value={searchText} onChange={(event) => onSearchTextChange(event.target.value)} placeholder={t("script.searchScripts")} />
            </label>
            <div className="script-manager-list" role="listbox" aria-label={t("script.existingScripts")}>
              {visibleProjects.map((project) => (
                <button
                  className={`script-manager-row ${project.project_id === selectedProjectId ? "active" : ""}`}
                  key={project.project_id}
                  onClick={() => onSelectProject(project.project_id)}
                  role="option"
                  type="button"
                  aria-selected={project.project_id === selectedProjectId}
                >
                  <span>
                    <strong>{project.title || project.project_id}</strong>
                    {project.project_id === currentProjectId && <small>{t("app.currentProject")}</small>}
                  </span>
                  <em>{t("script.projectRowMeta", { lines: project.line_count, revisions: project.parse_revision_count ?? 0 })}</em>
                </button>
              ))}
              {visibleProjects.length === 0 && (
                <div className="empty-row project-empty-state">
                  <strong>{t("empty.noProjects")}</strong>
                  <span>{t("script.noProjectMatches")}</span>
                </div>
              )}
            </div>
          </aside>

          <section className="script-manager-preview-panel">
            <div className="script-manager-selected-head">
              <div>
                <span>{selectedIsCurrent ? t("script.activeScript") : t("script.selectedScript")}</span>
                <strong>{selectedTitle || t("empty.noProjectSelected")}</strong>
              </div>
              {selectedProjectId && (
                <button className="secondary-button compact-button" type="button" onClick={() => onOpenProject(selectedProjectId)} disabled={selectedIsCurrent}>
                  <FolderOpen size={13} /> {selectedIsCurrent ? t("app.currentProject") : t("script.openScript")}
                </button>
              )}
            </div>

            <div className="script-manager-stats" aria-label={t("script.previewStats")}>
              <div><span>{t("script.lineCount")}</span><strong>{lineCount}</strong></div>
              <div><span>{t("script.characters")}</span><strong>{characterCount}</strong></div>
              <div><span>{t("script.scriptRevisions")}</span><strong>{scriptRevisionCount}</strong></div>
              <div><span>{t("script.parseRevisions")}</span><strong>{parseRevisionCount}</strong></div>
            </div>

            <div className="script-manager-preview">
              {isSelectedProjectLoading ? (
                <div className="table-empty"><Loader2 className="spin" size={16} /> {t("script.loadingScript")}</div>
              ) : sourceDraft.trim() ? (
                <div className="markdown-preview">{renderMarkdownPreview(sourceDraft)}</div>
              ) : (
                <div className="table-empty"><FileText size={16} /> {t("script.emptySourcePreview")}</div>
              )}
            </div>
          </section>

          <aside className="script-manager-action-panel">
            <section className="script-manager-card">
              <strong>{t("script.newScript")}</strong>
              <label>
                <span>{t("script.newScriptTitle")}</span>
                <input value={newScriptTitle} onChange={(event) => onNewScriptTitleChange(event.target.value)} placeholder={t("script.newScriptTitlePlaceholder")} />
              </label>
              <label>
                <span>{t("script.newScriptSource")}</span>
                <textarea value={newScriptSource} onChange={(event) => onNewScriptSourceChange(event.target.value)} placeholder={t("script.newScriptSourcePlaceholder")} rows={4} />
              </label>
              <button className="primary-button" type="button" onClick={onCreateScript} disabled={busy}>
                {isCreatingScript ? <Loader2 className="spin" size={15} /> : <Plus size={15} />} {t("script.createScript")}
              </button>
            </section>

            <section className="script-manager-card">
              <strong>{t("script.editSelectedScript")}</strong>
              <label>
                <span>{t("script.newScriptTitle")}</span>
                <input value={titleDraft} onChange={(event) => onTitleDraftChange(event.target.value)} disabled={!selectedProjectId || isSelectedProjectLoading} />
              </label>
              <button className="secondary-button" type="button" onClick={onRenameScript} disabled={!selectedProjectId || isSelectedProjectLoading || busy}>
                <Edit3 size={14} /> {t("script.renameScript")}
              </button>
              <label>
                <span>{t("script.currentSource")}</span>
                <textarea className="script-manager-source-editor" value={sourceDraft} onChange={(event) => onSourceDraftChange(event.target.value)} disabled={!selectedProjectId || isSelectedProjectLoading} rows={8} />
              </label>
              <div className="script-manager-action-grid">
                <button className="secondary-button" type="button" onClick={onSaveRevision} disabled={!selectedProjectId || isSelectedProjectLoading || busy}>
                  {isSavingScript ? <Loader2 className="spin" size={14} /> : <Save size={14} />} {t("script.saveRevision")}
                </button>
                <button className="primary-button" type="button" onClick={onParseRevision} disabled={!selectedProjectId || isSelectedProjectLoading || busy}>
                  {isParsingScript ? <Loader2 className="spin" size={14} /> : <Wand2 size={14} />} {t("script.parseRevision")}
                </button>
              </div>
            </section>

            <section className="script-manager-card danger">
              <strong>{t("script.dangerZone")}</strong>
              <span>{t("script.deleteScriptHint")}</span>
              <button className="secondary-button danger-button" type="button" onClick={onDeleteScript} disabled={!selectedProjectId || isSelectedProjectLoading || busy}>
                {deletingProjectId ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />} {t("script.deleteScript")}
              </button>
            </section>
          </aside>
        </div>
      </section>
  );

  return (
    <div className="script-manager-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      {content}
    </div>
  );
}

function renderMarkdownPreview(markdown: string) {
  return markdown.split(/\r?\n/).map((line, index) => {
    const key = `${index}-${line.slice(0, 12)}`;
    if (!line.trim()) return <div className="markdown-line blank" key={key} />;
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) return <div className={`markdown-line heading h${heading[1].length}`} key={key}>{heading[2]}</div>;
    if (line.trimStart().startsWith(">")) return <blockquote className="markdown-line quote" key={key}>{line.replace(/^\s*>\s?/, "")}</blockquote>;
    if (line.trimStart().startsWith("`") && line.trimEnd().endsWith("`")) return <code className="markdown-line inline-code" key={key}>{line.replace(/^`|`$/g, "")}</code>;
    return <p className="markdown-line" key={key}>{line}</p>;
  });
}
