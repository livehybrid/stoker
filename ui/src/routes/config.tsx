import { useRef, useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/States";
import { useToast } from "../components/Toast";
import { Bullets, Callout, Inline, Mono, Muted, Stack, Strong } from "../components/text";
import { absoluteTime, relativeTime } from "../features/format";
import type { ConfigImportReport, ConfigSourceInfo, ConfigSyncResult } from "../lib/types";

// Configuration backup: download this instance's targets, pack repos and specs
// as JSON, and restore one; and the shared copy in STOKER_CONFIG_SOURCE, pulled
// at boot and pushed on change. Admin only, matching the API.
//
// The restore that matters for a rebuilt environment is STOKER_CONFIG_IMPORT at
// boot, which needs no UI at all; this page exists so an operator can obtain the
// file to mount, and apply one to a running instance without reaching for curl.

function ConfigPage() {
  const { isAdmin } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [report, setReport] = useState<null | ConfigImportReport>(null);

  // Deployment environment plus the last pull/push; refreshed after either.
  const sourceQ = useQuery({
    queryKey: ["config-source"],
    queryFn: api.config.source,
    enabled: isAdmin,
    retry: false,
  });

  const pullM = useMutation({
    mutationFn: api.config.pull,
    onSuccess: (r) => {
      setReport(r);
      toast.success(
        r.found === false
          ? "The shared copy is empty: nothing has been pushed yet"
          : `Pulled ${r.targets} new target(s), ${r.repos} repo(s), ${r.specs} spec(s)`,
      );
      qc.invalidateQueries();
    },
    onError: (e) => {
      toast.error(e instanceof Error ? e.message : "Pull failed");
      qc.invalidateQueries({ queryKey: ["config-source"] });
    },
  });

  const pushM = useMutation({
    mutationFn: api.config.push,
    onSuccess: (r) => {
      toast.success(`Pushed ${r.targets} target(s), ${r.repos} repo(s), ${r.specs} spec(s)`);
      qc.invalidateQueries({ queryKey: ["config-source"] });
    },
    onError: (e) => {
      toast.error(e instanceof Error ? e.message : "Push failed");
      qc.invalidateQueries({ queryKey: ["config-source"] });
    },
  });

  const importM = useMutation({
    mutationFn: async (file: File) => {
      const text = await file.text();
      let doc: unknown;
      try {
        doc = JSON.parse(text);
      } catch {
        throw new Error(`${file.name} is not valid JSON`);
      }
      return api.config.import(doc);
    },
    onSuccess: (r) => {
      setReport(r);
      toast.success(
        `Imported ${r.targets} target(s), ${r.repos} repo(s), ${r.specs} spec(s)`,
      );
      // Everything this touches is listed elsewhere in the app.
      qc.invalidateQueries();
    },
    onError: (e) =>
      toast.error(e instanceof Error ? e.message : "Import failed"),
  });

  if (!isAdmin) {
    return (
      <Stack $gap="large">
        <PageHeader title="Configuration" />
        <Card>
          <EmptyState
            title="Administrator access required"
            message="Configuration carries every target's encrypted HEC token, so it is admin only."
          />
        </Card>
      </Stack>
    );
  }

  return (
    <Stack $gap="large">
      <PageHeader
        title="Configuration"
        subtitle="Back up this instance's targets, pack repos and specs, or restore them."
      />

      <Card title="Shared copy">
        <SharedCopy
          info={sourceQ.data}
          loading={sourceQ.isPending}
          pulling={pullM.isPending}
          pushing={pushM.isPending}
          onPull={() => pullM.mutate()}
          onPush={() => pushM.mutate()}
        />
      </Card>

      <Card title="Download">
        <Stack $gap="small">
          <Muted>
            Targets, pack repos and specs as JSON. Runs, metrics and the packs
            themselves are not included: this is the configuration you authored,
            not what the instance has done.
          </Muted>
          <Inline>
            <a href={api.config.exportUrl(true)} download>
              <Button variant="primary">Download config</Button>
            </a>
            <a href={api.config.exportUrl(false)} download>
              <Button variant="secondary">Download without secrets</Button>
            </a>
          </Inline>
          <Callout $tone="info">
            <Strong>Secrets travel encrypted.</Strong> HEC tokens and repo
            credentials are exported as the ciphertext they are stored as, so a
            restore needs the same <code>STOKER_MASTER_KEY</code> — in Kubernetes
            that is a Secret, which survives the volume teardown this exists for.
            The file records a fingerprint of the key, so restoring under a
            different one tells you rather than leaving every target silently
            unable to authenticate. Use <em>without secrets</em> for a copy safe
            to commit; it restores the configuration with those fields blank.
          </Callout>
        </Stack>
      </Card>

      <Card title="Restore">
        <Stack $gap="small">
          <Muted>
            Applying a file is an idempotent upsert: re-applying the same one
            changes nothing, and a partial restore can simply be run again.
            Matching is by name, never by id, so a file restores onto a fresh
            instance whose row ids differ.
          </Muted>
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) importM.mutate(f);
              e.target.value = "";
            }}
          />
          <Inline>
            <Button
              variant="secondary"
              onClick={() => fileRef.current?.click()}
              disabled={importM.isPending}
            >
              {importM.isPending ? "Applying…" : "Choose a config file…"}
            </Button>
          </Inline>
          {report && (
            <Stack $gap="small">
              <Muted>
                Created <Strong>{report.targets}</Strong> target(s),{" "}
                <Strong>{report.repos}</Strong> repo(s),{" "}
                <Strong>{report.specs}</Strong> spec(s). Anything already present
                was updated in place.
              </Muted>
              {report.warnings.length > 0 && (
                <Callout $tone="warning">
                  <Bullets>
                    {report.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </Bullets>
                </Callout>
              )}
              {report.skipped.length > 0 && (
                <Callout $tone="info">
                  <Strong>Skipped:</Strong>
                  <Bullets>
                    {report.skipped.map((sk) => (
                      <li key={sk}>{sk}</li>
                    ))}
                  </Bullets>
                  <Muted $small>
                    A spec naming a pack that is not indexed yet is normal when
                    its repo was created by this same import; run it again once
                    the repo has synced.
                  </Muted>
                </Callout>
              )}
            </Stack>
          )}
        </Stack>
      </Card>

      <Card title="Restoring automatically">
        <Muted>
          For a rebuilt environment, mount the file and set{" "}
          <code>STOKER_CONFIG_IMPORT</code> to its path (or to the JSON itself).
          It is applied at boot, before traffic is served, and never raises: a
          malformed file is logged and skipped, because a typo in a ConfigMap
          must not stop the control plane starting.
        </Muted>
      </Card>
    </Stack>
  );
}

