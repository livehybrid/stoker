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
import { Inline, Muted, Stack } from "../components/text";
import TabBar from "@splunk/react-ui/TabBar";

// Run detail — the flagship live view (design section 10.3). Polls run + metrics
// at 5 s while the run is active and stops once it reaches a terminal state.
// Charts: target-vs-actual events/s + bytes/s; HEC outcomes. Plus the
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

  // The server reads `window` as "this far back from NOW", so the default 15 m
  // rolling window returns nothing for a run that finished more than 15 minutes
  // ago. For a terminal run, widen the window to reach back past the run's
  // start (now − start, plus slack for clock skew) so its whole history loads;
  // a live run keeps the rolling view.
  const startIso = run.data ? run.data.t0 ?? run.data.created_at : null;
  const metricsWindow = useMemo(() => {
    if (!terminal) return "15m";
    const startMs = startIso ? Date.parse(startIso) : NaN;
    if (Number.isNaN(startMs)) return "all";
    const seconds = Math.max(0, Math.ceil((Date.now() - startMs) / 1000)) + 300;
    return `${seconds}s`;
  }, [terminal, startIso]);

  const metrics = useQuery({
    queryKey: ["run", id, "metrics", metricsWindow],
    queryFn: () => api.runs.metrics(id, "5s", metricsWindow),
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
      <Stack $gap="large">
        <PageHeader title={`Run #${runId}`} />
        <Card>
          <LoadingState />
        </Card>
      </Stack>
    );
  }

  if (run.isError) {
    return (
      <Stack $gap="large">
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
      </Stack>
    );
  }

  const data = run.data;
  const snap = (data.spec_snapshot_json as Record<string, unknown> | null) ?? {};
  const rateMode = typeof snap.rate_mode === "string" ? snap.rate_mode : undefined;
  const rateValue =
    typeof snap.rate_value === "number" ? snap.rate_value : null;
  const workers = leases.length;

  return (
    <Stack $gap="large">
      <PageHeader
        title={`Run #${data.id}`}
        subtitle={
          <Inline>
            <span>
              Spec{" "}
              <Link
                to="/specs"
              >
                #{data.spec_id}
              </Link>
            </span>
            <Muted>·</Muted>
            <span>started {fmtDateTime(data.t0 ?? data.created_at)}</span>
            {data.ended_at && (
              <>
                <Muted>·</Muted>
                <span>ended {fmtDateTime(data.ended_at)}</span>
              </>
            )}
          </Inline>
        }
        actions={
          <Inline>
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
          </Inline>
        }
      />

      <WarningBanners run={data} leases={leases} peakLagS={lagPeak} />

      <Card title="Totals">
        <TotalsStrip run={data} />
      </Card>

      <Card
        title="Throughput"
        actions={
          <Muted $small>
            target vs actual events/s · bytes/s on the right axis
          </Muted>
        }
      >
        {metrics.isPending ? (
          <LoadingState />
        ) : metrics.isError ? (
          <ErrorState error={metrics.error} onRetry={() => metrics.refetch()} />
        ) : (
          <RateChart points={rate} terminal={terminal} />
        )}
      </Card>

      <Card title="HEC delivery">
        {metrics.isPending ? (
          <LoadingState />
        ) : metrics.isError ? (
          <ErrorState error={metrics.error} onRetry={() => metrics.refetch()} />
        ) : (
          <HecChart points={hec} terminal={terminal} />
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
          <Muted>
            This run is {data.state}; controls are disabled. Use Re-run on the runs
            list to launch its spec again.
          </Muted>
        )}
      </Card>

      <Card title="Lease roster">
        <LeaseTable leases={leases} latest={latest} />
      </Card>

      <Card>
        {/* Splunk's TabBar: the selected state, the keyboard behaviour and the
            accessibility relationships between tab and panel come with it. */}
        <TabBar activeTabId={tab} onChange={(_e, { selectedTabId }) => setTab(selectedTabId as Tab)}>
          <TabBar.Tab label="Spec snapshot" tabId="snapshot" />
          <TabBar.Tab label="Event log" tabId="events" />
          <TabBar.Tab label="Log tail" tabId="logs" />
        </TabBar>

        {tab === "snapshot" && (
          <SpecSnapshotPanel snapshot={data.spec_snapshot_json} />
        )}
        {tab === "events" && <EventLogPanel runId={id} active={!terminal} />}
        {tab === "logs" && (
          <LogTailPanel runId={id} leases={leases} active={!terminal} />
        )}
      </Card>
    </Stack>
  );
}

export const Route = createFileRoute("/runs_/$runId")({
  component: RunDetailPage,
});
