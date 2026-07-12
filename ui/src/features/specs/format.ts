// Small display formatters for spec/rate values. British English copy.

import type { RateMode } from "../../lib/types";

export const RATE_MODE_LABEL: Record<RateMode, string> = {
  eps: "events/s",
  per_day_gb: "GB/day",
  count_interval: "count / interval",
};

/** One-line human summary of a spec's rate, e.g. "12 345 events/s" or
 *  "5 GB/day" or "100 every 10 s". */
export function formatRate(
  rateMode: string,
  rateValue: number | null | undefined,
  intervalS: number | null | undefined,
): string {
  if (rateMode === "count_interval") {
    const count = rateValue != null ? formatNumber(rateValue) : "?";
    const iv = intervalS != null ? `${intervalS} s` : "interval";
    return `${count} every ${iv}`;
  }
  const label = RATE_MODE_LABEL[rateMode as RateMode] ?? rateMode;
  if (rateValue == null) return label;
  return `${formatNumber(rateValue)} ${label}`;
}

/** Thousands-separated number with a sensible number of decimals. */
export function formatNumber(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  const abs = Math.abs(n);
  const dp = abs >= 100 ? 0 : abs >= 1 ? 2 : 4;
  return n.toLocaleString("en-GB", {
    minimumFractionDigits: 0,
    maximumFractionDigits: dp,
  });
}

/** Duration in seconds -> compact human string, or "unbounded" for null. */
export function formatDuration(durationS: number | null | undefined): string {
  if (durationS == null) return "unbounded";
  if (durationS <= 0) return "unbounded";
  const h = Math.floor(durationS / 3600);
  const m = Math.floor((durationS % 3600) / 60);
  const s = Math.floor(durationS % 60);
  const parts: string[] = [];
  if (h) parts.push(`${h} h`);
  if (m) parts.push(`${m} min`);
  if (s && !h) parts.push(`${s} s`);
  return parts.length ? parts.join(" ") : `${durationS} s`;
}
