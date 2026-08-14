import { useEffect, useState } from "react";
import { KeyRound, X } from "lucide-react";

import { fetchAuthStatus, getApiToken, setApiToken } from "../api";

/**
 * Optional API-token gate.
 *
 * When the backend has ``TTS_MORE_API_TOKEN`` configured, mutating requests
 * return 401 until a valid token is supplied. This component:
 *  - checks ``GET /api/auth/status`` on mount;
 *  - listens for ``tts-more:auth-required`` events (dispatched by api.ts on a
 *    401) and opens a small dialog to enter the token;
 *  - holds the token in memory only (never persisted), so a reload clears it
 *    and this gate re-prompts when the backend requires auth.
 *
 * When auth is disabled on the backend, this component renders nothing.
 */
export function TokenGate() {
  const [authRequired, setAuthRequired] = useState(false);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetchAuthStatus()
      .then((status) => {
        if (!cancelled) {
          setAuthRequired(status.auth_required);
          if (status.auth_required && !getApiToken()) {
            setOpen(true);
          }
        }
      })
      .catch(() => {
        /* backend unreachable; nothing to gate */
      });
    const onAuthRequired = () => setOpen(true);
    window.addEventListener("tts-more:auth-required", onAuthRequired);
    return () => {
      cancelled = true;
      window.removeEventListener("tts-more:auth-required", onAuthRequired);
    };
  }, []);

  if (!authRequired && !open) return null;

  const saved = () => {
    setApiToken(draft.trim());
    setOpen(false);
    setDraft("");
    // No reload: the token is held in memory only, so a reload would clear it
    // and immediately re-trigger the gate. Subsequent requests carry the token.
  };

  if (!open) {
    return (
      <button
        type="button"
        className="token-gate-badge"
        title="API token configured — click to change"
        onClick={() => {
          setDraft(getApiToken());
          setOpen(true);
        }}
      >
        <KeyRound size={14} />
      </button>
    );
  }

  return (
    <div className="token-gate-overlay" role="dialog" aria-modal="true">
      <div className="token-gate-dialog">
        <div className="token-gate-header">
          <span>
            <KeyRound size={16} /> API Token
          </span>
          {getApiToken() && (
            <button type="button" onClick={() => setOpen(false)} aria-label="close">
              <X size={16} />
            </button>
          )}
        </div>
        <p className="token-gate-hint">
          后端已启用 API Token 认证。请输入共享 Token 以继续操作。
        </p>
        <input
          type="password"
          value={draft}
          autoFocus
          placeholder="Bearer token"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") saved();
          }}
        />
        <div className="token-gate-actions">
          <button type="button" className="primary" onClick={saved} disabled={!draft.trim()}>
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
