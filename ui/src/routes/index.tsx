import { createFileRoute, Link } from "@tanstack/react-router";
import { useQueries, useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import { POLL_MS } from "../lib/queryClient";
import type { MetricsOut, RunOut, SpecOut, TargetOut } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Table, type Column } from "../components/Table";
import { StatusBadge } from "../components/Badge";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { ActiveRunCard } from "../features/dashboard/ActiveRunCard";
import {
  activeRuns,
  fleetThroughputSeries,
  formatEps,
  liveMetrics,
  recentFailures,
} from "../features/dashboard/metrics";
import { FleetThroughputChart } from "../features/dashboard/FleetThroughputChart";
import { Between, BigNumber, Grid, Label, Muted, Panel, Stack, Strong } from "../components/text";

// Dashboard: fleet health and live runs at a glance. Active-run cards (live EPS,
// target, workers), an aggregate strip, a target-health strip and the most
// recent failures. All live data polls at 5 s.

function Dashboard() {
  const runsQ = useQuery({
    queryKey: ["runs"],
    queryFn: () => api.runs.list(),
    refetchInterval: POLL_MS,
  });
  const targetsQ = useQuery({
    queryKey: ["targets"],
    queryFn: () => api.targets.list(),
    refetchInterval: POLL_MS,
  });
  const specsQ = useQuery({
    queryKey: ["specs"],
    queryFn: () => api.specs.list(),
    refetchInterval: POLL_MS,
  });

  const runs: RunOut[] = runsQ.data ?? [];
  const active = activeRuns(runs);
  const failures = recentFailures(runs);

  // Resolve a run -> its target via the spec it was launched from.
  const specById = new Map<number, SpecOut>(
    (specsQ.data ?? []).map((s) => [s.id, s]),
  );
  const targetById = new Map<number, TargetOut>(
    (targetsQ.data ?? []).map((t) => [t.id, t]),
  );
  const targetForRun = (run: RunOut): TargetOut | undefined => {
    const spec = specById.get(run.spec_id);
    return spec ? targetById.get(spec.target_id) : undefined;
  };

  // Per-active-run live metrics (short window; only the newest sample matters).
  // useQueries keeps the hook count stable while the run set changes.
  const metricsResults = useQueries({
    queries: active.map((run) => ({
      queryKey: ["run", run.id, "metrics", "dashboard"],
      // A 15 m window at 30 s resolution feeds both the live-EPS tiles (newest
      // sample per slot) and the fleet throughput chart (the whole series).
      queryFn: () => api.runs.metrics(run.id, "30s", "15m"),
      refetchInterval: POLL_MS,
    })),
  });

  const perRun = active.map((run, i) => {
    const res = metricsResults[i];
    const live = liveMetrics(res?.data as MetricsOut | undefined);
    const specWorkers = specById.get(run.spec_id)?.workers ?? 0;
    return {
      run,
      target: targetForRun(run),
      eps: live.eps,
      workers: live.hasData ? live.workers : specWorkers,
      hasMetrics: live.hasData,
      metricsPending: res?.isPending ?? false,
    };
  });

  const totalEps = perRun.reduce((sum, r) => sum + r.eps, 0);
  const throughput = fleetThroughputSeries(
    metricsResults.map((r) => r.data as MetricsOut | undefined),
  );
  const totalMbps = throughput.length
    ? throughput[throughput.length - 1].mbps
    : 0;

  const failureColumns: Column<RunOut>[] = [
    { key: "id", header: "Run", cell: (r) => `#${r.id}` },
    {
      key: "target",
      header: "Target",
      cell: (r) => targetForRun(r)?.name ?? `spec #${r.spec_id}`,
    },
    {
      key: "reason",
      header: "End reason",
      cell: (r) => (
        <span>{r.end_reason ?? "—"}</span>
      ),
    },
    {
      key: "when",
      header: "Ended",
      cell: (r) =>
        r.ended_at ? new Date(r.ended_at).toLocaleString("en-GB") : "—",
    },
  ];

  return (
    <Stack $gap="large">
      <PageHeader
        title="Dashboard"
        subtitle="Fleet health and live runs at a glance."
      />

      {/* Aggregate strip */}
      <Grid $min="160px">
        <StatTile
          label="Active runs"
          value={runsQ.isPending ? "…" : String(active.length)}
        />
        <StatTile
          label="Total events / s"
          value={runsQ.isPending ? "…" : formatEps(totalEps)}
        />
        <StatTile
          label="Total MB / s"
          value={runsQ.isPending ? "…" : totalMbps.toFixed(totalMbps >= 10 ? 0 : 1)}
        />
        <StatTile
          label="Targets"
          value={targetsQ.isPending ? "…" : String((targetsQ.data ?? []).length)}
        />
      </Grid>

      {/* Fleet throughput over time (delivered eps + MB/s across all runs) */}
      <Card title="Fleet throughput">
        <FleetThroughputChart points={throughput} />
      </Card>

      {/* Active runs */}
      <Card
        title="Active runs"
        actions={
          <Link
            to="/runs"
          >
            All runs →
          </Link>
        }
      >
        {runsQ.isPending ? (
          <LoadingState />
        ) : runsQ.isError ? (
          <ErrorState error={runsQ.error} onRetry={() => runsQ.refetch()} />
        ) : active.length === 0 ? (
          <EmptyState
            title="No active runs"
            message="Launch a spec to start streaming data to a target."
          />
        ) : (
          <Grid $min="260px">
            {perRun.map((r) => (
              <ActiveRunCard
                key={r.run.id}
                run={r.run}
                target={r.target}
                eps={r.eps}
                workers={r.workers}
                hasMetrics={r.hasMetrics}
                metricsPending={r.metricsPending}
              />
            ))}
          </Grid>
        )}
      </Card>

      {/* Target health strip */}
      <Card
        title="Target health"
        actions={
          <Link
            to="/targets"
          >
            Manage →
          </Link>
        }
      >
        {targetsQ.isPending ? (
          <LoadingState />
        ) : targetsQ.isError ? (
          <ErrorState
            error={targetsQ.error}
            onRetry={() => targetsQ.refetch()}
          />
        ) : (targetsQ.data ?? []).length === 0 ? (
          <Muted>No targets registered.</Muted>
        ) : (
          <Grid $min="260px">
            {(targetsQ.data ?? []).map((t) => (
              <li
                key={t.id}
              >
                <Between>
                  <Strong>
                    {t.name}
                  </Strong>
                  <StatusBadge state={t.health_state} />
                </Between>
                {t.health_detail && (
                  <Muted $small>
                    {t.health_detail}
                  </Muted>
                )}
              </li>
            ))}
          </Grid>
        )}
      </Card>

      {/* Recent failures */}
      <Card title="Recent failures">
        {runsQ.isPending ? (
          <LoadingState />
        ) : runsQ.isError ? (
          <ErrorState error={runsQ.error} onRetry={() => runsQ.refetch()} />
        ) : (
          <Table
            columns={failureColumns}
            rows={failures}
            rowKey={(r) => r.id}
            empty={<EmptyState title="No recent failures" />}
          />
        )}
      </Card>
    </Stack>
  );
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <Panel>
      <Label>
        {label}
      </Label>
      <BigNumber>
        {value}
      </BigNumber>
    </Panel>
  );
}

export const Route = createFileRoute("/")({
  component: Dashboard,
});
