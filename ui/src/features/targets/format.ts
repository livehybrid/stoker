// Small formatting helpers for the Targets page. No secret values ever pass
// through here (tokens are write-only server-side and never returned).

/** Format a GB/day number for display, or an em-dash placeholder when unset. */
export function formatGb(
  value: number | null | undefined,
  suffix = "",
): string {
  if (value === null || value === undefined) return "—";
  // Trim to at most 3 dp; String() drops any trailing zeros for tidy display.
  const text = String(Math.round(value * 1000) / 1000);
  return suffix ? `${text} ${suffix}` : text;
}

/** Human label for a target's concurrent GB/day cap ("no cap" when unset). */
export function formatCap(value: number | null | undefined): string {
  if (value === null || value === undefined || value <= 0) return "no cap";
  return `${formatGb(value)} GB/day`;
}
