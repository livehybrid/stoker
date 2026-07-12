import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import type { TargetCreate, TargetOut } from "../../lib/types";
import { Field, TextInput, Select } from "../../components/Field";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";

// Env tags the design references (lab / staging / prod). Free enough that a new
// tag is not needed; kept as a select so the strip colouring stays predictable.
const ENV_TAGS = ["lab", "staging", "prod"];

interface FormState {
  name: string;
  hec_url: string;
  token: string;
  default_index: string;
  env_tag: string;
  max_concurrent_gb_day: string;
  verify_tls: boolean;
}

const EMPTY: FormState = {
  name: "",
  hec_url: "",
  token: "",
  default_index: "",
  env_tag: "lab",
  max_concurrent_gb_day: "",
  verify_tls: true,
};

/**
 * Create-a-target form. The HEC token is write-only: it is sent on create and
 * never rendered back (the API stores it encrypted and never echoes it). The
 * field is cleared on success along with the rest of the form.
 */
export function NewTargetForm({ onCreated }: { onCreated?: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [form, setForm] = useState<FormState>(EMPTY);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const create = useMutation({
    mutationFn: (body: TargetCreate) => api.targets.create(body),
    onSuccess: (t: TargetOut) => {
      toast.success(`Target “${t.name}” created.`);
      setForm(EMPTY);
      qc.invalidateQueries({ queryKey: ["targets"] });
      onCreated?.();
    },
    onError: (err: unknown) => {
      const msg =
        err instanceof ApiError ? err.message : "Could not create the target.";
      toast.error(msg);
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setFieldError(null);

    const name = form.name.trim();
    const hecUrl = form.hec_url.trim();
    if (!name) return setFieldError("A name is required.");
    if (!hecUrl) return setFieldError("A HEC URL is required.");
    if (!/^https?:\/\//i.test(hecUrl)) {
      return setFieldError("HEC URL must start with http:// or https://.");
    }
    if (!form.token.trim()) return setFieldError("A HEC token is required.");

    let cap: number | null = null;
    if (form.max_concurrent_gb_day.trim() !== "") {
      const parsed = Number(form.max_concurrent_gb_day);
      if (!Number.isFinite(parsed) || parsed < 0) {
        return setFieldError("Concurrent cap must be a non-negative number.");
      }
      cap = parsed;
    }

    const body: TargetCreate = {
      name,
      hec_url: hecUrl,
      token: form.token,
      default_index: form.default_index.trim() || null,
      env_tag: form.env_tag,
      max_concurrent_gb_day: cap,
      verify_tls: form.verify_tls,
    };
    create.mutate(body);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label="Name">
          <TextInput
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder="prod-hec-eu"
            autoComplete="off"
          />
        </Field>

        <Field label="Environment">
          <Select
            value={form.env_tag}
            onChange={(e) => set("env_tag", e.target.value)}
          >
            {ENV_TAGS.map((tag) => (
              <option key={tag} value={tag}>
                {tag}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="HEC URL"
          hint="Base collector URL, e.g. https://hec.example.com:8088"
        >
          <TextInput
            value={form.hec_url}
            onChange={(e) => set("hec_url", e.target.value)}
            placeholder="https://hec.example.com:8088"
            autoComplete="off"
            inputMode="url"
          />
        </Field>

        <Field
          label="HEC token"
          hint="Write-only. Stored encrypted and never shown again."
        >
          <TextInput
            type="password"
            value={form.token}
            onChange={(e) => set("token", e.target.value)}
            placeholder="••••••••-••••-••••-••••-••••••••••••"
            autoComplete="new-password"
          />
        </Field>

        <Field label="Default index" hint="Optional; blank uses the token default.">
          <TextInput
            value={form.default_index}
            onChange={(e) => set("default_index", e.target.value)}
            placeholder="main"
            autoComplete="off"
          />
        </Field>

        <Field
          label="Concurrent cap (GB/day)"
          hint="Optional ceiling across this target's active runs. Blank = no cap."
        >
          <TextInput
            type="number"
            min={0}
            step="any"
            value={form.max_concurrent_gb_day}
            onChange={(e) => set("max_concurrent_gb_day", e.target.value)}
            placeholder="e.g. 250"
          />
        </Field>
      </div>

      <label className="flex items-center gap-2 text-sm text-slate-300">
        <input
          type="checkbox"
          checked={form.verify_tls}
          onChange={(e) => set("verify_tls", e.target.checked)}
          className="h-4 w-4 rounded border-surface-muted bg-surface text-sky-500 focus:ring-sky-500"
        />
        Verify TLS certificate
      </label>

      {fieldError && <p className="text-sm text-red-400">{fieldError}</p>}

      <div className="flex items-center gap-3">
        <Button type="submit" variant="primary" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create target"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          disabled={create.isPending}
          onClick={() => {
            setForm(EMPTY);
            setFieldError(null);
          }}
        >
          Reset
        </Button>
      </div>
    </form>
  );
}
