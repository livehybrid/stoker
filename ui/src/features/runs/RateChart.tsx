import { LineChart, PALETTE } from "../../components/chart";
import { Muted } from "../../components/text";
import type { RatePoint } from "./metrics";

// Top chart of the run detail (section 10.3): target vs actual events/s overlaid
// (the gap between them is the headline signal) with bytes/s on a second Y axis.
//
// The target is dashed and shares the actual rate's colour, because they are
// the same quantity: one asked for, one delivered. Bytes/s is on Splunk's
// overlay axis, because it is three orders of magnitude larger and would
// otherwise flatten both event rates onto the x-axis.

export function RateChart({
  points,
  terminal = false,
}: {
  points: RatePoint[];
  terminal?: boolean;
}) {
  const hasTarget = points.some((p) => p.target != null);
  return (
    <LineChart
      height={288}
      leftTitle="events/s"
      rightTitle="bytes/s"
      empty={
        <Muted>
          {terminal
            ? "No metric samples were recorded for this run (older samples may have been pruned)."
            : "No metric samples yet."}
        </Muted>
      }
      series={[
        ...(hasTarget
          ? [
              {
                label: "Target ev/s",
                color: PALETTE[1],
                dashed: true,
                points: points.map((p) => [p.ts, p.target] as [number, number | null]),
              },
            ]
          : []),
        {
          label: "Actual ev/s",
          color: PALETTE[1],
          points: points.map((p) => [p.ts, p.eps] as [number, number | null]),
        },
        {
          label: "Bytes/s",
          axis: "right" as const,
          points: points.map((p) => [p.ts, p.bps] as [number, number | null]),
        },
      ]}
    />
  );
}
