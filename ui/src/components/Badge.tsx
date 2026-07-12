import type { ReactNode } from "react";
import { cn } from "./cn";

type Tone = "neutral" | "green" | "amber" | "red" | "sky" | "slate";

const TONES: Record<Tone, string> = {
  neutral: "bg-surface-muted text-slate-200",
  green: "bg-emerald-900/60 text-emerald-300 border border-emerald-700/60",
  amber: "bg-amber-900/50 text-amber-300 border border-amber-700/60",
  red: "bg-red-900/60 text-red-300 border border-red-700/60",
  sky: "bg-sky-900/60 text-sky-300 border border-sky-700/60",
  slate: "bg-slate-800 text-slate-400 border border-slate-700",
};

export function Badge({
  tone = "neutral",
  children,
}: {
  tone?: Tone;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

// Map a target/run health or state string to a badge tone.
// Target health: unknown | green | amber | red.
// Run states: pending/preparing/provisioning/releasing/running/draining +
// terminal (finished/failed/aborted/…). Kept permissive so unknown states from
// a newer server still render (as neutral) rather than breaking.
export function toneForState(state: string | null | undefined): Tone {
  const s = (state || "").toLowerCase();
  if (s === "green" || s === "running" || s === "finished" || s === "up") {
    return "green";
  }
  if (
    s === "amber" ||
    s === "draining" ||
    s === "releasing" ||
    s === "preparing" ||
    s === "provisioning" ||
    s === "pending"
  ) {
    return "amber";
  }
  if (
    s === "red" ||
    s === "failed" ||
    s === "aborted" ||
    s === "error" ||
    s === "down" ||
    s === "denied"
  ) {
    return "red";
  }
  if (s === "unknown" || s === "") return "slate";
  return "neutral";
}

/** A badge whose colour is derived from a state/health string. */
export function StatusBadge({ state }: { state: string | null | undefined }) {
  return <Badge tone={toneForState(state)}>{state || "unknown"}</Badge>;
}
