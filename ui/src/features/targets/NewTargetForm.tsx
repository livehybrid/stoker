import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import type { TargetCreate, TargetOut, TargetUpdate } from "../../lib/types";
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

function fromTarget(t: TargetOut): FormState {
  return {
    name: t.name,
    hec_url: t.hec_url,
    token: "", // write-only: never echoed, so it starts blank on edit
    default_index: t.default_index ?? "",
    env_tag: t.env_tag,
    max_concurrent_gb_day:
      t.max_concurrent_gb_day != null ? String(t.max_concurrent_gb_day) : "",
    verify_tls: t.verify_tls,
  };
}

/**
 * Target form. Creates a target, or edits an existing one when `target` is
 * given. The HEC token is write-only: on create it is required; on edit, leaving
 * it blank keeps the stored token and entering a value rotates it. Capacity
 * (concurrent GB/day cap) is editable in both modes.
 */
export function NewTargetForm({
  target,
  onCreated,
  onDone,
}: {
  target?: TargetOut;
  onCreated?: () => void;
  onDone?: () => void;
}) {
  const editing = target != null;
  const toast = useToast();
  const qc = useQueryClient();
  const [form, setForm] = useState<FormState>(
    editing ? fromTarget(target) : EMPTY,
  );
  const [fieldError, setFieldError] = useState<string | null>(null);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const save = useMutation({
    mutationFn: (vars: { create?: TargetCreate; update?: TargetUpdate }) =>
      editing && vars.update
        ? api.targets.update(target.id, vars.update)
        : api.targets.create(vars.create as TargetCreate),
    onSuccess: (t: TargetOut) => {
      toast.success(
        editing ? `Target “${t.name}” updated.` : `Target “${t.name}” created.`,
      );
      if (!editing) setForm(EMPTY);
      qc.invalidateQueries({ queryKey: ["targets"] });
      onCreated?.();
      onDone?.();
    },
    onError: (err: unknown) => {
      const fallback = editing
        ? "Could not update the target."
        : "Could not create the target.";
      toast.error(err instanceof ApiError ? err.message : fallback);
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
    // On create the token is required; on edit it is optional (blank = keep).
    if (!editing && !form.token.trim()) {
      return setFieldError("A HEC token is required.");
    }

    let cap: number | null = null;
    if (form.max_concurrent_gb_day.trim() !== "") {
      const parsed = Number(form.max_concurrent_gb_day);
      if (!Number.isFinite(parsed) || parsed < 0) {
        return setFieldError("Concurrent cap must be a non-negative number.");
      }
      cap = parsed;
    }

    if (editing) {
      const update: TargetUpdate = {
        name,
        hec_url: hecUrl,
        default_index: form.default_index.trim() || null,
        env_tag: form.env_tag,
        max_concurrent_gb_day: cap,
        verify_tls: form.verify_tls,
      };
      if (form.token.trim()) update.token = form.token; // only rotate when given
      save.mutate({ update });
    } else {
      save.mutate({
        create: {
          name,
          hec_url: hecUrl,
          token: form.token,
          default_index: form.default_index.trim() || null,
          env_tag: form.env_tag,
          max_concurrent_gb_day: cap,
          verify_tls: form.verify_tls,
        },
      });
    }
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
          hint={
            editing
              ? "Write-only. Leave blank to keep the current token; enter a value to rotate it."
              : "Write-only. Stored encrypted and never shown again."
          }
        >
          <TextInput
            type="password"
            value={form.token}
            onChange={(e) => set("token", e.target.value)}
            placeholder={
              editing
                ? "leave blank to keep current"
                : "••••••••-••••-••••-••••-••••••••••••"
            }
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
        <Button type="submit" variant="primary" disabled={save.isPending}>
          {save.isPending
            ? editing
              ? "Saving…"
              : "Creating…"
            : editing
              ? "Save changes"
              : "Create target"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          disabled={save.isPending}
          onClick={() => {
            if (editing) {
              onDone?.();
            } else {
              setForm(EMPTY);
              setFieldError(null);
            }
          }}
        >
          {editing ? "Cancel" : "Reset"}
        </Button>
      </div>
    </form>
  );
}
