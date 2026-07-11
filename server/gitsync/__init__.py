"""Git repo sync for sample packs (Phase 1, stage 3).

The control plane manages repositories of eventgen sample packs. A repo is
cloned into the control-plane volume, its pack roots are walked, each pack is
linted and indexed as a :class:`~server.models.Pack` row, and a JobSpec's ``ref``
resolves to a concrete SHA at run start (pinning). Credentials (a PAT or an SSH
deploy key) are entered write-only, Fernet-encrypted, and never leave the
control plane; they are never placed on a subprocess ``argv`` and never logged.

Public surface (see :mod:`server.gitsync.sync`):

* :func:`clone_or_fetch` — shallow clone or fetch a repo at its default ref and
  resolve that ref to a head SHA.
* :func:`index_packs` — walk a clone for pack roots, synthesise ``pack.yaml``
  when absent, lint, enforce the custom-code default-deny, and upsert Pack rows.
* :func:`sync_repo` — the full cycle (clone/fetch then index), updating the repo
  row's head SHA / last-synced / sync-error.
* :func:`resolve_pack_dir` — the local directory of a pack at its indexed SHA,
  for the bundle builder (reuses the clone).
* :class:`GitSyncError` — a clear, secret-free failure surfaced to ``sync_error``.
"""

from __future__ import annotations

from .sync import (
    GitSyncError,
    clone_or_fetch,
    index_packs,
    resolve_pack_dir,
    sync_repo,
)

__all__ = [
    "GitSyncError",
    "clone_or_fetch",
    "index_packs",
    "resolve_pack_dir",
    "sync_repo",
]
