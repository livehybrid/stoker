// Pack picker panel for the job wizard: a selectable list of indexed packs with
// verified / replay / trusted-code badges and a parsed-stanza + sample preview
// for the selected pack (GET /packs/{id}/preview).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { TextInput } from "../../components/Field";
import { LoadingState, ErrorState } from "../../components/States";
import { cn } from "../../components/cn";
import { packLooksReplay, packLooksTrusted } from "./replay";
import { packIsMetrics } from "../metrics/config";

function PackBadges({ pack }: { pack: PackOut }) {
  return (
    <span className="flex flex-wrap items-center gap-1">
      {pack.verified ? (
        <Badge tone="green">verified</Badge>
      ) : (
        <Badge tone="slate">unverified</Badge>
      )}
      {pack.lint_status !== "ok" && <StatusBadge state={pack.lint_status} />}
      {packLooksReplay(pack) && <Badge tone="amber">replay</Badge>}
      {packLooksTrusted(pack) && <Badge tone="sky">trusted code</Badge>}
    </span>
  );
}

function PackPreview({ packId }: { packId: number }) {
  const q = useQuery({
    queryKey: ["pack-preview", packId],
    queryFn: () => api.packs.preview(packId),
    staleTime: 30_000,
  });
  if (q.isPending) return <LoadingState label="Loading preview…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;

  const { stanzas, sample_lines, lint_status, lint_errors } = q.data;
  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-slate-400">Lint:</span>
        <StatusBadge state={lint_status} />
      </div>
      {lint_errors.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-red-300">
          {lint_errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}
      <div>
        <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">
          Stanzas ({stanzas.length})
        </div>
        {stanzas.length === 0 ? (
          <p className="text-xs text-slate-500">No stanzas parsed.</p>
        ) : (
          <div className="space-y-2">
            {stanzas.map((s) => {
              const lines = sample_lines[s] ?? [];
              return (
                <div
                  key={s}
                  className="rounded-md border border-surface-muted bg-surface"
                >
                  <div className="border-b border-surface-muted px-2 py-1 font-mono text-xs text-slate-300">
                    [{s}]
                  </div>
                  {lines.length > 0 ? (
                    <pre className="max-h-40 overflow-auto px-2 py-1 text-[11px] leading-relaxed text-slate-400">
                      {lines.join("\n")}
                    </pre>
                  ) : (
                    <p className="px-2 py-1 text-[11px] text-slate-600">
                      No sample lines.
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// A pack that can join a multi-pack merge: eventgen only (replay is
// single-worker/engine-paced; metrics packs are built from a builder config).
// Mirrors the server's multi_pack_engine_unsupported gate.
export function packMergeable(pack: PackOut): boolean {
  return !packLooksReplay(pack) && !packIsMetrics(pack);
}

export function PackPicker({
  packs,
  selectedId,
  onSelect,
  extraIds,
  onToggleExtra,
}: {
  packs: PackOut[];
  selectedId: number | null;
  onSelect: (pack: PackOut) => void;
  // Multi-pack merge (optional): ids of ADDITIONAL packs merged with the
  // selected one into a single run bundle. When `onToggleExtra` is provided,
  // each non-selected mergeable row gets an "+ Add" toggle; the single-pack
  // flow is untouched when these props are omitted.
  extraIds?: number[];
  onToggleExtra?: (pack: PackOut) => void;
}) {
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? packs.filter(
        (p) =>
          p.name.toLowerCase().includes(needle) ||
          (p.description ?? "").toLowerCase().includes(needle) ||
          (p.sourcetypes_json ?? []).some((s) =>
            String(s).toLowerCase().includes(needle),
          ),
      )
    : packs;

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div className="space-y-2">
        <TextInput
          placeholder="Filter packs…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <div className="max-h-96 space-y-1 overflow-auto pr-1">
          {shown.length === 0 ? (
            <p className="px-2 py-6 text-center text-sm text-slate-500">
              No packs match.
            </p>
          ) : (
            shown.map((p) => {
              const active = p.id === selectedId;
              const inMerge = (extraIds ?? []).includes(p.id);
              // The "+ Add" toggle appears only when a mergeable primary is
              // already chosen and this row is another mergeable pack.
              const primary = packs.find((pk) => pk.id === selectedId);
              const canToggle =
                onToggleExtra != null &&
                !active &&
                selectedId != null &&
                primary != null &&
                packMergeable(primary) &&
                packMergeable(p);
              return (
                <div
                  key={p.id}
                  className={cn(
                    "flex w-full items-stretch gap-1 rounded-md border transition-colors",
                    active
                      ? "border-sky-600 bg-sky-950/40"
                      : inMerge
                        ? "border-sky-800/70 bg-sky-950/20"
                        : "border-surface-muted bg-surface hover:bg-surface-muted/40",
                  )}
                >
                  <button
                    type="button"
                    onClick={() => onSelect(p)}
                    className="min-w-0 flex-1 px-3 py-2 text-left"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-slate-100">
                        {p.name}
                      </span>
                      <span className="shrink-0 text-xs text-slate-500">
                        {p.stanza_count ?? "—"} stanza
                        {p.stanza_count === 1 ? "" : "s"}
                      </span>
                    </div>
                    {p.description && (
                      <p className="mt-0.5 truncate text-xs text-slate-500">
                        {p.description}
                      </p>
                    )}
                    <div className="mt-1.5 flex flex-wrap items-center gap-1">
                      <PackBadges pack={p} />
                      {inMerge && <Badge tone="sky">in merge</Badge>}
                    </div>
                  </button>
                  {canToggle && (
                    <button
                      type="button"
                      title={
                        inMerge
                          ? "Remove from this run's merge"
                          : "Also send this pack in the same run"
                      }
                      onClick={() => onToggleExtra(p)}
                      className={cn(
                        "shrink-0 self-center rounded-md border px-2 py-1 mr-2 text-xs transition-colors",
                        inMerge
                          ? "border-sky-700 bg-sky-900/50 text-sky-200 hover:bg-sky-900/80"
                          : "border-surface-muted text-slate-400 hover:bg-surface-muted/60 hover:text-slate-200",
                      )}
                    >
                      {inMerge ? "✓ Added" : "+ Add"}
                    </button>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>

      <div className="rounded-md border border-surface-muted bg-surface-soft p-3">
        {selectedId != null ? (
          <PackPreview packId={selectedId} />
        ) : (
          <p className="text-sm text-slate-500">
            Select a pack to preview its stanzas and sample lines.
          </p>
        )}
      </div>
    </div>
  );
}
