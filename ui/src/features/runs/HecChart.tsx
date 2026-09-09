import { LineChart } from "../../components/chart";
import { Muted, Negative, Positive, Stack } from "../../components/text";
import type { HecPoint } from "./metrics";
import { fmtInt } from "./format";

// Second chart of the run detail (section 10.3): HEC outcomes per interval —
// deltas of the cumulative counters (computed in metrics.ts), so this reads as
// a rate.
//
// The two-axis split is the substantive decision here and it survives the move
// to Splunk's charts. 2xx volume is typically orders of magnitude larger than
// the error counts, so putting everything on one axis left a ZERO-error series
// sitting on top of the 2xx line, reading as "everything failed". Instead 2xx
// keeps the left axis, and 4xx/5xx/timeouts go on Splunk's overlay axis, scaled
// to the errors alone: zero errors sit flat at zero, and a handful of errors is
// still visible against millions of successes.
//
// The 2xx series used to be a filled area. Splunk's Area visualization draws
// every series as an area including the overlay ones, which is not what this
// chart wants, so all four are lines.

export function HecChart({
  points,
  terminal = false,
}: {
  points: HecPoint[];
  terminal?: boolean;
}) {
  const errorTotal = points.reduce(
    (sum, p) => sum + p.client + p.server + p.timeout,
    0,
  );
  const at = (p: HecPoint, v: number) => [p.ts, v] as [number, number | null];

  return (
    <Stack>
      <LineChart
        height={224}
        leftTitle="2xx per interval"
        rightTitle="errors per interval"
        empty={
          <Muted>
            {terminal
              ? "No HEC delivery samples were recorded for this run."
              : "No HEC delivery samples yet."}
          </Muted>
        }
        series={[
          { label: "2xx", points: points.map((p) => at(p, p.ok)) },
          { label: "4xx", axis: "right" as const, points: points.map((p) => at(p, p.client)) },
          { label: "5xx", axis: "right" as const, points: points.map((p) => at(p, p.server)) },
          {
            label: "timeout",
            axis: "right" as const,
            points: points.map((p) => at(p, p.timeout)),
          },
        ]}
      />
      {points.length > 0 &&
        (errorTotal === 0 ? (
          <Positive>All HEC responses 2xx over this window.</Positive>
        ) : (
          <Negative>
            {fmtInt(errorTotal)} non-2xx/timed-out HEC responses over this window (right
            axis).
          </Negative>
        ))}
    </Stack>
  );
}
