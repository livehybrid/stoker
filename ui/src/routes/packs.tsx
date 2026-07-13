import { useMemo, useState } from "react";
import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import type { PackOut } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { Field, Select } from "../components/Field";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { PackCard } from "../features/packs/PackCard";
import { PackPreviewDrawer } from "../features/packs/PackPreviewDrawer";

// Packs page: a filterable grid of indexed sample packs (filter by repo, wired
// to the URL so a "View indexed packs" link from a repo card deep-links here),
// each pack shown with lint/verified badges, sourcetypes and size estimates,
// a preview drawer and a "New job from pack" jump (design section 10.4).

interface PacksSearch {
  repo?: number;
}

function Packs() {
  const navigate = useNavigate({ from: Route.fullPath });
  const { repo } = Route.useSearch();
  const [preview, setPreview] = useState<PackOut | null>(null);

  const packsQ = useQuery({
    queryKey: ["packs", repo ?? null],
    queryFn: () => api.packs.list(repo),
  });
  // Repos populate the filter dropdown (and let us label a pack's origin).
  const reposQ = useQuery({ queryKey: ["repos"], queryFn: () => api.repos.list() });

  const repoOptions = useMemo(() => reposQ.data ?? [], [reposQ.data]);

  function setRepoFilter(value: string) {
    const next = value === "" ? undefined : Number(value);
    navigate({ search: (prev) => ({ ...prev, repo: next }) });
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Packs"
        subtitle="Sample packs to launch jobs from: indexed eventgen packs and metric packs you build here."
        actions={
          <Link to="/metric-packs/new">
            <Button variant="primary">+ New metric pack</Button>
          </Link>
        }
      />

      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-72 max-w-full">
            <Field label="Filter by repo">
              <Select
                value={repo === undefined ? "" : String(repo)}
                onChange={(e) => setRepoFilter(e.target.value)}
              >
                <option value="">All repos</option>
                {repoOptions.map((r) => (
                  <option key={r.id} value={String(r.id)}>
                    {r.url}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          {packsQ.data && (
            <p className="pb-2 text-xs text-slate-500">
              {packsQ.data.length} pack{packsQ.data.length === 1 ? "" : "s"}
              {repo !== undefined ? " in this repo" : " total"}
            </p>
          )}
        </div>
      </Card>

      {packsQ.isPending ? (
        <LoadingState />
      ) : packsQ.isError ? (
        <ErrorState error={packsQ.error} onRetry={() => packsQ.refetch()} />
      ) : packsQ.data.length === 0 ? (
        <EmptyState
          title="No packs indexed"
          message={
            repo !== undefined
              ? "This repo has no indexed packs yet. Sync it from the Repos page."
              : "Register a repo and sync it to index its sample packs."
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {packsQ.data.map((pack) => (
            <PackCard key={pack.id} pack={pack} onPreview={setPreview} />
          ))}
        </div>
      )}

      <PackPreviewDrawer pack={preview} onClose={() => setPreview(null)} />
    </div>
  );
}

export const Route = createFileRoute("/packs")({
  validateSearch: (search: Record<string, unknown>): PacksSearch => {
    const raw = search.repo;
    const n = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(n) && n > 0 ? { repo: n } : {};
  },
  component: Packs,
});
