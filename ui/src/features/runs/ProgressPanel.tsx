import { useMemo } from "react";
import Progress from "@splunk/react-ui/Progress";

import type { RunProgress } from "../../lib/types";
import { LineChart, PALETTE } from "../../components/chart";
import { BigNumber, Label, Muted, Stack, Stat, StatRow } from "../../components/text";
import { fmtDateTime, fmtInt, fmtNum } from "./format";

// Progress of a bounded run, and when it should finish.
//
// A backfill counts events done against the events it was planned to write,
// and its finish is the events left divided by the throughput the fleet is
// actually holding (the trailing five minutes), not the delivery target: a
// fleet holding 90% of its target finishes about a tenth later than the target
// says, and the target's finish is shown beside it for that comparison.
//
// The burn-down plots the events left over time (solid), the projection from
// now at the actual rate (dashed, same colour), and the pace the target would
// have kept from the start (dashed, second colour).

/** Seconds to "2 d 03 h", "5 h 07 m", "12 min". */
function fmtEta(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "—";
  const total = Math.max(0, Math.round(s));
  if (total < 90) return `${total} s`;
  const mins = Math.floor(total / 60);
  if (mins < 90) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours} h ${String(mins % 60).padStart(2, "0")} m`;
  return `${Math.floor(hours / 24)} d ${String(hours % 24).padStart(2, "0")} h`;
}

type Point = [number, number | null];

export function burnDownSeries(p: RunProgress, nowMs: number) {
  const planned = p.planned_events ?? 0;
  const startMs = p.started_at ? Date.parse(p.started_at) : NaN;
  // The projection starts where the actual line ends, so the two join.
  const fromMs = p.series.length ? p.series[p.series.length - 1].ts : nowMs;
  const expectedMs = p.expected_end ? Date.parse(p.expected_end) : NaN;
  const targetEps = p.target_eps ?? 0;
  const paceEndMs =
    Number.isFinite(startMs) && targetEps > 0 ? startMs + (planned / targetEps) * 1000 : NaN;

  // Every series needs a value at every x, or the chart draws gaps.
  const xs = new Set<number>(p.series.map((pt) => pt.ts));
  if (Number.isFinite(startMs)) xs.add(startMs);
  if (Number.isFinite(expectedMs)) {
    xs.add(fromMs);
    xs.add(expectedMs);
  }
  if (Number.isFinite(paceEndMs)) xs.add(paceEndMs);
  const sorted = Array.from(xs).sort((a, b) => a - b);

  const remainingAt = new Map(p.series.map((pt) => [pt.ts, Math.max(0, planned - pt.done)]));
  const remainingNow = p.remaining_events ?? 0;
  const rate = p.recent_eps ?? 0;

  const actual: Point[] = sorted.map((x) => [x, remainingAt.get(x) ?? null]);
  const projected: Point[] = sorted.map((x) => [
    x,
    Number.isFinite(expectedMs) && x >= fromMs
      ? Math.max(0, remainingNow - (rate * (x - fromMs)) / 1000)
      : null,
  ]);
  const pace: Point[] = sorted.map((x) => [
    x,
    Number.isFinite(paceEndMs) && x >= startMs
      ? Math.max(0, planned - (targetEps * (x - startMs)) / 1000)
      : null,
  ]);
  return { actual, projected, pace };
}

export function ProgressPanel({ progress }: { progress: RunProgress }) {
  const p = progress;
  const series = useMemo(() => burnDownSeries(p, Date.now()), [p]);

  if (p.kind === "duration") {
    return (
      <Stack $gap="medium">
        <Progress percentage={Math.round((p.fraction ?? 0) * 1000) / 10} />
        <StatRow>
          <Stat>
            <BigNumber $size="medium">{fmtNum((p.fraction ?? 0) * 100, 1)}%</BigNumber>
            <Label>of its duration</Label>
          </Stat>
          <Stat>
            <BigNumber $size="medium">{fmtEta(p.eta_s)}</BigNumber>
            <Label>left · ends {fmtDateTime(p.expected_end)}</Label>
          </Stat>
        </StatRow>
      </Stack>
    );
  }

  const pct = (p.fraction ?? 0) * 100;
  const ofTarget =
    p.recent_eps != null && p.target_eps ? (p.recent_eps / p.target_eps) * 100 : null;
  const finishing = !["completed", "stopped", "failed", "paused"].includes(p.state);

  return (
    <Stack $gap="medium">
      <Progress percentage={Math.round(pct * 10) / 10} />
      <StatRow>
        <Stat>
          <BigNumber $size="medium">{fmtNum(pct, 1)}%</BigNumber>
          <Label>
            {fmtInt(p.done_events)} of {fmtInt(p.planned_events)} events
          </Label>
        </Stat>
        <Stat>
          <BigNumber $size="medium">{fmtInt(p.recent_eps)}</BigNumber>
          <Label>
            ev/s actual, last {Math.round(p.rate_window_s / 60)} min
            {ofTarget != null && ` · ${fmtNum(ofTarget, 0)}% of the ${fmtInt(p.target_eps)} target`}
          </Label>
        </Stat>
        {finishing && (
          <Stat>
            <BigNumber $size="medium">{fmtEta(p.eta_s)}</BigNumber>
            <Label>
              {p.expected_end
                ? `left · estimated finish ${fmtDateTime(p.expected_end)}`
                : p.stale
                  ? "no recent heartbeats to estimate from"
                  : "left · waiting for throughput"}
            </Label>
          </Stat>
        )}
        {finishing && p.target_end && (
          <Stat>
            <BigNumber $size="medium">{fmtDateTime(p.target_end)}</BigNumber>
            <Label>finish if the target rate were held</Label>
          </Stat>
        )}
      </StatRow>
      {p.state === "paused" && <Muted>Paused: no finish is estimated until it resumes.</Muted>}
      <LineChart
        height={240}
        leftTitle="events left"
        empty={<Muted>No heartbeats yet to draw a burn-down from.</Muted>}
        series={[
          { label: "Events left", color: PALETTE[0], points: series.actual },
          {
            label: "Projected at the actual rate",
            color: PALETTE[0],
            dashed: true,
            points: series.projected,
          },
          {
            label: "Target pace from the start",
            color: PALETTE[1],
            dashed: true,
            points: series.pace,
          },
        ]}
      />
    </Stack>
  );
}
