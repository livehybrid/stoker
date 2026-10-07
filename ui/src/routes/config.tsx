import { useRef, useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { EmptyState } from "../components/States";
import { useToast } from "../components/Toast";
import { Bullets, Callout, Inline, Muted, Stack, Strong } from "../components/text";

// Configuration backup: download this instance's targets, pack repos and specs
// as JSON, and restore one. Admin only, matching the API.
//
// The restore that matters for a rebuilt environment is STOKER_CONFIG_IMPORT at
// boot, which needs no UI at all; this page exists so an operator can obtain the
// file to mount, and apply one to a running instance without reaching for curl.

function ConfigPage() {
  const { isAdmin } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [report, setReport] = useState<null | {
    targets: number;
    repos: number;
    specs: number;
    skipped: string[];
    warnings: string[];
  }>(null);

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

export const Route = createFileRoute("/config")({ component: ConfigPage });
