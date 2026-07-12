// Renders a SpecEstimate (local preview or GET /specs/{id}/estimate) as the
// wizard's live arithmetic panel: per-worker share, % of ceiling (green/red),
// approximate EPS/GB, and the suggested worker count when a slice is over.

import type { ReactNode } from "react";

import type { SpecEstimate } from "../../lib/types";
import { Badge } from "../../components/Badge";
import { formatNumber } from "./format";

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-md border border-surface-muted bg-surface px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div className="mt-0.5 text-sm font-medium text-slate-100">{value}</div>
    </div>
  );
}

export function EstimatePanel({
  estimate,
  workers,
  source,
}: {
  estimate: SpecEstimate;
  workers: number;
  /** "live" (server endpoint) or "preview" (client arithmetic) — shown as a hint. */
  source?: "live" | "preview";
}) {
  const over = !estimate.ok;
  const pct = estimate.ceiling_pct;
  const pctTone = over ? "red" : pct != null && pct >= 90 ? "amber" : "green";

  const eps = estimate.per_worker_eps;
  const gb = estimate.per_worker_gb_day;
  const totalEps = eps != null ? eps * workers : null;
  const totalGb = gb != null ? gb * workers : null;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Workers" value={workers} />
        <Stat
          label="Per-worker share"
          value={
            estimate.per_worker_share != null
              ? formatNumber(estimate.per_worker_share)
              : "engine-paced"
          }
        />
        <Stat
          label="≈ per worker"
          value={
            eps != null || gb != null ? (
              <span>
                {eps != null ? `${formatNumber(eps)} ev/s` : "—"}
                {gb != null ? ` · ${formatNumber(gb)} GB/day` : ""}
              </span>
            ) : (
              "—"
            )
          }
        />
        <Stat
          label="% of ceiling"
          value={
            pct != null ? (
              <Badge tone={pctTone}>{formatNumber(pct)}%</Badge>
            ) : (
              <span className="text-slate-500">n/a</span>
            )
          }
        />
      </div>

      {(totalEps != null || totalGb != null) && (
        <p className="text-sm text-slate-400">
          {workers} worker{workers === 1 ? "" : "s"}
          {totalGb != null && gb != null ? (
            <>
              {" "}
              × {formatNumber(gb)} GB/day ={" "}
              <span className="text-slate-200">
                {formatNumber(totalGb)} GB/day
              </span>
            </>
          ) : null}
          {totalEps != null ? (
            <>
              {" "}
              ≈{" "}
              <span className="text-slate-200">
                {formatNumber(totalEps)} events/s
              </span>{" "}
              in aggregate
            </>
          ) : null}
          {estimate.ceiling_limit != null && !over ? (
            <>
              , {pct != null ? `${formatNumber(pct)}% of ` : ""}the{" "}
              {formatNumber(estimate.ceiling_limit)}{" "}
              {estimate.limiting_factor === "gb_day" ? "GB/day" : "EPS"} per-worker
              ceiling
            </>
          ) : null}
          .
        </p>
      )}

      {over && (
        <div className="rounded-md border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-200">
          <p className="font-medium">Slice exceeds the engine ceiling.</p>
          <p className="mt-1 text-red-300/90">
            {estimate.detail ?? "Reduce the rate or add workers."}
            {estimate.suggested_workers
              ? ` Use at least ${estimate.suggested_workers} workers.`
              : ""}
          </p>
        </div>
      )}

      {estimate.rate_mode === "count_interval" && (
        <p className="text-xs text-slate-500">
          count / interval is engine-paced: no rate ceiling and no exact-rate
          guarantee.
        </p>
      )}

      {source && (
        <p className="text-[11px] text-slate-600">
          {source === "live"
            ? "Live estimate from the control plane."
            : "Preview computed locally; the control plane re-checks at launch."}
        </p>
      )}
    </div>
  );
}
