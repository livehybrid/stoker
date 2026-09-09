// Renders a SpecEstimate (local preview or GET /specs/{id}/estimate) as the
// wizard's live arithmetic panel: per-worker share, % of ceiling (green/red),
// approximate EPS/GB, and the suggested worker count when a slice is over.

import type { ReactNode } from "react";

import type { SpecEstimate } from "../../lib/types";
import { Badge } from "../../components/Badge";
import { formatNumber } from "./format";
import { Callout, Grid, Label, Muted, Negative, Panel, Stack, Strong } from "../../components/text";

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <Panel>
      <Label>
        {label}
      </Label>
      <Strong>{value}</Strong>
    </Panel>
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
    <Stack>
      <Grid $min="160px">
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
              <Muted>n/a</Muted>
            )
          }
        />
      </Grid>

      {(totalEps != null || totalGb != null) && (
        <Muted>
          {workers} worker{workers === 1 ? "" : "s"}
          {totalGb != null && gb != null ? (
            <>
              {" "}
              × {formatNumber(gb)} GB/day ={" "}
              <Strong>
                {formatNumber(totalGb)} GB/day
              </Strong>
            </>
          ) : null}
          {totalEps != null ? (
            <>
              {" "}
              ≈{" "}
              <Strong>
                {formatNumber(totalEps)} events/s
              </Strong>{" "}
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
        </Muted>
      )}

      {over && (
        <Callout $tone="error">
          <Strong>Slice exceeds the engine ceiling.</Strong>
          <Negative>
            {estimate.detail ?? "Reduce the rate or add workers."}
            {estimate.suggested_workers
              ? ` Use at least ${estimate.suggested_workers} workers.`
              : ""}
          </Negative>
        </Callout>
      )}

      {estimate.rate_mode === "count_interval" && (
        <Muted $small>
          count / interval is engine-paced: no rate ceiling and no exact-rate
          guarantee.
        </Muted>
      )}

      {estimate.rate_mode !== "count_interval" &&
        estimate.ok &&
        estimate.ceiling_limit == null && (
          <Muted $small>
            No per-worker ceiling applies here (disabled or none configured for
            this engine/fleet); the control plane will not block on rate.
          </Muted>
        )}

      {source && (
        <Muted $small>
          {source === "live"
            ? "Live estimate from the control plane."
            : "Preview computed locally; the control plane re-checks at launch."}
        </Muted>
      )}
    </Stack>
  );
}
