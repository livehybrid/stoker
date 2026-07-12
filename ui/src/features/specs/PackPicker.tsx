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

export function PackPicker({
  packs,
  selectedId,
  onSelect,
}: {
  packs: PackOut[];
  selectedId: number | null;
  onSelect: (pack: PackOut) => void;
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
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => onSelect(p)}
                  className={cn(
                    "w-full rounded-md border px-3 py-2 text-left transition-colors",
                    active
                      ? "border-sky-600 bg-sky-950/40"
                      : "border-surface-muted bg-surface hover:bg-surface-muted/40",
                  )}
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
                  <div className="mt-1.5">
                    <PackBadges pack={p} />
                  </div>
                </button>
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
