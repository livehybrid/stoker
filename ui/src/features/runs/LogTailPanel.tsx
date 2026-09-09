import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { POLL_MS } from "../../lib/queryClient";
import { Button } from "../../components/Button";
import { Select } from "../../components/Field";
import { ErrorState, LoadingState } from "../../components/States";
import type { LeaseOut } from "../../lib/types";
import { Inline, Panel, Pre, Stack } from "../../components/text";

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
    <Stack>
      <Inline>
        <Inline>
          Slot
          <Select
            value={slotSel}
            onChange={(_e, { value }) => setSlotSel(String(value))}
          >
            <Select.Option value={ALL_SLOTS} label="All slots" />
            {leases.map((l) => (
              <Select.Option key={l.slot} value={String(l.slot)} label={`slot ${l.slot}
                ${l.holder ? ` · $${l.holder}` : ""}`} />
            ))}
          </Select>
        </Inline>
        <Inline>
          Tail
          <Select
            value={String(tail)}
            onChange={(_e, { value }) => setTail(Number(value))}
          >
            {TAIL_OPTIONS.map((n) => (
              <Select.Option key={n} value={String(n)} label={`${n} lines`} />
            ))}
          </Select>
        </Inline>
        <Button variant="ghost" onClick={() => q.refetch()} disabled={q.isFetching}>
          {q.isFetching ? "Refreshing…" : "Refresh"}
        </Button>
      </Inline>

      {q.isPending ? (
        <LoadingState />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : q.data.lines.length === 0 ? (
        <Panel>
          No log lines available for this scope.
        </Panel>
      ) : (
        <Pre>
          {q.data.lines.join("\n")}
        </Pre>
      )}
    </Stack>
  );
}
