import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import type { RepoAuthKind, RepoCreate, RepoCreated } from "../../lib/types";
import { Button } from "../../components/Button";
import { Field, Select, TextInput } from "../../components/Field";
import { Callout, Form, Grid, Inline, Muted, Panel, Strong } from "../../components/text";
import Switch from "@splunk/react-ui/Switch";

// The "Register repo" form body (rendered inside a Modal by the Repos page).
// Fields mirror RepoCreate exactly: url, auth_kind, secret (write-only, only
// required for pat/deploy_key), default_ref, trusted_code. On success the parent
// is handed the RepoCreated so it can reveal the one-time webhook_secret.
interface Props {
  onCreated: (repo: RepoCreated) => void;
  onCancel: () => void;
}

const AUTH_KINDS: { value: RepoAuthKind; label: string }[] = [
  { value: "none", label: "None (public repo)" },
  { value: "pat", label: "Personal access token" },
  { value: "deploy_key", label: "Deploy key (SSH private key)" },
];

export function RegisterRepoForm({ onCreated, onCancel }: Props) {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [authKind, setAuthKind] = useState<RepoAuthKind>("none");
  const [secret, setSecret] = useState("");
  const [defaultRef, setDefaultRef] = useState("main");
  const [trustedCode, setTrustedCode] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const needsSecret = authKind === "pat" || authKind === "deploy_key";

  const mutation = useMutation({
    mutationFn: (body: RepoCreate) => api.repos.create(body),
    onSuccess: (repo) => {
      qc.invalidateQueries({ queryKey: ["repos"] });
      onCreated(repo);
    },
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setFieldError(null);
    const trimmedUrl = url.trim();
    if (!trimmedUrl) {
      setFieldError("A repository URL is required.");
      return;
    }
    if (needsSecret && !secret.trim()) {
      setFieldError(
        authKind === "pat"
          ? "A personal access token is required for PAT auth."
          : "A deploy key is required for deploy-key auth.",
      );
      return;
    }
    const body: RepoCreate = {
      url: trimmedUrl,
      auth_kind: authKind,
      default_ref: defaultRef.trim() || "main",
      trusted_code: trustedCode,
    };
    // Only send the secret when the auth kind uses one; never send an empty
    // string (that would store an empty credential).
    if (needsSecret && secret.trim()) {
      body.secret = secret;
    }
    mutation.mutate(body);
  }

  const apiMessage =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.error instanceof Error
        ? mutation.error.message
        : null;

  return (
    <Form onSubmit={submit}>
      <Field
        label="Repository URL"
        hint="https://, ssh://, git://, file:// or scp-style user@host:path"
      >
        <TextInput
          value={url}
          onChange={(_e, { value }) => setUrl(value)}
          placeholder="https://github.com/org/eventgen-packs.git"
          autoFocus
          spellCheck={false}
        />
      </Field>

      <Grid $min="180px">
        <Field label="Auth kind">
          <Select
            value={authKind}
            onChange={(_e, { value }) => {
              const next = value as RepoAuthKind;
              setAuthKind(next);
              if (next === "none") setSecret("");
            }}
          >
            {AUTH_KINDS.map((a) => (
              <Select.Option key={a.value} value={a.value} label={a.label} />
            ))}
          </Select>
        </Field>

        <Field label="Default ref" hint="Branch, tag or SHA (defaults to main)">
          <TextInput
            value={defaultRef}
            onChange={(_e, { value }) => setDefaultRef(value)}
            placeholder="main"
            spellCheck={false}
          />
        </Field>
      </Grid>

      {needsSecret && (
        <Field
          label={authKind === "pat" ? "Personal access token" : "Deploy key"}
          hint="Write-only. Stored encrypted at rest and never shown again."
        >
          <TextInput
            type="password"
            value={secret}
            onChange={(_e, { value }) => setSecret(value)}
            placeholder={authKind === "pat" ? "ghp_…" : "-----BEGIN OPENSSH PRIVATE KEY-----"}
            autoComplete="new-password"
            spellCheck={false}
          />
        </Field>
      )}

      <Panel>
        <Switch
          appearance="checkbox"
          selected={trustedCode}
          onClick={() => setTrustedCode(!trustedCode)}
        >
          <Strong>Trusted code</Strong>
          <Muted $small>
            {" "}
            — allow packs from this repo to run code (e.g. custom eventgen
            plugins). Leave off for untrusted sources.
          </Muted>
        </Switch>
      </Panel>

      {(fieldError || apiMessage) && (
        <Callout $tone="error">
          {fieldError || apiMessage}
        </Callout>
      )}

      <Inline>
        <Button type="button" variant="ghost" onClick={onCancel} disabled={mutation.isPending}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" disabled={mutation.isPending}>
          {mutation.isPending ? "Registering…" : "Register repo"}
        </Button>
      </Inline>
    </Form>
  );
}
