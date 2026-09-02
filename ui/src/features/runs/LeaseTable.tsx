import { Table, type Column } from "../../components/Table";
import { StatusBadge } from "../../components/Badge";
import type { LeaseOut } from "../../lib/types";
import type { SlotLatest } from "./metrics";
import { leaseTargetShare } from "./metrics";
import { fmtElapsed, fmtInt, fmtNum } from "./format";

// Lease roster (section 10.3): slot, holder, node, assigned work, EPS, lag s,
// queue depth, RSS, restarts, state. Live gauge columns (EPS/lag/queue/RSS)
// come from the latest metric sample per slot; identity + restarts + heartbeat
// come off the lease. "Assigned" is the worker's own report of how many work
// units it holds (metrics series / eventgen stanzas / a replay dataset) — a
// slot legitimately holding NONE is flagged in red with the worker's reason,
// so a 0 EPS row is never a silent mystery.

// A lag figure over this many seconds is shown in red on the row.
const LAG_WARN_S = 300;

/**
 * The worker-reported count of work units this lease holds, or null when the
 * worker has not (yet) reported one — an old worker image, or a lease that has
 * not heart-beaten since claiming. Reads the top-level field when the server
 * exposes one, falling back to the private `_assigned_work` key the control
 * plane stores inside share_json.
 */
export function leaseAssignedWork(lease: LeaseOut): number | null {
  if (typeof lease.assigned_work === "number") return lease.assigned_work;
  const v = lease.share_json?.["_assigned_work"];
  return typeof v === "number" && !Number.isNaN(v) ? v : null;
}

/** The worker's short explanation for holding no work (null when absent). */
export function leaseAssignedReason(lease: LeaseOut): string | null {
  if (typeof lease.assigned_reason === "string" && lease.assigned_reason) {
    return lease.assigned_reason;
  }
  const v = lease.share_json?.["_assigned_reason"];
  return typeof v === "string" && v ? v : null;
}

export function LeaseTable({
  leases,
  latest,
}: {
  leases: LeaseOut[];
  latest: Map<number, SlotLatest>;
}) {
  const columns: Column<LeaseOut>[] = [
    { key: "slot", header: "Slot", className: "tabular-nums", cell: (l) => l.slot },
    {
      key: "holder",
      header: "Holder",
      cell: (l) => (
        <span className="font-mono text-xs text-slate-300">{l.holder ?? "—"}</span>
      ),
    },
    { key: "node", header: "Node", cell: (l) => l.node ?? "—" },
    {
      key: "assigned",
      header: "Assigned work",
      className: "text-right tabular-nums",
      cell: (l) => {
        const assigned = leaseAssignedWork(l);
        if (assigned == null) return "—";
        if (assigned > 0) return fmtInt(assigned);
        const reason = leaseAssignedReason(l);
        return (
          <span
            className="font-medium text-red-400"
            title={reason ?? "this worker holds no work and will generate nothing"}
          >
            none
          </span>
        );
      },
    },
    {
      key: "target",
      header: "Target",
      className: "text-right tabular-nums",
      cell: (l) => {
        const v = leaseTargetShare(l);
        return v != null ? fmtNum(v, 1) : "—";
      },
    },
    {
      key: "eps",
      header: "EPS",
      className: "text-right tabular-nums",
      cell: (l) => fmtNum(latest.get(l.slot)?.eps ?? null, 1),
    },
    {
      key: "lag",
      header: "Lag s",
      className: "text-right tabular-nums",
      cell: (l) => {
        const lag = latest.get(l.slot)?.lag_s ?? null;
        if (lag == null) return "—";
        const warn = lag > LAG_WARN_S;
        return <span className={warn ? "text-red-400" : undefined}>{fmtNum(lag, 0)}</span>;
      },
    },
    {
      key: "queue",
      header: "Queue",
      className: "text-right tabular-nums",
      cell: (l) => fmtInt(latest.get(l.slot)?.queue_depth ?? null),
    },
    {
      key: "rss",
      header: "RSS MB",
      className: "text-right tabular-nums",
      cell: (l) => fmtNum(latest.get(l.slot)?.rss_mb ?? null, 0),
    },
    {
      key: "restarts",
      header: "Restarts",
      className: "text-right tabular-nums",
      cell: (l) => (
        <span className={l.restarts > 0 ? "text-amber-400" : undefined}>
          {l.restarts}
        </span>
      ),
    },
    {
      key: "heartbeat",
      header: "Heartbeat",
      className: "whitespace-nowrap text-slate-400",
      cell: (l) =>
        l.last_heartbeat_at ? `${fmtElapsed(l.last_heartbeat_at)} ago` : "—",
    },
    {
      key: "state",
      header: "State",
      cell: (l) => <StatusBadge state={l.state} />,
    },
  ];

  return (
    <Table
      columns={columns}
      rows={leases}
      rowKey={(l) => l.slot}
      empty={<p className="px-1 py-6 text-sm text-slate-500">No leases on this run.</p>}
    />
  );
}
