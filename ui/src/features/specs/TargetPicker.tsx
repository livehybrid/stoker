// Target picker panel for the job wizard: a selectable list of HEC targets with
// health badge and env tag, plus a Test Connection button (POST /targets/{id}/
// test) that probes health + auth and refreshes the stored health_state.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { TargetOut, TargetTestResult } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { cn } from "../../components/cn";

function TestResultLine({ result }: { result: TargetTestResult }) {
  return (
    <div
      className={cn(
        "mt-2 rounded-md border px-3 py-2 text-xs",
        result.ok
          ? "border-emerald-800/60 bg-emerald-950/40 text-emerald-200"
          : "border-red-800/60 bg-red-950/40 text-red-200",
      )}
    >
      <span className="font-medium">{result.ok ? "Reachable" : "Problem"}</span>
      {" · "}
      health {result.health ?? "?"} · auth {result.auth ?? "?"}
      {result.latency_ms != null ? ` · ${result.latency_ms} ms` : ""}
      {result.detail ? <div className="mt-1 text-red-300/90">{result.detail}</div> : null}
    </div>
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
      <p className="text-sm text-slate-500">
        No targets defined. Add a target first, then create the spec.
      </p>
    );
  }

  return (
    <div className="space-y-1">
      {targets.map((t) => {
        const active = t.id === selectedId;
        const testing = test.isPending && test.variables === t.id;
        const result = results[t.id];
        return (
          <div
            key={t.id}
            className={cn(
              "rounded-md border px-3 py-2 transition-colors",
              active
                ? "border-sky-600 bg-sky-950/40"
                : "border-surface-muted bg-surface",
            )}
          >
            <div className="flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => onSelect(t)}
                className="min-w-0 flex-1 text-left"
              >
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-slate-100">
                    {t.name}
                  </span>
                  <StatusBadge state={t.health_state} />
                  <Badge tone={t.env_tag === "prod" ? "amber" : "slate"}>
                    {t.env_tag}
                  </Badge>
                </div>
                <p className="mt-0.5 truncate font-mono text-xs text-slate-500">
                  {t.hec_url}
                </p>
              </button>
              <Button
                type="button"
                variant="secondary"
                disabled={testing}
                onClick={() => test.mutate(t.id)}
              >
                {testing ? "Testing…" : "Test"}
              </Button>
            </div>
            {result && <TestResultLine result={result} />}
            {active && t.health_state === "red" && !result && (
              <p className="mt-2 text-xs text-red-300">
                This target last probed unhealthy; the control plane will reject a
                launch. Test it first.
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
