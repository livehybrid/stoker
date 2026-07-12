import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { RatePoint } from "./metrics";
import { fmtBps, fmtInt, fmtNum } from "./format";

// Top chart of the run detail (section 10.3): target vs actual events/s overlaid
// (the gap between them is the headline signal) with bytes/s on a second Y axis.

const TOOLTIP_STYLE = {
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 6,
  fontSize: 12,
} as const;

function tickInt(v: number): string {
  return fmtInt(v);
}
function tickBytes(v: number): string {
  return fmtBps(v);
}

export function RateChart({ points }: { points: RatePoint[] }) {
  if (points.length === 0) {
    return <p className="text-sm text-slate-500">No metric samples yet.</p>;
  }
  const hasTarget = points.some((p) => p.target != null);
  return (
    <div className="h-72">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
          <XAxis dataKey="label" stroke="#94a3b8" fontSize={11} minTickGap={40} />
          <YAxis
            yAxisId="eps"
            stroke="#94a3b8"
            fontSize={11}
            tickFormatter={tickInt}
            width={56}
          />
          <YAxis
            yAxisId="bps"
            orientation="right"
            stroke="#64748b"
            fontSize={11}
            tickFormatter={tickBytes}
            width={72}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            formatter={(value: number, name: string) => {
              if (name === "Bytes/s") return [fmtBps(value), name];
              return [`${fmtNum(value, 1)} ev/s`, name];
            }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {hasTarget && (
            <Line
              yAxisId="eps"
              type="monotone"
              dataKey="target"
              name="Target ev/s"
              stroke="#f59e0b"
              strokeDasharray="5 4"
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          )}
          <Line
            yAxisId="eps"
            type="monotone"
            dataKey="eps"
            name="Actual ev/s"
            stroke="#38bdf8"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            yAxisId="bps"
            type="monotone"
            dataKey="bps"
            name="Bytes/s"
            stroke="#a78bfa"
            dot={false}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
