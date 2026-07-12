import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import type { RepoCreated } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Button } from "../components/Button";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { Modal } from "../features/ui/Modal";
import { RegisterRepoForm } from "../features/repos/RegisterRepoForm";
import { WebhookSecretReveal } from "../features/repos/WebhookSecretReveal";
import { RepoCard } from "../features/repos/RepoCard";

// Repos page: register git repos of sample packs, sync them, and drill into the
// packs they index. Cards show sync state, head SHA and the trusted-code flag
// (design section 10.4). A newly-registered repo's webhook secret is surfaced
// once via a non-dismissible reveal.
function Repos() {
  const [registerOpen, setRegisterOpen] = useState(false);
  const [revealed, setRevealed] = useState<RepoCreated | null>(null);

  const q = useQuery({ queryKey: ["repos"], queryFn: () => api.repos.list() });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Repos"
        subtitle="Git repositories of eventgen sample packs."
        actions={
          <Button variant="primary" onClick={() => setRegisterOpen(true)}>
            Register repo
          </Button>
        }
      />

      {q.isPending ? (
        <LoadingState />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : q.data.length === 0 ? (
        <EmptyState
          title="No repositories registered"
          message="Register a git repo to index its eventgen sample packs, then create jobs from them."
          action={
            <Button variant="primary" onClick={() => setRegisterOpen(true)}>
              Register your first repo
            </Button>
          }
        />
      ) : (
        <div className="space-y-3">
          {q.data.map((repo) => (
            <RepoCard key={repo.id} repo={repo} onDeleted={() => q.refetch()} />
          ))}
        </div>
      )}

      <Modal
        open={registerOpen}
        onClose={() => setRegisterOpen(false)}
        title="Register repository"
      >
        <RegisterRepoForm
          onCancel={() => setRegisterOpen(false)}
          onCreated={(repo) => {
            setRegisterOpen(false);
            // Only pop the reveal when the server actually returned a secret.
            if (repo.webhook_secret) {
              setRevealed(repo);
            }
          }}
        />
      </Modal>

      <Modal
        open={revealed !== null}
        onClose={() => setRevealed(null)}
        title="Webhook secret — copy it now"
        dismissible={false}
        footer={
          <Button variant="primary" onClick={() => setRevealed(null)}>
            I have copied it
          </Button>
        }
      >
        {revealed && (
          <WebhookSecretReveal
            url={revealed.url}
            secret={revealed.webhook_secret ?? ""}
          />
        )}
      </Modal>
    </div>
  );
}

export const Route = createFileRoute("/repos")({
  component: Repos,
});
