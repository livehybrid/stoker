import { Link } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { RepoOut, RepoSyncResult } from "../../lib/types";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { absoluteTime, relativeTime, shortSha } from "../format";
import { Between, Callout, Grid, Inline, Mono, Muted, Panel, Strong } from "../../components/text";

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
    <Panel>
      <Between>
        <div>
          <Inline>
            <Mono title={repo.url}>
              {repo.url}
            </Mono>
            {repo.trusted_code ? (
              <Badge tone="amber">trusted code</Badge>
            ) : (
              <Badge tone="slate">untrusted</Badge>
            )}
          </Inline>
          <Muted $small>
            ref <span>{repo.default_ref}</span>
            {" · "}auth <span>{authLabel(repo.auth_kind)}</span>
            {repo.has_secret ? " (credential set)" : ""}
          </Muted>
        </div>
        <Inline>
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
        </Inline>
      </Between>

      <Grid $min="160px">
        <div>
          <Muted>Head SHA</Muted>
          <Mono>{shortSha(repo.head_sha)}</Mono>
        </div>
        <div>
          <Muted>Last synced</Muted>
          <Strong title={absoluteTime(repo.last_synced_at)}>
            {relativeTime(repo.last_synced_at)}
          </Strong>
        </div>
        <div>
          <Muted>Packs</Muted>
          <div>
            <Link
              to="/packs"
              search={{ repo: repo.id }}
            >
              View indexed packs →
            </Link>
          </div>
        </div>
      </Grid>

      {repo.sync_error && (
        <Callout $tone="error">
          <Strong>Last sync failed:</Strong> {repo.sync_error}
        </Callout>
      )}
    </Panel>
  );
}
