import { useEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { ErrorState, LoadingState } from "../../components/States";
import { Drawer } from "../ui/Drawer";

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
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge state={q.data.lint_status} />
            {pack.verified ? (
              <Badge tone="green">verified</Badge>
            ) : (
              <Badge tone="slate">unverified</Badge>
            )}
            <Badge tone="neutral">
              {q.data.stanzas.length} stanza{q.data.stanzas.length === 1 ? "" : "s"}
            </Badge>
          </div>

          {/* Rendered-events preview: a lightweight in-process render (tokens
              like timestamp / ipv4 / integer substituted), no fleet or HEC.
              Opt-in so it only runs when the author asks. */}
          <div className="rounded-md border border-surface-muted bg-surface px-3 py-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h4 className="text-xs font-semibold text-slate-200">
                  Rendered events
                </h4>
                <p className="mt-0.5 text-[11px] text-slate-500">
                  A few sample events with tokens applied. No fleet, no HEC.
                </p>
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
            </div>
            {showEvents &&
              (eventsQ.isPending ? (
                <LoadingState label="Rendering events…" />
              ) : eventsQ.isError ? (
                <ErrorState error={eventsQ.error} onRetry={() => eventsQ.refetch()} />
              ) : eventsQ.data.events.length === 0 ? (
                <p className="mt-2 text-[11px] text-slate-500">
                  No events could be rendered (no sample-mode stanza or no
                  readable sample file).
                </p>
              ) : (
                <pre className="mt-2 max-h-56 overflow-auto rounded-md border border-surface-muted bg-surface-soft px-3 py-2 text-[11px] leading-relaxed text-slate-300">
                  {eventsQ.data.events.join("\n")}
                </pre>
              ))}
          </div>

          {q.data.lint_errors.length > 0 && (
            <div className="rounded-md border border-red-800/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
              <p className="font-medium">Lint errors</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {q.data.lint_errors.map((err, i) => (
                  <li key={i}>{err}</li>
                ))}
              </ul>
            </div>
          )}

          {q.data.stanzas.length === 0 ? (
            <p className="text-sm text-slate-500">
              No stanzas found in this pack's eventgen.conf.
            </p>
          ) : (
            <div className="space-y-4">
              {q.data.stanzas.map((stanza) => {
                const lines = q.data.sample_lines[stanza] ?? [];
                return (
                  <div key={stanza}>
                    <div className="flex items-center justify-between gap-2">
                      <h4 className="font-mono text-xs font-semibold text-slate-200">
                        [{stanza}]
                      </h4>
                      <span className="text-[11px] text-slate-500">
                        {lines.length
                          ? `first ${lines.length} line${lines.length === 1 ? "" : "s"}`
                          : "no sample lines"}
                      </span>
                    </div>
                    {lines.length > 0 && (
                      <pre className="mt-1 max-h-56 overflow-auto rounded-md border border-surface-muted bg-surface px-3 py-2 text-[11px] leading-relaxed text-slate-300">
                        {lines.join("\n")}
                      </pre>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}
