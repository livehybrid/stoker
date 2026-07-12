import { Link } from "@tanstack/react-router";

import type { RunOut, TargetOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { formatEps } from "./metrics";

interface ActiveRunCardProps {
  run: RunOut;
  target?: TargetOut;
  /** Live EPS summed across the run's workers (from metric samples). */
  eps: number;
  /** Observed live worker count; falls back to the spec's declared workers. */
  workers: number;
  /** Whether the live metrics query for this run is still loading. */
  metricsPending: boolean;
  hasMetrics: boolean;
}

/**
 * A single active-run card for the dashboard grid. Click-through to the run
 * detail. Shows state, live EPS, its target and the live worker count.
 */
export function ActiveRunCard({
  run,
  target,
  eps,
  workers,
  metricsPending,
  hasMetrics,
}: ActiveRunCardProps) {
  return (
    <Link
      to="/runs/$runId"
      params={{ runId: String(run.id) }}
      className="block rounded-lg border border-surface-muted bg-surface-soft p-4 shadow-sm transition-colors hover:border-slate-500 hover:bg-surface-muted/40"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-slate-100">Run #{run.id}</p>
          <p className="mt-0.5 text-xs text-slate-500">
            {target ? target.name : `target #${run.spec_id}`}
            {target?.env_tag ? ` · ${target.env_tag}` : ""}
          </p>
        </div>
        <span className="flex items-center gap-1.5">
          <StatusBadge state={run.state} />
          {run.degraded && <Badge tone="amber">degraded</Badge>}
        </span>
      </div>

      <div className="mt-4 flex items-end justify-between gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            Events / s
          </p>
          <p className="text-2xl font-semibold tabular-nums text-slate-100">
            {metricsPending && !hasMetrics ? "…" : formatEps(eps)}
          </p>
        </div>
        <div className="text-right">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            Workers
          </p>
          <p className="text-lg font-medium tabular-nums text-slate-200">
            {workers}
          </p>
        </div>
      </div>
    </Link>
  );
}