function SyncLine({ label, result }: { label: string; result: ConfigSyncResult | null }) {
  if (!result) {
    return (
      <Muted>
        {label}: <Strong>not since this instance started</Strong>
      </Muted>
    );
  }
  let what = "";
  if (!result.ok) what = result.error ?? "failed";
  else if (result.found === false) what = "nothing there yet";
  else if (result.created)
    what = `${result.created.targets} target(s), ${result.created.repos} repo(s), ${result.created.specs} spec(s) new`;
  else if (result.counts)
    what = `${result.counts.targets} target(s), ${result.counts.repos} repo(s), ${result.counts.specs} spec(s)`;
  return (
    <Muted>
      {label}: <span title={absoluteTime(result.at)}>{relativeTime(result.at)}</span>{" "}
      <Badge tone={result.ok ? "green" : "red"}>{result.ok ? "ok" : "failed"}</Badge>{" "}
      {what}
    </Muted>
  );
}

function SharedCopy({
  info,
  loading,
  pulling,
  pushing,
  onPull,
  onPush,
}: {
  info: ConfigSourceInfo | undefined;
  loading: boolean;
  pulling: boolean;
  pushing: boolean;
  onPull: () => void;
  onPush: () => void;
}) {
  if (loading) return <Muted>Checking the config source…</Muted>;
  if (!info || !info.configured) {
    return (
      <Muted>
        Not configured. Set <code>STOKER_CONFIG_SOURCE</code> to an{" "}
        <code>s3://bucket/prefix</code> (or a directory) and this instance pulls
        its configuration from there at boot, so a fresh or rebuilt instance
        comes up ready to run. Add <code>STOKER_CONFIG_SOURCE_WRITE=1</code> and
        every change made here is pushed back automatically.
      </Muted>
    );
  }
  if (info.error) {
    return (
      <Callout $tone="warning">
        <Strong>The config source cannot be used:</Strong> {info.error}
      </Callout>
    );
  }
  return (
    <Stack $gap="small">
      <Inline>
        <Mono $break>
          {info.location}/{info.object}
        </Mono>
        <Badge tone={info.writable ? "green" : "slate"}>
          {info.writable ? "pushes changes" : "read only"}
        </Badge>
      </Inline>
      <Muted>
        Pulled at boot and with <em>Pull now</em>: the same upsert as a restore,
        so nothing made here is removed.{" "}
        {info.writable
          ? "Every change to a target, repo or spec here is pushed within seconds, secrets as ciphertext."
          : "This instance does not write the shared copy; set STOKER_CONFIG_SOURCE_WRITE=1 to let it."}
      </Muted>
      <SyncLine label="Last pull" result={info.last_pull} />
      <SyncLine label="Last push" result={info.last_push} />
      {info.carried > 0 && (
        <Callout $tone="info">
          {info.carried} row(s) in the shared copy could not be applied here
          (usually a spec whose pack has not synced yet). They are kept in every
          push rather than deleted; <em>Pull now</em> again once the pack is in.
        </Callout>
      )}
      <Inline>
        <Button variant="secondary" onClick={onPull} disabled={pulling}>
          {pulling ? "Pulling…" : "Pull now"}
        </Button>
        {info.writable && (
          <Button variant="secondary" onClick={onPush} disabled={pushing}>
            {pushing ? "Pushing…" : "Push now"}
          </Button>
        )}
      </Inline>
    </Stack>
  );
}

export const Route = createFileRoute("/config")({ component: ConfigPage });
