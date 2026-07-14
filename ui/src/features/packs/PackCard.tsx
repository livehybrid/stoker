import { Link } from "@tanstack/react-router";

import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { formatBytes, formatGbDay, shortSha } from "../format";
import { packIsMetrics } from "../metrics/config";

// A pack card: lint + verified badges, sourcetypes, estimated bytes/event and
// declared GB/day, with Preview and "New job from pack" (design section 10.4).
interface Props {
  pack: PackOut;
  onPreview: (pack: PackOut) => void;
}

function asStringList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => String(x)).filter(Boolean);
}

export function PackCard({ pack, onPreview }: Props) {
  const sourcetypes = asStringList(pack.sourcetypes_json);
  const engines = asStringList(pack.engines_json);
  const tags = asStringList(pack.tags_json);
  const isMetric = packIsMetrics(pack);

  return (
    <section className="flex flex-col rounded-lg border border-surface-muted bg-surface-soft p-4 shadow-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-slate-100" title={pack.name}>
            {pack.name}
          </h3>
          {pack.description && (
            <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">
              {pack.description}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          {/* Only surface lint state when it is a problem; a clean pack shows the
              "verified" badge, so an extra "ok" pill was just noise. */}
          {pack.lint_status !== "ok" && <StatusBadge state={pack.lint_status} />}
          {pack.verified ? (
            <Badge tone="green">verified</Badge>
          ) : (
            <Badge tone="slate">unverified</Badge>
          )}
        </div>
      </div>

      {(sourcetypes.length > 0 || engines.length > 0 || tags.length > 0) && (
        <div className="mt-3 flex flex-wrap gap-1">
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
        </div>
      )}

      <dl className="mt-3 grid grid-cols-3 gap-x-3 gap-y-2 text-xs">
        <div>
          <dt className="text-slate-500">Stanzas</dt>
          <dd className="mt-0.5 text-slate-200">{pack.stanza_count ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Bytes/event</dt>
          <dd className="mt-0.5 text-slate-200">
            {formatBytes(pack.est_bytes_per_event)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Declared</dt>
          <dd className="mt-0.5 text-slate-200">
            {formatGbDay(pack.declared_per_day_gb)}
          </dd>
        </div>
      </dl>

      {pack.lint_status !== "ok" &&
        Array.isArray(pack.lint_errors_json) &&
        pack.lint_errors_json.length > 0 && (
          <p className="mt-3 rounded-md border border-red-800/60 bg-red-950/40 px-3 py-1.5 text-[11px] text-red-300">
            {String(pack.lint_errors_json[0])}
            {pack.lint_errors_json.length > 1
              ? ` (+${pack.lint_errors_json.length - 1} more)`
              : ""}
          </p>
        )}

      <div className="mt-auto flex items-center justify-between gap-2 pt-3">
        <span className="text-[11px] text-slate-600">
          {pack.indexed_sha ? `indexed ${shortSha(pack.indexed_sha)}` : "local pack"}
        </span>
        <div className="flex items-center gap-2">
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
        </div>
      </div>
    </section>
  );
}
