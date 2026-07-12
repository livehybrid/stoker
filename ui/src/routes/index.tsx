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
  formatEps,
  liveMetrics,
  recentFailures,
} from "../features/dashboard/metrics";

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
      queryFn: () => api.runs.metrics(run.id, "5s", "60s"),
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
        <span className="text-slate-300">{r.end_reason ?? "—"}</span>
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
    <div className="space-y-5">
      <PageHeader
        title="Dashboard"
        subtitle="Fleet health and live runs at a glance."
      />

      {/* Aggregate strip */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatTile
          label="Active runs"
          value={runsQ.isPending ? "…" : String(active.length)}
        />
        <StatTile
          label="Total events / s"
          value={runsQ.isPending ? "…" : formatEps(totalEps)}
        />
        <StatTile
          label="Targets"
          value={targetsQ.isPending ? "…" : String((targetsQ.data ?? []).length)}
        />
      </div>

      {/* Active runs */}
      <Card
        title="Active runs"
        actions={
          <Link
            to="/runs"
            className="text-xs font-medium text-sky-400 hover:text-sky-300"
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
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
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
          </div>
        )}
      </Card>

      {/* Target health strip */}
      <Card
        title="Target health"
        actions={
          <Link
            to="/targets"
            className="text-xs font-medium text-sky-400 hover:text-sky-300"
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
          <p className="text-sm text-slate-500">No targets registered.</p>
        ) : (
          <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {(targetsQ.data ?? []).map((t) => (
              <li
                key={t.id}
                className="rounded-md border border-surface-muted px-3 py-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm text-slate-200">
                    {t.name}
                  </span>
                  <StatusBadge state={t.health_state} />
                </div>
                {t.health_detail && (
                  <p className="mt-1 truncate text-xs text-slate-500">
                    {t.health_detail}
                  </p>
                )}
              </li>
            ))}
          </ul>
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
    </div>
  );
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-surface-muted bg-surface-soft p-4">
      <p className="text-[11px] uppercase tracking-wide text-slate-500">
        {label}
      </p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
        {value}
      </p>
    </div>
  );
}

export const Route = createFileRoute("/")({
  component: Dashboard,
});
