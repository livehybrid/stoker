import { useMemo, useState } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../lib/api";
import { POLL_MS } from "../lib/queryClient";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Table, type Column } from "../components/Table";
import { Badge, StatusBadge } from "../components/Badge";
import { Button } from "../components/Button";
import { Field, Select, TextInput } from "../components/Field";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useToast } from "../components/Toast";
import type { RunOut, SpecOut, TargetOut } from "../lib/types";
import {
  endReasonLabel,
  fmtBytes,
  fmtDateTime,
  fmtInt,
  isTerminal,
  toDateKey,
} from "../features/runs/format";

// Runs history (section 10.7): a filterable list of every run (newest first)
// with per-run totals, end reasons and the degraded flag, a Re-run action and
// click-through to the flagship detail page. Live via a 5 s poll.

const ALL = "__all__";

// Total events pulled from a run's folded totals_json (server sums worker
// summaries into it; keys can be absent on a run that never reported).
function totalEvents(run: RunOut): number | null {
  const t = run.totals_json as Record<string, unknown> | null | undefined;
  const v = t?.events_total;
  return typeof v === "number" ? v : null;
}
function totalBytes(run: RunOut): number | null {
  const t = run.totals_json as Record<string, unknown> | null | undefined;
  const v = t?.bytes_total;
  return typeof v === "number" ? v : null;
}

