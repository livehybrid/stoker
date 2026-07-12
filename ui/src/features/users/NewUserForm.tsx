import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import type { Role, UserCreate, UserOut } from "../../lib/types";
import { ROLES } from "../../lib/types";
import { Field, TextInput, Select } from "../../components/Field";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { USERS_QUERY_KEY } from "./keys";

interface FormState {
  username: string;
  password: string;
  role: Role;
  email: string;
}

const EMPTY: FormState = {
  username: "",
  password: "",
  role: "operator",
  email: "",
};

/**
 * Create-a-local-user form (admin only). The password is write-only: it is sent
 * on create and never rendered back (the API stores only a bcrypt hash and never
 * echoes it). Cleared on success along with the rest of the form.
 */
export function NewUserForm() {
  const toast = useToast();
  const qc = useQueryClient();
  const [form, setForm] = useState<FormState>(EMPTY);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const create = useMutation({
    mutationFn: (body: UserCreate) => api.users.create(body),
    onSuccess: (u: UserOut) => {
      toast.success(`User “${u.username}” created.`);
      setForm(EMPTY);
      qc.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
    onError: (err: unknown) => {
      // 409 on a duplicate username; surface the API's message.
      toast.error(
        err instanceof ApiError ? err.message : "Could not create the user.",
      );
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setFieldError(null);

    const username = form.username.trim();
    if (!username) return setFieldError("A username is required.");
    if (form.password.length < 8) {
      return setFieldError("Use a password of at least 8 characters.");
    }

    const body: UserCreate = {
      username,
      password: form.password,
      role: form.role,
      email: form.email.trim() || null,
    };
    create.mutate(body);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label="Username">
          <TextInput
            value={form.username}
            onChange={(e) => set("username", e.target.value)}
            placeholder="jsmith"
            autoComplete="off"
          />
        </Field>

        <Field label="Role">
          <Select
            value={form.role}
            onChange={(e) => set("role", e.target.value as Role)}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Password"
          hint="Write-only. Stored hashed and never shown again."
        >
          <TextInput
            type="password"
            value={form.password}
            onChange={(e) => set("password", e.target.value)}
            placeholder="at least 8 characters"
            autoComplete="new-password"
          />
        </Field>

        <Field label="Email" hint="Optional.">
          <TextInput
            type="email"
            value={form.email}
            onChange={(e) => set("email", e.target.value)}
            placeholder="jsmith@example.com"
            autoComplete="off"
          />
        </Field>
      </div>

      {fieldError && <p className="text-sm text-red-400">{fieldError}</p>}

      <div className="flex items-center gap-3">
        <Button type="submit" variant="primary" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create user"}
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
