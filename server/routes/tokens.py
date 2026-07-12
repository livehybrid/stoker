"""API-token management: non-interactive bearer credentials for CI/CD.

:data:`router` (prefix ``/api/tokens``) is **admin only**: every handler depends
on :func:`server.auth.require_admin`, and the app.py middleware additionally gates
``/api/tokens`` to the admin role (defence in depth). A token authenticates a
machine caller as a transient principal with the token's role (see
:func:`server.auth.resolve_user`); managing tokens is a strictly higher privilege
than holding one.

The plaintext secret is returned exactly **once**, from ``POST /api/tokens``
(:class:`~server.schemas.ApiTokenCreated`). Only its sha256 hash is stored, so it
is unrecoverable afterwards; listings (:class:`~server.schemas.ApiTokenOut`) carry
metadata only, never the secret or its hash. Deletion is a **soft-revoke** (sets
``revoked_at``) so the audit row survives. No secret is ever logged.
"""

from __future__ import annotations

import datetime
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..db import get_db
from ..models import ApiToken, User, utcnow
from ..schemas import ApiTokenCreate, ApiTokenCreated, ApiTokenOut

log = logging.getLogger("stoker.routes.tokens")

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


@router.post("", response_model=ApiTokenCreated, status_code=201)
def create_token(
    body: ApiTokenCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Create an API token (admin only); return the secret exactly once.

    409 on a duplicate name. The plaintext ``token`` in the response is the only
    time it exists (only its hash is stored, so it cannot be retrieved later).
    """
    name = body.name.strip()
    existing = db.execute(
        select(ApiToken).where(ApiToken.name == name)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="a token named %r already exists" % name)

    secret, token_hash, token_prefix = auth.generate_api_token()

    expires_at = None  # type: object
    if body.expires_in_days is not None:
        expires_at = utcnow() + datetime.timedelta(days=body.expires_in_days)

    token = ApiToken(
        name=name,
        token_hash=token_hash,
        token_prefix=token_prefix,
        role=body.role,
        created_by=admin.username,
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    # Never log the secret; the prefix is a safe, non-credential label.
    log.info("admin %r created API token %r (role=%s, prefix=%s)",
             admin.username, token.name, token.role, token.token_prefix)
    return ApiTokenCreated(
        id=token.id,
        name=token.name,
        role=token.role,
        token=secret,
        prefix=token.token_prefix,
        created_at=token.created_at,
        expires_at=token.expires_at,
    )


@router.get("", response_model=List[ApiTokenOut])
def list_tokens(
    db: Session = Depends(get_db),
    _admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """List all tokens (admin only). Metadata only: no secret or hash appears
    here by construction, so a listing can never leak a usable credential."""
    return list(db.execute(select(ApiToken).order_by(ApiToken.id)).scalars().all())


@router.delete("/{token_id}", status_code=204)
def revoke_token(
    token_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Soft-revoke a token (admin only): set ``revoked_at``, keep the audit row.

    Idempotent: revoking an already-revoked token is a no-op that still returns
    204. 404 on an unknown id. A revoked token no longer authenticates.
    """
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="unknown token")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        db.commit()
        log.info("admin %r revoked API token %r (id=%s)",
                 admin.username, token.name, token.id)
    return Response(status_code=204)


__all__ = ["router"]
