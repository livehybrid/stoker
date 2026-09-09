import type { ReactNode } from "react";
import { fmtDurationS } from "./format";
import { Grid, Label, Mono, Muted, Stack, Strong } from "../../components/text";
import Table from "@splunk/react-ui/Table";

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
    <Stack>
      <Label>{label}</Label>
      <Strong>{value ?? "—"}</Strong>
    </Stack>
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
    return <Muted>No spec snapshot recorded.</Muted>;
  }
  const snap = snapshot as Snapshot;
  const overrides = Object.entries(snap.overrides ?? {});
  const target = snap.target ?? {};

  return (
    <Stack $gap="large">
      <Grid $min="180px">
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
      </Grid>

      <div>
        <Label>
          Target
        </Label>
        <Grid $min="180px">
          <Row label="Name" value={target.name ?? (target.id != null ? `#${target.id}` : "—")} />
          <Row
            label="HEC URL"
            value={
              target.hec_url ? (
                <Mono $break>{target.hec_url}</Mono>
              ) : (
                "—"
              )
            }
          />
          <Row label="Default index" value={target.default_index} />
          <Row label="Env tag" value={target.env_tag} />
          <Row label="Verify TLS" value={target.verify_tls ? "yes" : "no"} />
        </Grid>
      </div>

      {overrides.length > 0 && (
        <div>
          <Label>
            Overrides
          </Label>
          <Table>
            <Table.Head>
              <Table.HeadCell>Key</Table.HeadCell>
              <Table.HeadCell>Value</Table.HeadCell>
            </Table.Head>
            <Table.Body>
              {overrides.map(([k, v]) => (
                <Table.Row key={k}>
                  <Table.Cell>
                    <Mono>{k}</Mono>
                  </Table.Cell>
                  <Table.Cell>
                    <Mono>{String(v)}</Mono>
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        </div>
      )}
    </Stack>
  );
}
