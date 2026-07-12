import type { ReactNode } from "react";
import { cn } from "./cn";

interface CardProps {
  title?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children?: ReactNode;
}

/** A surface panel with an optional header row (title + actions). */
export function Card({ title, actions, className, children }: CardProps) {
  return (
    <section
      className={cn(
        "rounded-lg border border-surface-muted bg-surface-soft shadow-sm",
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-surface-muted px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}
