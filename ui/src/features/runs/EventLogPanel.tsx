import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { POLL_MS } from "../../lib/queryClient";
import { Table, type Column } from "../../components/Table";
import { ErrorState, LoadingState } from "../../components/States";
import type { RunEventOut } from "../../lib/types";
import { fmtDateTime } from "./format";

// "Run event log" tab (section 10.3): the append-only audit trail from
// GET /runs/{id}/events — every state transition and operator action. Polls at
// 5 s while the run is active; frozen once terminal.

function detailText(detail: unknown): string {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (typeof detail === "object") {
    const keys = Object.keys(detail as Record<string, unknown>);
    if (keys.length === 0) return "";
    try {
      return JSON.stringify(detail);
    } catch {
      return "";
    }
  }
  return String(detail);
}

const columns: Column<RunEventOut & { _i: number }>[] = [
  {
    key: "ts",
    header: "Time",
    className: "whitespace-nowrap text-slate-400",
    cell: (e) => fmtDateTime(e.ts),
  },
  { key: "actor", header: "Actor", cell: (e) => e.actor },
  {
    key: "kind",
    header: "Event",
    cell: (e) => <span className="font-medium text-slate-200">{e.kind}</span>,
  },
  {
    key: "detail",
    header: "Detail",
    cell: (e) => {
      const t = detailText(e.detail_json);
      return t ? (
        <span className="break-all font-mono text-xs text-slate-400">{t}</span>
      ) : (
        "—"
      );
    },
  },
];

export function EventLogPanel({
  runId,
  active,
}: {
  runId: number;
  active: boolean;
}) {
  const q = useQuery({
    queryKey: ["run", runId, "events"],
    queryFn: () => api.runs.events(runId),
    refetchInterval: active ? POLL_MS : false,
  });

  if (q.isPending) return <LoadingState />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;

  // Newest first for scanning; index keeps a stable row key (events lack an id).
  const rows = [...q.data]
    .map((e, i) => ({ ...e, _i: i }))
    .sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts) || b._i - a._i);

  return (
    <Table
      columns={columns}
      rows={rows}
      rowKey={(e) => `${e.ts}-${e._i}`}
      empty={<p className="px-1 py-6 text-sm text-slate-500">No events recorded.</p>}
    />
  );
}
