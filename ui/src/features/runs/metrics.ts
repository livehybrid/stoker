// Metric-sample shaping for the Run detail charts.
//
// The API returns raw per-slot metric_samples (server/schemas.py MetricSampleOut):
// one row per worker slot per heartbeat (~5 s). eps/bps/lag_s/queue_depth/rss_mb
// are instantaneous per-slot gauges; events_total/bytes_total/hec_* are CUMULATIVE
// counters. To draw fleet-wide series we:
//   * bucket rows by timestamp (rows already share a heartbeat instant),
//   * SUM the gauges (eps, bps) across slots in a bucket for the actual-rate line,
//   * DIFF the cumulative HEC counters per slot between consecutive buckets and
//     sum those deltas for the stacked 2xx/4xx/5xx/timeout chart.
//
// Pure functions over MetricSampleOut[]; no API access here.

import type { LeaseOut, MetricSampleOut } from "../../lib/types";
import { shareValue } from "./format";

export interface RatePoint {
  ts: number; // epoch ms (bucket key)
  label: string; // clock label for the axis
  eps: number; // summed actual events/s across slots
  bps: number; // summed actual bytes/s across slots
  target: number | null; // target events/s (from lease shares), constant line
}

export interface HecPoint {
  ts: number;
  label: string;
  ok: number; // 2xx in this interval (delta, summed across slots)
  client: number; // 4xx
  server: number; // 5xx
  timeout: number; // timeouts
}

export interface LagPoint {
  ts: number;
  label: string;
  lag: number; // max lag_s across slots in the bucket (worst slot)
  queue: number; // summed queue depth across slots
}

function clockLabel(ms: number): string {
  return new Date(ms).toLocaleTimeString("en-GB");
}

// Round a sample timestamp to whole seconds so slot rows from the same heartbeat
// collapse onto one bucket even if their inserts differ by a few milliseconds.
function bucketKey(iso: string): number {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return 0;
  return Math.round(ms / 1000) * 1000;
}

/** Group samples by second, preserving order. */
function groupByBucket(
  samples: MetricSampleOut[],
): Map<number, MetricSampleOut[]> {
  const buckets = new Map<number, MetricSampleOut[]>();
  for (const s of samples) {
    const key = bucketKey(s.ts);
    const arr = buckets.get(key);
    if (arr) arr.push(s);
    else buckets.set(key, [s]);
  }
  return buckets;
}

/**
 * Target events/s for the run: the sum of the per-slot EPS shares. Only defined
 * when the run is rate-driven by eps (per_day_gb/count runs have no eps target,
 * so the overlay line is omitted). Returns null when unknown.
 */
export function targetEps(leases: LeaseOut[]): number | null {
  if (!leases.length) return null;
  let total = 0;
  let found = false;
  for (const lease of leases) {
    const share = lease.share_json as Record<string, unknown> | null;
    if (share && typeof share.eps === "number") {
      total += share.eps;
      found = true;
    }
  }
  return found ? total : null;
}

/**
 * Fleet-wide actual rate series: one point per heartbeat bucket, eps/bps summed
 * across slots, with the (constant) target eps attached for the overlay.
 */
export function rateSeries(
  samples: MetricSampleOut[],
  leases: LeaseOut[],
): RatePoint[] {
  const target = targetEps(leases);
  const buckets = groupByBucket(samples);
  const keys = [...buckets.keys()].sort((a, b) => a - b);
  return keys.map((ts) => {
    const rows = buckets.get(ts)!;
    let eps = 0;
    let bps = 0;
    for (const r of rows) {
      if (typeof r.eps === "number") eps += r.eps;
      if (typeof r.bps === "number") bps += r.bps;
    }
    return { ts, label: clockLabel(ts), eps, bps, target };
  });
}

/**
 * Stacked HEC outcome series. hec_* are cumulative per slot, so we diff each
 * slot between consecutive buckets and sum the non-negative deltas. Counter
 * resets (a restarted worker) clamp to 0 rather than going negative.
 */
export function hecSeries(samples: MetricSampleOut[]): HecPoint[] {
  const buckets = groupByBucket(samples);
  const keys = [...buckets.keys()].sort((a, b) => a - b);

  // last-seen cumulative value per slot per counter
  const last: Record<number, { ok: number; client: number; server: number; timeout: number }> = {};

  const points: HecPoint[] = [];
  for (const ts of keys) {
    const rows = buckets.get(ts)!;
    let ok = 0;
    let client = 0;
    let server = 0;
    let timeout = 0;
    for (const r of rows) {
      const prev = last[r.slot] ?? { ok: 0, client: 0, server: 0, timeout: 0 };
      const cur = {
        ok: r.hec_2xx ?? prev.ok,
        client: r.hec_4xx ?? prev.client,
        server: r.hec_5xx ?? prev.server,
        timeout: r.hec_timeouts ?? prev.timeout,
      };
      ok += Math.max(0, cur.ok - prev.ok);
      client += Math.max(0, cur.client - prev.client);
      server += Math.max(0, cur.server - prev.server);
      timeout += Math.max(0, cur.timeout - prev.timeout);
      last[r.slot] = cur;
    }
    points.push({ ts, label: clockLabel(ts), ok, client, server, timeout });
  }
  // Drop the first bucket: with no prior sample its delta is just the baseline
  // and would spike the chart. Keep it only when it is the sole bucket.
  return points.length > 1 ? points.slice(1) : points;
}

/** Lag + queue-depth series (worst-slot lag, summed queue) per bucket. */
export function lagSeries(samples: MetricSampleOut[]): LagPoint[] {
  const buckets = groupByBucket(samples);
  const keys = [...buckets.keys()].sort((a, b) => a - b);
  return keys.map((ts) => {
    const rows = buckets.get(ts)!;
    let lag = 0;
    let queue = 0;
    for (const r of rows) {
      if (typeof r.lag_s === "number") lag = Math.max(lag, r.lag_s);
      if (typeof r.queue_depth === "number") queue += r.queue_depth;
    }
    return { ts, label: clockLabel(ts), lag, queue };
  });
}

// The latest per-slot sample, keyed by slot — used to fill live columns
// (EPS, lag, queue, RSS) into the lease roster table.
export interface SlotLatest {
  eps: number | null;
  bps: number | null;
  lag_s: number | null;
  queue_depth: number | null;
  rss_mb: number | null;
  cpu_pct: number | null;
  ts: string | null;
}

/** Most-recent metric sample per slot (samples arrive time-ordered). */
export function latestBySlot(
  samples: MetricSampleOut[],
): Map<number, SlotLatest> {
  const out = new Map<number, SlotLatest>();
  for (const s of samples) {
    out.set(s.slot, {
      eps: s.eps ?? null,
      bps: s.bps ?? null,
      lag_s: s.lag_s ?? null,
      queue_depth: s.queue_depth ?? null,
      rss_mb: s.rss_mb ?? null,
      cpu_pct: s.cpu_pct ?? null,
      ts: s.ts,
    });
  }
  return out;
}

/** Peak lag across every sample (drives the lag > 300 s warning banner). */
export function peakLag(samples: MetricSampleOut[]): number {
  let peak = 0;
  for (const s of samples) {
    if (typeof s.lag_s === "number" && s.lag_s > peak) peak = s.lag_s;
  }
  return peak;
}

/**
 * The per-slot target share value (any rate mode), for the roster's "target"
 * column. Falls back to null when a lease has no share.
 */
export function leaseTargetShare(lease: LeaseOut): number | null {
  return shareValue(lease.share_json as Record<string, unknown> | null);
}
