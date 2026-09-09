import { Link } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { formatBytes, formatGbDay, shortSha } from "../format";
import { packIsMetrics } from "../metrics/config";
import { Between, Callout, Grid, Inline, Muted, Panel, Strong, Tags } from "../../components/text";

// A pack card: lint + verified badges, sourcetypes, estimated bytes/event and
// declared GB/day, with Preview and "New job from pack" (design section 10.4).
// Local packs (uploaded / registered / built here — no repo) also get Delete;
// a repo-indexed pack's lifecycle belongs to its repo, so no delete appears
// for those (the server refuses it anyway).
interface Props {
  pack: PackOut;
  onPreview: (pack: PackOut) => void;
}

function asStringList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => String(x)).filter(Boolean);
}

export function PackCard({ pack, onPreview }: Props) {
  const qc = useQueryClient();
  const toast = useToast();
  const sourcetypes = asStringList(pack.sourcetypes_json);
  const engines = asStringList(pack.engines_json);
  const tags = asStringList(pack.tags_json);
  const isMetric = packIsMetrics(pack);
  const isLocal = pack.repo_id === null || pack.repo_id === undefined;

  const del = useMutation({
    mutationFn: () => api.packs.delete(pack.id),
    onSuccess: () => {
      toast.success(`Pack "${pack.name}" deleted`);
      qc.invalidateQueries({ queryKey: ["packs"] });
    },
    onError: (e) =>
      toast.error(e instanceof Error ? e.message : "Delete failed"),
  });

  function confirmDelete() {
    if (
      window.confirm(
        `Delete pack ${pack.name}? An uploaded pack's extracted files are removed too (refused if a spec uses it).`,
      )
    ) {
      del.mutate();
    }
  }

  return (
    <Panel>
      <Between>
        <div>
          <Strong title={pack.name}>
            {pack.name}
          </Strong>
          {pack.description && (
            <Muted $small>
              {pack.description}
            </Muted>
          )}
        </div>
        <Tags>
          {/* Only surface lint state when it is a problem; a clean pack shows the
              "verified" badge, so an extra "ok" pill was just noise. */}
          {pack.lint_status !== "ok" && <StatusBadge state={pack.lint_status} />}
          {pack.verified ? (
            <Badge tone="green">verified</Badge>
          ) : (
            <Badge tone="slate">unverified</Badge>
          )}
        </Tags>
      </Between>

      {(sourcetypes.length > 0 || engines.length > 0 || tags.length > 0) && (
        <Tags>
          {engines.map((e) => (
            <Badge key={`e-${e}`} tone="sky">
              {e}
            </Badge>
          ))}
          {sourcetypes.map((s) => (
            <Badge key={`s-${s}`} tone="neutral">
              {s}
            </Badge>
          ))}
          {tags.map((t) => (
            <Badge key={`t-${t}`} tone="amber">
              {t}
            </Badge>
          ))}
        </Tags>
      )}

      <Grid $min="140px">
        <div>
          <Muted>Stanzas</Muted>
          <Strong>{pack.stanza_count ?? "—"}</Strong>
        </div>
        <div>
          <Muted>Bytes/event</Muted>
          <Strong>
            {formatBytes(pack.est_bytes_per_event)}
          </Strong>
        </div>
        <div>
          <Muted>Declared</Muted>
          <Strong>
            {formatGbDay(pack.declared_per_day_gb)}
          </Strong>
        </div>
      </Grid>

      {pack.lint_status !== "ok" &&
        Array.isArray(pack.lint_errors_json) &&
        pack.lint_errors_json.length > 0 && (
          <Callout $tone="error">
            {String(pack.lint_errors_json[0])}
            {pack.lint_errors_json.length > 1
              ? ` (+${pack.lint_errors_json.length - 1} more)`
              : ""}
          </Callout>
        )}

      <Between>
        <Muted $small>
          {pack.indexed_sha ? `indexed ${shortSha(pack.indexed_sha)}` : "local pack"}
        </Muted>
        <Inline>
          {isLocal && (
            <Button
              variant="danger"
              onClick={confirmDelete}
              disabled={del.isPending}
            >
              Delete
            </Button>
          )}
          {isMetric ? (
            // A metric pack has no eventgen stanzas to preview; edit it in the
            // builder instead.
            <Link to="/metric-packs/new" search={{ edit: pack.id }}>
              <Button variant="secondary">Edit</Button>
            </Link>
          ) : (
            <Button variant="secondary" onClick={() => onPreview(pack)}>
              Preview
            </Button>
          )}
          {/* Pre-selects this pack in the wizard via its ?pack=<id> search
              param (validated by src/routes/specs.new.tsx). */}
          <Link to="/specs/new" search={{ pack: pack.id }}>
            <Button variant="primary">New job</Button>
          </Link>
        </Inline>
      </Between>
    </Panel>
  );
}
