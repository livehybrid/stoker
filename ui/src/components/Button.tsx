import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "./cn";

type Variant = "primary" | "secondary" | "danger" | "ghost";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  children?: ReactNode;
}

const VARIANTS: Record<Variant, string> = {
  primary: "bg-sky-600 hover:bg-sky-500 text-white border-sky-600",
  secondary:
    "bg-surface-muted hover:bg-slate-600 text-slate-100 border-surface-muted",
  danger: "bg-red-600 hover:bg-red-500 text-white border-red-600",
  ghost:
    "bg-transparent hover:bg-surface-muted text-slate-200 border-transparent",
};

export function Button({
  variant = "secondary",
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
        "focus:outline-none focus:ring-2 focus:ring-sky-500/60",
        "disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
