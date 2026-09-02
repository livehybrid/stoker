// Client-side rate arithmetic for the job wizard.
//
// The authoritative estimate is GET /specs/{id}/estimate (server/engines/
// ceilings.py + api._estimate). But the wizard must show live arithmetic while
// the operator is still choosing values, before any spec is saved (a new spec
// has no id to estimate against). So this mirrors the ARITHMETIC of ceilings.py
// exactly, while the CEILING VALUES come from the server: ceilings are
// configurable (env vars, per-fleet config_json overrides), and GET /api/fleets
// reports each fleet's effective per-engine table (FleetOut.ceilings), which
// the wizard threads in via `checkSlice`/`localEstimate`'s `ceilings` argument.
//
// Keep the check arithmetic in lock-step with server/engines/ceilings.py.

import type { RateMode, SpecEstimate } from "../../lib/types";

// Effective per-worker ceilings for one engine, as the server reports them
// (FleetOut.ceilings[engine] / SpecEstimate.ceilings). A null/absent bound is
// DISABLED: no limit on that axis.
export interface EffectiveCeiling {
  max_gb_day_per_worker?: number | null;
  max_eps_per_worker?: number | null;
}

// engine -> per-worker ceilings (mirrors the built-in default CEILINGS table in
// ceilings.py). FALLBACK ONLY: used until the server has answered with the
// effective (configurable) ceilings — see the module comment above. Once
// FleetOut.ceilings is available it always wins, so these constants can only
// disagree with the server for the moment the fleets query is in flight.
export const CEILINGS: Record<string, EffectiveCeiling> = {
  eventgen: { max_gb_day_per_worker: 25.0, max_eps_per_worker: 5000.0 },
};

/**
 * Read the server-reported effective ceilings for one engine off a fleet row
 * (`FleetOut.ceilings`, from GET /api/fleets). Typed structurally so the shared
 * FleetOut type does not have to be imported here. Returns null when the server
 * has not answered yet — callers then fall back to the CEILINGS defaults above.
 */
export function fleetCeilings(
  fleet:
    // `name` overlaps with FleetOut so the structural (weak) type is accepted
    // without importing FleetOut here.
    | { name?: string; ceilings?: Record<string, EffectiveCeiling | null> | null }
    | null
    | undefined,
  engine: string,
): EffectiveCeiling | null {
  return fleet?.ceilings?.[engine] ?? null;
}

const SECONDS_PER_DAY = 86400.0;
const BYTES_PER_GB = 1_000_000_000.0; // decimal GB, matching eventgen perDayVolume

export function ceilingFor(engine: string): EffectiveCeiling {
  return CEILINGS[engine] ?? {};
}

/** GB/day -> approximate EPS given bytes/event (null when unknown). */
export function gbDayToEps(
  perDayGb: number,
  bytesPerEvent: number | null | undefined,
): number | null {
  if (!bytesPerEvent || bytesPerEvent <= 0) return null;
  const bytesPerDay = perDayGb * BYTES_PER_GB;
  return bytesPerDay / bytesPerEvent / SECONDS_PER_DAY;
}

/** EPS -> GB/day given bytes/event (null when unknown). */
export function epsToGbDay(
  eps: number,
  bytesPerEvent: number | null | undefined,
): number | null {
  if (!bytesPerEvent || bytesPerEvent <= 0) return null;
  return (eps * bytesPerEvent * SECONDS_PER_DAY) / BYTES_PER_GB;
}

/**
 * Largest-remainder apportionment of a total across N workers, returning the
 * largest single share (the one the ceiling binds on). Mirrors
 * lifecycle.build_share_list + api._per_worker_share: for EPS the total is
 * split into integer shares (remainder distributed to the low slots), so the
 * max share is ceil(total / workers); for per_day_gb it is an even float split.
 */
export function perWorkerShare(
  rateMode: RateMode,
  rateValue: number | null | undefined,
  workers: number,
): number | null {
  if (rateMode === "count_interval") return null;
  if (rateValue == null || rateValue <= 0) return null;
  const w = Math.max(1, Math.floor(workers) || 1);
  if (rateMode === "eps") {
    // integer largest-remainder: max slot = ceil(total / workers)
    return Math.ceil(rateValue / w);
  }
  // per_day_gb: even float split
  return rateValue / w;
}

/**
 * Result of the client-side ceiling check (mirrors ceilings.check_slice +
 * ceilings._exceeded). `suggestedWorkers` is the smallest fleet that brings the
 * per-worker share under the binding ceiling.
 */
export interface CeilingCheck {
  ok: boolean;
  suggestedWorkers: number | null;
  limitingFactor: "eps" | "gb_day" | null;
  detail: string | null;
}

function exceeded(
  factor: "eps" | "gb_day",
  value: number,
  ceiling: number,
): CeilingCheck {
  let suggested = Math.ceil(value / ceiling);
  if (suggested < 2) suggested = 2; // already over at 1 worker
  const unit = factor === "eps" ? "EPS" : "GB/day";
  return {
    ok: false,
    suggestedWorkers: suggested,
    limitingFactor: factor,
    detail: `per-worker ${unit} ${value.toFixed(2)} exceeds the ${unit} ceiling of ${ceiling.toFixed(2)}; use at least ${suggested} workers`,
  };
}

