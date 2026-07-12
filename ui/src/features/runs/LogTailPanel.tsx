import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { POLL_MS } from "../../lib/queryClient";
import { Button } from "../../components/Button";
import { Select } from "../../components/Field";
import { ErrorState, LoadingState } from "../../components/States";
import type { LeaseOut } from "../../lib/types";

// "Log tail" tab (section 10.3): recent worker log lines from
// GET /runs/{id}/logs — live from the driver while provisioned, falling back to
// the leases' stored final_log_tail after the workload is gone. A slot selector
// scopes to one worker (whole fleet by default); tail size is adjustable.

const ALL_SLOTS = "__all__";
const TAIL_OPTIONS = [100, 200, 500, 1000];

export function LogTailPanel({
  runId,
  leases,
  active,
}: {
  runId: number;
  leases: LeaseOut[];
  active: boolean;
}) {
  const [slotSel, setSlotSel] = useState(ALL_SLOTS);
  const [tail, setTail] = useState(200);

  const slot = slotSel === ALL_SLOTS ? undefined : Number(slotSel);

  const q = useQuery({
    queryKey: ["run", runId, "logs", slotSel, tail],
    queryFn: () => api.runs.logs(runId, { slot, tail }),
    refetchInterval: active ? POLL_MS : false,
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-slate-400">
          Slot
          <Select
            value={slotSel}
            onChange={(e) => setSlotSel(e.target.value)}
            className="w-auto"
          >
            <option value={ALL_SLOTS}>All slots</option>
            {leases.map((l) => (
              <option key={l.slot} value={String(l.slot)}>
                slot {l.slot}
                {l.holder ? ` · ${l.holder}` : ""}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-2 text-xs text-slate-400">
          Tail
          <Select
            value={String(tail)}
            onChange={(e) => setTail(Number(e.target.value))}
            className="w-auto"
          >
            {TAIL_OPTIONS.map((n) => (
              <option key={n} value={String(n)}>
                {n} lines
              </option>
            ))}
          </Select>
        </label>
        <Button variant="ghost" onClick={() => q.refetch()} disabled={q.isFetching}>
          {q.isFetching ? "Refreshing…" : "Refresh"}
        </Button>
      </div>

      {q.isPending ? (
        <LoadingState />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : q.data.lines.length === 0 ? (
        <p className="rounded-md border border-surface-muted bg-surface px-3 py-6 text-center text-sm text-slate-500">
          No log lines available for this scope.
        </p>
      ) : (
        <pre className="max-h-[28rem] overflow-auto rounded-md border border-surface-muted bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-300">
          {q.data.lines.join("\n")}
        </pre>
      )}
    </div>
  );
}
