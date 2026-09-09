import { Table, type Column } from "../../components/Table";
import { StatusBadge } from "../../components/Badge";
import type { LeaseOut } from "../../lib/types";
import type { SlotLatest } from "./metrics";
import { leaseTargetShare } from "./metrics";
import { fmtElapsed, fmtInt, fmtNum } from "./format";
import { EmptyState } from "../../components/States";
import { Mono, Negative } from "../../components/text";

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
    { key: "slot", header: "Slot",  cell: (l) => l.slot },
    {
      key: "holder",
      header: "Holder",
      cell: (l) => (
        <Mono>{l.holder ?? "—"}</Mono>
      ),
    },
    { key: "node", header: "Node", cell: (l) => l.node ?? "—" },
    {
      key: "assigned",
      header: "Assigned work",
      align: "right",
      cell: (l) => {
        const assigned = leaseAssignedWork(l);
        if (assigned == null) return "—";
        if (assigned > 0) return fmtInt(assigned);
        const reason = leaseAssignedReason(l);
        return (
          <Negative
            title={reason ?? "this worker holds no work and will generate nothing"}
          >
            none
          </Negative>
        );
      },
    },
    {
      key: "target",
      header: "Target",
      align: "right",
      cell: (l) => {
        const v = leaseTargetShare(l);
        return v != null ? fmtNum(v, 1) : "—";
      },
    },
    {
      key: "eps",
      header: "EPS",
      align: "right",
      cell: (l) => fmtNum(latest.get(l.slot)?.eps ?? null, 1),
    },
    {
      key: "lag",
      header: "Lag s",
      align: "right",
      cell: (l) => {
        const lag = latest.get(l.slot)?.lag_s ?? null;
        if (lag == null) return "—";
        const warn = lag > LAG_WARN_S;
        return warn ? <Negative>{fmtNum(lag, 0)}</Negative> : <span>{fmtNum(lag, 0)}</span>;
      },
    },
    {
      key: "queue",
      header: "Queue",
      align: "right",
      cell: (l) => fmtInt(latest.get(l.slot)?.queue_depth ?? null),
    },
    {
      key: "rss",
      header: "RSS MB",
      align: "right",
      cell: (l) => fmtNum(latest.get(l.slot)?.rss_mb ?? null, 0),
    },
    {
      key: "restarts",
      header: "Restarts",
      align: "right",
      cell: (l) =>
        l.restarts > 0 ? (
          <Negative>{l.restarts}</Negative>
        ) : (
          <span>{l.restarts}</span>
        ),
    },
    {
      key: "heartbeat",
      header: "Heartbeat",
      
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
      empty={<EmptyState title="No leases on this run." />}
    />
  );
}
