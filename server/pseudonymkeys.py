"""The instance key behind consistent pseudonymisation.

One key per Stoker instance, named ``default``, created the first time an
operator saves a pack with a pseudonym field. It is what makes stand-ins
consistent **across packs**: two packs built here give the same stand-in for the
same identifier, so a correlation search spanning both works. A per-save random
salt would be simpler and would satisfy one pack in isolation, but it could
never do that, and it would make re-opening a pack to add events impossible.

Storage mirrors ``targets.token_encrypted`` exactly: the raw 32 bytes are Fernet
ciphertext under the control plane's master key, and no schema serialises them.
What travels in a pack is the ``fingerprint`` only (the first 8 hex of the key's
SHA-256), which identifies the key without being usable as one.

Two refusals, both deliberate:

* **No key is created while the master key is auto-generated.** A dev instance
  with no ``STOKER_MASTER_KEY`` mints an ephemeral one at boot, so a key stored
  under it is unreadable after the next restart - and the packs built with it
  would then be orphaned, their stand-ins unreproducible. Better to refuse the
  save and say so.
* **Reading never creates.** The endpoint that reports whether a key exists must
  not bring one into being as a side effect of someone opening a page.

Losing the row (a restore from before the first pseudonymising save, or a master
key rotation) does not break existing packs - their stand-ins are already
written - but new packs will not correlate with them, and that cannot be undone.
Migration 0006's downgrade therefore keeps the table.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto
from .packbuilder import pseudonym as ps
from .models import PseudonymKey

log = logging.getLogger("stoker.pseudonymkeys")

DEFAULT_NAME = "default"
KEY_BYTES = 32


class PseudonymKeyError(RuntimeError):
    """The key cannot be created or read, with an operator-facing reason."""


def peek(db, name=DEFAULT_NAME):
    # type: (Session, str) -> Optional[PseudonymKey]
    """The key row if it exists. Never creates one (see the module docstring)."""
    return db.execute(
        select(PseudonymKey).where(PseudonymKey.name == name)).scalars().first()


def _settings():
    # type: () -> Any
    from .config import get_settings

    return get_settings()


def ensure(db, name=DEFAULT_NAME, actor=None, settings=None):
    # type: (Session, str, Optional[str], Optional[Any]) -> PseudonymKey
    """The key row, creating it on first use.

    Raises :class:`PseudonymKeyError` when the master key is ephemeral, because
    a key written under it is unreadable after a restart and would orphan every
    pack built with it.
    """
    existing = peek(db, name)
    if existing is not None:
        return existing
    if settings is None:
        settings = _settings()
    if getattr(settings, "master_key_generated", False):
        raise PseudonymKeyError(
            "refusing to create a pseudonym key while STOKER_MASTER_KEY is "
            "auto-generated: the key would be unreadable after the next restart "
            "and every pack built with it would be orphaned. Set "
            "STOKER_MASTER_KEY (or STOKER_MASTER_KEY_FILE) first.")
    raw = os.urandom(KEY_BYTES)
    row = PseudonymKey(
        name=name,
        key_encrypted=crypto.encrypt(raw.hex(), settings=settings),
        fingerprint=ps.fingerprint(raw),
        algorithm=ps.ALGORITHM,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    log.info("created pseudonym key %r (fingerprint %s) for %s",
             name, row.fingerprint, actor or "an operator")
    return row


def raw_key(row, settings=None):
    # type: (PseudonymKey, Optional[Any]) -> bytes
    """Decrypt a key row to its raw bytes."""
    if settings is None:
        settings = _settings()
    try:
        return bytes.fromhex(crypto.decrypt(row.key_encrypted, settings=settings))
    except Exception as exc:  # noqa: BLE001 - any failure is the same outcome
        raise PseudonymKeyError(
            "the pseudonym key cannot be decrypted (%s). It was stored under a "
            "different master key, so packs built with it cannot be reproduced; "
            "restore the master key, or delete the key row to start a new one "
            "(packs built before will no longer correlate with packs built "
            "after)." % exc.__class__.__name__)


def subkey(db, name=DEFAULT_NAME, actor=None, settings=None, create=True):
    # type: (Session, str, Optional[str], Optional[Any], bool) -> Tuple[bytes, PseudonymKey]
    """``(derived subkey, row)`` ready for :func:`pseudonym.pseudonym`.

    With ``create`` false an absent key raises instead of being created, which
    is what a read-only caller (a preview of a pack with no pseudonym field, a
    status endpoint) wants.
    """
    row = ensure(db, name, actor=actor, settings=settings) if create else peek(db, name)
    if row is None:
        raise PseudonymKeyError("no pseudonym key exists on this instance yet")
    return ps.derive_subkey(raw_key(row, settings=settings)), row


def describe(row):
    # type: (Optional[PseudonymKey]) -> dict
    """The public, non-secret view of the key for an API response."""
    if row is None:
        return {"exists": False, "name": DEFAULT_NAME, "fingerprint": None,
                "algorithm": ps.ALGORITHM, "created_at": None}
    return {
        "exists": True,
        "name": row.name,
        "fingerprint": row.fingerprint,
        "algorithm": row.algorithm,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


__all__ = ["DEFAULT_NAME", "KEY_BYTES", "PseudonymKeyError", "describe", "ensure",
           "peek", "raw_key", "subkey"]
