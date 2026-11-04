import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";

import { api, ApiError } from "../../lib/api";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import type { RunOut } from "../../lib/types";
import { Inline } from "../../components/text";
import { isTerminal } from "./format";

// Lifecycle actions for one run: Pause / Resume, Stop, Force stop and Clone.
// Shared by the run detail header and each row of the runs list, so both offer
// the same controls with the same rules. Every button stops click propagation
// so a press inside a clickable table row does not also open the run.
//
// Pause holds the fleet's output; a bounded run's end moves out by the time it
// spends paused. Only a rate-paced run (eps, per_day_gb) that is running can be
// paused: count_interval is paced by the engine, which spaces and stamps events
// itself. Clone opens the spec wizard prefilled with what this run was
// launched with, to tweak and save as a new spec.

function errText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export function RunActions({
  run,
  rateMode,
  compact = false,
}: {
  run: RunOut;
  rateMode?: string;
  /** Row form for the runs list: quieter buttons. */
  compact?: boolean;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const id = run.id;
  const terminal = isTerminal(run.state);
  const paused = run.state === "paused";
  const pausable = run.state === "running" && rateMode !== "count_interval";
  const stopping = run.state === "draining";

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["run", id] });
    qc.invalidateQueries({ queryKey: ["runs"] });
  };

  const stop = useMutation({
    mutationFn: (force: boolean) => api.runs.stop(id, { force }),
    onSuccess: (_r, force) => {
      toast.success(force ? `Run #${id} force stopped` : `Run #${id} draining`);
      invalidate();
    },
    onError: (err) => toast.error(errText(err, "Stop failed")),
  });
  const pause = useMutation({
    mutationFn: () => api.runs.pause(id),
    onSuccess: () => {
      toast.success(`Run #${id} paused`);
      invalidate();
    },
    onError: (err) => toast.error(errText(err, "Pause failed")),
  });
  const resume = useMutation({
    mutationFn: () => api.runs.resume(id),
    onSuccess: () => {
      toast.success(`Run #${id} resumed`);
      invalidate();
    },
    onError: (err) => toast.error(errText(err, "Resume failed")),
  });

  const busy = stop.isPending || pause.isPending || resume.isPending;
  const ghost = compact ? "ghost" : "secondary";

  const confirmForce = () =>
    window.confirm(
      `Force stop run #${id}? This destroys the fleet immediately (no drain).`,
    );

  return (
    <Inline>
      {!terminal && (paused ? (
        <Button
          variant={compact ? "ghost" : "primary"}
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            resume.mutate();
          }}
          title="Resume output; the run's end moves out by the time it was paused"
        >
          Resume
        </Button>
      ) : (
        <Button
          variant={ghost}
          disabled={busy || !pausable}
          onClick={(e) => {
            e.stopPropagation();
            pause.mutate();
          }}
          title={
            rateMode === "count_interval"
              ? "count_interval runs are paced by the engine and cannot be paused"
              : run.state === "running"
                ? "Hold output; the run's end moves out by the time it is paused"
                : "Only a running run can be paused"
          }
        >
          Pause
        </Button>
      ))}
      {!terminal && (
        <Button
          variant={ghost}
          disabled={busy || stopping}
          onClick={(e) => {
            e.stopPropagation();
            stop.mutate(false);
          }}
          title="Drain: workers flush and report, then the fleet is removed"
        >
          Stop
        </Button>
      )}
      {!terminal && (
        <Button
          variant={compact ? "ghost" : "danger"}
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            if (confirmForce()) stop.mutate(true);
          }}
          title="Destroy the fleet now, without draining"
        >
          Force stop
        </Button>
      )}
      <Button
        variant="ghost"
        onClick={(e) => {
          e.stopPropagation();
          navigate({ to: "/specs/new", search: { cloneRun: id } });
        }}
        title="Open a new spec prefilled with this run's settings"
      >
        Clone
      </Button>
    </Inline>
  );
}
