import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { HecPoint } from "./metrics";
import { fmtInt } from "./format";

// Second chart of the run detail (section 10.3): stacked HEC outcomes per
// interval — 2xx (ok), 4xx (client), 5xx (server) and timeouts. Deltas of the
// cumulative counters (computed in metrics.ts), so this reads as a rate.

const TOOLTIP_STYLE = {
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 6,
  fontSize: 12,
} as const;

export function HecChart({ points }: { points: HecPoint[] }) {
  if (points.length === 0) {
    return <p className="text-sm text-slate-500">No HEC delivery samples yet.</p>;
  }
  const anyErrors = points.some((p) => p.client || p.server || p.timeout);
  return (
    <div className="space-y-2">
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
            <XAxis dataKey="label" stroke="#94a3b8" fontSize={11} minTickGap={40} />
            <YAxis stroke="#94a3b8" fontSize={11} tickFormatter={fmtInt} width={56} />
            <Tooltip
              contentStyle={TOOLTIP_STYLE}
              formatter={(value: number, name: string) => [fmtInt(value), name]}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Area
              type="monotone"
              stackId="hec"
              dataKey="ok"
              name="2xx"
              stroke="#10b981"
              fill="#10b981"
              fillOpacity={0.5}
              isAnimationActive={false}
            />
            <Area
              type="monotone"
              stackId="hec"
              dataKey="client"
              name="4xx"
              stroke="#f59e0b"
              fill="#f59e0b"
              fillOpacity={0.55}
              isAnimationActive={false}
            />
            <Area
              type="monotone"
              stackId="hec"
              dataKey="server"
              name="5xx"
              stroke="#ef4444"
              fill="#ef4444"
              fillOpacity={0.6}
              isAnimationActive={false}
            />
            <Area
              type="monotone"
              stackId="hec"
              dataKey="timeout"
              name="timeout"
              stroke="#a855f7"
              fill="#a855f7"
              fillOpacity={0.55}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {!anyErrors && (
        <p className="text-xs text-emerald-400/80">
          All HEC responses 2xx over this window.
        </p>
      )}
    </div>
  );
}
