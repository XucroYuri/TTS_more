import { AlertTriangle, Database, Loader2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { VoiceCatalogPublicView, VoiceResourceState } from "../../types";
import "./voice-matching.css";

interface VoiceAssetStatusPanelProps {
  catalog: VoiceCatalogPublicView | null;
  syncing: boolean;
  error?: string | null;
  onSync: () => void;
}

function stateKey(state: VoiceResourceState): string {
  return `voiceMatching.resourceState.${state}`;
}

export function VoiceAssetStatusPanel({ catalog, syncing, error, onSync }: VoiceAssetStatusPanelProps) {
  const { t } = useTranslation();
  const needsAction = catalog?.resources.filter((resource) => resource.state !== "ready") ?? [];

  return (
    <section className="voice-catalog-panel" aria-label={t("voiceMatching.catalogTitle")}>
      <div className="voice-panel-heading">
        <div>
          <span className="voice-panel-eyebrow"><Database size={14} />{t("voiceMatching.catalogEyebrow")}</span>
          <h3>{t("voiceMatching.catalogTitle")}</h3>
        </div>
        <button className="secondary-button compact-button" type="button" disabled={syncing} onClick={onSync}>
          {syncing ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
          {syncing ? t("voiceMatching.syncing") : t("voiceMatching.sync")}
        </button>
      </div>

      {catalog ? (
        <>
          <div className="voice-catalog-summary">
            <span className={`voice-state-pill state-${catalog.state}`}>{t(`voiceMatching.catalogState.${catalog.state}`)}</span>
            <strong>{t("voiceMatching.resourceCount", { count: catalog.counts.resources })}</strong>
            <span>{t("voiceMatching.referenceCount", { count: catalog.counts.references })}</span>
            <span>{t("voiceMatching.weightCount", { count: catalog.counts.weights })}</span>
          </div>
          {needsAction.length > 0 && (
            <div className="voice-resource-state-list">
              {needsAction.slice(0, 4).map((resource) => (
                <div className="voice-resource-state-row" key={resource.resource_id}>
                  <span>{resource.character_id || resource.resource_id}</span>
                  <strong>{t(stateKey(resource.state))}</strong>
                </div>
              ))}
            </div>
          )}
          {catalog.diagnostics.length > 0 && (
            <details className="voice-catalog-diagnostics">
              <summary><AlertTriangle size={13} />{t("voiceMatching.diagnostics", { count: catalog.diagnostics.length })}</summary>
              <ul>
                {catalog.diagnostics.slice(0, 8).map((item, index) => (
                  <li key={`${item.code}-${item.field_path}-${index}`}><code>{item.code}</code><span>{item.field_path}</span></li>
                ))}
              </ul>
            </details>
          )}
        </>
      ) : (
        <p className="voice-panel-empty">{syncing ? t("voiceMatching.syncing") : t("voiceMatching.catalogUnavailable")}</p>
      )}
      {error && <p className="voice-panel-error" role="alert">{error}</p>}
    </section>
  );
}
