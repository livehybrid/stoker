import { useSplunkTheme } from "@splunk/themes";

import { LineChart, PALETTE } from "../../components/chart";
import { Muted } from "../../components/text";
import type { MetricPreviewResponse } from "../../lib/types";

// The builder's live preview: one metric's value over a 24 h day. The solid line
// is a sampled value (with noise), the dashed line the pattern centre, and the
// three horizontal guides are the min / p95 / max the values move between.
//
// The guides are flat two-point series rather than reference lines: Splunk's
// Line chart has no y-reference-line option, and a series spanning the axis
// draws the same thing, appears in the legend and reads in the tooltip.

const Dim = ({ children }: { children: React.ReactNode }) => (
  <div style={{ opacity: 0.6, transition: "opacity 120ms" }}>{children}</div>
);

export function PreviewChart({
  data,
  loading,
}: {
  data: MetricPreviewResponse | null;
  loading: boolean;
}) {
  const theme = useSplunkTheme() as unknown as Record<string, string>;

  if (!data) {
    return (
      <Muted>{loading ? "Rendering preview…" : "No preview yet."}</Muted>
    );
  }

  const { min, p95, max } = data.guides;
  const hours = data.points.map((p) => p.hour);
  const span: [number, number] = [
    hours.length ? Math.min(...hours) : 0,
    hours.length ? Math.max(...hours) : 24,
  ];
  const guide = (label: string, value: number, color: string) => ({
    label,
    color,
    dashed: true,
    points: span.map((h) => [h, value] as [number, number | null]),
  });

  const chart = (
    <LineChart
      height={288}
      xKind="category"
      xTitle="hour"
      leftTitle={data.unit || undefined}
      empty={<Muted>No preview points.</Muted>}
      series={[
        {
          label: "Centre",
          color: theme.contentColorMuted,
          dashed: true,
          points: data.points.map((p) => [p.hour, p.center] as [number, number | null]),
        },
        {
          label: `${data.metric}${data.unit ? ` (${data.unit})` : ""}`,
          color: PALETTE[1],
          points: data.points.map((p) => [p.hour, p.value] as [number, number | null]),
        },
        guide("max", max, theme.errorColor),
        guide("p95", p95, theme.successColor),
        guide("min", min, theme.warningColor),
      ]}
    />
  );

  return (
    <div>
      {loading ? <Dim>{chart}</Dim> : chart}
      <Muted $small>
        {data.kind} · min {min} · p95 {p95} · max {max} · {data.series_count} series
      </Muted>
    </div>
  );
}
