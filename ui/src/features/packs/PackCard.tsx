import { useState } from "react";
import { Link } from "@tanstack/react-router";
import Switch from "@splunk/react-ui/Switch";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { formatBytes, formatGbDay, shortSha } from "../format";
import { packIsMetrics } from "../metrics/config";
import { Modal } from "../ui/Modal";
import {
  Between, Callout, Inline, Label, Mono, Muted, Panel, PanelFooter, Stack, Stat, StatRow,
  Strong, Tags, TitleBlock,
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
// a repo-indexed pack's lifecycle belongs to its repo, and a builtin pack
// re-registers at every boot, so no delete appears for those (the server
// refuses both anyway). When the pack source is writable the delete dialog
// offers to remove the bucket copy too: without that, the boot sync imports
// the pack straight back.
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
  const canDelete = isLocal && !pack.builtin;
  const [confirming, setConfirming] = useState(false);
  const [alsoRemote, setAlsoRemote] = useState(false);

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
    mutationFn: (remote: boolean) => api.packs.delete(pack.id, remote),
    onSuccess: (r) => {
      setConfirming(false);
      if (r) {
        toast.success(
          r.remote_existed
            ? `Pack "${pack.name}" deleted, and ${r.remote_key} removed from the pack source`
            : `Pack "${pack.name}" deleted (${r.remote_key} was not in the pack source)`,
        );
      } else {
        toast.success(`Pack "${pack.name}" deleted`);
      }
      qc.invalidateQueries({ queryKey: ["packs"] });
    },
    onError: (e) =>
      toast.error(e instanceof Error ? e.message : "Delete failed"),
  });

  function openDelete() {
    setAlsoRemote(false);
    setConfirming(true);
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
          {canDelete && (
            <Button
              variant="danger"
              onClick={openDelete}
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
      <Modal
        open={confirming}
        onClose={() => !del.isPending && setConfirming(false)}
        title={`Delete pack ${pack.name}`}
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => setConfirming(false)}
              disabled={del.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => del.mutate(canPush && alsoRemote)}
              disabled={del.isPending}
            >
              {del.isPending ? "Deleting…" : "Delete"}
            </Button>
          </>
        }
      >
        <Stack>
          <span>
            The pack is removed from this instance. An uploaded pack&apos;s extracted
            files go with it. This is refused while a spec uses the pack.
          </span>
          {canPush ? (
            <Switch
              appearance="checkbox"
              selected={alsoRemote}
              onClick={() => setAlsoRemote(!alsoRemote)}
            >
              Also delete its archive from <Mono>{source.data?.location}</Mono>
            </Switch>
          ) : (
            source.data?.configured && (
              <Muted $small>
                The pack source is read-only here, so a copy in it is left alone and
                will be imported again at the next restart.
              </Muted>
            )
          )}
          {canPush && !alsoRemote && (
            <Muted $small>
              Left in the pack source, the pack is imported again at the next restart.
            </Muted>
          )}
        </Stack>
      </Modal>
    </Panel>
  );
}
