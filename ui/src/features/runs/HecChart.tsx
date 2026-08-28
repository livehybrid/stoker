import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { HecPoint } from "./metrics";
import { fmtInt } from "./format";

// Second chart of the run detail (section 10.3): HEC outcomes per interval —
// deltas of the cumulative counters (computed in metrics.ts), so this reads as
// a rate. 2xx volume is typically orders of magnitude larger than the error
// counts, so stacking everything on one axis put a ZERO-error series on top of
// the 2xx area, reading as "everything failed". Instead: 2xx stays a filled
// area on the left axis, and 4xx/5xx/timeouts are UNSTACKED lines on their own
// right-hand axis scaled to the errors alone — zero errors sit flat at zero,
// and a handful of errors is still visible against millions of successes.

const TOOLTIP_STYLE = {
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 6,
  fontSize: 12,
} as const;

export function HecChart({
  points,
  terminal = false,
}: {
  points: HecPoint[];
  terminal?: boolean;
}) {
  if (points.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        {terminal
          ? "No HEC delivery samples were recorded for this run."
          : "No HEC delivery samples yet."}
      </p>
    );
  }
  const errorTotal = points.reduce(
    (sum, p) => sum + p.client + p.server + p.timeout,
    0,
  );
  return (
    <div className="space-y-2">
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
            <XAxis dataKey="label" stroke="#94a3b8" fontSize={11} minTickGap={40} />
            <YAxis
              yAxisId="ok"
              stroke="#94a3b8"
              fontSize={11}
              tickFormatter={fmtInt}
              width={56}
            />
            <YAxis
              yAxisId="err"
              orientation="right"
              stroke="#f87171"
              fontSize={11}
              tickFormatter={fmtInt}
              width={48}
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={TOOLTIP_STYLE}
              formatter={(value: number, name: string) => [fmtInt(value), name]}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Area
              yAxisId="ok"
              type="monotone"
              dataKey="ok"
              name="2xx"
              stroke="#10b981"
              fill="#10b981"
              fillOpacity={0.5}
              isAnimationActive={false}
            />
            <Line
              yAxisId="err"
              type="monotone"
              dataKey="client"
              name="4xx"
              stroke="#f59e0b"
              dot={false}
              isAnimationActive={false}
            />
            <Line
              yAxisId="err"
              type="monotone"
              dataKey="server"
              name="5xx"
              stroke="#ef4444"
              dot={false}
              isAnimationActive={false}
            />
            <Line
              yAxisId="err"
              type="monotone"
              dataKey="timeout"
              name="timeout"
              stroke="#a855f7"
              dot={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {errorTotal === 0 ? (
        <p className="text-xs text-emerald-400/80">
          All HEC responses 2xx over this window.
        </p>
      ) : (
        <p className="text-xs text-red-400">
          {fmtInt(errorTotal)} non-2xx/timed-out HEC responses over this window
          (right axis).
        </p>
      )}
    </div>
  );
}
