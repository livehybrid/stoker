import { useMemo, useState } from "react";
import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import type { PackOut } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { Field, Select, TextInput } from "../components/Field";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useToast } from "../components/Toast";
import { Modal } from "../features/ui/Modal";
import { PackCard } from "../features/packs/PackCard";
import { PackPreviewDrawer } from "../features/packs/PackPreviewDrawer";
import { UploadPackForm } from "../features/packs/UploadPackForm";
import { Grid, Inline, Muted, Stack } from "../components/text";

// Packs page: a filterable grid of indexed sample packs (filter by repo, wired
// to the URL so a "View indexed packs" link from a repo card deep-links here),
// each pack shown with lint/verified badges, sourcetypes and size estimates,
// a preview drawer, a "New job from pack" jump, an archive upload for the
// no-git path, and delete for local packs (design section 10.4).

interface PacksSearch {
  repo?: number;
}

function Packs() {
  const navigate = useNavigate({ from: Route.fullPath });
  const { repo } = Route.useSearch();
  const toast = useToast();
  const [preview, setPreview] = useState<PackOut | null>(null);
  const [search, setSearch] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);

  const packsQ = useQuery({
    queryKey: ["packs", repo ?? null],
    queryFn: () => api.packs.list(repo),
  });
  // Repos populate the filter dropdown (and let us label a pack's origin).
  const reposQ = useQuery({ queryKey: ["repos"], queryFn: () => api.repos.list() });

  const repoOptions = useMemo(() => reposQ.data ?? [], [reposQ.data]);

  // Free-text search across name, description, tags, sourcetypes and engines.
  const filtered = useMemo(() => {
    const list = packsQ.data ?? [];
    const q = search.trim().toLowerCase();
    if (!q) return list;
    return list.filter((p) => {
      const hay = [
        p.name,
        p.description ?? "",
        ...(Array.isArray(p.tags_json) ? p.tags_json.map(String) : []),
        ...(Array.isArray(p.sourcetypes_json) ? p.sourcetypes_json.map(String) : []),
        ...(Array.isArray(p.engines_json) ? p.engines_json.map(String) : []),
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [packsQ.data, search]);

  function setRepoFilter(value: string) {
    const next = value === "" ? undefined : Number(value);
    navigate({ search: (prev) => ({ ...prev, repo: next }) });
  }

  return (
    <Stack $gap="large">
      <PageHeader
        title="Packs"
        subtitle="Sample packs to launch jobs from: indexed eventgen packs and metric packs you build here."
        actions={
          <Inline>
            <Button variant="secondary" onClick={() => setUploadOpen(true)}>
              Upload pack
            </Button>
            <Link to="/metric-packs/new">
              <Button variant="primary">+ New metric pack</Button>
            </Link>
          </Inline>
        }
      />

      <Card>
        <Inline>
          <div>
            <Field label="Search" hint="name, description, tags, sourcetype or engine">
              <TextInput
                value={search}
                onChange={(_e, { value }) => setSearch(value)}
                placeholder="e.g. aws, cloudtrail, metrics, web…"
                autoComplete="off"
              />
            </Field>
          </div>
          <div>
            <Field label="Filter by repo">
              <Select
                value={repo === undefined ? "" : String(repo)}
                onChange={(_e, { value }) => setRepoFilter(String(value))}
              >
                <Select.Option value="" label="All repos" />
                {repoOptions.map((r) => (
                  <Select.Option key={r.id} value={String(r.id)} label={r.url} />
                ))}
              </Select>
            </Field>
          </div>
          {packsQ.data && (
            <Muted $small>
              {filtered.length} of {packsQ.data.length} pack
              {packsQ.data.length === 1 ? "" : "s"}
              {repo !== undefined ? " in this repo" : ""}
            </Muted>
          )}
        </Inline>
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
              : "Register a repo and sync it, or build a metric pack, to get started."
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No packs match your search"
          message={`Nothing matches "${search.trim()}". Clear the search to see all packs.`}
        />
      ) : (
        <Grid $min="260px">
          {filtered.map((pack) => (
            <PackCard key={pack.id} pack={pack} onPreview={setPreview} />
          ))}
        </Grid>
      )}

      <Modal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        title="Upload pack"
      >
        <UploadPackForm
          onCancel={() => setUploadOpen(false)}
          onUploaded={(pack) => {
            setUploadOpen(false);
            toast.success(`Pack "${pack.name}" uploaded and verified`);
          }}
        />
      </Modal>

      <PackPreviewDrawer pack={preview} onClose={() => setPreview(null)} />
    </Stack>
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
