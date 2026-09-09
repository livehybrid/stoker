import type { ReactNode } from "react";
import Chip from "@splunk/react-ui/Chip";

/*
 * A status pill.
 *
 * This is Splunk's Chip. The tone names are kept because call sites all over
 * the app pass them, and because they say what the colour MEANS ("amber" for
 * a transitional state) rather than which Splunk appearance implements it.
 */
type Tone = "neutral" | "green" | "amber" | "red" | "sky" | "slate";

// Chip's neutral appearance is the absence of one, so `neutral` maps to
// undefined rather than to a name.
type ChipAppearance = "outline" | "info" | "success" | "warning" | "error";

const TONES: Record<Tone, ChipAppearance | undefined> = {
  neutral: undefined,
  green: "success",
  amber: "warning",
  red: "error",
  sky: "info",
  slate: "outline",
};

export function Badge({
  tone = "neutral",
  children,
}: {
  tone?: Tone;
  children: ReactNode;
}) {
  return <Chip appearance={TONES[tone]}>{children}</Chip>;
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
