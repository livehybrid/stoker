import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { ErrorState, LoadingState } from "../../components/States";
import { Drawer } from "../ui/Drawer";

// Preview drawer for a pack: GET /packs/{id}/preview returns each stanza plus
// the first ~10 sample lines. Fetched lazily (only when a pack is selected).
// Also offers the "New job from this pack" jump into the wizard.
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
