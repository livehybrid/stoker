import { useMemo, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import { POLL_MS } from "../lib/queryClient";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Badge, StatusBadge } from "../components/Badge";
import { Button } from "../components/Button";
import { ErrorState, LoadingState } from "../components/States";
import { cn } from "../components/cn";

import {
  endReasonLabel,
  endReasonTone,
  fmtDateTime,
  isTerminal,
} from "../features/runs/format";
import {
  hecSeries,
  latestBySlot,
  peakLag,
  rateSeries,
} from "../features/runs/metrics";
import { RateChart } from "../features/runs/RateChart";
import { HecChart } from "../features/runs/HecChart";
import { LeaseTable } from "../features/runs/LeaseTable";
import { RunControls } from "../features/runs/RunControls";
import { WarningBanners } from "../features/runs/WarningBanners";
import { TotalsStrip } from "../features/runs/TotalsStrip";
import { SpecSnapshotPanel } from "../features/runs/SpecSnapshotPanel";
import { EventLogPanel } from "../features/runs/EventLogPanel";
import { LogTailPanel } from "../features/runs/LogTailPanel";

// Run detail — the flagship live view (design section 10.3). Polls run + metrics
// at 5 s while the run is active and stops once it reaches a terminal state.
// Charts: target-vs-actual events/s + bytes/s; stacked HEC outcomes. Plus the
// lease roster, controls, warning banners and the snapshot/events/logs tabs.

type Tab = "snapshot" | "events" | "logs";

function RunDetailPage() {
  const { runId } = Route.useParams();
  const id = Number(runId);
  const [tab, setTab] = useState<Tab>("snapshot");

  // First fetch decides polling; subsequent renders read the live state. We keep
  // polling on until we KNOW the run is terminal (so a run mid-drain still ticks).
  const run = useQuery({
    queryKey: ["run", id],
    queryFn: () => api.runs.get(id),
    refetchInterval: (query) =>
      isTerminal(query.state.data?.state) ? false : POLL_MS,
  });

  const terminal = isTerminal(run.data?.state);

  const metrics = useQuery({
    queryKey: ["run", id, "metrics"],
    queryFn: () => api.runs.metrics(id),
    refetchInterval: terminal ? false : POLL_MS,
    // Only fetch metrics once the run is known to exist (a 404 run short-circuits
    // to the error view below and never needs its metrics).
    enabled: run.isSuccess,
  });

  const samples = metrics.data?.samples ?? [];
  const leases = run.data?.leases ?? [];

  const rate = useMemo(() => rateSeries(samples, leases), [samples, leases]);
  const hec = useMemo(() => hecSeries(samples), [samples]);
  const latest = useMemo(() => latestBySlot(samples), [samples]);
  const lagPeak = useMemo(() => peakLag(samples), [samples]);

  if (run.isPending) {
    return (
      <div className="space-y-5">
        <PageHeader title={`Run #${runId}`} />
        <Card>
          <LoadingState />
        </Card>
      </div>
    );
  }

  if (run.isError) {
    return (
      <div className="space-y-5">
        <PageHeader
          title={`Run #${runId}`}
          actions={
            <Link to="/runs">
              <Button variant="ghost">Back to runs</Button>
            </Link>
          }
        />
        <Card>
          <ErrorState error={run.error} onRetry={() => run.refetch()} />
        </Card>
      </div>
    );
  }

  const data = run.data;
  const snap = (data.spec_snapshot_json as Record<string, unknown> | null) ?? {};
  const rateMode = typeof snap.rate_mode === "string" ? snap.rate_mode : undefined;
  const rateValue =
    typeof snap.rate_value === "number" ? snap.rate_value : null;
  const workers = leases.length;

  return (
    <div className="space-y-5">
      <PageHeader
        title={`Run #${data.id}`}
        subtitle={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span>
              Spec{" "}
              <Link
                to="/specs"
                className="text-sky-400 hover:text-sky-300"
              >
                #{data.spec_id}
              </Link>
            </span>
            <span className="text-slate-600">·</span>
            <span>started {fmtDateTime(data.t0 ?? data.created_at)}</span>
            {data.ended_at && (
              <>
                <span className="text-slate-600">·</span>
                <span>ended {fmtDateTime(data.ended_at)}</span>
              </>
            )}
          </span>
        }
        actions={
          <span className="flex items-center gap-1.5">
            <StatusBadge state={data.state} />
            {data.degraded && <Badge tone="amber">degraded</Badge>}
            {data.end_reason && (
              <Badge tone={endReasonTone(data.end_reason)}>
                {endReasonLabel(data.end_reason)}
              </Badge>
            )}
            <Link to="/runs">
              <Button variant="ghost">All runs</Button>
            </Link>
          </span>
        }
      />

      <WarningBanners run={data} leases={leases} peakLagS={lagPeak} />

      <Card title="Totals">
        <TotalsStrip run={data} />
      </Card>

      <Card
        title="Throughput"
        actions={
          <span className="text-xs text-slate-500">
            target vs actual events/s · bytes/s on the right axis
          </span>
        }
      >
        {metrics.isPending ? (
          <LoadingState />
        ) : metrics.isError ? (
          <ErrorState error={metrics.error} onRetry={() => metrics.refetch()} />
        ) : (
          <RateChart points={rate} />
        )}
      </Card>

      <Card title="HEC delivery">
        {metrics.isPending ? (
          <LoadingState />
        ) : metrics.isError ? (
          <ErrorState error={metrics.error} onRetry={() => metrics.refetch()} />
        ) : (
          <HecChart points={hec} />
        )}
      </Card>

      <Card title="Controls">
        <RunControls
          run={data}
          terminal={terminal}
          workers={workers}
          rateMode={rateMode}
          rateValue={rateValue}
        />
        {terminal && (
          <p className="mt-3 text-xs text-slate-500">
            This run is {data.state}; controls are disabled. Use Re-run on the runs
            list to launch its spec again.
          </p>
        )}
      </Card>

      <Card title="Lease roster">
        <LeaseTable leases={leases} latest={latest} />
      </Card>

      <Card>
        <div className="mb-4 flex gap-1 border-b border-surface-muted">
          {(
            [
              ["snapshot", "Spec snapshot"],
              ["events", "Event log"],
              ["logs", "Log tail"],
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={cn(
                "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
                tab === key
                  ? "border-sky-500 text-slate-100"
                  : "border-transparent text-slate-400 hover:text-slate-200",
              )}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === "snapshot" && (
          <SpecSnapshotPanel snapshot={data.spec_snapshot_json} />
        )}
        {tab === "events" && <EventLogPanel runId={id} active={!terminal} />}
        {tab === "logs" && (
          <LogTailPanel runId={id} leases={leases} active={!terminal} />
        )}
      </Card>
    </div>
  );
}

export const Route = createFileRoute("/runs/$runId")({
  component: RunDetailPage,
});
