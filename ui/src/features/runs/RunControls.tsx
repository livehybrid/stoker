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


// Run controls (section 10.3): Scale ± (workers) and Rescale rate. Lifecycle
// actions (pause, resume, stop, force stop, clone) live in RunActions in the
// page header. Each wires straight to its endpoint; all disable on a terminal
// or paused run and while a mutation is in flight. A successful action
// invalidates the run so the 5 s poll reflects the new state immediately.

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

  // Scale and rescale wait for a resume (the server answers 409 while paused).
  const locked = terminal || run.state === "paused";
  const busy = scale.isPending || rescale.isPending;
  const parsedRate = Number(rateInput);
  const rateValid = rateInput !== "" && Number.isFinite(parsedRate) && parsedRate > 0;
  const rateChanged = rateValid && parsedRate !== rateValue;

  return (
    <Controls>
      {/* Scale workers */}
      <Group>
        <Label>Workers ({workers})</Label>
        <Inline>
          <Button
            variant="secondary"
            disabled={locked || busy || workers <= 1}
            onClick={() => scale.mutate(workers - 1)}
            title="Scale down one worker"
          >
            −
          </Button>
          <Count>{workers}</Count>
          <Button
            variant="secondary"
            disabled={locked || busy}
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
              disabled={locked || busy}
            />
            <Button
              variant="primary"
              disabled={locked || busy || !rateChanged}
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
