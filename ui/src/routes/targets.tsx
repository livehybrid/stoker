import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../lib/api";
import type { TargetOut, TargetTestResult } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Table, type Column } from "../components/Table";
import { Badge, StatusBadge } from "../components/Badge";
import { Button } from "../components/Button";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useToast } from "../components/Toast";
import { formatCap, formatGb } from "../features/targets/format";
import { NewTargetForm } from "../features/targets/NewTargetForm";
import { Inline, Mono, Muted, Stack, Strong } from "../components/text";

// Targets page: HEC destinations for load. Lists each target with health, cap
// and lifetime volume; a Test Connection probe per row; a create form (with a
// write-only token that is never displayed); and a guarded delete.

function Targets() {
  const toast = useToast();
  const qc = useQueryClient();

  const q = useQuery({
    queryKey: ["targets"],
    queryFn: () => api.targets.list(),
  });

  // Latest probe result per target id (from the on-demand Test Connection).
  const [testResults, setTestResults] = useState<
    Record<number, TargetTestResult>
  >({});
  // The target currently awaiting a delete confirmation (two-step guard).
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  // The target currently being edited (id), or null. Its form pre-fills from the
  // row; the HEC token starts blank (write-only) and capacity is editable.
  const [editing, setEditing] = useState<number | null>(null);
  const editingTarget = editing != null ? q.data?.find((t) => t.id === editing) : undefined;

  const test = useMutation({
    mutationFn: (id: number) => api.targets.test(id),
    onSuccess: (result, id) => {
      setTestResults((prev) => ({ ...prev, [id]: result }));
      if (result.ok) {
        toast.success(
          `Target reachable${
            result.latency_ms != null ? ` (${result.latency_ms} ms)` : ""
          }.`,
        );
      } else {
        toast.error(result.detail || "Target probe failed.");
      }
      // The probe updates health_state server-side; refresh the row badges.
      qc.invalidateQueries({ queryKey: ["targets"] });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof ApiError ? err.message : "Probe failed.");
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.targets.delete(id),
    onSuccess: (_data, id) => {
      toast.success("Target deleted.");
      setConfirmDelete((cur) => (cur === id ? null : cur));
      setTestResults((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      qc.invalidateQueries({ queryKey: ["targets"] });
    },
    onError: (err: unknown) => {
      // 409 when a spec references the target: surface the API's guidance.
      toast.error(
        err instanceof ApiError ? err.message : "Could not delete the target.",
      );
      setConfirmDelete(null);
    },
  });

  const columns: Column<TargetOut>[] = [
    {
      key: "name",
      header: "Name",
      cell: (t) => <Strong>{t.name}</Strong>,
    },
    {
      key: "hec_url",
      header: "HEC URL",
      cell: (t) => (
        <Mono $break>{t.hec_url}</Mono>
      ),
    },
    {
      key: "env",
      header: "Env",
      cell: (t) => <Badge tone="sky">{t.env_tag}</Badge>,
    },
    {
      key: "health",
      header: "Health",
      cell: (t) => {
        const probe = testResults[t.id];
        const detail = probe?.detail ?? t.health_detail;
        return (
          <Stack>
            <StatusBadge state={t.health_state} />
            {detail && (
              <Muted $small>{detail}</Muted>
            )}
          </Stack>
        );
      },
    },
    {
      key: "cap",
      header: "Concurrent cap",
      cell: (t) => (
        <span>{formatCap(t.max_concurrent_gb_day)}</span>
      ),
    },
    {
      key: "lifetime",
      header: "Lifetime",
      cell: (t) => (
        <span>{formatGb(t.lifetime_gb, "GB")}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (t) => {
        const testing = test.isPending && test.variables === t.id;
        const deleting = remove.isPending && remove.variables === t.id;
        const isConfirming = confirmDelete === t.id;
        return (
          <Inline>
            <Button
              variant="secondary"
              onClick={() => test.mutate(t.id)}
              disabled={testing}
            >
              {testing ? "Testing…" : "Test connection"}
            </Button>
            <Button variant="ghost" onClick={() => setEditing(t.id)}>
              Edit
            </Button>
            {isConfirming ? (
              <>
                <Button
                  variant="danger"
                  onClick={() => remove.mutate(t.id)}
                  disabled={deleting}
                >
                  {deleting ? "Deleting…" : "Confirm delete"}
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => setConfirmDelete(null)}
                  disabled={deleting}
                >
                  Cancel
                </Button>
              </>
            ) : (
              <Button variant="ghost" onClick={() => setConfirmDelete(t.id)}>
                Delete
              </Button>
            )}
          </Inline>
        );
      },
    },
  ];

  return (
    <Stack $gap="large">
      <PageHeader
        title="Targets"
        subtitle="HEC destinations for load. Tokens are write-only and never shown."
      />

      {editingTarget && (
        <Card title={`Edit target: ${editingTarget.name}`}>
          <NewTargetForm
            key={editingTarget.id}
            target={editingTarget}
            onDone={() => setEditing(null)}
          />
        </Card>
      )}

      <Card title="Registered targets">
        {q.isPending ? (
          <LoadingState />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => q.refetch()} />
        ) : (
          <Table
            columns={columns}
            rows={q.data}
            rowKey={(t) => t.id}
            empty={
              <EmptyState
                title="No targets yet"
                message="Register a HEC destination below to start sending load."
              />
            }
          />
        )}
      </Card>

      <Card title="New target">
        <NewTargetForm />
      </Card>
    </Stack>
  );
}

export const Route = createFileRoute("/targets")({
  component: Targets,
});
