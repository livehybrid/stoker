import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import { Button } from "../../components/Button";
import { TextInput } from "../../components/Field";
import { useToast } from "../../components/Toast";
import type { RunDetail } from "../../lib/types";

import styled from "styled-components";
import { variables } from "@splunk/themes";
import { Inline, Label } from "../../components/text";

const Controls = styled.div`
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: ${variables.spacingMedium} ${variables.spacingXXLarge};
`;

const Group = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${variables.spacingXSmall};
`;

const Count = styled.span`
  min-width: 2ch;
  text-align: center;
  font-variant-numeric: tabular-nums;
`;


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
    <Controls>
      {/* Stop / force stop */}
      <Group>
        <Label>Lifecycle</Label>
        <Inline>
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
        </Inline>
      </Group>

      {/* Scale workers */}
      <Group>
        <Label>Workers ({workers})</Label>
        <Inline>
          <Button
            variant="secondary"
            disabled={terminal || busy || workers <= 1}
            onClick={() => scale.mutate(workers - 1)}
            title="Scale down one worker"
          >
            −
          </Button>
          <Count>{workers}</Count>
          <Button
            variant="secondary"
            disabled={terminal || busy}
            onClick={() => scale.mutate(workers + 1)}
            title="Scale up one worker"
          >
            +
          </Button>
        </Inline>
      </Group>

      {/* Rescale rate */}
      {rescalable && (
        <Group>
          <Label>Rate ({rateMode})</Label>
          <Inline>
            <TextInput
              type="number"
              value={rateInput}
              onChange={(_e, { value }) => setRateInput(value)}
              inline
              disabled={terminal || busy}
            />
            <Button
              variant="primary"
              disabled={terminal || busy || !rateChanged}
              onClick={() => rescale.mutate(parsedRate)}
            >
              Rescale
            </Button>
          </Inline>
        </Group>
      )}
    </Controls>
  );
}
