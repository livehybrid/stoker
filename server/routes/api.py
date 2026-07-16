"""Operator API (``/api``): targets, packs, specs, runs.

Unauthenticated behind the Traefik LAN allowlist this stage (users/roles arrive
in stage 3). Every endpoint from the contract is present here with its correct
path, method and pydantic types. Bodies delegate to :mod:`server.bundles`,
:mod:`server.lifecycle` and the model layer.

Response models never include secret fields (a test asserts no token leaks in
any GET body): a target's HEC token lives only in ``token_encrypted`` (Fernet
ciphertext) and is decrypted transiently for ``/targets/{id}/test`` and when a
run is provisioned; it is never serialised and never logged.
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bundles, crypto, gitsync, lifecycle, preview
from ..db import get_db
from ..drivers import get_driver
from ..drivers.base import DriverError
from ..engines import ceilings
from ..models import (
    MetricSample,
    Pack,
    Repo,
    Run,
    RunEvent,
    Spec,
    Target,
    WorkerLease,
    utcnow,
)
from ..schemas import (
    BackfillEstimate,
    BackfillEstimateRequest,
    MetricSampleOut,
    MetricsOut,
    PackCreate,
    PackOut,
    PackPreview,
    PackPreviewRun,
    RepoCreate,
    RepoCreated,
    RepoOut,
    RepoSyncResult,
    RescaleRequest,
    RunCreated,
    RunDetail,
    RunEventOut,
    RunLaunch,
    RunLogsOut,
    RunOut,
    ScaleRequest,
    SpecCreate,
    SpecEstimate,
    SpecOut,
    SpecUpdate,
    StopRequest,
    TargetCreate,
    TargetOut,
    TargetTestResult,
    TargetUpdate,
)

log = logging.getLogger("stoker.routes.api")

router = APIRouter(prefix="/api", tags=["operator"])

# HTTP timeout for the on-demand target probe (short; this is a liveness check,
# not a data path). The background health loop is deferred (contract).
_PROBE_TIMEOUT_S = 8.0

# Health-state values mirrored onto the target row from a probe outcome.
_HEALTH_GREEN = "green"
_HEALTH_AMBER = "amber"
_HEALTH_RED = "red"


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #

@router.post("/targets", response_model=TargetOut, status_code=201)
def create_target(body: TargetCreate, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Create a target; the HEC token is Fernet-encrypted at rest, never echoed."""
    existing = db.execute(
        select(Target).where(Target.name == body.name)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="a target named %r already exists" % body.name)
    try:
        token_ct = crypto.encrypt(body.token)
    except crypto.CryptoError as exc:
        # Do not surface the token; only that encryption failed.
        raise HTTPException(status_code=500, detail="could not encrypt target token: %s" % exc)
    target = Target(
        name=body.name,
        hec_url=body.hec_url.rstrip("/"),
        token_encrypted=token_ct,
        default_index=body.default_index,
        env_tag=body.env_tag,
        max_concurrent_gb_day=body.max_concurrent_gb_day,
        verify_tls=body.verify_tls,
        health_state="unknown",
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    log.info("created target %s (id=%s) -> %s", target.name, target.id, target.hec_url)
    return target


@router.get("/targets", response_model=List[TargetOut])
def list_targets(db: Session = Depends(get_db)):
    # type: (...) -> Any
    """List targets (no token fields by construction)."""
    return list(db.execute(select(Target).order_by(Target.id)).scalars().all())


@router.get("/targets/{target_id}", response_model=TargetOut)
def get_target(target_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown target")
    return target


@router.patch("/targets/{target_id}", response_model=TargetOut)
def update_target(target_id: int, body: TargetUpdate, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Partially update a target. Only fields present in the body change. The HEC
    token is re-encrypted when a non-empty value is supplied (write-only, never
    echoed); an omitted or empty token keeps the stored one. Changing the
    endpoint, token or TLS setting resets health to ``unknown`` (re-run /test)."""
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown target")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return target

    new_name = fields.get("name")
    if new_name is not None and new_name != target.name:
        clash = db.execute(
            select(Target).where(Target.name == new_name, Target.id != target_id)
        ).scalars().first()
        if clash is not None:
            raise HTTPException(
                status_code=409, detail="a target named %r already exists" % new_name)
        target.name = new_name

    conn_changed = False
    if fields.get("hec_url"):
        target.hec_url = fields["hec_url"].rstrip("/")
        conn_changed = True
    if "token" in fields and fields["token"]:  # non-empty -> rotate; empty -> keep
        try:
            target.token_encrypted = crypto.encrypt(fields["token"])
        except crypto.CryptoError as exc:
            raise HTTPException(
                status_code=500, detail="could not encrypt target token: %s" % exc)
        conn_changed = True
    if "default_index" in fields:
        target.default_index = fields["default_index"]
    if fields.get("env_tag"):
        target.env_tag = fields["env_tag"]
    if "max_concurrent_gb_day" in fields:
        target.max_concurrent_gb_day = fields["max_concurrent_gb_day"]
    if "verify_tls" in fields and fields["verify_tls"] is not None:
        target.verify_tls = fields["verify_tls"]
        conn_changed = True

    if conn_changed:
        target.health_state = "unknown"
        target.health_detail = None

    db.commit()
    db.refresh(target)
    log.info("updated target %s (id=%s)", target.name, target.id)
    return target


@router.post("/targets/{target_id}/test", response_model=TargetTestResult)
def test_target(target_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Probe ``/services/collector/health`` + an auth ping; update health_state."""
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown target")

    token = None  # type: Optional[str]
    if target.token_encrypted:
        try:
            token = crypto.decrypt(target.token_encrypted)
        except crypto.CryptoError as exc:
            # Cannot decrypt (e.g. master key rotated) -> red, but never echo it.
            _apply_health(target, _HEALTH_RED, "cannot decrypt stored token: %s" % exc)
            db.commit()
            return TargetTestResult(ok=False, detail="stored token could not be decrypted")

    result = _probe_target(target.hec_url, token, target.verify_tls)
    new_state = _health_from_probe(result)
    _apply_health(target, new_state, result.detail)
    db.commit()
    return result


@router.delete("/targets/{target_id}", status_code=204)
def delete_target(target_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Delete a target (guarded when referenced by a spec)."""
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown target")
    ref = db.execute(
        select(Spec.id).where(Spec.target_id == target_id).limit(1)
    ).scalars().first()
    if ref is not None:
        raise HTTPException(
            status_code=409,
            detail="target %s is referenced by one or more specs; delete those first" % target_id,
        )
    db.delete(target)
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Repos (git repo sync for sample packs)
# --------------------------------------------------------------------------- #

_REPO_AUTH_KINDS = ("none", "pat", "deploy_key")


@router.post("/repos", response_model=RepoCreated, status_code=201)
def create_repo(body: RepoCreate, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Register a git repo; the credential is Fernet-encrypted, never echoed.

    ``auth_kind`` selects how the credential is applied (none | pat | deploy_key).
    A per-repo ``webhook_secret`` is generated and returned once so the operator
    can configure the GitHub push webhook; it is not returned on later GETs.
    """
    if body.auth_kind not in _REPO_AUTH_KINDS:
        raise HTTPException(
            status_code=422,
            detail="auth_kind must be one of %s" % ", ".join(_REPO_AUTH_KINDS))
    if body.auth_kind in ("pat", "deploy_key") and not body.secret:
        raise HTTPException(
            status_code=422,
            detail="auth_kind %r requires a secret (write-only credential)" % body.auth_kind)

    secret_ct = None  # type: Optional[str]
    if body.secret:
        try:
            secret_ct = crypto.encrypt(body.secret)
        except crypto.CryptoError as exc:
            # Never surface the secret; only that encryption failed.
            raise HTTPException(
                status_code=500, detail="could not encrypt repo credential: %s" % exc)

    webhook_secret = secrets.token_hex(32)
    repo = Repo(
        url=body.url.strip(),
        auth_kind=body.auth_kind,
        secret_encrypted=secret_ct,
        default_ref=(body.default_ref or "main").strip() or "main",
        webhook_secret=webhook_secret,
        trusted_code=bool(body.trusted_code),
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    log.info("registered repo %s (id=%s) auth=%s trusted_code=%s",
             repo.url, repo.id, repo.auth_kind, repo.trusted_code)
    out = _repo_view(repo, RepoCreated)
    out.webhook_secret = webhook_secret
    return out


@router.get("/repos", response_model=List[RepoOut])
def list_repos(db: Session = Depends(get_db)):
    # type: (...) -> Any
    """List repos (no credential fields by construction)."""
    repos = list(db.execute(select(Repo).order_by(Repo.id)).scalars().all())
    return [_repo_view(r, RepoOut) for r in repos]


@router.get("/repos/{repo_id}", response_model=RepoOut)
def get_repo(repo_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="unknown repo")
    return _repo_view(repo, RepoOut)


@router.delete("/repos/{repo_id}", status_code=204)
def delete_repo(repo_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Delete a repo (guarded when a pack it indexed is referenced by a spec).

    Packs indexed from the repo are removed with it, but only if none of them is
    referenced by a spec; otherwise the delete is refused so a running/defined
    job never loses its pack out from under it.
    """
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="unknown repo")

    pack_ids = list(db.execute(
        select(Pack.id).where(Pack.repo_id == repo_id)).scalars().all())
    if pack_ids:
        ref = db.execute(
            select(Spec.id).where(Spec.pack_id.in_(pack_ids)).limit(1)
        ).scalars().first()
        if ref is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "repo %s has packs referenced by one or more specs; delete "
                    "those specs first" % repo_id),
            )
        for pack in db.execute(
                select(Pack).where(Pack.id.in_(pack_ids))).scalars().all():
            db.delete(pack)
    db.delete(repo)
    db.commit()
    return Response(status_code=204)


@router.post("/repos/{repo_id}/sync", response_model=RepoSyncResult)
def sync_repo_endpoint(repo_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Clone/fetch the repo and index its packs; returns the sync counts.

    Rejections: ``404`` unknown repo, ``502 sync_failed`` when git or indexing
    fails (the secret-free reason is stored on ``repo.sync_error`` and returned).
    """
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="unknown repo")
    try:
        result = gitsync.sync_repo(db, repo)
    except gitsync.GitSyncError as exc:
        db.commit()  # persist repo.sync_error recorded by sync_repo
        raise HTTPException(
            status_code=502,
            detail={"error": "sync_failed", "detail": str(exc)})
    db.commit()
    return RepoSyncResult(**result)


@router.post("/hooks/github")
async def github_webhook(request: Request, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """GitHub push webhook: HMAC-verified, then resync the matching repo.

    Unauthenticated (no forward-auth): GitHub cannot present a LAN credential, so
    trust rests entirely on the per-repo HMAC signature over the raw body. The
    matching repo is found by ``webhook_secret`` (each repo has its own), and its
    delivery must carry a valid ``X-Hub-Signature-256``. A ``push`` event triggers
    a resync; other events are acknowledged and ignored. Never reveals whether a
    given secret exists (constant-time compare, uniform responses).
    """
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event", "")

    repo = _match_webhook_repo(db, raw, sig)
    if repo is None:
        # Uniform 401 regardless of whether the signature was malformed or just
        # matched no repo: no oracle for probing secrets.
        raise HTTPException(status_code=401, detail="invalid or unrecognised signature")

    if event and event != "push":
        return {"ok": True, "ignored": event}

    try:
        result = gitsync.sync_repo(db, repo)
    except gitsync.GitSyncError as exc:
        db.commit()
        # 200 with an error field: the webhook was authentic; the sync failed.
        return {"ok": False, "repo_id": repo.id, "error": str(exc)}
    db.commit()
    log.info("webhook resynced repo %s at %s (%d packs)",
             repo.id, (result.get("head_sha") or "")[:12], result.get("packs_indexed", 0))
    return {"ok": True, "repo_id": repo.id, **result}


# --------------------------------------------------------------------------- #
# Packs
# --------------------------------------------------------------------------- #

@router.post("/packs", response_model=PackOut, status_code=201)
def register_pack(body: PackCreate, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Register and lint a local pack directory; set verified/lint_status."""
    source_path = body.source_path
    lint = bundles.lint_pack(source_path)
    pack = Pack(
        name=body.name,
        source_path=source_path,
        description=body.description,
        tags_json=[],
        engines_json=lint.engines,
        # A directory metric pack's validated metricgen becomes its builder config
        # (same downstream path as a UI-authored metric pack); None for others.
        builder_config_json=lint.metricgen,
        sourcetypes_json=lint.sourcetypes,
        stanza_count=lint.stanza_count,
        est_bytes_per_event=lint.est_bytes_per_event,
        declared_per_day_gb=lint.declared_per_day_gb,
        verified=lint.ok,
        lint_status="ok" if lint.ok else "error",
        lint_errors_json=lint.errors,
    )
    db.add(pack)
    db.commit()
    db.refresh(pack)
    log.info("registered pack %s (id=%s) lint=%s stanzas=%d",
             pack.name, pack.id, pack.lint_status, lint.stanza_count)
    return pack


@router.get("/packs", response_model=List[PackOut])
def list_packs(
    repo: Optional[int] = Query(default=None, description="filter to packs indexed from this repo id"),
    repo_id: Optional[int] = Query(default=None, description="alias of 'repo'"),
    db: Session = Depends(get_db),
):
    # type: (...) -> Any
    """List packs, optionally filtered to those indexed from a given repo.

    The filter accepts either ``?repo=<id>`` or its alias ``?repo_id=<id>``
    (``repo`` wins when both are supplied).
    """
    filter_repo = repo if repo is not None else repo_id
    stmt = select(Pack)
    if filter_repo is not None:
        stmt = stmt.where(Pack.repo_id == filter_repo)
    stmt = stmt.order_by(Pack.id)
    return list(db.execute(stmt).scalars().all())


@router.get("/packs/{pack_id}", response_model=PackOut)
def get_pack(pack_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    pack = db.get(Pack, pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="unknown pack")
    return pack


@router.get("/packs/{pack_id}/preview", response_model=PackPreview)
def preview_pack(pack_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Return stanza names plus the first 10 sample lines per stanza."""
    pack = db.get(Pack, pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="unknown pack")
    # A metric pack (UI-authored builder config) has no eventgen source directory
    # and no stanzas/samples; the metric builder owns its own preview. Return an
    # empty, ok preview rather than linting a non-existent directory.
    if pack.builder_config_json is not None:
        errors = bundles.lint_metrics_config(pack.builder_config_json)
        return PackPreview(stanzas=[], sample_lines={},
                           lint_status="ok" if not errors else "error",
                           lint_errors=errors)
    lint = bundles.lint_pack(pack.source_path)
    sample_lines = _preview_sample_lines(pack.source_path, lint.stanzas)
    return PackPreview(
        stanzas=lint.stanzas,
        sample_lines=sample_lines,
        lint_status="ok" if lint.ok else "error",
        lint_errors=lint.errors,
    )


@router.get("/packs/{pack_id}/preview_run", response_model=PackPreviewRun)
def preview_run_pack(
    pack_id: int,
    n: int = Query(default=preview.PREVIEW_N_DEFAULT, description="events to render"),
    db: Session = Depends(get_db),
):
    # type: (...) -> Any
    """Render ``n`` sample events from a pack in-process (no fleet, no HEC).

    A lightweight preview for pack authoring and the wizard: cycles the pack's
    sample lines and applies the common token replacements (timestamp / ipv4 /
    integer) the worker's engine would. Side-effect-free (no network, no
    subprocess); ``n`` is clamped to a sane maximum. Reads only inside the pack
    directory (a sample/mvfile token that would escape the pack root is refused).
    """
    pack = db.get(Pack, pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="unknown pack")
    # Metric packs have no eventgen samples to render; use the metric builder's
    # own preview (/api/metric-packs/preview) instead of this eventgen renderer.
    if pack.builder_config_json is not None:
        return PackPreviewRun(events=[])
    events = preview.preview_pack(pack.source_path, n=n)
    return PackPreviewRun(events=events)


# --------------------------------------------------------------------------- #
# Specs
# --------------------------------------------------------------------------- #

@router.post("/specs", response_model=SpecOut, status_code=201)
def create_spec(body: SpecCreate, db: Session = Depends(get_db)):
    # type: (...) -> Any
    _require_pack(db, body.pack_id)
    _require_target(db, body.target_id)
    _validate_rate(body.rate_mode, body.rate_value)
    if body.workers < 1:
        raise HTTPException(status_code=422, detail="workers must be >= 1")
    spec = Spec(
        name=body.name,
        pack_id=body.pack_id,
        target_id=body.target_id,
        ref=body.ref,
        engine=body.engine,
        overrides_json=body.overrides or None,
        rate_mode=body.rate_mode,
        rate_value=body.rate_value,
        interval_s=body.interval_s,
        workers=body.workers,
        duration_s=body.duration_s,
        fleet=body.fleet,
        strict_release=body.strict_release,
        driver_opts_json=body.driver_opts or None,
    )
    db.add(spec)
    db.commit()
    db.refresh(spec)
    log.info("created spec %s (id=%s) pack=%s target=%s workers=%d",
             spec.name, spec.id, spec.pack_id, spec.target_id, spec.workers)
    return spec


@router.get("/specs", response_model=List[SpecOut])
def list_specs(db: Session = Depends(get_db)):
    # type: (...) -> Any
    return list(db.execute(select(Spec).order_by(Spec.id)).scalars().all())


@router.get("/specs/{spec_id}", response_model=SpecOut)
def get_spec(spec_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    spec = db.get(Spec, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown spec")
    return spec


@router.get("/specs/{spec_id}/estimate", response_model=SpecEstimate)
def estimate_spec(spec_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Per-worker share, pct of ceiling, approx eps/gb and an ok bool."""
    spec = db.get(Spec, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown spec")
    pack = db.get(Pack, spec.pack_id)
    bytes_per_event = pack.est_bytes_per_event if pack is not None else None
    return _estimate(spec, bytes_per_event)


@router.post("/specs/{spec_id}/backfill_estimate", response_model=BackfillEstimate)
def backfill_estimate(spec_id: int, body: BackfillEstimateRequest,
                      db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Size a would-be backfill run (events, ~time, bytes) without launching it."""
    spec = db.get(Spec, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown spec")
    pack = db.get(Pack, spec.pack_id)
    series = 0
    pack_res = None
    if pack is not None and pack.builder_config_json is not None:
        series = bundles.metrics_series_count(pack.builder_config_json)
        pack_res = (pack.builder_config_json or {}).get("resolution_s")
    live_eps = spec.rate_value if spec.rate_mode == "eps" else None
    res = body.resolution_s or pack_res
    plan = lifecycle.plan_backfill(spec.engine, series, live_eps, body.window_s,
                                   res, body.cap_eps, time.time())
    bpe = pack.est_bytes_per_event if pack is not None else None
    return BackfillEstimate(
        engine=spec.engine,
        events=plan["events"],
        bytes=int(plan["events"] * bpe) if bpe else None,
        seconds=round(plan["seconds"], 1),
        cap_eps=plan["cap_eps"],
        deliver_eps=plan["deliver_eps"],
        series=series if spec.engine == "metrics" else None,
        warning=("Re-running a backfill appends duplicate points; mstats will "
                 "double-count. Run once, or clear the window first."),
    )


@router.put("/specs/{spec_id}", response_model=SpecOut)
def update_spec(spec_id: int, body: SpecUpdate, db: Session = Depends(get_db)):
    # type: (...) -> Any
    spec = db.get(Spec, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown spec")
    data = body.model_dump(exclude_unset=True)
    if "pack_id" in data and data["pack_id"] is not None:
        _require_pack(db, data["pack_id"])
    if "target_id" in data and data["target_id"] is not None:
        _require_target(db, data["target_id"])
    # Resolve the effective rate mode/value after the patch for validation.
    new_mode = data.get("rate_mode", spec.rate_mode)
    new_value = data.get("rate_value", spec.rate_value)
    _validate_rate(new_mode, new_value)
    if "workers" in data and data["workers"] is not None and data["workers"] < 1:
        raise HTTPException(status_code=422, detail="workers must be >= 1")

    # Map schema field names to their ORM ``*_json`` columns where they differ.
    column_aliases = {
        "overrides": "overrides_json",
        "driver_opts": "driver_opts_json",
    }
    for key, value in data.items():
        setattr(spec, column_aliases.get(key, key), value)
    db.commit()
    db.refresh(spec)
    return spec


@router.delete("/specs/{spec_id}", status_code=204)
def delete_spec(spec_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    spec = db.get(Spec, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown spec")
    ref = db.execute(
        select(Run.id).where(Run.spec_id == spec_id).limit(1)
    ).scalars().first()
    if ref is not None:
        raise HTTPException(
            status_code=409,
            detail="spec %s has runs; delete or retain them (runs reference the spec)" % spec_id,
        )
    db.delete(spec)
    db.commit()
    return Response(status_code=204)


def _actor(request):
    # type: (Request) -> str
    """The resolved caller for the audit trail (a username or 'token:<name>',
    stashed by the auth middleware). Falls back to 'operator' when absent (the
    bootstrap window or STOKER_AUTH_DISABLED), so attribution degrades safely."""
    return getattr(request.state, "actor", None) or "operator"


@router.post("/specs/{spec_id}/run", response_model=RunCreated, status_code=201)
def run_spec(spec_id: int, body: RunLaunch, request: Request, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Validate + snapshot + bundle + apportion + provision a spec into a run.

    Rejections per the contract:
    ``422 slice_exceeds_ceiling{suggested_workers}``,
    ``409 target_unhealthy``, ``409 target_cap_exceeded{headroom_gb_day}``,
    ``409 replay_single_worker``. On success ``201 {run_id, state}``.
    """
    spec = db.get(Spec, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown spec")
    target = db.get(Target, spec.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="spec target no longer exists")
    pack = db.get(Pack, spec.pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="spec pack no longer exists")

    bytes_per_event = pack.est_bytes_per_event

    # --- Submit-time validation gates (order: cheap/local first) ----------- #

    # 1. Lint ok: the pack must currently lint clean (a stale/broken pack is a
    #    hard stop; 422 with the lint errors so the operator can fix it). A
    #    UI-authored metrics pack has no source directory; lint its stored config.
    if pack.builder_config_json is not None:
        lint_errors = bundles.lint_metrics_config(pack.builder_config_json)
    else:
        lint_errors = bundles.lint_pack(pack.source_path).errors
    if lint_errors:
        raise HTTPException(
            status_code=422,
            detail={"error": "pack_lint_failed", "errors": lint_errors},
        )

    # 1b. Engine/pack consistency: a rawreplay spec needs a rawreplay pack (its
    #     bundle must carry the replay config the worker reads); running the
    #     rawreplay engine against a plain eventgen pack would fail at the worker.
    #     An eventgen spec against a rawreplay pack that ships an eventgen-fallback
    #     conf is allowed (the pack lints as eventgen-runnable too).
    if (spec.engine or "").strip() == bundles.RAWREPLAY_ENGINE \
            and not bundles.is_rawreplay_pack(pack.source_path):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "engine_pack_mismatch",
                "detail": (
                    "spec engine is 'rawreplay' but pack %s is not a rawreplay "
                    "pack (no replay config); use a pack that declares "
                    "engine: rawreplay with a dataset" % pack.id),
            },
        )

    # 1c. Metrics engine/pack consistency. A metrics pack (UI-authored builder
    #     config) must run under the metrics engine (its bundle ships only a stub
    #     eventgen.conf), and the metrics engine needs a metrics pack (it reads a
    #     metricgen config). The metrics engine is also engine-paced, so it
    #     requires count_interval pacing (an eps/per_day_gb bucket would throttle
    #     the fixed-resolution grid).
    is_metrics_pack = pack.builder_config_json is not None
    is_metrics_spec = (spec.engine or "").strip() == bundles.METRICS_ENGINE
    if is_metrics_spec and not is_metrics_pack:
        raise HTTPException(
            status_code=422,
            detail={"error": "engine_pack_mismatch",
                    "detail": "spec engine is 'metrics' but pack %s is not a "
                              "metrics pack (no metricgen config)" % pack.id})
    if is_metrics_pack and not is_metrics_spec:
        raise HTTPException(
            status_code=422,
            detail={"error": "engine_pack_mismatch",
                    "detail": "pack %s is a metrics pack; the spec engine must be "
                              "'metrics'" % pack.id})
    if is_metrics_spec and spec.rate_mode != "count_interval":
        raise HTTPException(
            status_code=422,
            detail={"error": "metrics_rate_mode",
                    "detail": "the metrics engine is engine-paced; use "
                              "rate_mode 'count_interval' (interval = resolution)"})

    # 2. replay-single-worker: replay is engine-paced and the control plane
    #    guarantees workers = 1. This covers an eventgen pack with a
    #    ``mode = replay`` stanza AND a rawreplay (Piston) spec/pack, which
    #    replays a whole dataset and cannot be rate-sharded.
    if _is_replay_run(spec, pack) and spec.workers != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "replay_single_worker",
                "detail": (
                    "this is a replay run (rawreplay engine or a replay stanza); "
                    "replay runs must use exactly 1 worker"),
                "workers": spec.workers,
            },
        )

    # 3. Ceiling: apportion the rate across workers and check the per-worker
    #    share against the engine ceiling. Over -> 422 with suggested_workers.
    per_worker = _per_worker_share(spec.rate_mode, spec.rate_value, spec.workers)
    check = ceilings.check_slice(
        spec.rate_mode, per_worker, bytes_per_event=bytes_per_event, engine=spec.engine)
    if not check.ok:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "slice_exceeds_ceiling",
                "suggested_workers": check.suggested_workers,
                "limiting_factor": check.limiting_factor,
                "detail": check.detail,
            },
        )

    # 4. Per-target concurrent-GB cap: the sum of this run's GB/day plus the
    #    GB/day of the target's already-active runs must fit under the cap.
    headroom = _target_headroom_gb_day(db, target, spec, bytes_per_event)
    if headroom is not None and headroom < 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "target_cap_exceeded",
                "headroom_gb_day": round(headroom, 3),
                "detail": (
                    "target %s concurrent GB/day cap of %.3f would be exceeded"
                    % (target.name, target.max_concurrent_gb_day)
                ),
            },
        )

    # 5. Target health: a red target blocks (unknown/amber pass; the operator
    #    can pre-flight with /targets/{id}/test). This is last so cheaper local
    #    rejections happen first.
    if target.health_state == _HEALTH_RED:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "target_unhealthy",
                "health_state": target.health_state,
                "detail": target.health_detail or "target last probed unhealthy",
            },
        )

    # --- Provision (delegates to the Core lifecycle) ----------------------- #
    backfill = None
    if body.backfill_window_s and body.backfill_window_s > 0:
        backfill = {"window_s": body.backfill_window_s,
                    "resolution_s": body.backfill_resolution_s,
                    "cap_eps": body.backfill_cap_eps}

    driver = get_driver(spec.fleet)
    try:
        run = lifecycle.provision_run(
            db, spec, driver, overrides=body.overrides,
            started_by=_actor(request), backfill=backfill)
    except DriverError as exc:
        # The fleet could not be materialised (e.g. Portainer unreachable / a
        # swarm fleet with no PORTAINER_HOST). provision_run has already moved the
        # run to FAILED (end_reason=provision-failed); COMMIT so that failed run
        # and its audit trail survive. Rolling back would erase the attempt
        # entirely — losing the operator-visible record and the failed-run marker
        # boot reconciliation's stray sweep uses to reap any half-created fleet.
        try:
            db.commit()
        except Exception:  # pragma: no cover - defensive: fall back to a clean slate
            db.rollback()
        log.error("provision failed for spec %s on fleet %s: %s", spec_id, spec.fleet, exc)
        raise HTTPException(
            status_code=502,
            detail={"error": "provision_failed", "detail": str(exc)},
        )
    db.commit()
    db.refresh(run)
    log.info("provisioned run %s from spec %s (state=%s)", run.id, spec_id, run.state)
    return RunCreated(run_id=run.id, state=run.state)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

@router.get("/runs", response_model=List[RunOut])
def list_runs(db: Session = Depends(get_db)):
    # type: (...) -> Any
    return list(db.execute(select(Run).order_by(Run.id.desc())).scalars().all())


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Run detail: state, snapshot, totals, lease roster and event log."""
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return run


@router.get("/runs/{run_id}/metrics", response_model=MetricsOut)
def run_metrics(
    run_id: int,
    res: str = Query(default="5s"),
    window: str = Query(default="15m"),
    db: Session = Depends(get_db),
):
    # type: (...) -> Any
    """Metric samples for a run within ``window`` at resolution ``res``.

    Raw 5 s inserts are returned as-is this stage (rollup/downsampling by ``res``
    is deferred per the contract; ``res`` is echoed back for the UI). ``window``
    bounds how far back to read from ``now``.
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    since = _window_start(window)
    stmt = select(MetricSample).where(MetricSample.run_id == run_id)
    if since is not None:
        stmt = stmt.where(MetricSample.ts >= since)
    stmt = stmt.order_by(MetricSample.ts, MetricSample.slot)
    samples = list(db.execute(stmt).scalars().all())
    return MetricsOut(
        run_id=run_id,
        resolution=res,
        window=window,
        samples=[MetricSampleOut.model_validate(s) for s in samples],
    )


@router.get("/runs/{run_id}/logs", response_model=RunLogsOut)
def run_logs(
    run_id: int,
    slot: Optional[int] = Query(default=None),
    tail: int = Query(default=200),
    db: Session = Depends(get_db),
):
    # type: (...) -> Any
    """Recent worker log lines (whole fleet when ``slot`` omitted).

    Pulls live logs from the run's driver when it is provisioned; falls back to
    a lease's stored ``final_log_tail`` (captured at final) for a finished run
    whose workload is gone.
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    tail = max(1, min(int(tail), 5000))
    lines = _run_logs(db, run, slot, tail)
    return RunLogsOut(run_id=run_id, slot=slot, tail=tail, lines=lines)


@router.get("/runs/{run_id}/events", response_model=List[RunEventOut])
def run_events(run_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """The run's append-only audit trail (every state transition + action)."""
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    stmt = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.ts, RunEvent.id)
    return list(db.execute(stmt).scalars().all())


@router.post("/runs/{run_id}/stop", response_model=RunOut)
def stop_run_endpoint(run_id: int, body: StopRequest, request: Request, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Drain a run (``force`` destroys immediately)."""
    run = _load_active_run(db, run_id)
    driver = _run_driver(db, run)
    try:
        run = lifecycle.stop_run(db, run, driver, force=body.force, actor=_actor(request))
    except DriverError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail={"error": "stop_failed", "detail": str(exc)})
    db.commit()
    db.refresh(run)
    return run


@router.post("/runs/{run_id}/scale", response_model=RunOut)
def scale_run_endpoint(run_id: int, body: ScaleRequest, request: Request, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Change worker count; re-apportion and push ``retarget`` shares."""
    if body.workers < 1:
        raise HTTPException(status_code=422, detail="workers must be >= 1")
    run = _load_active_run(db, run_id)
    # Replay (rawreplay/Piston) is single-worker: scaling would duplicate the
    # dataset stream N times, so reject a grow with the same 409 the submit guard
    # uses (lifecycle.scale_run also clamps defensively).
    engine = (run.spec_snapshot_json or {}).get("engine")
    if lifecycle.effective_workers(engine, body.workers) != body.workers:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "replay_single_worker",
                "detail": (
                    "this is a replay run (%s engine); replay replays a fixed "
                    "dataset on exactly 1 worker and cannot be scaled"
                    % (engine or "rawreplay")),
                "workers": body.workers,
            },
        )
    driver = _run_driver(db, run)
    try:
        run = lifecycle.scale_run(db, run, driver, body.workers, actor=_actor(request))
    except DriverError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail={"error": "scale_failed", "detail": str(exc)})
    db.commit()
    db.refresh(run)
    return run


@router.post("/runs/{run_id}/rescale", response_model=RunOut)
def rescale_run_endpoint(run_id: int, body: RescaleRequest, request: Request, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Change total rate at the same worker count; push ``retarget`` shares."""
    run = _load_active_run(db, run_id)
    if body.rate_value <= 0:
        raise HTTPException(status_code=422, detail="rate_value must be > 0")
    driver = _run_driver(db, run)
    try:
        run = lifecycle.rescale_run(db, run, driver, body.rate_value, actor=_actor(request))
    except DriverError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail={"error": "rescale_failed", "detail": str(exc)})
    db.commit()
    db.refresh(run)
    return run


# --------------------------------------------------------------------------- #
# Helpers (module-private; no secret material is ever logged or returned).
# --------------------------------------------------------------------------- #

def _repo_view(repo, model):
    # type: (Repo, Any) -> Any
    """Build a repo response model with the computed ``has_secret`` flag.

    The credential ciphertext is never read back out; only its presence is
    reported. ``webhook_secret`` is left unset here (the create route sets it
    once on the response).
    """
    return model(
        id=repo.id,
        url=repo.url,
        auth_kind=repo.auth_kind,
        has_secret=bool(repo.secret_encrypted),
        default_ref=repo.default_ref,
        head_sha=repo.head_sha,
        last_synced_at=repo.last_synced_at,
        sync_error=repo.sync_error,
        trusted_code=repo.trusted_code,
        created_at=repo.created_at,
    )


def _match_webhook_repo(db, raw_body, signature_header):
    # type: (Session, bytes, str) -> Optional[Repo]
    """Return the repo whose webhook secret validates ``signature_header``.

    ``signature_header`` is GitHub's ``sha256=<hex>``. Each repo has its own
    secret, so we compute the expected HMAC per repo and compare in constant
    time. Returns the first match, or ``None`` when the header is malformed or no
    repo matches (the caller returns a uniform 401, giving no probing oracle).
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return None
    provided = signature_header.split("=", 1)[1].strip()
    if not provided:
        return None
    repos = db.execute(
        select(Repo).where(Repo.webhook_secret.is_not(None))
    ).scalars().all()
    for repo in repos:
        secret = repo.webhook_secret or ""
        expected = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, provided):
            return repo
    return None


def _require_pack(db, pack_id):
    # type: (Session, int) -> Pack
    pack = db.get(Pack, pack_id)
    if pack is None:
        raise HTTPException(status_code=422, detail="unknown pack_id %s" % pack_id)
    return pack


def _require_target(db, target_id):
    # type: (Session, int) -> Target
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=422, detail="unknown target_id %s" % target_id)
    return target


def _validate_rate(rate_mode, rate_value):
    # type: (str, Optional[float]) -> None
    """Reject an inconsistent rate mode / value at spec write time (422)."""
    if rate_mode not in ("eps", "per_day_gb", "count_interval"):
        raise HTTPException(status_code=422, detail="unknown rate_mode %r" % rate_mode)
    if rate_mode in ("eps", "per_day_gb"):
        if rate_value is None or rate_value <= 0:
            raise HTTPException(
                status_code=422,
                detail="%s mode requires rate_value > 0" % rate_mode,
            )


def _per_worker_share(rate_mode, rate_value, workers):
    # type: (str, Optional[float], int) -> Optional[float]
    """The largest single-worker share for this run (the ceiling binds on it).

    Uses the same apportioner the provisioner and worker use; the max slot share
    is what the ceiling must clear. ``count_interval`` has no rate ceiling, so
    ``None`` is returned (the ceiling check treats it as always-ok).
    """
    if rate_mode == "count_interval":
        return None
    if rate_value is None or rate_value <= 0:
        return None
    workers = max(1, int(workers))
    shares = lifecycle.build_share_list(rate_mode, rate_value, workers)
    key = "eps" if rate_mode == "eps" else "per_day_gb"
    return max((s.get(key, 0.0) for s in shares), default=0.0)


def _estimate(spec, bytes_per_event):
    # type: (Spec, Optional[float]) -> SpecEstimate
    """Build the estimate view for a spec (per-worker share + ceiling headroom).

    For a single-worker engine (rawreplay) the worker count is clamped to 1 so
    the estimate reflects what the run will actually do (the control plane forces
    workers = 1 for a replay run regardless of the spec's requested count).
    """
    workers = lifecycle.effective_workers(spec.engine, max(1, int(spec.workers)))
    per_worker = _per_worker_share(spec.rate_mode, spec.rate_value, workers)
    check = ceilings.check_slice(
        spec.rate_mode, per_worker, bytes_per_event=bytes_per_event, engine=spec.engine)

    per_worker_eps = None  # type: Optional[float]
    per_worker_gb = None  # type: Optional[float]
    ceiling_limit = None  # type: Optional[float]
    ceiling_pct = None  # type: Optional[float]
    limiting = None  # type: Optional[str]

    table = ceilings.ceiling_for(spec.engine)
    max_eps = table.get("max_eps_per_worker")
    max_gb = table.get("max_gb_day_per_worker")

    if spec.rate_mode == "eps":
        per_worker_eps = per_worker
        per_worker_gb = ceilings.eps_to_gb_day(per_worker, bytes_per_event) if per_worker else None
        ceiling_limit = max_eps
        limiting = "eps"
        if per_worker and max_eps:
            ceiling_pct = round(100.0 * per_worker / max_eps, 2)
    elif spec.rate_mode == "per_day_gb":
        per_worker_gb = per_worker
        per_worker_eps = ceilings.gb_day_to_eps(per_worker, bytes_per_event) if per_worker else None
        ceiling_limit = max_gb
        limiting = "gb_day"
        if per_worker and max_gb:
            ceiling_pct = round(100.0 * per_worker / max_gb, 2)
    else:  # count_interval: engine-paced, no rate ceiling
        limiting = None

    # When the check flags a different binding factor (e.g. eps mode limited by
    # gb/day), reflect that in the reported limiting factor.
    if not check.ok and check.limiting_factor:
        limiting = check.limiting_factor

    return SpecEstimate(
        workers=workers,
        rate_mode=spec.rate_mode,
        per_worker_share=per_worker,
        per_worker_eps=round(per_worker_eps, 3) if per_worker_eps is not None else None,
        per_worker_gb_day=round(per_worker_gb, 4) if per_worker_gb is not None else None,
        ceiling_pct=ceiling_pct,
        ceiling_limit=ceiling_limit,
        limiting_factor=limiting,
        ok=check.ok,
        suggested_workers=check.suggested_workers,
        detail=check.detail,
    )


def _run_gb_day(spec, bytes_per_event):
    # type: (Spec, Optional[float]) -> Optional[float]
    """Approximate total GB/day this whole run would push to its target.

    ``per_day_gb`` mode is the value directly. ``eps`` mode converts via
    bytes/event when known. ``count_interval`` is engine-paced and open-ended, so
    the volume is unknown (``None``) and the per-target cap cannot bind on it.
    """
    if spec.rate_mode == "per_day_gb":
        return float(spec.rate_value) if spec.rate_value else None
    if spec.rate_mode == "eps":
        if not spec.rate_value:
            return None
        return ceilings.eps_to_gb_day(float(spec.rate_value), bytes_per_event)
    return None


def _target_headroom_gb_day(db, target, spec, bytes_per_event):
    # type: (Session, Target, Spec, Optional[float]) -> Optional[float]
    """Remaining GB/day headroom on the target after admitting this run.

    ``None`` when the target has no cap or the run's volume is unknowable
    (``count_interval`` / no bytes-per-event). Otherwise:
    ``cap - active_runs_gb_day - this_run_gb_day``. A negative result means the
    run does not fit (the caller raises ``409 target_cap_exceeded``).
    """
    cap = target.max_concurrent_gb_day
    if not cap or cap <= 0:
        return None
    this_run = _run_gb_day(spec, bytes_per_event)
    if this_run is None:
        # Unknowable volume: the cap cannot bind on this run.
        return None
    active = _active_gb_day_for_target(db, target.id)
    return float(cap) - active - this_run


def _active_gb_day_for_target(db, target_id):
    # type: (Session, int) -> float
    """Sum the estimated GB/day of the target's currently-active runs.

    Reads each active run's frozen snapshot (rate_mode/value) plus the pack's
    bytes/event estimate; unknowable-volume runs contribute 0 to the sum.
    """
    active_states = (
        lifecycle.STATE_PENDING, lifecycle.STATE_PREPARING, lifecycle.STATE_PROVISIONING,
        lifecycle.STATE_RELEASING, lifecycle.STATE_RUNNING, lifecycle.STATE_DRAINING,
    )
    stmt = (
        select(Run)
        .join(Spec, Run.spec_id == Spec.id)
        .where(Spec.target_id == target_id, Run.state.in_(active_states))
    )
    total = 0.0
    for run in db.execute(stmt).scalars().all():
        snap = run.spec_snapshot_json or {}
        rate_mode = snap.get("rate_mode")
        rate_value = snap.get("rate_value")
        bpe = None
        pack_id = _snapshot_pack_id(run)
        if pack_id is not None:
            pack = db.get(Pack, pack_id)
            if pack is not None:
                bpe = pack.est_bytes_per_event
        gb = _rate_to_gb_day(rate_mode, rate_value, bpe)
        if gb:
            total += gb
    return total


def _rate_to_gb_day(rate_mode, rate_value, bytes_per_event):
    # type: (Optional[str], Optional[float], Optional[float]) -> Optional[float]
    if not rate_value:
        return None
    if rate_mode == "per_day_gb":
        return float(rate_value)
    if rate_mode == "eps":
        return ceilings.eps_to_gb_day(float(rate_value), bytes_per_event)
    return None


def _snapshot_pack_id(run):
    # type: (Run) -> Optional[int]
    """Best-effort pack id for an active run via its spec relationship."""
    spec = getattr(run, "spec", None)
    if spec is not None:
        return spec.pack_id
    return None


def _is_replay_run(spec, pack):
    # type: (Spec, Pack) -> bool
    """True when a run is a replay run (single-worker), for the submit guard.

    Three triggers, any one of which forces a replay run:

    * the spec's engine is ``rawreplay`` (Piston);
    * the pack is a rawreplay pack (``pack.yaml`` engine/``replay:`` section);
    * the pack's eventgen.conf declares a ``mode = replay`` stanza.

    All are engine-paced and the control plane guarantees workers = 1 for them.
    """
    if (spec.engine or "").strip() == bundles.RAWREPLAY_ENGINE:
        return True
    if bundles.is_rawreplay_pack(pack.source_path):
        return True
    return _pack_has_replay(pack.source_path)


def _pack_has_replay(pack_dir):
    # type: (str) -> bool
    """True when the pack's eventgen.conf declares any ``mode = replay`` stanza.

    Replay is engine-paced; the control plane guarantees a single worker for it,
    so a multi-worker spec against a replay pack is rejected at submit.
    """
    conf_path = os.path.join(pack_dir, bundles.CONF_RELPATH)
    if not os.path.isfile(conf_path):
        return False
    parser = configparser.RawConfigParser(
        delimiters=("=",), strict=False, allow_no_value=True, interpolation=None)
    parser.optionxform = str
    try:
        parser.read(conf_path, encoding="utf-8")
    except configparser.Error:
        return False
    for section in parser.sections():
        if section.lower() in ("global", "default"):
            continue
        mode = (parser.get(section, "mode", fallback="sample") or "sample").strip()
        if mode == "replay":
            return True
    return False


def _preview_sample_lines(pack_dir, stanzas, limit=10):
    # type: (str, List[str], int) -> Dict[str, List[str]]
    """First ``limit`` sample lines per stanza for the operator preview.

    Resolves each stanza's sample file the way the linter does (explicit
    ``sampleFile`` or the stanza name, under ``samples/`` or the pack root).
    Missing/unreadable files yield an empty list for that stanza.
    """
    conf_path = os.path.join(pack_dir, bundles.CONF_RELPATH)
    parser = configparser.RawConfigParser(
        delimiters=("=",), strict=False, allow_no_value=True, interpolation=None)
    parser.optionxform = str
    if os.path.isfile(conf_path):
        try:
            parser.read(conf_path, encoding="utf-8")
        except configparser.Error:
            pass
    samples_dir = os.path.join(pack_dir, "samples")
    out = {}  # type: Dict[str, List[str]]
    for section in stanzas:
        sample_name = None
        if parser.has_section(section):
            sample_name = parser.get(section, "sampleFile", fallback=None)
        sample_name = sample_name or section
        lines = []  # type: List[str]
        for base in (samples_dir, pack_dir):
            # Contain the sample path inside the pack root: a pack's conf is
            # attacker-influenced (operator source_path / git-synced repos), so a
            # sampleFile of /etc/passwd or ../../secret must never be read.
            path = preview._safe_join(pack_dir, base, sample_name)
            if path is not None and os.path.isfile(path):
                lines = _read_first_lines(path, limit)
                break
        out[section] = lines
    return out


def _read_first_lines(path, limit):
    # type: (str, int) -> List[str]
    lines = []  # type: List[str]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                stripped = raw.rstrip("\r\n")
                if not stripped:
                    continue
                lines.append(stripped)
                if len(lines) >= limit:
                    break
    except OSError:
        return []
    return lines


def _probe_target(hec_url, token, verify_tls):
    # type: (str, Optional[str], bool) -> TargetTestResult
    """Probe a HEC endpoint: the ``/health`` endpoint plus a zero-event auth ping.

    * health: ``GET /services/collector/health`` -> 200 means the collector is up.
    * auth: ``POST /services/collector/event`` with an empty body. A valid token
      yields ``400 {"code":5,"text":"No data"}`` (data required); an invalid or
      missing token yields ``401/403`` (``code`` 2/3/4). We send **no event**, so
      this never generates load. ``ok`` is health-up AND auth-accepted.

    The token, if present, rides only the ``Authorization`` header and is never
    logged or returned.
    """
    base = hec_url.rstrip("/")
    health = None  # type: Optional[str]
    auth = None  # type: Optional[str]
    detail_bits = []  # type: List[str]
    started = time.monotonic()
    verify = bool(verify_tls)

    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT_S, verify=verify) as http:
            # 1. Health endpoint (no token required on most deployments).
            try:
                hr = http.get(base + "/services/collector/health")
                health = "up" if hr.status_code == 200 else "down"
                if hr.status_code != 200:
                    detail_bits.append("health HTTP %d" % hr.status_code)
            except httpx.HTTPError as exc:
                health = "down"
                detail_bits.append("health error: %s" % _safe_err(exc))

            # 2. Auth ping: empty event POST, token in the header only.
            headers = {}
            if token:
                headers["Authorization"] = "Splunk %s" % token
            try:
                ar = http.post(base + "/services/collector/event", headers=headers, content=b"")
                auth = _classify_auth(ar.status_code)
                if auth != "ok":
                    detail_bits.append("auth HTTP %d" % ar.status_code)
            except httpx.HTTPError as exc:
                auth = "error"
                detail_bits.append("auth error: %s" % _safe_err(exc))
    except httpx.HTTPError as exc:
        detail_bits.append("connect error: %s" % _safe_err(exc))

    latency_ms = round((time.monotonic() - started) * 1000.0, 1)
    ok = (health == "up") and (auth == "ok")
    detail = "; ".join(detail_bits) if detail_bits else None
    return TargetTestResult(ok=ok, health=health, auth=auth, latency_ms=latency_ms, detail=detail)


def _classify_auth(status_code):
    # type: (int) -> str
    """Map a HEC event-POST status to an auth verdict for the empty-event ping.

    200/201 (accepted) and 400 (valid token, but we sent no data) both mean the
    token authenticated. 401/403 mean it did not. Anything else is 'unknown'.
    """
    if status_code in (200, 201, 400):
        return "ok"
    if status_code in (401, 403):
        return "denied"
    return "unknown"


def _health_from_probe(result):
    # type: (TargetTestResult) -> str
    """Reduce a probe result to a target health_state (green/amber/red)."""
    if result.ok:
        return _HEALTH_GREEN
    if result.auth == "denied":
        # Reachable but the token is rejected: a real, actionable fault.
        return _HEALTH_RED
    if result.health == "up":
        # Collector up but the auth ping was inconclusive: amber.
        return _HEALTH_AMBER
    return _HEALTH_RED


def _apply_health(target, state, detail):
    # type: (Target, str, Optional[str]) -> None
    target.health_state = state
    target.health_detail = detail
    target.last_health_at = utcnow()


def _safe_err(exc):
    # type: (Exception) -> str
    """A concise, secret-free string for an httpx error (class + message)."""
    return "%s: %s" % (type(exc).__name__, exc)


def _window_start(window):
    # type: (str) -> Optional[Any]
    """Parse a ``window`` like ``15m`` / ``2h`` / ``30s`` into a UTC 'since' ts.

    Returns ``None`` for ``all`` / an unparseable value (no lower bound).
    """
    import datetime

    if not window or window.strip().lower() in ("all", "0"):
        return None
    seconds = _duration_to_seconds(window)
    if seconds is None:
        return None
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)


def _duration_to_seconds(value):
    # type: (str) -> Optional[float]
    """Parse ``<n><unit>`` (s/m/h/d) or a bare number of seconds."""
    text = value.strip().lower()
    if not text:
        return None
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    unit = text[-1]
    if unit in units:
        num = text[:-1]
        factor = units[unit]
    else:
        num = text
        factor = 1.0
    try:
        return float(num) * factor
    except ValueError:
        return None


def _run_logs(db, run, slot, tail):
    # type: (Session, Run, Optional[int], int) -> List[str]
    """Fetch a run's logs: live from the driver, else stored final_log_tail."""
    ref = lifecycle.driver_ref_of(run)
    if ref is not None and run.state not in lifecycle.TERMINAL_STATES:
        driver = _resolve_driver_quiet(db, run)
        if driver is not None:
            try:
                text = driver.logs(ref, slot, tail)
            except DriverError as exc:
                log.info("driver logs unavailable for run %s: %s", run.id, exc)
            else:
                if text:
                    return text.splitlines()[-tail:]
    # Fallback: the leases' captured final tails (whole fleet or one slot).
    return _stored_log_tail(db, run, slot, tail)


def _stored_log_tail(db, run, slot, tail):
    # type: (Session, Run, Optional[int], int) -> List[str]
    stmt = select(WorkerLease).where(WorkerLease.run_id == run.id)
    if slot is not None:
        stmt = stmt.where(WorkerLease.slot == slot)
    stmt = stmt.order_by(WorkerLease.slot)
    leases = list(db.execute(stmt).scalars().all())
    multi = slot is None and len(leases) > 1
    lines = []  # type: List[str]
    for lease in leases:
        tail_lines = lease.final_log_tail_json or []
        if not isinstance(tail_lines, list):
            continue
        if multi:
            lines.append("--- slot %d (%s) ---" % (lease.slot, lease.holder or "?"))
        lines.extend(str(x) for x in tail_lines)
    return lines[-tail:]


def _run_driver(db, run):
    # type: (Session, Run) -> Any
    """Resolve the run's execution driver or 409 when it has none (unprovisioned).

    Operator stop/scale/rescale require a provisioned run with a live fleet.
    """
    driver = _resolve_driver_quiet(db, run)
    if driver is None:
        raise HTTPException(
            status_code=409,
            detail="run %s has no resolvable driver (fleet unknown or not configured)" % run.id,
        )
    return driver


def _resolve_driver_quiet(db, run):
    # type: (Session, Run) -> Any
    """Resolve a run's driver by fleet name, returning None instead of raising."""
    fleet_name = None
    spec = getattr(run, "spec", None)
    if spec is not None:
        fleet_name = spec.fleet
    if not fleet_name:
        fleet_name = (run.spec_snapshot_json or {}).get("fleet")
    if not fleet_name:
        return None
    try:
        return get_driver(fleet_name)
    except DriverError as exc:
        log.info("driver for fleet %s unavailable: %s", fleet_name, exc)
        return None


def _load_active_run(db, run_id):
    # type: (Session, int) -> Run
    """Load a run and reject operations on an already-terminal one (409)."""
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    if run.state in lifecycle.TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail="run %s is already %s" % (run_id, run.state),
        )
    return run


__all__ = ["router"]
