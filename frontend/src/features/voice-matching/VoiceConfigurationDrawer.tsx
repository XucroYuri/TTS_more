import { X } from "lucide-react";
import { type ReactNode, useEffect, useRef } from "react";

import "./voice-configuration-drawer.css";

interface VoiceConfigurationDrawerProps {
  open: boolean;
  title: string;
  closeLabel: string;
  onClose: () => void;
  children: ReactNode;
}

export function VoiceConfigurationDrawer({ open, title, closeLabel, onClose, children }: VoiceConfigurationDrawerProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      restoreFocusRef.current?.focus();
    };
  }, [open]);

  return (
    <div
      className="voice-config-backdrop"
      role="presentation"
      hidden={!open}
      style={open ? undefined : { display: "none" }}
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
    >
      <aside className="voice-config-drawer" role="dialog" aria-modal="true" aria-label={title} aria-hidden={!open} inert={!open ? true : undefined}>
        <header className="voice-config-head">
          <strong>{title}</strong>
          <button ref={closeRef} className="icon-button small" type="button" aria-label={closeLabel} onClick={onClose}>
            <X size={14} />
          </button>
        </header>
        <div className="voice-config-body">{children}</div>
      </aside>
    </div>
  );
}
