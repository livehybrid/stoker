import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { MetricPreviewResponse } from "../../lib/types";

// The builder's live preview: one metric's value over a 24 h day. The solid line
// is a sampled value (with noise), the dashed line the pattern centre, and the
// three horizontal guides are the min / p95 / max the values move between.

const TOOLTIP_STYLE = {
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 6,
  fontSize: 12,
} as const;

function hourLabel(h: number): string {
  const hh = Math.floor(h) % 24;
  return `${String(hh).padStart(2, "0")}:00`;
}

export function PreviewChart({
  data,
  loading,
}: {
  data: MetricPreviewResponse | null;
  loading: boolean;
}) {
  if (!data) {
    return (
      <p className="flex h-72 items-center justify-center text-sm text-slate-500">
        {loading ? "Rendering preview…" : "No preview yet."}
      </p>
    );
  }
  const rows = data.points.map((p) => ({
    hour: p.hour,
    value: p.value,
    center: p.center,
  }));
  const { min, p95, max } = data.guides;
  return (
    <div className={loading ? "opacity-60 transition-opacity" : ""}>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
            <XAxis
              dataKey="hour"
              type="number"
              domain={[0, 24]}
              ticks={[0, 3, 6, 9, 12, 15, 18, 21, 24]}
              tickFormatter={hourLabel}
              stroke="#94a3b8"
              fontSize={11}
            />
            <YAxis stroke="#94a3b8" fontSize={11} width={56} />
            <Tooltip
              contentStyle={TOOLTIP_STYLE}
              labelFormatter={(h: number) => hourLabel(h)}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <ReferenceLine y={max} stroke="#ef4444" strokeDasharray="2 4" label={{ value: "max", fill: "#ef4444", fontSize: 10, position: "right" }} />
            <ReferenceLine y={p95} stroke="#10b981" strokeDasharray="2 4" label={{ value: "p95", fill: "#10b981", fontSize: 10, position: "right" }} />
            <ReferenceLine y={min} stroke="#f59e0b" strokeDasharray="2 4" label={{ value: "min", fill: "#f59e0b", fontSize: 10, position: "right" }} />
            <Line
              type="monotone"
              dataKey="center"
              name="Centre"
              stroke="#94a3b8"
              strokeDasharray="5 4"
              dot={false}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="value"
              name={`${data.metric}${data.unit ? ` (${data.unit})` : ""}`}
              stroke="#38bdf8"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-center text-[11px] text-slate-500">
        {data.kind} · min {min} · p95 {p95} · max {max} · {data.series_count} series
      </p>
    </div>
  );
}
