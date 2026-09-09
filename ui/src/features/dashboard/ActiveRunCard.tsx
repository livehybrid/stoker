import { Link } from "@tanstack/react-router";

import type { RunOut, TargetOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { formatEps } from "./metrics";
import { Between, BigNumber, Inline, Label, Muted, Strong } from "../../components/text";

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
    >
      <Between>
        <div>
          <Strong>Run #{run.id}</Strong>
          <Muted $small>
            {target ? target.name : `target #${run.spec_id}`}
            {target?.env_tag ? ` · ${target.env_tag}` : ""}
          </Muted>
        </div>
        <Inline>
          <StatusBadge state={run.state} />
          {run.degraded && <Badge tone="amber">degraded</Badge>}
        </Inline>
      </Between>

      <Between>
        <div>
          <Label>
            Events / s
          </Label>
          <BigNumber>
            {metricsPending && !hasMetrics ? "…" : formatEps(eps)}
          </BigNumber>
        </div>
        <div>
          <Label>
            Workers
          </Label>
          <BigNumber $size="medium">
            {workers}
          </BigNumber>
        </div>
      </Between>
    </Link>
  );
}
