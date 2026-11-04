import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { Modal } from "../ui/Modal";
import { Muted, Stack } from "../../components/text";

/*
 * Confirm, then delete one or more finished runs.
 *
 * Used by the run page (one run) and the Runs list (the selected rows). Runs
 * are deleted one request at a time so a refusal on one (it was restarted, or
 * someone else already deleted it) is reported against that run and the rest
 * still go. Deleting a run takes its leases, metric samples and events with
 * it; the spec stays, so Re-run from another run of the same spec still works.
 */
export function DeleteRunsModal({
  ids,
  open,
  onClose,
  onDeleted,
}: {
  ids: number[];
  open: boolean;
  onClose: () => void;
  /** Called with the ids that were actually deleted. */
  onDeleted?: (deleted: number[]) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    const deleted: number[] = [];
    const failed: string[] = [];
    for (const id of ids) {
      try {
        await api.runs.delete(id);
        deleted.push(id);
      } catch (e) {
        failed.push(`#${id}: ${e instanceof Error ? e.message : "failed"}`);
      }
    }
    setBusy(false);
    for (const id of deleted) qc.removeQueries({ queryKey: ["run", id] });
    qc.invalidateQueries({ queryKey: ["runs"] });
    if (deleted.length) {
      toast.success(
        deleted.length === 1 ? `Run #${deleted[0]} deleted` : `${deleted.length} runs deleted`,
      );
    }
    if (failed.length) toast.error(`Not deleted: ${failed.join("; ")}`);
    onDeleted?.(deleted);
    onClose();
  }

  const label = ids.length === 1 ? `run #${ids[0]}` : `${ids.length} runs`;
  return (
    <Modal
      open={open && ids.length > 0}
      onClose={() => !busy && onClose()}
      title={ids.length === 1 ? `Delete run #${ids[0]}` : `Delete ${ids.length} runs`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" onClick={run} disabled={busy}>
            {busy ? "Deleting…" : "Delete"}
          </Button>
        </>
      }
    >
      <Stack>
        <span>
          Delete {label}? Its totals, metrics, lease roster and event log are removed
          for good.
        </span>
        <Muted $small>The spec is kept. Runs that are still live cannot be deleted.</Muted>
      </Stack>
    </Modal>
  );
}
