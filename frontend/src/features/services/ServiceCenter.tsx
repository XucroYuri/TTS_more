import { useEffect, useRef, type ReactNode } from "react";
import { Loader2, RefreshCw, X } from "lucide-react";

import "./service-center.css";

export interface ServiceCenterProps {
  title: string;
  description?: string;
  className?: string;
  refreshing?: boolean;
  refreshLabel: string;
  closeLabel: string;
  onRefresh: () => void;
  onClose: () => void;
  children: ReactNode;
}

export function ServiceCenter({
  title,
  description,
  className = "",
  refreshing = false,
  refreshLabel,
  closeLabel,
  onRefresh,
  onClose,
  children
}: ServiceCenterProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", closeOnEscape);
    closeRef.current?.focus();
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  return (
    <div className="service-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className={`service-modal ${className}`} role="dialog" aria-modal="true" aria-label={title}>
        <header className="service-modal-head">
          <div>
            <strong>{title}</strong>
            {description && <span>{description}</span>}
          </div>
          <div className="service-modal-actions">
            <button className="icon-button small" type="button" onClick={onRefresh} aria-label={refreshLabel} disabled={refreshing}>
              {refreshing ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
            </button>
            <button ref={closeRef} className="icon-button small" type="button" onClick={onClose} aria-label={closeLabel}><X size={14} /></button>
          </div>
        </header>
        <div className="service-modal-body">{children}</div>
      </section>
    </div>
  );
}