function Runs() {
  const navigate = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();

  const runsQ = useQuery({
    queryKey: ["runs"],
    queryFn: () => api.runs.list(),
    refetchInterval: POLL_MS,
  });
  // Specs + targets label the spec/target columns and populate the filters.
  const specsQ = useQuery({ queryKey: ["specs"], queryFn: () => api.specs.list() });
  const targetsQ = useQuery({ queryKey: ["targets"], queryFn: () => api.targets.list() });

  const specById = useMemo(() => {
    const m = new Map<number, SpecOut>();
    for (const s of specsQ.data ?? []) m.set(s.id, s);
    return m;
  }, [specsQ.data]);
  const targetById = useMemo(() => {
    const m = new Map<number, TargetOut>();
    for (const t of targetsQ.data ?? []) m.set(t.id, t);
    return m;
  }, [targetsQ.data]);

  // The target a run hit is resolved through its spec (run -> spec -> target).
  const targetIdForRun = (run: RunOut): number | null =>
    specById.get(run.spec_id)?.target_id ?? null;

  // --- Filters --------------------------------------------------------------
  const [stateFilter, setStateFilter] = useState(ALL);
  const [specFilter, setSpecFilter] = useState(ALL);
  const [targetFilter, setTargetFilter] = useState(ALL);
  const [dateFilter, setDateFilter] = useState(""); // yyyy-mm-dd, "" = any

  const stateOptions = useMemo(() => {
    const s = new Set<string>();
    for (const r of runsQ.data ?? []) s.add(r.state);
    return [...s].sort();
  }, [runsQ.data]);

  const filtered = useMemo(() => {
    const rows = runsQ.data ?? [];
    return rows.filter((r) => {
      if (stateFilter !== ALL && r.state !== stateFilter) return false;
      if (specFilter !== ALL && String(r.spec_id) !== specFilter) return false;
      if (targetFilter !== ALL) {
        if (String(targetIdForRun(r) ?? "") !== targetFilter) return false;
      }
      if (dateFilter && toDateKey(r.created_at) !== dateFilter) return false;
      return true;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runsQ.data, stateFilter, specFilter, targetFilter, dateFilter, specById]);

  const activeCount = useMemo(
    () => (runsQ.data ?? []).filter((r) => !isTerminal(r.state)).length,
    [runsQ.data],
  );

  // --- Re-run ---------------------------------------------------------------
  const rerun = useMutation({
    mutationFn: (specId: number) => api.specs.run(specId),
    onSuccess: (created) => {
      toast.success(`Re-run started (run #${created.run_id})`);
      qc.invalidateQueries({ queryKey: ["runs"] });
      navigate({ to: "/runs/$runId", params: { runId: String(created.run_id) } });
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.message : "Re-run failed";
      toast.error(msg);
    },
  });

  const columns: Column<RunOut>[] = [
    { key: "id", header: "Run", cell: (r) => <span className="font-medium text-slate-200">#{r.id}</span> },
    {
      key: "spec",
      header: "Spec",
      cell: (r) => specById.get(r.spec_id)?.name ?? `#${r.spec_id}`,
    },
    {
      key: "target",
      header: "Target",
      cell: (r) => {
        const tid = targetIdForRun(r);
        return tid != null ? (targetById.get(tid)?.name ?? `#${tid}`) : "—";
      },
    },
    {
      key: "state",
      header: "State",
      cell: (r) => (
        <span className="flex items-center gap-1.5">
          <StatusBadge state={r.state} />
          {r.degraded && <Badge tone="amber">degraded</Badge>}
        </span>
      ),
    },
    {
      key: "events",
      header: "Events",
      className: "text-right tabular-nums",
      cell: (r) => fmtInt(totalEvents(r)),
    },
    {
      key: "bytes",
      header: "Volume",
      className: "text-right tabular-nums",
      cell: (r) => fmtBytes(totalBytes(r)),
    },
    {
      key: "end_reason",
      header: "End reason",
      cell: (r) => endReasonLabel(r.end_reason),
    },
    {
      key: "created",
      header: "Started",
      className: "whitespace-nowrap text-slate-400",
      cell: (r) => fmtDateTime(r.t0 ?? r.created_at),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (r) => (
        <Button
          variant="ghost"
          onClick={(e) => {
            e.stopPropagation();
            rerun.mutate(r.spec_id);
          }}
          disabled={rerun.isPending}
          title="Launch this run's spec again (byte-identical)"
        >
          Re-run
        </Button>
      ),
    },
  ];

  const anyFilter =
    stateFilter !== ALL ||
    specFilter !== ALL ||
    targetFilter !== ALL ||
    dateFilter !== "";

  return (
    <div className="space-y-5">
      <PageHeader
        title="Runs"
        subtitle="Live and historical runs."
        actions={
          <span className="text-sm text-slate-500">
            {activeCount} active · {(runsQ.data ?? []).length} total
          </span>
        }
      />

      <Card
        title="Filters"
        actions={
          anyFilter ? (
            <Button
              variant="ghost"
              onClick={() => {
                setStateFilter(ALL);
                setSpecFilter(ALL);
                setTargetFilter(ALL);
                setDateFilter("");
              }}
            >
              Clear
            </Button>
          ) : undefined
        }
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="State">
            <Select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
              <option value={ALL}>All states</option>
              {stateOptions.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Spec">
            <Select value={specFilter} onChange={(e) => setSpecFilter(e.target.value)}>
              <option value={ALL}>All specs</option>
              {(specsQ.data ?? []).map((s) => (
                <option key={s.id} value={String(s.id)}>
                  {s.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Target">
            <Select value={targetFilter} onChange={(e) => setTargetFilter(e.target.value)}>
              <option value={ALL}>All targets</option>
              {(targetsQ.data ?? []).map((t) => (
                <option key={t.id} value={String(t.id)}>
                  {t.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Date started">
            <TextInput
              type="date"
              value={dateFilter}
              onChange={(e) => setDateFilter(e.target.value)}
            />
          </Field>
        </div>
      </Card>

      <Card>
        {runsQ.isPending ? (
          <LoadingState />
        ) : runsQ.isError ? (
          <ErrorState error={runsQ.error} onRetry={() => runsQ.refetch()} />
        ) : (
          <Table
            columns={columns}
            rows={filtered}
            rowKey={(r) => r.id}
            onRowClick={(r) =>
              navigate({ to: "/runs/$runId", params: { runId: String(r.id) } })
            }
            empty={
              <EmptyState
                title={anyFilter ? "No runs match these filters" : "No runs yet"}
                message={
                  anyFilter
                    ? "Adjust or clear the filters to see more."
                    : "Launch a spec to create your first run."
                }
              />
            }
          />
        )}
      </Card>
    </div>
  );
}

export const Route = createFileRoute("/runs")({
  component: Runs,
});
