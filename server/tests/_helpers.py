"""Shared builders for the lifecycle/route tests.

These assemble a fully-provisioned run (target, pack, bundle, spec, run,
``worker_leases`` rows) **directly** against the DB using the *implemented*
pure helpers from :mod:`server.lifecycle` (``build_spec_snapshot``,
``build_share_list``, ``seed_leases``). That mirrors the end state
``provision_run`` produces, so the lease / release / heartbeat tests can drive
the frozen ``claim_lease`` / ``mark_ready`` / ``record_heartbeat`` /
``record_final`` interfaces without going through the operator ``run_spec``
route (owned by a separate builder). Nothing here reimplements domain logic; it
only sets up fixtures the domain functions then act on.

The helpers are deliberately small and explicit so a failing test points at the
scenario, not at a factory. All timestamps are timezone-aware UTC to match the
models. Commit is the caller's job (mirroring the lifecycle contract).
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from server import crypto, lifecycle
from server.bundles import build_from_pack
from server.models import Bundle, Pack, Run, Spec, Target, WorkerLease, utcnow


def make_target(db, name="t-loadtest", hec_url="http://127.0.0.1:18088",
                default_index="loadtest", token="hec-secret-token",
                env_tag="lab", verify_tls=False, max_concurrent_gb_day=None,
                health_state="green", settings=None):
    # type: (...) -> Target
    """Insert a target whose HEC token is Fernet-encrypted (never plaintext)."""
    target = Target(
        name=name,
        hec_url=hec_url,
        token_encrypted=crypto.encrypt(token, settings=settings) if token else None,
        default_index=default_index,
        verify_tls=verify_tls,
        env_tag=env_tag,
        max_concurrent_gb_day=max_concurrent_gb_day,
        health_state=health_state,
    )
    db.add(target)
    db.flush()
    return target


def make_pack(db, pack_dir, name="flatline-test", est_bytes_per_event=120.0):
    # type: (...) -> Pack
    """Insert a lint-ok pack row pointing at a real on-disk pack directory."""
    pack = Pack(
        name=name,
        source_path=pack_dir,
        engines_json=["eventgen"],
        stanza_count=1,
        est_bytes_per_event=est_bytes_per_event,
        verified=True,
        lint_status="ok",
    )
    db.add(pack)
    db.flush()
    return pack


def make_bundle(db, pack, pack_dir, settings):
    # type: (...) -> Bundle
    """Build the real content-addressed bundle from ``pack_dir`` and upsert a row."""
    built = build_from_pack(pack_dir, bundle_dir=settings.bundle_dir)
    bundle = Bundle(
        pack_id=pack.id,
        digest=built.digest,
        size_bytes=built.size_bytes,
        path=built.path,
    )
    db.add(bundle)
    db.flush()
    return bundle


def make_spec(db, pack, target, name="s1", engine="eventgen", rate_mode="eps",
              rate_value=1000.0, workers=4, duration_s=None, fleet="fake-local",
              overrides=None, strict_release=False, driver_opts=None):
    # type: (...) -> Spec
    """Insert a spec row (the JobSpec)."""
    spec = Spec(
        name=name,
        pack_id=pack.id,
        target_id=target.id,
        engine=engine,
        rate_mode=rate_mode,
        rate_value=rate_value,
        workers=workers,
        duration_s=duration_s,
        fleet=fleet,
        overrides_json=overrides if overrides is not None else {},
        strict_release=strict_release,
        driver_opts_json=driver_opts or {},
    )
    db.add(spec)
    db.flush()
    return spec


def provision_manual(db, spec, target, bundle, settings, driver=None,
                     state=None, seed=True):
    # type: (...) -> Run
    """Build a provisioned :class:`Run` + seeded ``free`` leases, like the Core.

    Freezes the snapshot via :func:`lifecycle.build_spec_snapshot`, apportions
    shares via :func:`lifecycle.build_share_list`, seeds one ``free`` lease per
    slot via :func:`lifecycle.seed_leases`, mints the run JWT kid, and (when a
    ``driver`` is given) records a real ``DriverRef`` by calling
    ``driver.create`` so a subsequent ``stop``/``status`` has something to act
    on. Returns the run in ``provisioning`` (or the requested ``state``).
    """
    state = state or lifecycle.STATE_PROVISIONING
    run = Run(
        spec_id=spec.id,
        spec_snapshot_json=lifecycle.build_spec_snapshot(spec, target),
        resolved_sha=bundle.digest,
        bundle_id=bundle.id,
        state=state,
        jwt_kid=crypto.new_kid(),
    )
    db.add(run)
    db.flush()
    if seed:
        shares = lifecycle.build_share_list(spec.rate_mode, spec.rate_value, spec.workers)
        lifecycle.seed_leases(db, run, shares)
        db.flush()
    if driver is not None:
        snap = lifecycle.build_run_snapshot(run, spec, target, None, settings=settings)
        ref = driver.create(snap, spec.workers)
        run.driver_ref_json = ref.to_json()
        db.flush()
    return run


def full_run(db, pack_dir, settings, driver=None, workers=4, rate_mode="eps",
             rate_value=1000.0, duration_s=None, fleet="fake-local",
             overrides=None, strict_release=False, state=None):
    # type: (...) -> Dict[str, Any]
    """One call: target + pack + bundle + spec + provisioned run with leases.

    Returns a dict with the created rows (``target``, ``pack``, ``bundle``,
    ``spec``, ``run``) so a test can assert against any of them. Commits so the
    rows are visible to a separate session (e.g. the app's request session).
    """
    target = make_target(db, settings=settings)
    pack = make_pack(db, pack_dir)
    bundle = make_bundle(db, pack, pack_dir, settings)
    spec = make_spec(db, pack, target, rate_mode=rate_mode, rate_value=rate_value,
                     workers=workers, duration_s=duration_s, fleet=fleet,
                     overrides=overrides, strict_release=strict_release)
    run = provision_manual(db, spec, target, bundle, settings, driver=driver, state=state)
    db.commit()
    return {"target": target, "pack": pack, "bundle": bundle, "spec": spec, "run": run}


def leases_by_slot(db, run):
    # type: (Any, Run) -> Dict[int, WorkerLease]
    """Return ``{slot: lease}`` for a run (fresh from the DB)."""
    return {l.slot: l for l in lifecycle.get_run_leases(db, run)}


def age_heartbeat(db, lease, seconds):
    # type: (Any, WorkerLease, float) -> None
    """Back-date a lease's ``last_heartbeat_at`` by ``seconds`` (for lapse tests)."""
    lease.last_heartbeat_at = utcnow() - datetime.timedelta(seconds=seconds)
    db.flush()


def bearer_for(run, settings):
    # type: (Run, Any) -> str
    """Mint a valid run JWT for ``run`` (the worker treats it as opaque)."""
    return crypto.mint_run_jwt(run.id, run.jwt_kid or crypto.new_kid(), settings=settings)


def auth_header(run, settings):
    # type: (Run, Any) -> Dict[str, str]
    """Build an ``Authorization: Bearer <jwt>`` header for agent requests."""
    return {"Authorization": "Bearer %s" % bearer_for(run, settings)}


def heartbeat_payload(lease, state="generating", **counters):
    # type: (WorkerLease, str, Any) -> Dict[str, Any]
    """Build a minimal heartbeat request body for a lease with optional counters."""
    body = {
        "slot": lease.slot,
        "lease_id": lease.lease_id,
        "protocol_version": 1,
        "state": state,
    }
    body.update(counters)
    return body
