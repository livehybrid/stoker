import { Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { formatBytes, formatGbDay, shortSha } from "../format";
import { packIsMetrics } from "../metrics/config";
import {
  Between, Callout, Inline, Label, Muted, Panel, PanelFooter, Stat, StatRow, Strong,
  Tags, TitleBlock,
} from "../../components/text";

// A pack card: what the pack is, what it emits and how big its events are,
// with Download, Preview and "New job from pack" (design section 10.4).
// Badges flag exceptions only -- a failed lint, or metadata Stoker measured
// rather than read from an author's pack.yaml -- because a badge that appears
// on every card carries no information. Download exports the pack as the archive another instance's
// "Upload pack" accepts, so a pack built here can move between instances; Push
// sends that same archive to the configured pack source (s3:// or a directory),
// and appears only when one is configured for writing.
// Local packs (uploaded / registered / built here — no repo) also get Delete;
// a repo-indexed pack's lifecycle belongs to its repo, so no delete appears
// for those (the server refuses it anyway).
interface Props {
  pack: PackOut;
  onPreview: (pack: PackOut) => void;
}

// The card stacks five sections; without a gap they read as one block.
const SPACING = "12px";

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

  // Whether pushing is possible at all. Deployment environment variables, so it
  // cannot change without a restart: fetched once and never refetched, and a
  // failure simply hides the button rather than breaking the card.
  const source = useQuery({
    queryKey: ["pack-source"],
    queryFn: api.packSource,
    staleTime: Infinity,
    retry: false,
  });
  const canPush = source.data?.writable === true;

  const push = useMutation({
    mutationFn: () => api.packs.publish(pack.id),
    onSuccess: (r) =>
      toast.success(
        r.key ? `Pushed "${pack.name}" as ${r.key}` : `Pushed "${pack.name}"`,
      ),
    onError: (e) => toast.error(e instanceof Error ? e.message : "Push failed"),
  });

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
    <Panel $gap={SPACING}>
      <Between>
        <TitleBlock>
          <Strong title={pack.name}>{pack.name}</Strong>
          {pack.description && <Muted $small>{pack.description}</Muted>}
        </TitleBlock>
        <Tags>
          {/* Two badges that were on nearly every card said nothing. What is
              worth flagging is the exception: a pack that failed lint, and one
              whose metadata Stoker guessed rather than read from an author's
              pack.yaml, because then the estimates below are guesses too. */}
          {pack.lint_status !== "ok" && <StatusBadge state={pack.lint_status} />}
          {!pack.verified && (
            <Badge tone="amber">
              <span title="No author pack.yaml: the figures below are measured by Stoker, not declared by the pack">
                estimated
              </span>
            </Badge>
          )}
        </Tags>
      </Between>

      {(engines.length > 0 || sourcetypes.length > 0 || tags.length > 0) && (
        <Stat>
          <Tags>
            {/* One colour family, ordered by what you actually scan for: the
                engine decides how the pack behaves, the sourcetype is what you
                search by, and free tags are last. Three competing colours just
                made the card loud without saying which was which. */}
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
          </Tags>
          {tags.length > 0 && (
            <Muted $small title="Pack tags">
              {tags.join(" · ")}
            </Muted>
          )}
        </Stat>
      )}

      <StatRow>
        <Stat>
          <Label>Stanzas</Label>
          <Strong>{pack.stanza_count ?? "—"}</Strong>
        </Stat>
        <Stat>
          <Label>Bytes/event</Label>
          <Strong>{formatBytes(pack.est_bytes_per_event)}</Strong>
        </Stat>
        <Stat>
          <Label>Declared</Label>
          <Strong>{formatGbDay(pack.declared_per_day_gb)}</Strong>
        </Stat>
      </StatRow>

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

      <PanelFooter>
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
          {/* Plain download link (not a fetch): the browser streams the
              archive to disk and sends the session cookie itself. The file is
              what Packs > Upload pack accepts on another instance. */}
          <a
            href={api.packs.exportUrl(pack.id)}
            download
            title="Download as .tar.gz to upload into another Stoker instance"
          >
            <Button variant="secondary">Download</Button>
          </a>
          {/* Saving a pack already mirrors it, but nothing said so and a pack
              that predates the source being configured was never mirrored at
              all. Shown only when a source is configured for writing, so it
              never appears as a button that cannot work. */}
          {canPush && (
            <Button
              variant="secondary"
              onClick={() => push.mutate()}
              disabled={push.isPending}
              title={`Push this pack to ${source.data?.location ?? "the pack source"}`}
            >
              {push.isPending ? "Pushing…" : "Push"}
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
          {!isMetric && isLocal && (pack.tags_json ?? []).includes("pack-builder") && (
            <Link to="/pack-builder" search={{ edit: pack.id }}>
              <Button variant="secondary">Edit in builder</Button>
            </Link>
          )}
          {/* Pre-selects this pack in the wizard via its ?pack=<id> search
              param (validated by src/routes/specs.new.tsx). */}
          <Link to="/specs/new" search={{ pack: pack.id }}>
            <Button variant="primary">New job</Button>
          </Link>
        </Inline>
      </PanelFooter>
    </Panel>
  );
}
