import { useEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { ErrorState, LoadingState } from "../../components/States";
import { Drawer } from "../ui/Drawer";
import { Between, Bullets, Callout, Inline, Mono, Muted, Panel, Pre, Stack, Strong } from "../../components/text";

// How many events the "Preview events" render requests (the server clamps to a
// sane max regardless).
const PREVIEW_EVENT_COUNT = 10;

// Preview drawer for a pack: GET /packs/{id}/preview returns each stanza plus
// the first ~10 sample lines. Fetched lazily (only when a pack is selected).
// Also offers a "Preview events" render (GET /packs/{id}/preview_run: a
// lightweight in-process render of a few events, no fleet / no HEC) and the
// "New job from this pack" jump into the wizard.
interface Props {
  pack: PackOut | null;
  onClose: () => void;
}

export function PackPreviewDrawer({ pack, onClose }: Props) {
  const open = pack !== null;
  const q = useQuery({
    queryKey: ["pack-preview", pack?.id],
    queryFn: () => api.packs.preview(pack!.id),
    enabled: open,
  });

  // "Preview events" is opt-in (a button), so the render only runs when asked.
  const [showEvents, setShowEvents] = useState(false);
  // Reset the render toggle whenever the selected pack changes so a freshly
  // opened drawer never shows the previous pack's events.
  useEffect(() => {
    setShowEvents(false);
  }, [pack?.id]);
  const eventsQ = useQuery({
    queryKey: ["pack-preview-run", pack?.id],
    queryFn: () => api.packs.previewRun(pack!.id, PREVIEW_EVENT_COUNT),
    enabled: open && showEvents,
  });

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={pack?.name ?? "Pack preview"}
      subtitle={pack?.source_path}
      actions={
        pack && (
          <Link to="/specs/new" search={{ pack: pack.id }}>
            <Button variant="primary">New job from this pack</Button>
          </Link>
        )
      }
    >
      {!pack ? null : q.isPending ? (
        <LoadingState label="Loading preview…" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : (
        <Stack $gap="medium">
          <Inline>
            <StatusBadge state={q.data.lint_status} />
            {pack.verified ? (
              <Badge tone="green">verified</Badge>
            ) : (
              <Badge tone="slate">unverified</Badge>
            )}
            <Badge tone="neutral">
              {q.data.stanzas.length} stanza{q.data.stanzas.length === 1 ? "" : "s"}
            </Badge>
          </Inline>

          {/* Rendered-events preview: a lightweight in-process render (tokens
              like timestamp / ipv4 / integer substituted), no fleet or HEC.
              Opt-in so it only runs when the author asks. */}
          <Panel>
            <Between>
              <div>
                <Strong>
                  Rendered events
                </Strong>
                <Muted $small>
                  A few sample events with tokens applied. No fleet, no HEC.
                </Muted>
              </div>
              <Button
                variant="secondary"
                onClick={() =>
                  showEvents ? eventsQ.refetch() : setShowEvents(true)
                }
                disabled={showEvents && eventsQ.isFetching}
              >
                {showEvents && eventsQ.isFetching
                  ? "Rendering…"
                  : showEvents
                    ? "Re-render"
                    : "Preview events"}
              </Button>
            </Between>
            {showEvents &&
              (eventsQ.isPending ? (
                <LoadingState label="Rendering events…" />
              ) : eventsQ.isError ? (
                <ErrorState error={eventsQ.error} onRetry={() => eventsQ.refetch()} />
              ) : eventsQ.data.events.length === 0 ? (
                <Muted $small>
                  No events could be rendered (no sample-mode stanza or no
                  readable sample file).
                </Muted>
              ) : (
                <Pre>
                  {eventsQ.data.events.join("\n")}
                </Pre>
              ))}
          </Panel>

          {q.data.lint_errors.length > 0 && (
            <Callout $tone="error">
              <Strong>Lint errors</Strong>
              <Bullets>
                {q.data.lint_errors.map((err, i) => (
                  <li key={i}>{err}</li>
                ))}
              </Bullets>
            </Callout>
          )}

          {q.data.stanzas.length === 0 ? (
            <Muted>
              No stanzas found in this pack's eventgen.conf.
            </Muted>
          ) : (
            <Stack $gap="medium">
              {q.data.stanzas.map((stanza) => {
                const lines = q.data.sample_lines[stanza] ?? [];
                return (
                  <div key={stanza}>
                    <Between>
                      <Mono>
                        [{stanza}]
                      </Mono>
                      <Muted $small>
                        {lines.length
                          ? `first ${lines.length} line${lines.length === 1 ? "" : "s"}`
                          : "no sample lines"}
                      </Muted>
                    </Between>
                    {lines.length > 0 && (
                      <Pre>
                        {lines.join("\n")}
                      </Pre>
                    )}
                  </div>
                );
              })}
            </Stack>
          )}
        </Stack>
      )}
    </Drawer>
  );
}
