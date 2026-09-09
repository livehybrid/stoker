import { LineChart } from "../../components/chart";
import { Muted } from "../../components/text";
import type { FleetPoint } from "./metrics";

// Fleet throughput at a glance: delivered events/s and MB/s summed across every
// active run — the control plane's own answer to the Splunk dogfood dashboard,
// without leaving the UI. MB/s is on the overlay axis because the two differ by
// several orders of magnitude.

export function FleetThroughputChart({ points }: { points: FleetPoint[] }) {
  return (
    <LineChart
      height={240}
      leftTitle="events/s"
      rightTitle="MB/s"
      empty={<Muted>No throughput yet — start a run to see live fleet delivery.</Muted>}
      series={[
        {
          label: "events/s",
          points: points.map((p) => [p.t, p.eps] as [number, number | null]),
        },
        {
          label: "MB/s",
          axis: "right" as const,
          points: points.map((p) => [p.t, p.mbps] as [number, number | null]),
        },
      ]}
    />
  );
}
