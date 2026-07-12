// Dashboard-side derivations over the operator API shapes. Live EPS and the
// observed worker count are read from a run's metric samples (one row per slot
// per heartbeat tick); the run summary itself carries no live EPS.

import type { MetricsOut, RunOut } from "../../lib/types";

// Run states that count as "active" (a fleet is or is being provisioned). These
// mirror the control plane's non-terminal states; terminal = completed | stopped
// | failed. Kept permissive: an unknown state is treated as not-active.
export const ACTIVE_STATES = [
  "pending",
  "preparing",
  "provisioning",
  "releasing",
  "running",
  "draining",
] as const;

const ACTIVE_SET = new Set<string>(ACTIVE_STATES);

export function isActiveRun(run: RunOut): boolean {
  return ACTIVE_SET.has(run.state);
}

/** Newest active runs first isn't meaningful (list is id-desc already); keep id order. */
export function activeRuns(runs: RunOut[]): RunOut[] {
  return runs.filter(isActiveRun);
}

/** The most recent failed runs (state === "failed"), newest first, capped. */
export function recentFailures(runs: RunOut[], limit = 5): RunOut[] {
  return runs.filter((r) => r.state === "failed").slice(0, limit);
}

export interface LiveMetrics {
  /** Sum of the newest per-slot EPS across the run's workers. */
  eps: number;
  /** Distinct slots observed in the window (the live worker count). */
  workers: number;
  /** True when at least one sample was present. */
  hasData: boolean;
}

/**
 * Reduce a run's metric samples to a current EPS and worker count.
 *
 * Samples arrive as one row per slot per tick. We take the newest sample for
 * each slot (samples are ordered ts, slot ascending by the API) and sum their
 * EPS; the number of distinct slots is the observed worker count.
 */
export function liveMetrics(metrics: MetricsOut | undefined): LiveMetrics {
  if (!metrics || metrics.samples.length === 0) {
    return { eps: 0, workers: 0, hasData: false };
  }
  // newestBySlot: last-seen sample wins because the API returns ascending ts.
  const newestBySlot = new Map<number, number>();
  for (const s of metrics.samples) {
    newestBySlot.set(s.slot, s.eps ?? 0);
  }
  let eps = 0;
  for (const v of newestBySlot.values()) eps += v;
  return { eps, workers: newestBySlot.size, hasData: true };
}

/** Format an EPS number compactly (e.g. 1 040, 12.3k). */
export function formatEps(eps: number): string {
  if (!Number.isFinite(eps)) return "0";
  if (eps >= 10_000) return `${(eps / 1000).toFixed(1)}k`;
  if (eps >= 1000) return Math.round(eps).toLocaleString("en-GB");
  if (eps >= 100) return String(Math.round(eps));
  return eps.toFixed(eps >= 10 ? 0 : 1);
}
