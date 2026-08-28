import type { ReactNode } from "react";
import type { LeaseOut, RunDetail } from "../../lib/types";
import { leaseAssignedWork, leaseAssignedReason } from "./LeaseTable";

// Warning banners (section 10.3): degraded start, lag > 300 s, lost workers
// and workers holding no work (an over-provisioned run). Each is a coloured
// strip that only appears when its condition holds; a run with none of them
// shows no banner block at all.

const LAG_WARN_S = 300;

function Banner({
  tone,
  title,
  children,
}: {
  tone: "amber" | "red";
  title: string;
  children?: ReactNode;
}) {
  const styles =
    tone === "red"
      ? "border-red-800/70 bg-red-950/50 text-red-200"
      : "border-amber-800/70 bg-amber-950/50 text-amber-200";
  return (
    <div className={`rounded-md border px-4 py-2.5 text-sm ${styles}`}>
      <span className="font-semibold">{title}</span>
      {children ? <span> — {children}</span> : null}
    </div>
  );
}

export function WarningBanners({
  run,
  leases,
  peakLagS,
}: {
  run: RunDetail;
  leases: LeaseOut[];
  peakLagS: number;
}) {
  const lost = leases.filter((l: LeaseOut) => l.state === "lost");
  const banners: ReactNode[] = [];

  if (run.degraded) {
    banners.push(
      <Banner key="degraded" tone="amber" title="Degraded start">
        the run released on a subset of its requested workers; the same total rate
        is spread across the workers that came up.
      </Banner>,
    );
  }

  // Workers that reported they hold NO work (metrics: no series in their
  // stride shard; eventgen count_interval: every stanza count split to zero).
  // They are healthy and heart-beating but will generate nothing all run.
  const idle = leases.filter((l: LeaseOut) => leaseAssignedWork(l) === 0);
  if (idle.length > 0) {
    const slots = idle.map((l) => l.slot).join(", ");
    const reason = leaseAssignedReason(idle[0]);
    banners.push(
      <Banner
        key="no-work"
        tone="amber"
        title={`${idle.length} worker(s) hold no work`}
      >
        slot{idle.length === 1 ? "" : "s"} {slots} own no work and will generate
        nothing for the entire run — the run is over-provisioned for this pack
        (more workers than it has series/counts to shard).
        {reason ? ` Worker report: ${reason}.` : ""} Scale the run down (or use
        fewer workers next time) to put every slot to work.
      </Banner>,
    );
  }

  if (lost.length > 0) {
    banners.push(
      <Banner key="lost" tone="red" title={`${lost.length} worker(s) lost`}>
        slot{lost.length === 1 ? "" : "s"} {lost.map((l) => l.slot).join(", ")}{" "}
        stopped heart-beating. Sustained loss across more than half the fleet
        auto-aborts the run.
      </Banner>,
    );
  }

  if (peakLagS > LAG_WARN_S) {
    banners.push(
      <Banner key="lag" tone="amber" title="High lag">
        a worker is behind its schedule by {Math.round(peakLagS)} s (over the
        {" "}
        {LAG_WARN_S} s threshold); the generator cannot keep up with the target
        rate.
      </Banner>,
    );
  }

  if (banners.length === 0) return null;
  return <div className="space-y-2">{banners}</div>;
}
