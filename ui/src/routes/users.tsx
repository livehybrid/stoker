import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Role, UserOut, UserUpdate } from "../lib/types";
import { ROLES } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Table, type Column } from "../components/Table";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { Select } from "../components/Field";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useToast } from "../components/Toast";
import { NewUserForm } from "../features/users/NewUserForm";
import { ResetPasswordModal } from "../features/users/ResetPasswordModal";
import { USERS_QUERY_KEY } from "../features/users/keys";
import { absoluteTime, relativeTime } from "../features/format";

// Admin Users page. Lists every user; lets an admin create users, change a
// role, reset a password, (de)activate an account and delete one. The API owns
// the integrity guards (cannot delete/demote/deactivate the last admin; cannot
// delete yourself); this page surfaces those rejections as toasts rather than
// re-implementing them. Passwords/hashes are never rendered.

function roleTone(role: string): "sky" | "neutral" | "slate" {
  if (role === "admin") return "sky";
  if (role === "operator") return "neutral";
  return "slate";
}

function Users() {
  const toast = useToast();
  const qc = useQueryClient();
  const { user: me, isAdmin } = useAuth();

  const q = useQuery({
    queryKey: USERS_QUERY_KEY,
    queryFn: () => api.users.list(),
    // Only fetch when we know the caller is an admin (the API would 403 anyway).
    enabled: isAdmin,
  });

  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [resetFor, setResetFor] = useState<UserOut | null>(null);

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: UserUpdate }) =>
      api.users.update(id, body),
    onSuccess: (u: UserOut) => {
      toast.success(`Updated “${u.username}”.`);
      qc.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
    onError: (err: unknown) => {
      // 409 surfaces the last-admin guard; 4xx surfaces validation.
      toast.error(err instanceof ApiError ? err.message : "Update failed.");
      qc.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.users.delete(id),
    onSuccess: (_data, id) => {
      toast.success("User deleted.");
      setConfirmDelete((cur) => (cur === id ? null : cur));
      qc.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
    onError: (err: unknown) => {
      // 409 when deleting yourself or the last active admin.
      toast.error(err instanceof ApiError ? err.message : "Could not delete the user.");
      setConfirmDelete(null);
    },
  });

  // Defence in depth: the nav hides this page for non-admins, but a direct
  // navigation should also be refused clearly rather than firing a 403 fetch.
  if (!isAdmin) {
    return (
      <div className="space-y-5">
        <PageHeader title="Users" />
        <Card>
          <EmptyState
            title="Administrator access required"
            message="Only administrators can manage users. Ask an admin if you need access."
          />
        </Card>
      </div>
    );
  }

  const columns: Column<UserOut>[] = [
    {
      key: "username",
      header: "Username",
      cell: (u) => (
        <div className="flex items-center gap-2">
          <span className="font-medium text-slate-100">{u.username}</span>
          {me && u.id === me.id && (
            <Badge tone="sky">you</Badge>
          )}
        </div>
      ),
    },
    {
      key: "email",
      header: "Email",
      cell: (u) => (
        <span className="text-slate-300">{u.email || "—"}</span>
      ),
    },
    {
      key: "role",
      header: "Role",
      cell: (u) => {
        const saving = patch.isPending && patch.variables?.id === u.id;
        return (
          <div className="flex items-center gap-2">
            <Select
              value={u.role}
              disabled={saving}
              className="w-32"
              onChange={(e) =>
                patch.mutate({ id: u.id, body: { role: e.target.value as Role } })
              }
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </Select>
            <Badge tone={roleTone(u.role)}>{u.role}</Badge>
          </div>
        );
      },
    },
    {
      key: "source",
      header: "Source",
      cell: (u) => (
        <Badge tone={u.source === "proxy" ? "sky" : "slate"}>{u.source}</Badge>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (u) =>
        u.active ? (
          <Badge tone="green">active</Badge>
        ) : (
          <Badge tone="red">disabled</Badge>
        ),
    },
    {
      key: "last_login",
      header: "Last login",
      cell: (u) => (
        <span
          className="text-slate-400"
          title={u.last_login_at ? absoluteTime(u.last_login_at) : undefined}
        >
          {u.last_login_at ? relativeTime(u.last_login_at) : "never"}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "text-right whitespace-nowrap",
      cell: (u) => {
        const saving = patch.isPending && patch.variables?.id === u.id;
        const deleting = remove.isPending && remove.variables === u.id;
        const isConfirming = confirmDelete === u.id;
        const isSelf = !!me && u.id === me.id;
        return (
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="secondary"
              onClick={() => setResetFor(u)}
              disabled={saving}
            >
              Reset password
            </Button>
            <Button
              variant="ghost"
              disabled={saving}
              onClick={() =>
                patch.mutate({ id: u.id, body: { active: !u.active } })
              }
            >
              {u.active ? "Deactivate" : "Reactivate"}
            </Button>
            {isConfirming ? (
              <>
                <Button
                  variant="danger"
                  onClick={() => remove.mutate(u.id)}
                  disabled={deleting}
                >
                  {deleting ? "Deleting…" : "Confirm delete"}
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => setConfirmDelete(null)}
                  disabled={deleting}
                >
                  Cancel
                </Button>
              </>
            ) : (
              <Button
                variant="ghost"
                // Deleting yourself is refused by the API; disable it up front.
                disabled={isSelf}
                title={isSelf ? "You cannot delete your own account." : undefined}
                onClick={() => setConfirmDelete(u.id)}
              >
                Delete
              </Button>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Users"
        subtitle="Local accounts and SSO identities. Passwords are write-only and never shown."
      />

      <Card title="All users">
        {q.isPending ? (
          <LoadingState />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => q.refetch()} />
        ) : (
          <Table
            columns={columns}
            rows={q.data ?? []}
            rowKey={(u) => u.id}
            empty={
              <EmptyState
                title="No users yet"
                message="Create the first user below."
              />
            }
          />
        )}
      </Card>

      <Card title="New user">
        <NewUserForm />
      </Card>

      <ResetPasswordModal
        user={resetFor}
        open={resetFor !== null}
        onClose={() => setResetFor(null)}
      />
    </div>
  );
}

export const Route = createFileRoute("/users")({
  component: Users,
});
