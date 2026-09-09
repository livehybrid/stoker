// Target picker panel for the job wizard: a selectable list of HEC targets with
// health badge and env tag, plus a Test Connection button (POST /targets/{id}/
// test) that probes health + auth and refreshes the stored health_state.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { TargetOut, TargetTestResult } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Between, Callout, Inline, Mono, Muted, Negative, Stack, Strong } from "../../components/text";
import styled from "styled-components";
import { variables } from "@splunk/themes";

const Selectable = styled.div<{ $selected?: boolean; $disabled?: boolean }>`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: ${variables.spacingMedium};
  border: 1px solid
    ${(props) => (props.$selected ? variables.contentColorAccent : variables.borderColor)};
  border-radius: ${variables.borderRadius};
  background-color: ${(props) =>
    props.$selected ? variables.interactiveColorOverlaySelected : "transparent"};
  opacity: ${(props) => (props.$disabled ? 0.5 : 1)};

  &:hover {
    border-color: ${(props) =>
      props.$disabled ? variables.borderColor : variables.interactiveColorBorderHover};
  }
`;

function TestResultLine({ result }: { result: TargetTestResult }) {
  return (
    <Callout $tone={result.ok ? "info" : "error"}>
      <Strong>{result.ok ? "Reachable" : "Problem"}</Strong>
      {" · "}
      health {result.health ?? "?"} · auth {result.auth ?? "?"}
      {result.latency_ms != null ? ` · ${result.latency_ms} ms` : ""}
      {result.detail ? <Negative>{result.detail}</Negative> : null}
    </Callout>
  );
}

export function TargetPicker({
  targets,
  selectedId,
  onSelect,
}: {
  targets: TargetOut[];
  selectedId: number | null;
  onSelect: (target: TargetOut) => void;
}) {
  const qc = useQueryClient();
  const [results, setResults] = useState<Record<number, TargetTestResult>>({});

  const test = useMutation({
    mutationFn: (id: number) => api.targets.test(id),
    onSuccess: (res, id) => {
      setResults((prev) => ({ ...prev, [id]: res }));
      // Health may have changed; refresh the targets list so the badge updates.
      void qc.invalidateQueries({ queryKey: ["wizard-targets"] });
      void qc.invalidateQueries({ queryKey: ["targets"] });
    },
  });

  if (targets.length === 0) {
    return (
      <Muted>
        No targets defined. Add a target first, then create the spec.
      </Muted>
    );
  }

  return (
    <Stack>
      {targets.map((t) => {
        const active = t.id === selectedId;
        const testing = test.isPending && test.variables === t.id;
        const result = results[t.id];
        return (
          <Selectable key={t.id} $selected={active}>
            <Between>
              <button
                type="button"
                onClick={() => onSelect(t)}
              >
                <Inline>
                  <Strong>
                    {t.name}
                  </Strong>
                  <StatusBadge state={t.health_state} />
                  <Badge tone={t.env_tag === "prod" ? "amber" : "slate"}>
                    {t.env_tag}
                  </Badge>
                </Inline>
                <Mono>
                  {t.hec_url}
                </Mono>
              </button>
              <Button
                type="button"
                variant="secondary"
                disabled={testing}
                onClick={() => test.mutate(t.id)}
              >
                {testing ? "Testing…" : "Test"}
              </Button>
            </Between>
            {result && <TestResultLine result={result} />}
            {active && t.health_state === "red" && !result && (
              <Negative>
                This target last probed unhealthy; the control plane will reject a
                launch. Test it first.
              </Negative>
            )}
          </Selectable>
        );
      })}
    </Stack>
  );
}
