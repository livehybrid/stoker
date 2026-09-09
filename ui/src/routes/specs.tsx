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
import { useToast } from "../components/Toast";
import { parseApiError } from "../features/specs/errors";
import { formatDuration, formatRate } from "../features/specs/format";
import { EndRow, Inline, Muted, Stack, Strong } from "../components/text";
import { Modal } from "../features/ui/Modal";
import styled from "styled-components";
import { variables } from "@splunk/themes";

const Highlightable = styled.div<{ $on?: boolean }>`
  border-radius: ${variables.borderRadius};
  box-shadow: ${(props) => (props.$on ? variables.focusShadow : "none")};
  transition: box-shadow 200ms;
`;

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
    // Splunk's Modal, via the shared wrapper: focus trapping, Escape, and a
    // refusal to close on a click away, which is what a destructive
    // confirmation wants.
    <Modal open onClose={onClose} title="Delete spec" width="480px"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" onClick={onConfirm} disabled={busy}>
            {busy ? "Deleting…" : "Delete"}
          </Button>
        </>
      }
    >
      <Muted>
        Delete <Strong>{spec.name}</Strong>? This cannot be undone. A spec that
        has runs cannot be deleted (the runs reference it).
      </Muted>
    </Modal>
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
        <Inline>
          <Strong>{s.name}</Strong>
          {highlight === s.id && <Badge tone="sky">saved</Badge>}
        </Inline>
      ),
    },
    {
      key: "pack",
      header: "Pack",
      cell: (s) => {
        const p = packById.get(s.pack_id);
        return p ? (
          <span>{p.name}</span>
        ) : (
          <Muted>#{s.pack_id}</Muted>
        );
      },
    },
    {
      key: "target",
      header: "Target",
      cell: (s) => {
        const t = targetById.get(s.target_id);
        return t ? (
          <Inline>
            <span>{t.name}</span>
            <StatusBadge state={t.health_state} />
          </Inline>
        ) : (
          <Muted>#{s.target_id}</Muted>
        );
      },
    },
    {
      key: "rate",
      header: "Rate",
      cell: (s) => (
        <span>
          {formatRate(s.rate_mode, s.rate_value, s.interval_s)}
        </span>
      ),
    },
    { key: "workers", header: "Workers", cell: (s) => s.workers },
    {
      key: "duration",
      header: "Duration",
      cell: (s) => (
        <Muted>{formatDuration(s.duration_s)}</Muted>
      ),
    },
    { key: "fleet", header: "Fleet", cell: (s) => s.fleet },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (s) => (
        <EndRow>
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
        </EndRow>
      ),
    },
  ];

  const loading = specsQ.isPending || packsQ.isPending || targetsQ.isPending;

  return (
    <Stack $gap="large">
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
      {/* The card is highlighted briefly when arriving from a just-created
          spec, so the row you are looking for announces itself. */}
      <Highlightable $on={highlight != null}>
        <Card>
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
      </Highlightable>

      {toDelete && (
        <ConfirmDelete
          spec={toDelete}
          busy={deleteM.isPending}
          onClose={() => setToDelete(null)}
          onConfirm={() => deleteM.mutate(toDelete.id)}
        />
      )}
    </Stack>
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
