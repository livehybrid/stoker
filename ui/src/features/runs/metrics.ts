// Metric-sample shaping for the Run detail charts.
//
// The API returns raw per-slot metric_samples (server/schemas.py MetricSampleOut):
// one row per worker slot per heartbeat (~5 s). eps/bps/lag_s/queue_depth/rss_mb
// are instantaneous per-slot gauges; events_total/bytes_total/hec_* are CUMULATIVE
// counters. Slots heartbeat every ~5 s but are NOT synchronised, so any bucket
// narrower than the heartbeat holds only a subset of the fleet and a naive
// per-second sum sawtooths between one slot's share and the fleet total. To
// draw fleet-wide series we:
//   * bucket rows into heartbeat-sized windows (width estimated from the data),
//   * take the LATEST gauge (eps, bps) per slot in a bucket, carry a quiet
//     slot's last value forward (until it goes stale), and SUM across slots for
//     the actual-rate line,
//   * DIFF the cumulative HEC counters per slot between consecutive samples and
//     sum those deltas per bucket for the 2xx/4xx/5xx/timeout chart; a slot's
//     first sample only seeds its baseline.
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

// Nominal heartbeat interval — the bucket-width floor and the fallback when
// the data is too thin (one slot, one sample) to estimate a cadence.
const HEARTBEAT_MS = 5_000;
// Stop carrying a slot's last gauge forward once it is this many bucket widths
// stale: a finished/dead worker must not inflate the fleet total forever.
const STALE_BUCKETS = 3;

function sampleMs(iso: string): number {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? 0 : ms;
}

/**
 * Estimate the chart bucket width from the samples: the median gap between a
 * slot's consecutive heartbeats, rounded up to a whole second, floored at the
 * nominal 5 s heartbeat. Falls back to the nominal heartbeat when no slot has
 * two samples to measure a gap from.
 */
export function estimateBucketMs(samples: MetricSampleOut[]): number {
  const bySlot = new Map<number, number[]>();
  for (const s of samples) {
    const ms = sampleMs(s.ts);
    const arr = bySlot.get(s.slot);
    if (arr) arr.push(ms);
    else bySlot.set(s.slot, [ms]);
  }
  const gaps: number[] = [];
  for (const times of bySlot.values()) {
    times.sort((a, b) => a - b);
    for (let i = 1; i < times.length; i++) {
      const gap = times[i] - times[i - 1];
      if (gap > 0) gaps.push(gap);
    }
  }
  if (!gaps.length) return HEARTBEAT_MS;
  gaps.sort((a, b) => a - b);
  const median = gaps[Math.floor(gaps.length / 2)];
  return Math.max(HEARTBEAT_MS, Math.ceil(median / 1000) * 1000);
}

/** Group samples into fixed-width buckets, preserving order within a bucket. */
function groupByBucket(
  samples: MetricSampleOut[],
  bucketMs: number,
): Map<number, MetricSampleOut[]> {
  const buckets = new Map<number, MetricSampleOut[]>();
  for (const s of samples) {
    const key = Math.floor(sampleMs(s.ts) / bucketMs) * bucketMs;
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
 * Fleet-wide actual rate series: one point per heartbeat-sized bucket, with
 * the (constant) target eps attached for the overlay. eps/bps are per-slot
 * gauges, so within a bucket we keep the LATEST sample per slot (never sum two
 * samples from one slot), carry a quiet slot's last value forward so a merely
 * late heartbeat does not dip the total, and sum across slots. A slot more
 * than STALE_BUCKETS widths stale is dropped from the total for good.
 */
export function rateSeries(
  samples: MetricSampleOut[],
  leases: LeaseOut[],
  bucketMs = estimateBucketMs(samples),
): RatePoint[] {
  const target = targetEps(leases);
  const buckets = groupByBucket(samples, bucketMs);
  const keys = [...buckets.keys()].sort((a, b) => a - b);

  // last-known gauge per slot, carried across buckets until it goes stale
  const lastGauge = new Map<number, { eps: number; bps: number; ts: number }>();

  return keys.map((ts) => {
    for (const r of buckets.get(ts)!) {
      const ms = sampleMs(r.ts);
      const prev = lastGauge.get(r.slot);
      if (prev && prev.ts > ms) continue; // keep only the latest per slot
      lastGauge.set(r.slot, {
        eps: typeof r.eps === "number" ? r.eps : prev?.eps ?? 0,
        bps: typeof r.bps === "number" ? r.bps : prev?.bps ?? 0,
        ts: ms,
      });
    }
    let eps = 0;
    let bps = 0;
    for (const [slot, g] of lastGauge) {
      if (ts - g.ts > STALE_BUCKETS * bucketMs) {
        lastGauge.delete(slot); // stopped reporting — a dead worker rates 0
        continue;
      }
      eps += g.eps;
      bps += g.bps;
    }
    return { ts, label: clockLabel(ts), eps, bps, target };
  });
}

/**
 * HEC outcome series. hec_* are cumulative per slot, so we diff each slot
 * between consecutive samples and sum the non-negative deltas per bucket.
 * A slot's FIRST sample only seeds its baseline — its cumulative total is
 * history from before we saw it (a late joiner, a restarted worker), not
 * events delivered in that bucket. Counter resets (a restarted worker) clamp
 * to 0 rather than going negative. Uses the same bucket width as rateSeries
 * so the two charts share an x axis.
 */
export function hecSeries(
  samples: MetricSampleOut[],
  bucketMs = estimateBucketMs(samples),
): HecPoint[] {
  const buckets = groupByBucket(samples, bucketMs);
  const keys = [...buckets.keys()].sort((a, b) => a - b);

  // last-seen cumulative value per slot per counter (absent until seeded)
  const last: Record<number, { ok: number; client: number; server: number; timeout: number }> = {};

  const points: HecPoint[] = [];
  for (const ts of keys) {
    const rows = buckets.get(ts)!;
    let ok = 0;
    let client = 0;
    let server = 0;
    let timeout = 0;
    for (const r of rows) {
      const prev = last[r.slot];
      const cur = {
        ok: r.hec_2xx ?? prev?.ok ?? 0,
        client: r.hec_4xx ?? prev?.client ?? 0,
        server: r.hec_5xx ?? prev?.server ?? 0,
        timeout: r.hec_timeouts ?? prev?.timeout ?? 0,
      };
      if (prev) {
        ok += Math.max(0, cur.ok - prev.ok);
        client += Math.max(0, cur.client - prev.client);
        server += Math.max(0, cur.server - prev.server);
        timeout += Math.max(0, cur.timeout - prev.timeout);
      }
      last[r.slot] = cur;
    }
    points.push({ ts, label: clockLabel(ts), ok, client, server, timeout });
  }
  return points;
}

/** Lag + queue-depth series (worst-slot lag, summed queue) per bucket. */
export function lagSeries(
  samples: MetricSampleOut[],
  bucketMs = estimateBucketMs(samples),
): LagPoint[] {
  const buckets = groupByBucket(samples, bucketMs);
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
