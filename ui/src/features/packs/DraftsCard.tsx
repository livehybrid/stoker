import { Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackDraftOut } from "../../lib/types";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { useToast } from "../../components/Toast";
import { relativeTime } from "../format";
import { Between, Inline, Muted, Stack, Strong } from "../../components/text";

// Pack builder sessions saved before they build. They are not packs: nothing
// can run them, and the server deletes one when the pack it becomes is saved.
// Hidden when there are none, so the page is unchanged for anyone not using
// them.

export function DraftsCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const draftsQ = useQuery({ queryKey: ["pack-builder", "drafts"], queryFn: api.packBuilder.drafts });

  const del = useMutation({
    mutationFn: (d: PackDraftOut) => api.packBuilder.deleteDraft(d.id),
    onSuccess: (_r, d) => {
      toast.success(`Draft "${d.name || "untitled"}" discarded`);
      qc.invalidateQueries({ queryKey: ["pack-builder", "drafts"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Discard failed"),
  });

  const drafts = draftsQ.data ?? [];
  if (drafts.length === 0) return null;

  return (
    <Card title="Drafts">
      <Stack $gap="small">
        <Muted $small>
          Packs saved part-way through. A draft keeps your original sample (encrypted) until it is
          built, and is removed once the pack is saved.
        </Muted>
        {drafts.map((d) => (
          <Between key={d.id}>
            <div>
              <Strong>{d.name || "Untitled draft"}</Strong>{" "}
              <Muted $small>
                {d.pack_id !== null ? "editing an existing pack · " : ""}saved {relativeTime(d.updated_at)}
                {d.created_by ? ` by ${d.created_by}` : ""}
              </Muted>
            </div>
            <Inline>
              <Link
                to="/pack-builder"
                search={d.pack_id !== null ? { edit: d.pack_id, draft: d.id } : { draft: d.id }}
              >
                <Button variant="secondary">Continue</Button>
              </Link>
              <Button
                variant="secondary"
                disabled={del.isPending}
                onClick={() => {
                  if (window.confirm(`Discard draft ${d.name || "untitled"}? This cannot be undone.`)) {
                    del.mutate(d);
                  }
                }}
              >
                Discard
              </Button>
            </Inline>
          </Between>
        ))}
      </Stack>
    </Card>
  );
}
