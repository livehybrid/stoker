import type { ReactNode } from "react";
import { useEffect } from "react";
import { cn } from "../../components/cn";

// A centred modal dialog (page-owned). Used by the Repos "Register repo" form
// and the one-time webhook-secret reveal. Closes on backdrop click / Escape
// unless `dismissible` is false (the secret reveal forces an explicit
// acknowledge so the operator does not lose the value by clicking away).
interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  width?: string;
  dismissible?: boolean;
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = "max-w-lg",
  dismissible = true,
}: ModalProps) {
  useEffect(() => {
    if (!open || !dismissible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, dismissible, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:py-16"
      role="dialog"
      aria-modal="true"
    >
      <div
        className="absolute inset-0 bg-black/60"
        onClick={dismissible ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        className={cn(
          "relative w-full rounded-lg border border-surface-muted bg-surface-soft shadow-2xl",
          width,
        )}
      >
        {title && (
          <header className="flex items-center justify-between border-b border-surface-muted px-5 py-3">
            <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
            {dismissible && (
              <button
                onClick={onClose}
                aria-label="Close"
                className="rounded-md px-2 py-1 text-slate-400 hover:bg-surface-muted hover:text-slate-100"
              >
                ✕
              </button>
            )}
          </header>
        )}
        <div className="px-5 py-4">{children}</div>
        {footer && (
          <footer className="flex items-center justify-end gap-2 border-t border-surface-muted px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}
