import { useMemo, useState } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../lib/api";
import type { PackOut, SpecOut, TargetOut } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Table, type Column } from "../components/Table";
import { Button } from "../components/Button";
import { Badge, StatusBadge } from "../components/Badge";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { cn } from "../components/cn";
import { useToast } from "../components/Toast";
import { parseApiError } from "../features/specs/errors";
import { formatDuration, formatRate } from "../features/specs/format";

// ?highlight=<id> briefly emphasises a just-saved spec (set by the wizard on
// save). It is presentational only.
interface SpecsSearch {
  highlight?: number;
}

// Confirm dialog for the guarded delete (a spec with runs returns 409).
function ConfirmDelete({
  spec,
  onClose,
  onConfirm,
  busy,
}: {
  spec: SpecOut;
  onClose: () => void;
  onConfirm: () => void;
  busy: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-lg border border-surface-muted bg-surface-soft p-5 shadow-xl">
        <h2 className="text-sm font-semibold text-slate-100">Delete spec</h2>
        <p className="mt-2 text-sm text-slate-400">
          Delete <span className="text-slate-200">{spec.name}</span>? This cannot
          be undone. A spec that has runs cannot be deleted (the runs reference
          it).
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" onClick={onConfirm} disabled={busy}>
            {busy ? "Deleting…" : "Delete"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function Specs() {
  const navigate = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const { highlight } = Route.useSearch();

  const specsQ = useQuery({ queryKey: ["specs"], queryFn: () => api.specs.list() });
  const packsQ = useQuery({ queryKey: ["packs"], queryFn: () => api.packs.list() });
  const targetsQ = useQuery({
    queryKey: ["targets"],
    queryFn: () => api.targets.list(),
  });

  const packById = useMemo(() => {
    const m = new Map<number, PackOut>();
    packsQ.data?.forEach((p) => m.set(p.id, p));
    return m;
  }, [packsQ.data]);
  const targetById = useMemo(() => {
    const m = new Map<number, TargetOut>();
    targetsQ.data?.forEach((t) => m.set(t.id, t));
    return m;
  }, [targetsQ.data]);

  const [toDelete, setToDelete] = useState<SpecOut | null>(null);
  // The spec whose launch is in flight (disables its Run button + shows label).
  const [runningId, setRunningId] = useState<number | null>(null);

  const runM = useMutation({
    mutationFn: (id: number) => api.specs.run(id),
    onMutate: (id) => setRunningId(id),
    onSuccess: (created) => {
      toast.success(`Run #${created.run_id} launched.`);
      navigate({ to: "/runs/$runId", params: { runId: String(created.run_id) } });
    },
    onError: (err) => {
      const parsed = parseApiError(err);
      // Surface the actionable rejections with the concrete next step.
      const suffix =
        parsed.kind === "slice_exceeds_ceiling" && parsed.suggestedWorkers
          ? " Edit the spec to raise workers."
          : parsed.kind === "replay_single_worker"
            ? " Edit the spec and set workers to 1."
            : parsed.kind === "target_unhealthy"
              ? " Test the target from the wizard first."
              : "";
      toast.error(parsed.message + suffix);
    },
    onSettled: () => setRunningId(null),
  });

  const deleteM = useMutation({
    mutationFn: (id: number) => api.specs.delete(id),
    onSuccess: () => {
      toast.success("Spec deleted.");
      setToDelete(null);
      void qc.invalidateQueries({ queryKey: ["specs"] });
    },
    onError: (err) => {
      const parsed = parseApiError(err);
      toast.error(parsed.message);
    },
  });

  const columns: Column<SpecOut>[] = [
    {
      key: "name",
      header: "Name",
      cell: (s) => (
        <div className="flex items-center gap-2">
          <span className="font-medium text-slate-100">{s.name}</span>
          {highlight === s.id && <Badge tone="sky">saved</Badge>}
        </div>
      ),
    },
    {
      key: "pack",
      header: "Pack",
      cell: (s) => {
        const p = packById.get(s.pack_id);
        return p ? (
          <span className="text-slate-300">{p.name}</span>
        ) : (
          <span className="text-slate-500">#{s.pack_id}</span>
        );
      },
    },
    {
      key: "target",
      header: "Target",
      cell: (s) => {
        const t = targetById.get(s.target_id);
        return t ? (
          <span className="flex items-center gap-1.5">
            <span className="text-slate-300">{t.name}</span>
            <StatusBadge state={t.health_state} />
          </span>
        ) : (
          <span className="text-slate-500">#{s.target_id}</span>
        );
      },
    },
    {
      key: "rate",
      header: "Rate",
      cell: (s) => (
        <span className="text-slate-300">
          {formatRate(s.rate_mode, s.rate_value, s.interval_s)}
        </span>
      ),
    },
    { key: "workers", header: "Workers", cell: (s) => s.workers },
    {
      key: "duration",
      header: "Duration",
      cell: (s) => (
        <span className="text-slate-400">{formatDuration(s.duration_s)}</span>
      ),
    },
    { key: "fleet", header: "Fleet", cell: (s) => s.fleet },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (s) => (
        <div className="flex justify-end gap-1.5">
          <Button
            variant="primary"
            disabled={runM.isPending && runningId === s.id}
            onClick={() => runM.mutate(s.id)}
          >
            {runM.isPending && runningId === s.id ? "Launching…" : "Run"}
          </Button>
          <Button
            variant="secondary"
            onClick={() =>
              navigate({ to: "/specs/new", search: { edit: s.id } })
            }
          >
            Edit
          </Button>
          <Button
            variant="ghost"
            onClick={() =>
              navigate({ to: "/specs/new", search: { clone: s.id } })
            }
          >
            Clone
          </Button>
          <Button variant="danger" onClick={() => setToDelete(s)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const loading = specsQ.isPending || packsQ.isPending || targetsQ.isPending;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Specs"
        subtitle="Saved job specifications: launch, edit, clone or delete."
        actions={
          <Button
            variant="primary"
            onClick={() => navigate({ to: "/specs/new" })}
          >
            New spec
          </Button>
        }
      />
      <Card
        className={cn(
          highlight != null && "ring-1 ring-sky-700/50 transition-shadow",
        )}
      >
        {loading ? (
          <LoadingState />
        ) : specsQ.isError ? (
          <ErrorState error={specsQ.error} onRetry={() => specsQ.refetch()} />
        ) : (
          <Table
            columns={columns}
            rows={specsQ.data ?? []}
            rowKey={(s) => s.id}
            empty={
              <EmptyState
                title="No specs yet"
                message="Create a spec to define a load-generation job."
                action={
                  <Button
                    variant="primary"
                    onClick={() => navigate({ to: "/specs/new" })}
                  >
                    New spec
                  </Button>
                }
              />
            }
          />
        )}
      </Card>

      {toDelete && (
        <ConfirmDelete
          spec={toDelete}
          busy={deleteM.isPending}
          onClose={() => setToDelete(null)}
          onConfirm={() => deleteM.mutate(toDelete.id)}
        />
      )}
    </div>
  );
}

export const Route = createFileRoute("/specs")({
  validateSearch: (search: Record<string, unknown>): SpecsSearch => {
    const out: SpecsSearch = {};
    const highlight = Number(search.highlight);
    if (search.highlight != null && Number.isFinite(highlight))
      out.highlight = highlight;
    return out;
  },
  component: Specs,
});
