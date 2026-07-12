import type { ReactNode } from "react";
import type { RunDetail } from "../../lib/types";
import { fmtBytes, fmtElapsed, fmtInt } from "./format";

// A compact totals strip for the run header: cumulative events + volume + HEC
// outcome counts + elapsed. Values come from the folded totals_json (updated as
// workers report their final summaries) with a live elapsed clock.

function num(totals: Record<string, unknown> | null | undefined, key: string): number | null {
  const v = totals?.[key];
  return typeof v === "number" ? v : null;
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  tone?: "red" | "amber";
}) {
  const valueClass =
    tone === "red" ? "text-red-400" : tone === "amber" ? "text-amber-400" : "text-slate-100";
  return (
    <div className="min-w-[7rem]">
      <div className={`text-lg font-semibold tabular-nums ${valueClass}`}>{value}</div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
    </div>
  );
}

export function TotalsStrip({ run }: { run: RunDetail }) {
  const t = (run.totals_json as Record<string, unknown> | null) ?? {};
  const events = num(t, "events_total");
  const bytes = num(t, "bytes_total");
  const ok = num(t, "hec_2xx");
  const client = num(t, "hec_4xx");
  const server = num(t, "hec_5xx");
  const timeout = num(t, "hec_timeouts");
  const retries = num(t, "retries");

  return (
    <div className="flex flex-wrap gap-x-8 gap-y-4">
      <Stat label="Events" value={fmtInt(events)} />
      <Stat label="Volume" value={fmtBytes(bytes)} />
      <Stat label="HEC 2xx" value={fmtInt(ok)} />
      <Stat
        label="HEC 4xx"
        value={fmtInt(client)}
        tone={client && client > 0 ? "amber" : undefined}
      />
      <Stat
        label="HEC 5xx"
        value={fmtInt(server)}
        tone={server && server > 0 ? "red" : undefined}
      />
      <Stat
        label="Timeouts"
        value={fmtInt(timeout)}
        tone={timeout && timeout > 0 ? "red" : undefined}
      />
      <Stat
        label="Retries"
        value={fmtInt(retries)}
        tone={retries && retries > 0 ? "amber" : undefined}
      />
      <Stat label="Elapsed" value={fmtElapsed(run.t0 ?? run.created_at, run.ended_at)} />
    </div>
  );
}
