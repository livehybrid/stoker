import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import type { UserOut } from "../../lib/types";
import { Modal } from "../ui/Modal";
import { Field, TextInput } from "../../components/Field";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { Negative, Stack } from "../../components/text";

// Reset a user's password (admin action). The new password is write-only: it is
// PATCHed and never read back. Setting a password also flips a proxy/SSO account
// to a local credential (server-side), which the toast notes implicitly.
export function ResetPasswordModal({
  user,
  open,
  onClose,
}: {
  user: UserOut | null;
  open: boolean;
  onClose: () => void;
}) {
  const toast = useToast();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reset = useMutation({
    mutationFn: (pw: string) =>
      api.users.update(user!.id, { password: pw }),
    onSuccess: () => {
      toast.success(`Password reset for “${user?.username}”.`);
      close();
    },
    onError: (err: unknown) => {
      setError(err instanceof ApiError ? err.message : "Could not reset the password.");
    },
  });

  function close() {
    setPassword("");
    setConfirm("");
    setError(null);
    onClose();
  }

  function submit() {
    setError(null);
    if (password.length < 8) return setError("Use at least 8 characters.");
    if (password !== confirm) return setError("The passwords do not match.");
    reset.mutate(password);
  }

  return (
    <Modal
      open={open && user !== null}
      onClose={close}
      title={user ? `Reset password — ${user.username}` : "Reset password"}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={reset.isPending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={reset.isPending}>
            {reset.isPending ? "Saving…" : "Set new password"}
          </Button>
        </>
      }
    >
      <Stack $gap="medium">
        <Field label="New password" hint="At least 8 characters. Write-only.">
          <TextInput
            type="password"
            value={password}
            onChange={(_e, { value }) => setPassword(value)}
            autoComplete="new-password"
            autoFocus
          />
        </Field>
        <Field label="Confirm password">
          <TextInput
            type="password"
            value={confirm}
            onChange={(_e, { value }) => setConfirm(value)}
            autoComplete="new-password"
          />
        </Field>
        {error && <Negative>{error}</Negative>}
      </Stack>
    </Modal>
  );
}
