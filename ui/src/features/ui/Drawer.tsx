import type { ReactNode } from "react";
import { useEffect } from "react";
import { cn } from "../../components/cn";

// A right-side slide-over panel (page-owned, not part of the shared kit). Used
// by the Packs preview. Closes on the backdrop click or Escape. No Radix
// dependency (foundation deliberately omits it); this is a minimal, accessible
// overlay sufficient for a preview drawer.
interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  width?: string;
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  actions,
  children,
  width = "max-w-xl",
}: DrawerProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true">
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        className={cn(
          "relative flex h-full w-full flex-col border-l border-surface-muted bg-surface-soft shadow-2xl",
          width,
        )}
      >
        <header className="flex items-start justify-between gap-3 border-b border-surface-muted px-5 py-4">
          <div className="min-w-0">
            {title && (
              <h2 className="truncate text-base font-semibold text-slate-100">
                {title}
              </h2>
            )}
            {subtitle && (
              <p className="mt-0.5 truncate text-xs text-slate-500">{subtitle}</p>
            )}
          </div>
          <div className="flex items-center gap-2">
            {actions}
            <button
              onClick={onClose}
              aria-label="Close"
              className="rounded-md px-2 py-1 text-slate-400 hover:bg-surface-muted hover:text-slate-100"
            >
              ✕
            </button>
          </div>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
