import type { ReactNode } from "react";
import { fmtDurationS } from "./format";

// "Spec snapshot" tab (section 10.3): the frozen spec_snapshot_json a run was
// launched from (build_spec_snapshot in lifecycle.py). Non-secret by
// construction — the target is embedded by id + name + hec_url only, never a
// token. Rendered as a readable field grid plus the overrides map.

interface Snapshot {
  name?: string;
  engine?: string;
  ref?: string;
  rate_mode?: string;
  rate_value?: number | null;
  interval_s?: number | null;
  workers?: number;
  duration_s?: number | null;
  fleet?: string;
  strict_release?: boolean;
  index?: string | null;
  sourcetype?: string | null;
  telemetry_interval_s?: number;
  overrides?: Record<string, unknown> | null;
  driver_opts?: Record<string, unknown> | null;
  target?: {
    id?: number;
    name?: string;
    hec_url?: string;
    default_index?: string | null;
    verify_tls?: boolean;
    env_tag?: string;
  } | null;
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="text-sm text-slate-200">{value ?? "—"}</dd>
    </div>
  );
}

function rate(snap: Snapshot): string {
  if (snap.rate_mode === "count_interval") {
    const iv = snap.interval_s != null ? ` every ${snap.interval_s}s` : "";
    return `count / interval${iv}`;
  }
  if (snap.rate_value != null) return `${snap.rate_value} ${snap.rate_mode}`;
  return snap.rate_mode ?? "—";
}

export function SpecSnapshotPanel({ snapshot }: { snapshot: unknown }) {
  if (!snapshot || typeof snapshot !== "object") {
    return <p className="text-sm text-slate-500">No spec snapshot recorded.</p>;
  }
  const snap = snapshot as Snapshot;
  const overrides = Object.entries(snap.overrides ?? {});
  const target = snap.target ?? {};

  return (
    <div className="space-y-6">
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        <Row label="Name" value={snap.name} />
        <Row label="Engine" value={snap.engine} />
        <Row label="Ref" value={snap.ref} />
        <Row label="Fleet" value={snap.fleet} />
        <Row label="Rate" value={rate(snap)} />
        <Row label="Workers" value={snap.workers} />
        <Row
          label="Duration"
          value={snap.duration_s != null ? fmtDurationS(snap.duration_s) : "unbounded"}
        />
        <Row
          label="Strict release"
          value={snap.strict_release ? "yes" : "no"}
        />
        <Row label="Index" value={snap.index} />
        <Row label="Sourcetype" value={snap.sourcetype} />
        <Row
          label="Telemetry"
          value={snap.telemetry_interval_s != null ? `${snap.telemetry_interval_s}s` : "—"}
        />
      </dl>

      <div>
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
          Target
        </h4>
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          <Row label="Name" value={target.name ?? (target.id != null ? `#${target.id}` : "—")} />
          <Row
            label="HEC URL"
            value={
              target.hec_url ? (
                <span className="break-all font-mono text-xs">{target.hec_url}</span>
              ) : (
                "—"
              )
            }
          />
          <Row label="Default index" value={target.default_index} />
          <Row label="Env tag" value={target.env_tag} />
          <Row label="Verify TLS" value={target.verify_tls ? "yes" : "no"} />
        </dl>
      </div>

      {overrides.length > 0 && (
        <div>
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Overrides
          </h4>
          <div className="overflow-hidden rounded-md border border-surface-muted">
            <table className="w-full text-sm">
              <tbody>
                {overrides.map(([k, v]) => (
                  <tr key={k} className="border-b border-surface-muted/50 last:border-0">
                    <td className="w-1/3 px-3 py-1.5 font-mono text-xs text-slate-400">
                      {k}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-xs text-slate-200">
                      {String(v)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
