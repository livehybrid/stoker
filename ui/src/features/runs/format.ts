// Formatting + small domain helpers for the Runs pages.
//
// Page-specific (lives under src/features/runs). Nothing here talks to the API;
// it only shapes values that come off the wire (server/schemas.py) for display.

// Run states from server/lifecycle.py. Terminal states stop the 5 s poll.
export const TERMINAL_STATES = new Set(["completed", "stopped", "failed"]);

/** A run in a terminal state has finished; the detail view stops polling. */
export function isTerminal(state: string | null | undefined): boolean {
  return TERMINAL_STATES.has((state || "").toLowerCase());
}

// Human labels for the end_reason codes lifecycle.py stamps on a finished run.
const END_REASON_LABELS: Record<string, string> = {
  completed: "Completed",
  "duration-complete": "Duration reached",
  "operator-stop": "Stopped by operator",
  "provision-failed": "Provisioning failed",
  "provision-timeout": "Provisioning timed out",
  "strict-release-timeout": "Strict release timed out",
  "auto-abort-lost": "Auto-aborted (workers lost)",
  "auto-abort-auth": "Auto-aborted (HEC auth failed)",
  "all-workers-lost": "All workers lost",
  orphaned: "Orphaned",
};

/** A readable label for an end_reason code, falling back to the raw value. */
export function endReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "—";
  return END_REASON_LABELS[reason] ?? reason;
}

/** A tone for an end reason: green for clean finishes, red for faults. */
export function endReasonTone(
  reason: string | null | undefined,
): "green" | "red" | "slate" {
  if (!reason) return "slate";
  if (reason === "completed" || reason === "duration-complete") return "green";
  if (reason === "operator-stop") return "slate";
  return "red";
}

/** Compact integer with thin thousands separators (e.g. 1 040 123). */
export function fmtInt(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return Math.round(value).toLocaleString("en-GB");
}

/** A number to a fixed number of decimals, dropping a trailing ".0…". */
export function fmtNum(
  value: number | null | undefined,
  digits = 1,
): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-GB", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

/** Bytes to a human size (KB/MB/GB, base 1024). */
export function fmtBytes(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let n = value / 1024;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`;
}

/** Bytes-per-second to a human rate (e.g. 1.2 MB/s). */
export function fmtBps(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${fmtBytes(value)}/s`;
}

/** Seconds to a compact duration (e.g. 90 s, 12 min, 2 h 05 m). */
export function fmtDurationS(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  const s = Math.max(0, Math.round(value));
  if (s < 90) return `${s} s`;
  const mins = Math.floor(s / 60);
  if (mins < 90) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return `${h} h ${String(m).padStart(2, "0")} m`;
}

/** Elapsed time between two ISO timestamps (or from start to now). */
export function fmtElapsed(
  from: string | null | undefined,
  to?: string | null | undefined,
): string {
  if (!from) return "—";
  const start = Date.parse(from);
  if (Number.isNaN(start)) return "—";
  const end = to ? Date.parse(to) : Date.now();
  if (Number.isNaN(end)) return "—";
  return fmtDurationS((end - start) / 1000);
}

/** ISO timestamp -> local date+time (en-GB), or an em dash when absent. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Date(ms).toLocaleString("en-GB", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** ISO timestamp -> local clock time only (for chart axes / event logs). */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Date(ms).toLocaleTimeString("en-GB");
}

/** ISO timestamp -> a date-only key (yyyy-mm-dd) for the date filter. */
export function toDateKey(iso: string | null | undefined): string {
  if (!iso) return "";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// The rate mode a run was frozen with (from the snapshot). Drives whether the
// "target" line on the chart is an EPS or a GB/day-derived figure.
export type SnapshotRate = {
  rate_mode?: string;
  rate_value?: number | null;
  workers?: number;
};

/** Pull a single numeric share value from a lease's single-key share_json. */
export function shareValue(
  share: Record<string, unknown> | null | undefined,
): number | null {
  if (!share) return null;
  for (const key of ["eps", "per_day_gb", "count"]) {
    const v = share[key];
    if (typeof v === "number" && !Number.isNaN(v)) return v;
  }
  return null;
}
