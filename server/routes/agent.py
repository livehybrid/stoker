"""Agent-facing API (``/api/agent``): the worker's wire protocol.

Every request carries ``Authorization: Bearer <per-run JWT>``; the dependency
:func:`require_run_jwt` decodes it, checks the ``run_id`` claim equals the path
run id and that it is unexpired, else 401. The endpoint bodies delegate to
:mod:`server.lifecycle` (the Core builder fills those); the JWT dependency,
bundle streaming and response shaping live here and are complete.

Shapes are pinned to ``docs/WORKER-CONTRACT.md`` and the worker's
``control.py`` / ``slice.py``. Do not change field names or status codes here
without updating the worker.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, lifecycle
from ..db import get_db
from ..models import Bundle, Run
from ..schemas import (
    ClaimRequest,
    FinalRequest,
    HeartbeatCommand,
    HeartbeatRequest,
    ReadyRequest,
    SpecSliceOut,
)

log = logging.getLogger("stoker.routes.agent")

router = APIRouter(prefix="/api/agent", tags=["agent"])


# --------------------------------------------------------------------------- #
# Bearer authentication (implemented — shared by every agent endpoint).
# --------------------------------------------------------------------------- #

def _extract_bearer(authorization):
    # type: (Optional[str]) -> str
    if not authorization:
        raise HTTPException(status_code=401, detail="missing bearer token")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="malformed Authorization header")
    return parts[1].strip()


def require_run_jwt(
    run_id: int = Path(...),
    authorization: Optional[str] = Header(default=None),
):
    # type: (...) -> Dict[str, Any]
    """FastAPI dependency: verify the run JWT and return its claims.

    401 on a missing/malformed header, a bad signature, an expired token, or a
    ``run_id`` claim that does not match the path. The claims are returned so a
    handler can inspect ``kid`` etc.; the raw token is available separately via
    :func:`bearer_token` when a handler needs to consider a rolling refresh.
    """
    token = _extract_bearer(authorization)
    try:
        return crypto.verify_run_jwt(token, run_id)
    except crypto.JWTError as exc:
        # No token material in the log; only the reason.
        log.info("agent auth rejected for run %s: %s", run_id, exc)
        raise HTTPException(status_code=401, detail="invalid run token")


def bearer_token(authorization: Optional[str] = Header(default=None)):
    # type: (Optional[str]) -> str
    """Dependency returning the raw bearer token (for JWT-refresh decisions)."""
    return _extract_bearer(authorization)


def _load_run(db, run_id):
    # type: (Session, int) -> Run
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return run


# --------------------------------------------------------------------------- #
# Protocol endpoints (bodies delegate to lifecycle — Core builder fills).
# --------------------------------------------------------------------------- #

@router.post("/runs/{run_id}/claim", response_model=SpecSliceOut)
def claim(
    run_id: int,
    body: ClaimRequest,
    db: Session = Depends(get_db),
    _claims: Dict[str, Any] = Depends(require_run_jwt),
):
    # type: (...) -> Any
    """Claim a free lease; return the spec slice the worker generates against.

    Issues the lowest free lease (honouring ``hint_slot``), records the holder,
    sets state ``claimed``, stamps the heartbeat clock and ``effective_t0``, and
    returns the slice built by :func:`lifecycle.build_slice`.
    """
    run = _load_run(db, run_id)
    lease = lifecycle.claim_lease(db, run, body.holder, hint_slot=body.hint_slot)
    slice_doc = lifecycle.build_slice(run, lease)
    db.commit()
    return slice_doc


@router.post("/runs/{run_id}/ready")
def ready(
    run_id: int,
    body: ReadyRequest,
    db: Session = Depends(get_db),
    _claims: Dict[str, Any] = Depends(require_run_jwt),
):
    # type: (...) -> Any
    """Mark a lease ready. 409 when ``lease_id`` is not the slot holder.

    When all leases are ready (or the provisioning timeout has fired) the run's
    T0 is set and it moves to releasing/running.
    """
    run = _load_run(db, run_id)
    lifecycle.mark_ready(db, run, body.slot, body.lease_id)
    db.commit()
    return {}


@router.post("/runs/{run_id}/heartbeat", response_model=HeartbeatCommand,
             response_model_exclude_none=True)
def heartbeat(
    run_id: int,
    body: HeartbeatRequest,
    db: Session = Depends(get_db),
    _claims: Dict[str, Any] = Depends(require_run_jwt),
    token: str = Depends(bearer_token),
):
    # type: (...) -> Any
    """Process a heartbeat; return the command (continue/release/retarget/drain/
    superseded), possibly carrying a rolling ``jwt`` refresh.

    Renews the lease and appends the counters to ``metric_samples``. A
    ``superseded`` lease still returns 200 with ``{"command":"superseded"}`` (the
    worker treats that as a fatal drain), matching the worker's expectation.
    """
    run = _load_run(db, run_id)
    payload = body.model_dump()
    # The raw bearer is threaded in under "_bearer" so record_heartbeat can call
    # lifecycle.maybe_refresh_jwt(run, payload["_bearer"]) and attach a rolling
    # "jwt" to the command. It is popped before the counters hit metric_samples
    # and is never logged.
    payload["_bearer"] = token
    command = lifecycle.record_heartbeat(db, run, body.slot, body.lease_id, payload)
    db.commit()
    return command


@router.post("/runs/{run_id}/final")
def final(
    run_id: int,
    body: FinalRequest,
    db: Session = Depends(get_db),
    _claims: Dict[str, Any] = Depends(require_run_jwt),
):
    # type: (...) -> Any
    """Record a worker's final report; finalise its lease.

    409 when ``lease_id``-less slot is not resolvable to a live lease is left to
    the lifecycle implementation; on success returns ``{}``.
    """
    run = _load_run(db, run_id)
    lifecycle.record_final(db, run, body.slot, body.summary, body.log_tail,
                           lease_id=body.lease_id)
    db.commit()
    return {}


@router.get("/bundles/{digest}.tgz")
def download_bundle(
    digest: str,
    request: Request,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
):
    # type: (...) -> Any
    """Stream a bundle tarball to a worker (JWT-checked).

    The bearer must be a valid run JWT whose run references this bundle digest;
    otherwise 401/403. 404 when the digest is unknown or its file is missing.
    This is fully implemented (foundation): bundle delivery is on the critical
    path for the walking skeleton.
    """
    token = _extract_bearer(authorization)
    bundle = db.execute(
        select(Bundle).where(Bundle.digest == digest)
    ).scalars().first()
    if bundle is None:
        raise HTTPException(status_code=404, detail="unknown bundle")

    # The token authorises exactly one run; that run must reference this bundle.
    try:
        claims = crypto.decode_run_jwt(token)
    except crypto.JWTError as exc:
        log.info("bundle auth rejected for %s: %s", digest[:12], exc)
        raise HTTPException(status_code=401, detail="invalid run token")
    run_id = claims.get("run_id")
    run = db.get(Run, int(run_id)) if run_id is not None else None
    if run is None:
        raise HTTPException(status_code=401, detail="token run not found")
    if run.bundle_id != bundle.id and run.resolved_sha != digest:
        raise HTTPException(status_code=403, detail="run does not reference this bundle")

    if not bundle.path or not os.path.isfile(bundle.path):
        log.error("bundle %s row present but file missing at %s", digest[:12], bundle.path)
        raise HTTPException(status_code=404, detail="bundle file missing")
    return FileResponse(
        bundle.path,
        media_type="application/gzip",
        filename="%s.tgz" % digest,
    )


__all__ = ["router", "require_run_jwt", "bearer_token"]
