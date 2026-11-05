import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Modal } from "../ui/Modal";
import { Field, TextInput } from "../../components/Field";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { Muted, Negative, Stack } from "../../components/text";

// Rename a local pack. Specs and runs point at the pack's id, so they follow
// the rename without being touched. The server also rewrites the name in the
// pack's own files and, when the pack source is writable, moves its archive
// there; if the bucket cannot follow, the toast says the old copy is still
// there (it would come back under the old name at the next restart).
interface Props {
  pack: PackOut;
  open: boolean;
  onClose: () => void;
}

export function RenamePackModal({ pack, open, onClose }: Props) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState(pack.name);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName(pack.name);
      setError(null);
    }
  }, [open, pack.name]);

  const rename = useMutation({
    mutationFn: (next: string) => api.packs.rename(pack.id, next),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["packs"] });
      onClose();
      if (r.warning) {
        toast.error(`Renamed "${r.old_name}" to "${r.name}", but ${r.warning}`);
      } else {
        toast.success(`Renamed "${r.old_name}" to "${r.name}"`);
      }
    },
    onError: (e) => setError(e instanceof Error ? e.message : "Rename failed"),
  });

  const trimmed = name.trim();
  const unchanged = trimmed === pack.name;

  function submit() {
    if (!trimmed) {
      setError("A pack needs a name");
      return;
    }
    if (unchanged) {
      onClose();
      return;
    }
    setError(null);
    rename.mutate(trimmed);
  }

  return (
    <Modal
      open={open}
      onClose={() => !rename.isPending && onClose()}
      title={`Rename pack ${pack.name}`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={rename.isPending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={submit}
            disabled={rename.isPending || !trimmed || unchanged}
          >
            {rename.isPending ? "Renaming…" : "Rename"}
          </Button>
        </>
      }
    >
      <Stack $gap="medium">
        <Field label="New name">
          <TextInput
            value={name}
            onChange={(_e, { value }) => setName(value)}
            onKeyDown={(e: React.KeyboardEvent) => {
              if (e.key === "Enter") submit();
            }}
            autoFocus
          />
        </Field>
        <Muted $small>
          Jobs and past runs that use this pack keep using it under the new name.
        </Muted>
        {error && <Negative>{error}</Negative>}
      </Stack>
    </Modal>
  );
}
