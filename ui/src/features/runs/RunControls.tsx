import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import { Button } from "../../components/Button";
import { TextInput } from "../../components/Field";
import { useToast } from "../../components/Toast";
import type { RunDetail } from "../../lib/types";

// Run controls (section 10.3): Stop, Force stop, Scale ± (workers) and Rescale
// rate. Each wires straight to its endpoint; all disable on a terminal run and
// while a mutation is in flight. A successful action invalidates the run so the
// 5 s poll reflects the new state immediately.

function errText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export function RunControls({
  run,
  terminal,
  workers,
  rateMode,
  rateValue,
}: {
  run: RunDetail;
  terminal: boolean;
  workers: number; // current worker count (lease count)
  rateMode: string | undefined;
  rateValue: number | null | undefined;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const id = run.id;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["run", id] });
    qc.invalidateQueries({ queryKey: ["runs"] });
  };

  const stop = useMutation({
    mutationFn: (force: boolean) => api.runs.stop(id, { force }),
    onSuccess: (_r, force) => {
      toast.success(force ? "Force stop issued" : "Drain started");
      invalidate();
    },
    onError: (err) => toast.error(errText(err, "Stop failed")),
  });

  const scale = useMutation({
    mutationFn: (next: number) => api.runs.scale(id, { workers: next }),
    onSuccess: (_r, next) => {
      toast.success(`Scaling to ${next} worker${next === 1 ? "" : "s"}`);
      invalidate();
    },
    onError: (err) => toast.error(errText(err, "Scale failed")),
  });

  const rescale = useMutation({
    mutationFn: (value: number) => api.runs.rescale(id, { rate_value: value }),
    onSuccess: (_r, value) => {
      toast.success(`Rescaled to ${value} ${rateMode ?? ""}`.trim());
      invalidate();
    },
    onError: (err) => toast.error(errText(err, "Rescale failed")),
  });

  // Rescale is only meaningful for rate-driven modes (eps / per_day_gb).
  const rescalable = rateMode === "eps" || rateMode === "per_day_gb";
  const [rateInput, setRateInput] = useState<string>(
    rateValue != null ? String(rateValue) : "",
  );
  // Keep the input in step with the live snapshot value between edits.
  useEffect(() => {
    setRateInput(rateValue != null ? String(rateValue) : "");
  }, [rateValue]);

  const busy = stop.isPending || scale.isPending || rescale.isPending;
  const parsedRate = Number(rateInput);
  const rateValid = rateInput !== "" && Number.isFinite(parsedRate) && parsedRate > 0;
  const rateChanged = rateValid && parsedRate !== rateValue;

  return (
    <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
      {/* Stop / force stop */}
      <div className="space-y-1">
        <span className="block text-xs font-medium text-slate-400">Lifecycle</span>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={terminal || busy}
            onClick={() => stop.mutate(false)}
          >
            Stop
          </Button>
          <Button
            variant="danger"
            disabled={terminal || busy}
            onClick={() => {
              if (
                window.confirm(
                  "Force stop destroys the fleet immediately (no drain). Continue?",
                )
              ) {
                stop.mutate(true);
              }
            }}
          >
            Force stop
          </Button>
        </div>
      </div>

      {/* Scale workers */}
      <div className="space-y-1">
        <span className="block text-xs font-medium text-slate-400">
          Workers ({workers})
        </span>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            disabled={terminal || busy || workers <= 1}
            onClick={() => scale.mutate(workers - 1)}
            title="Scale down one worker"
          >
            −
          </Button>
          <span className="min-w-[2ch] text-center text-sm tabular-nums text-slate-200">
            {workers}
          </span>
          <Button
            variant="secondary"
            disabled={terminal || busy}
            onClick={() => scale.mutate(workers + 1)}
            title="Scale up one worker"
          >
            +
          </Button>
        </div>
      </div>

      {/* Rescale rate */}
      {rescalable && (
        <div className="space-y-1">
          <span className="block text-xs font-medium text-slate-400">
            Rate ({rateMode})
          </span>
          <div className="flex items-center gap-2">
            <TextInput
              type="number"
              min="0"
              step="any"
              value={rateInput}
              onChange={(e) => setRateInput(e.target.value)}
              className="w-32"
              disabled={terminal || busy}
            />
            <Button
              variant="primary"
              disabled={terminal || busy || !rateChanged}
              onClick={() => rescale.mutate(parsedRate)}
            >
              Rescale
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