export function checkSlice(
  rateMode: RateMode,
  perWorkerValue: number | null,
  bytesPerEvent: number | null | undefined,
  engine = "eventgen",
  // Server-resolved effective ceilings (fleetCeilings / SpecEstimate.ceilings);
  // falls back to the hardcoded defaults while the server has not answered. A
  // null bound inside is disabled and never checked (mirrors check_slice).
  ceilings?: EffectiveCeiling | null,
): CeilingCheck {
  const table = ceilings ?? CEILINGS[engine];
  if (!table) {
    return { ok: true, suggestedWorkers: null, limitingFactor: null, detail: null };
  }
  if (rateMode === "count_interval") {
    return { ok: true, suggestedWorkers: null, limitingFactor: null, detail: null };
  }
  if (perWorkerValue == null || perWorkerValue <= 0) {
    return { ok: true, suggestedWorkers: null, limitingFactor: null, detail: null };
  }
  const maxEps = table.max_eps_per_worker;
  const maxGb = table.max_gb_day_per_worker;

  if (rateMode === "eps") {
    const eps = perWorkerValue;
    const gbDay = epsToGbDay(eps, bytesPerEvent);
    if (maxEps != null && eps > maxEps) return exceeded("eps", eps, maxEps);
    if (maxGb != null && gbDay != null && gbDay > maxGb)
      return exceeded("gb_day", gbDay, maxGb);
    return { ok: true, suggestedWorkers: null, limitingFactor: null, detail: null };
  }
  // per_day_gb
  const gbDay = perWorkerValue;
  const eps = gbDayToEps(gbDay, bytesPerEvent);
  if (maxGb != null && gbDay > maxGb) return exceeded("gb_day", gbDay, maxGb);
  if (maxEps != null && eps != null && eps > maxEps)
    return exceeded("eps", eps, maxEps);
  return { ok: true, suggestedWorkers: null, limitingFactor: null, detail: null };
}

/**
 * Build a full SpecEstimate-shaped preview from wizard values, mirroring
 * api._estimate. Used only until a spec is saved and the real GET
 * /specs/{id}/estimate can be called. Shape matches SpecEstimate exactly so the
 * same renderer displays either the local preview or the server response.
 */
export function localEstimate(params: {
  rateMode: RateMode;
  rateValue: number | null;
  workers: number;
  engine: string;
  bytesPerEvent: number | null | undefined;
  /** Server-resolved effective ceilings for the chosen fleet + engine
   *  (fleetCeilings); omit/null to fall back to the hardcoded defaults. */
  ceilings?: EffectiveCeiling | null;
}): SpecEstimate {
  const { rateMode, rateValue, engine, bytesPerEvent } = params;
  const workers = Math.max(1, Math.floor(params.workers) || 1);
  const perWorker = perWorkerShare(rateMode, rateValue, workers);
  const check = checkSlice(rateMode, perWorker, bytesPerEvent, engine, params.ceilings);

  const table = params.ceilings ?? ceilingFor(engine);
  const maxEps = table.max_eps_per_worker ?? null;
  const maxGb = table.max_gb_day_per_worker ?? null;

  let perWorkerEps: number | null = null;
  let perWorkerGb: number | null = null;
  let ceilingLimit: number | null = null;
  let ceilingPct: number | null = null;
  let limiting: string | null = null;

  if (rateMode === "eps") {
    perWorkerEps = perWorker;
    perWorkerGb = perWorker != null ? epsToGbDay(perWorker, bytesPerEvent) : null;
    ceilingLimit = maxEps;
    limiting = "eps";
    if (perWorker && maxEps) ceilingPct = round(100.0 * (perWorker / maxEps), 2);
  } else if (rateMode === "per_day_gb") {
    perWorkerGb = perWorker;
    perWorkerEps = perWorker != null ? gbDayToEps(perWorker, bytesPerEvent) : null;
    ceilingLimit = maxGb;
    limiting = "gb_day";
    if (perWorker && maxGb) ceilingPct = round(100.0 * (perWorker / maxGb), 2);
  } else {
    limiting = null; // count_interval: engine-paced
  }

  if (!check.ok && check.limitingFactor) limiting = check.limitingFactor;

  return {
    workers,
    rate_mode: rateMode,
    per_worker_share: perWorker,
    per_worker_eps: perWorkerEps != null ? round(perWorkerEps, 3) : null,
    per_worker_gb_day: perWorkerGb != null ? round(perWorkerGb, 4) : null,
    ceiling_pct: ceilingPct,
    ceiling_limit: ceilingLimit,
    limiting_factor: limiting,
    ok: check.ok,
    suggested_workers: check.suggestedWorkers,
    detail: check.detail,
  };
}

function round(n: number, dp: number): number {
  const f = Math.pow(10, dp);
  return Math.round(n * f) / f;
}
