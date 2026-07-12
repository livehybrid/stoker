import { Link } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { RepoOut, RepoSyncResult } from "../../lib/types";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { absoluteTime, relativeTime, shortSha } from "../format";

// A repo card: URL, auth kind, head SHA, last-synced, trusted-code badge, and
// any sync error, plus the per-repo Sync now + Delete actions and a link to its
// indexed packs. Section 10.4: "Repo cards with sync state and head SHA".
interface Props {
  repo: RepoOut;
  onDeleted: () => void;
}

function authLabel(kind: string): string {
  if (kind === "pat") return "PAT";
  if (kind === "deploy_key") return "deploy key";
  return "none";
}

export function RepoCard({ repo, onDeleted }: Props) {
  const qc = useQueryClient();
  const toast = useToast();

  const sync = useMutation({
    mutationFn: () => api.repos.sync(repo.id),
    onSuccess: (r: RepoSyncResult) => {
      const parts = [
        r.head_sha ? `head ${shortSha(r.head_sha)}` : "no head",
        `${r.packs_indexed} pack${r.packs_indexed === 1 ? "" : "s"} indexed`,
      ];
      if (r.lint_failures > 0) {
        parts.push(`${r.lint_failures} lint failure${r.lint_failures === 1 ? "" : "s"}`);
      }
      toast.success(`Synced: ${parts.join(", ")}`);
      qc.invalidateQueries({ queryKey: ["repos"] });
      qc.invalidateQueries({ queryKey: ["packs"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Sync failed"),
  });

  const del = useMutation({
    mutationFn: () => api.repos.delete(repo.id),
    onSuccess: () => {
      toast.success("Repository deleted");
      qc.invalidateQueries({ queryKey: ["repos"] });
      qc.invalidateQueries({ queryKey: ["packs"] });
      onDeleted();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Delete failed"),
  });

  function confirmDelete() {
    if (
      window.confirm(
        `Delete repository ${repo.url}? Its indexed packs are removed too (refused if any pack is used by a spec).`,
      )
    ) {
      del.mutate();
    }
  }

  return (
    <section className="rounded-lg border border-surface-muted bg-surface-soft p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="truncate font-mono text-sm text-slate-100" title={repo.url}>
              {repo.url}
            </h3>
            {repo.trusted_code ? (
              <Badge tone="amber">trusted code</Badge>
            ) : (
              <Badge tone="slate">untrusted</Badge>
            )}
          </div>
          <p className="mt-1 text-xs text-slate-500">
            ref <span className="text-slate-300">{repo.default_ref}</span>
            {" · "}auth <span className="text-slate-300">{authLabel(repo.auth_kind)}</span>
            {repo.has_secret ? " (credential set)" : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => sync.mutate()}
            disabled={sync.isPending}
          >
            {sync.isPending ? "Syncing…" : "Sync now"}
          </Button>
          <Button variant="danger" onClick={confirmDelete} disabled={del.isPending}>
            Delete
          </Button>
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-slate-500">Head SHA</dt>
          <dd className="mt-0.5 font-mono text-slate-200">{shortSha(repo.head_sha)}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Last synced</dt>
          <dd className="mt-0.5 text-slate-200" title={absoluteTime(repo.last_synced_at)}>
            {relativeTime(repo.last_synced_at)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Packs</dt>
          <dd className="mt-0.5">
            <Link
              to="/packs"
              search={{ repo: repo.id }}
              className="text-sky-400 hover:text-sky-300"
            >
              View indexed packs →
            </Link>
          </dd>
        </div>
      </dl>

      {repo.sync_error && (
        <p className="mt-3 rounded-md border border-red-800/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
          <span className="font-medium">Last sync failed:</span> {repo.sync_error}
        </p>
      )}
    </section>
  );
}
