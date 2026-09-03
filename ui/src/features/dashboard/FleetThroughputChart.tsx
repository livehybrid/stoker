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

import type { FleetPoint } from "./metrics";

// Fleet throughput at a glance: delivered events/s (filled area, left axis) and
// MB/s (line, right axis) summed across every active run — the control plane's
// own answer to the Splunk dogfood dashboard, without leaving the UI.

const TOOLTIP_STYLE = {
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 6,
  fontSize: 12,
} as const;

function fmtEps(v: number): string {
  if (!Number.isFinite(v)) return "0";
  if (v >= 10_000) return `${(v / 1000).toFixed(1)}k`;
  return Math.round(v).toLocaleString("en-GB");
}
function fmtMbps(v: number): string {
  return `${v.toFixed(v >= 10 ? 0 : 1)}`;
}
function fmtTime(t: number): string {
  return new Date(t).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function FleetThroughputChart({ points }: { points: FleetPoint[] }) {
  if (points.length === 0) {
    return (
      <div className="flex h-56 items-center justify-center text-sm text-slate-500">
        No throughput yet — start a run to see live fleet delivery.
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={240}>
      <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis
          dataKey="t"
          type="number"
          scale="time"
          domain={["dataMin", "dataMax"]}
          tickFormatter={fmtTime}
          stroke="#64748b"
          fontSize={11}
        />
        <YAxis
          yAxisId="eps"
          stroke="#38bdf8"
          fontSize={11}
          tickFormatter={fmtEps}
          width={48}
        />
        <YAxis
          yAxisId="mbps"
          orientation="right"
          stroke="#f59e0b"
          fontSize={11}
          tickFormatter={fmtMbps}
          width={40}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(t) => fmtTime(t as number)}
          formatter={(value, name) =>
            name === "MB/s"
              ? [fmtMbps(value as number), "MB/s"]
              : [fmtEps(value as number), "events/s"]
          }
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Area
          yAxisId="eps"
          type="monotone"
          dataKey="eps"
          name="events/s"
          stroke="#38bdf8"
          fill="#38bdf8"
          fillOpacity={0.15}
          isAnimationActive={false}
        />
        <Line
          yAxisId="mbps"
          type="monotone"
          dataKey="mbps"
          name="MB/s"
          stroke="#f59e0b"
          dot={false}
          strokeWidth={1.5}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
