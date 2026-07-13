"""Server-owned run lifecycle: the state machine, provisioning and supervisor.

The DB is the source of truth. Drivers are queried, never trusted as a store.
Commands ride heartbeat responses (push, not pull). This module defines the
complete set of function signatures the domain needs; the **stubbed** ones
(raising :class:`NotImplementedError`) are filled by the Core builder against
these exact signatures. The **pure helpers** at the bottom are implemented here
so every builder shares one copy (slice construction, slot selection, state
transitions, run-event append, share/override templating, JWT-refresh policy).

State machine (contract section "Run lifecycle"):

    pending -> preparing -> provisioning -> releasing -> running
            -> draining -> {completed | stopped}
    (failed reachable from provisioning and any auto-abort)

Every transition appends a ``run_events`` row via :func:`append_event`.

Terminology used across signatures:

* ``db``       an active SQLAlchemy ``Session``.
* ``run``      a ``models.Run`` (attached to ``db``).
* ``spec``     a ``models.Spec``.
* ``driver``   an :class:`~server.drivers.base.ExecutionDriver` for the run's fleet.
* ``drivers``  a mapping ``fleet_name -> ExecutionDriver`` (supervisor/boot).
* ``lease``    a ``models.WorkerLease``.
"""

from __future__ import annotations

import datetime
import logging
import math
import secrets
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto
from .config import Settings, get_settings
from .drivers.base import DriverRef, ExecutionDriver, RunSnapshot
from .models import (
    Bundle,
    Fleet,
    MetricSample,
    Run,
    RunEvent,
    Spec,
    Target,
    WorkerLease,
    utcnow,
)
from .slice_format import format_iso8601

log = logging.getLogger("stoker.lifecycle")

# --------------------------------------------------------------------------- #
# Constants (contract-defined windows).
# --------------------------------------------------------------------------- #

# Run states.
STATE_PENDING = "pending"
STATE_PREPARING = "preparing"
STATE_PROVISIONING = "provisioning"
STATE_RELEASING = "releasing"
STATE_RUNNING = "running"
STATE_DRAINING = "draining"
STATE_COMPLETED = "completed"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"

TERMINAL_STATES = frozenset((STATE_COMPLETED, STATE_STOPPED, STATE_FAILED))

# Lease states.
LEASE_FREE = "free"
LEASE_CLAIMED = "claimed"
LEASE_READY = "ready"
LEASE_RUNNING = "running"
LEASE_LOST = "lost"
LEASE_DONE = "done"

# Timing windows.
RELEASE_DELAY_S = 2.0            # T0 = now + 2 s once all ready / timeout fires
PROVISION_TIMEOUT_S = 120.0      # provisioning -> releasing timeout with >=1 ready
LEASE_LAPSE_S = 30.0            # no heartbeat for this long -> lease lost
BOOT_GRACE_S = 60.0            # extra grace after a control-plane restart
STOP_GRACE_S = 45              # SIGTERM budget handed to the driver
AUTO_ABORT_LOST_FRACTION = 0.5  # >50% leases lost ...
AUTO_ABORT_LOST_S = 300.0       # ... sustained for 5 min -> fail
JWT_REFRESH_FRACTION = 0.2      # roll the JWT within 20% of expiry

# Engines the control plane forces to a single worker: replay (rawreplay/Piston)
# reproduces a recorded dataset and cannot be rate-sharded across a fleet, so the
# control plane guarantees workers = 1 (mirroring the eventgen ``mode = replay``
# single-worker rule enforced at submit).
SINGLE_WORKER_ENGINES = frozenset(("rawreplay",))


def effective_workers(engine, workers):
    # type: (Optional[str], int) -> int
    """Clamp ``workers`` to 1 for a single-worker engine (rawreplay); else pass.

    A replay run cannot be rate-sharded (it replays one dataset), so the control
    plane forces exactly one worker regardless of the spec's requested count. The
    submit route rejects a multi-worker replay spec up front; this is the
    belt-and-braces invariant at provision time.
    """
    if (engine or "").strip() in SINGLE_WORKER_ENGINES:
        return 1
    return max(1, int(workers))


# --------------------------------------------------------------------------- #
# Provisioning and the operator-driven transitions (STUBBED — Core fills).
# --------------------------------------------------------------------------- #

# Backfill delivers historical data as fast as the target accepts, up to this
# eps ceiling, so a large window does not overwhelm Splunk. The run is a gated
# eps run at (at most) this cap.
DEFAULT_BACKFILL_CAP_EPS = 5000.0


def plan_backfill(engine, series_count, live_eps, window_s, resolution_s, cap_eps, now):
    # type: (str, int, Optional[float], float, Optional[float], Optional[float], float) -> Dict[str, Any]
    """Size a backfill run: window, delivery cap, total events, duration backstop.

    ``events``: metrics = ceil(window/resolution) x series; eventgen = window x
    the effective live eps. ``duration_s`` is a backstop deadline (1.5x the
    delivery time + margin) so an eventgen backfill (which is bounded by the
    deadline, not engine-exit) always completes. Metrics exits on its own when the
    window is done; the backstop just guards against a stall.
    """
    window_s = float(window_s)
    cap = float(cap_eps) if cap_eps and cap_eps > 0 else DEFAULT_BACKFILL_CAP_EPS
    cap = max(1.0, min(cap, DEFAULT_BACKFILL_CAP_EPS))
    bf_res = None
    if engine == "metrics":
        res = float(resolution_s) if resolution_s and resolution_s > 0 else 10.0
        bf_res = res
        ticks = int(math.ceil(window_s / res)) if res > 0 else 0
        events = ticks * max(1, int(series_count or 1))
    else:
        rate = float(live_eps) if live_eps and live_eps > 0 else cap
        events = int(math.ceil(window_s * rate))
    duration_s = max(15.0, math.ceil(events / cap * 1.5) + 15.0)
    return {
        "start_s": now - window_s,
        "end_s": now,
        "resolution_s": bf_res,
        "cap_eps": cap,
        "events": int(events),
        "duration_s": float(duration_s),
        "seconds": float(events / cap) if cap else 0.0,
    }


def provision_run(db, spec, driver, overrides=None, started_by=None, settings=None,
                  backfill=None):
    # type: (Session, Spec, ExecutionDriver, Optional[Dict[str, str]], Optional[str], Optional[Settings], Optional[Dict[str, Any]]) -> Run
    """Create and provision a run from a spec.

    ``backfill`` (``{window_s, resolution_s?, cap_eps?}``) makes this a backfill
    run: the effective rate is overridden to an eps delivery cap, a duration
    backstop is set, and the historical window is frozen into the snapshot (and
    thence the claim slice) for the worker.

    Full flow (contract "Provision"):

    1. Validate (caller has already run the submit-time gates: target health,
       ceiling, replay-single-worker, per-target cap, lint ok — see the route).
    2. Freeze ``spec_snapshot_json`` (non-secret only, target embedded by id +
       non-secret fields) via :func:`build_spec_snapshot`.
    3. Resolve the bundle (build from the pack dir if absent) and set
       ``bundle_id`` / ``resolved_sha``.
    4. Apportion shares across ``spec.workers`` slots by largest remainder and
       seed ``worker_leases`` rows (all ``free``) via :func:`seed_leases`.
    5. Mint the per-run JWT (store ``jwt_kid`` on the run).
    6. ``driver.create(RunSnapshot, workers)`` and store the ``DriverRef``.
    7. State -> ``provisioning`` (append a run_event).

    Args:
        db: active session (the caller commits).
        spec: the spec to run.
        driver: the execution driver for ``spec.fleet``.
        overrides: last-minute override values merged over the spec's.
        started_by: operator identity for the audit trail.
        settings: config (defaults to :func:`get_settings`).

    Returns:
        the created :class:`~server.models.Run` in state ``provisioning``.
    """
    if settings is None:
        settings = get_settings()

    target = spec.target
    if target is None:
        target = db.get(Target, spec.target_id)
    if target is None:
        raise ValueError("spec %s references unknown target %s" % (spec.id, spec.target_id))

    # 0. Replay is a single-worker engine: force workers = 1 for rawreplay so a
    #    replay run is never rate-sharded (mirrors the submit-time replay guard).
    #    The snapshot's worker count is corrected too so every claim/slice agrees.
    workers = effective_workers(spec.engine, spec.workers)
    if workers != spec.workers:
        log.info("run for spec %s engine=%s: forcing workers %s -> 1 (replay is "
                 "single-worker)", spec.id, spec.engine, spec.workers)

    # 0b. Backfill: override the effective rate to an eps delivery cap, freeze the
    #     historical window, and set a duration backstop.
    eff_rate_mode = spec.rate_mode
    eff_rate_value = spec.rate_value
    eff_duration_s = spec.duration_s
    backfill_block = None
    if backfill and backfill.get("window_s"):
        from .models import Pack

        pack = spec.pack if spec.pack is not None else db.get(Pack, spec.pack_id)
        series = 0
        pack_res = None
        if pack is not None and pack.builder_config_json is not None:
            from . import bundles

            series = bundles.metrics_series_count(pack.builder_config_json)
            pack_res = (pack.builder_config_json or {}).get("resolution_s")
        live_eps = spec.rate_value if spec.rate_mode == "eps" else None
        res = backfill.get("resolution_s") or pack_res
        plan = plan_backfill(spec.engine, series, live_eps, backfill["window_s"],
                             res, backfill.get("cap_eps"), time.time())
        eff_rate_mode, eff_rate_value = "eps", plan["cap_eps"]
        eff_duration_s = plan["duration_s"]
        backfill_block = {"start_s": plan["start_s"], "end_s": plan["end_s"],
                          "resolution_s": plan["resolution_s"]}

    # 1. Freeze the non-secret snapshot (target embedded by id + non-secret fields).
    snapshot = build_spec_snapshot(spec, target, overrides=overrides,
                                   rate_mode=eff_rate_mode, rate_value=eff_rate_value,
                                   duration_s=eff_duration_s, backfill=backfill_block)
    if workers != spec.workers:
        snapshot["workers"] = workers

    # 2. Resolve the bundle (build from the pack dir if absent).
    bundle = _resolve_bundle(db, spec, settings=settings)

    # 3. Create the run row (pending) so it has an id for leases/events/JWT.
    run = Run(
        spec_id=spec.id,
        spec_snapshot_json=snapshot,
        resolved_sha=bundle.digest,
        bundle_id=bundle.id,
        state=STATE_PENDING,
        jwt_kid=crypto.new_kid(),
        started_by=started_by,
        totals_json={},
    )
    db.add(run)
    db.flush()  # assign run.id
    append_event(db, run, "created",
                 {"spec_id": spec.id, "workers": workers,
                  "fleet": spec.fleet, "bundle": bundle.digest},
                 actor=started_by or "operator")

    # preparing: snapshot frozen, bundle resolved.
    transition_run(db, run, STATE_PREPARING, actor=started_by or "operator")

    # 4. Apportion shares across the slots and seed free leases (a backfill run
    #    apportions the eps delivery cap exactly like a normal eps run).
    shares = build_share_list(eff_rate_mode, eff_rate_value, workers)
    seed_leases(db, run, shares)
    db.flush()

    # 5/6. Launch the fleet via the driver and store its handle.
    hec_token = None
    if target.token_encrypted:
        hec_token = crypto.decrypt(target.token_encrypted, settings=settings)
    run_snapshot = build_run_snapshot(run, spec, target, hec_token,
                                      settings=settings, workers=workers,
                                      duration_s=eff_duration_s)
    try:
        ref = driver.create(run_snapshot, workers)
    except Exception as exc:
        # A failed launch fails the run loudly (never a silent hang).
        log.error("run %s provision failed at driver.create: %s", run.id, exc)
        transition_run(db, run, STATE_FAILED,
                       {"error": str(exc)}, end_reason="provision-failed")
        raise
    run.driver_ref_json = ref.to_json()

    # 7. provisioning: fleet asked for, workers will claim.
    transition_run(db, run, STATE_PROVISIONING,
                   {"driver": ref.kind, "driver_id": ref.id})
    return run


def _resolve_bundle(db, spec, settings=None):
    # type: (Session, Spec, Optional[Settings]) -> Bundle
    """Return the run's bundle, building it from the spec's pack when absent.

    Content-addressed: an existing bundle row for the built digest is reused, so
    a re-run of the same pack never rebuilds. The bundle row is added/flushed so
    ``bundle.id`` is available for the run's ``bundle_id`` foreign key.
    """
    from .bundles import build_from_metrics_config, build_from_pack

    pack = spec.pack
    if pack is None:
        from .models import Pack

        pack = db.get(Pack, spec.pack_id)
    if pack is None:
        raise ValueError("spec %s references unknown pack %s" % (spec.id, spec.pack_id))

    if settings is None:
        settings = get_settings()
    # A UI-authored metrics pack has no source directory: its bundle is
    # synthesised from the stored builder config. Every other pack builds from
    # its on-disk source_path (a local directory or a repo clone).
    if pack.builder_config_json is not None:
        built = build_from_metrics_config(
            pack.name, pack.builder_config_json, bundle_dir=settings.bundle_dir)
    else:
        built = build_from_pack(pack.source_path, bundle_dir=settings.bundle_dir)

    existing = db.execute(
        select(Bundle).where(Bundle.digest == built.digest)
    ).scalars().first()
    if existing is not None:
        return existing
    bundle = Bundle(
        pack_id=pack.id,
        digest=built.digest,
        size_bytes=built.size_bytes,
        path=built.path,
    )
    db.add(bundle)
    db.flush()
    return bundle


def stop_run(db, run, driver, force=False, actor="operator"):
    # type: (Session, Run, ExecutionDriver, bool, str) -> Run
    """Begin draining a run.

    Move the run to ``draining`` so subsequent heartbeats answer ``drain``, call
    ``driver.stop(ref, STOP_GRACE_S)``, and (when ``force``) ``driver.destroy``
    immediately rather than waiting for leases to finalise. The supervisor
    completes the transition to ``stopped`` once leases are done/lost. ``actor``
    is recorded on the audit event (the operator/token that initiated the stop).
    """
    if run.state in TERMINAL_STATES:
        return run
    transition_run(db, run, STATE_DRAINING,
                   {"force": bool(force)}, actor=actor,
                   end_reason="operator-stop")
    ref = driver_ref_of(run)
    if ref is not None:
        try:
            driver.stop(ref, STOP_GRACE_S)
        except Exception as exc:  # a stop that cannot reach the driver still drains logically
            log.warning("run %s driver.stop failed: %s", run.id, exc)
            append_event(db, run, "driver_error",
                         {"op": "stop", "error": str(exc)})
        if force:
            try:
                driver.destroy(ref)
            except Exception as exc:
                log.warning("run %s driver.destroy failed: %s", run.id, exc)
                append_event(db, run, "driver_error",
                             {"op": "destroy", "error": str(exc)})
    if force:
        # Forced stop: finalise every lease now so the run completes immediately.
        # A live worker's lease becomes done (it drained); a never-claimed free
        # slot becomes lost (no worker to finalise it).
        for lease in get_run_leases(db, run):
            if lease.state in (LEASE_CLAIMED, LEASE_READY, LEASE_RUNNING):
                lease.state = LEASE_DONE
            elif lease.state == LEASE_FREE:
                lease.state = LEASE_LOST
        maybe_complete_run(db, run)
    return run


def scale_run(db, run, driver, workers, actor="operator"):
    # type: (Session, Run, ExecutionDriver, int, str) -> Run
    """Change a run's worker count.

    ``driver.scale(ref, workers)``, then add/remove ``worker_leases`` rows and
    re-apportion shares across the new slot count; changed shares are pushed to
    live workers as ``retarget`` on their next heartbeat. Growing adds ``free``
    leases for the new slots; shrinking marks the removed slots' leases for
    supersede/drain.
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if run.state in TERMINAL_STATES:
        return run

    # Replay (rawreplay/Piston) is single-worker and cannot be rate-sharded:
    # clamp any scale to 1 so a live replay run can never be duplicated into N
    # identical streams. Mirrors the submit-time 409 and the provision-time
    # effective_workers clamp; the route rejects workers>1 up front with a clear
    # 409, so this is the belt-and-braces invariant for any other caller.
    engine = (run.spec_snapshot_json or {}).get("engine")
    workers = effective_workers(engine, workers)

    ref = driver_ref_of(run)
    if ref is not None:
        try:
            driver.scale(ref, workers)
        except Exception as exc:
            log.warning("run %s driver.scale failed: %s", run.id, exc)
            append_event(db, run, "driver_error",
                         {"op": "scale", "error": str(exc)})
            raise

    snap = dict(run.spec_snapshot_json or {})
    rate_mode = snap.get("rate_mode") or "eps"
    rate_value = snap.get("rate_value")

    leases = get_run_leases(db, run)
    # Grow: add free leases for the new high slots.
    current = len(leases)
    if workers > current:
        for slot in range(current, workers):
            lease = WorkerLease(
                run_id=run.id, slot=slot,
                share_json={}, lease_id=new_lease_id(),
                state=LEASE_FREE, restarts=0,
            )
            db.add(lease)
        db.flush()
    # Shrink: remove the excess high-slot leases so their workers self-supersede
    # (a heartbeat from a slot with no lease returns superseded -> drain-and-exit).
    elif workers < current:
        for lease in leases:
            if lease.slot >= workers:
                db.delete(lease)
        db.flush()

    # Re-apportion across the surviving slots and flag live leases to retarget.
    shares = build_share_list(rate_mode, rate_value, workers)
    survivors = get_run_leases(db, run)
    for lease in survivors:
        if lease.slot >= len(shares):
            continue
        new_share = shares[lease.slot]
        if lease.state in (LEASE_CLAIMED, LEASE_READY, LEASE_RUNNING):
            mark_retarget(lease, new_share)
        else:
            lease.share_json = dict(new_share)

    # Keep the snapshot's worker count honest for future claims/slices.
    snap["workers"] = workers
    run.spec_snapshot_json = snap
    append_event(db, run, "scaled",
                 {"workers": workers, "rate_mode": rate_mode}, actor=actor)
    return run


def rescale_run(db, run, driver, rate_value, actor="operator"):
    # type: (Session, Run, ExecutionDriver, float, str) -> Run
    """Change a run's total rate without changing the worker count.

    Re-apportion ``rate_value`` across the existing slots and push the new
    shares as ``retarget`` on the next heartbeat of each live lease. ``actor``
    is recorded on the audit event (the operator/token that rescaled).
    """
    if run.state in TERMINAL_STATES:
        return run
    snap = dict(run.spec_snapshot_json or {})
    rate_mode = snap.get("rate_mode") or "eps"
    leases = get_run_leases(db, run)
    workers = len(leases)
    if workers < 1:
        return run

    shares = build_share_list(rate_mode, rate_value, workers)
    for lease in leases:
        if lease.slot >= len(shares):
            continue
        new_share = shares[lease.slot]
        if lease.state in (LEASE_CLAIMED, LEASE_READY, LEASE_RUNNING):
            mark_retarget(lease, new_share)
        else:
            lease.share_json = dict(new_share)

    # Record the new run rate on the snapshot for the audit/estimate views.
    snap["rate_value"] = rate_value
    run.spec_snapshot_json = snap
    append_event(db, run, "rescaled",
                 {"rate_mode": rate_mode, "rate_value": rate_value}, actor=actor)
    return run


def supervisor_tick(db, drivers, boot_time):
    # type: (Session, Mapping[str, ExecutionDriver], datetime.datetime) -> None
    """One pass of the background supervisor (called every ~2 s in the lifespan).

    Responsibilities (contract "Supervisor tick"):

    * **Lease lapse**: a claimed/running lease with
      ``now - last_heartbeat_at > LEASE_LAPSE_S`` becomes ``lost`` and its share
      is freed for re-claim; guarded by ``max(last_heartbeat_at, boot_time) +
      BOOT_GRACE_S`` so a control-plane restart never lapses the estate.
    * **Release timeout**: a ``provisioning`` run past ``PROVISION_TIMEOUT_S``
      with >=1 ready lease releases the ready subset, re-apportions across the
      ready slots and marks the run ``degraded`` (unless ``strict_release`` ->
      fail).
    * **Auto-abort subset**: >``AUTO_ABORT_LOST_FRACTION`` leases lost for
      ``AUTO_ABORT_LOST_S`` -> fail; a sustained HEC auth-fail flag across half
      the fleet -> fail + flag the target unhealthy; duration elapsed -> drain.
    * **Completion**: all leases done/lost -> the terminal state its
      ``end_reason`` dictates.

    Args:
        db: active session (the loop commits after the tick).
        drivers: fleet-name -> driver, so per-run driver ops are available.
        boot_time: process start instant, for the restart grace window.
    """
    now = utcnow()
    active_states = (
        STATE_PROVISIONING, STATE_RELEASING, STATE_RUNNING, STATE_DRAINING,
        STATE_PREPARING,
    )
    stmt = select(Run).where(Run.state.in_(active_states))
    runs = list(db.execute(stmt).scalars().all())
    for run in runs:
        try:
            _supervise_run(db, run, drivers, boot_time, now)
        except Exception as exc:  # one bad run must not stall the whole estate
            log.warning("supervisor: run %s tick error: %s", run.id, exc)


def _supervise_run(db, run, drivers, boot_time, now):
    # type: (Session, Run, Mapping[str, ExecutionDriver], datetime.datetime, datetime.datetime) -> None
    leases = get_run_leases(db, run)

    # 1. Lease lapse: renew-or-lose each live lease past its deadline.
    _lapse_stale_leases(db, run, leases, boot_time, now)

    # 2. Release timeout: a provisioning run stuck past the timeout releases the
    #    ready subset (degraded) rather than waiting for absent workers forever.
    if run.state == STATE_PROVISIONING and run.t0 is None:
        started = run.created_at or boot_time
        if _seconds_between(started, now) > PROVISION_TIMEOUT_S:
            if any(l.state == LEASE_READY for l in leases):
                evaluate_release(db, run, force_partial=True)
            elif not any(l.state in (LEASE_FREE, LEASE_CLAIMED) for l in leases):
                # No worker readied and none is still free or claimed: every
                # lease has lapsed to lost (the fleet came up then died before
                # readying). Nothing will ever ready, so fail the run rather than
                # sit in provisioning forever pinning the target's concurrent-GB
                # cap. (A fleet still free/claimed may yet ready and is left be;
                # a claimed worker that hangs lapses to lost first, then this
                # fires.)
                transition_run(db, run, STATE_FAILED,
                               {"reason": "no worker reached ready",
                                "leases": len(leases)},
                               end_reason="provision-timeout")
                _destroy_fleet(db, run, drivers, "provision-timeout")
                log.warning("run %s failed: no worker ready within %.0fs",
                            run.id, PROVISION_TIMEOUT_S)

    # 3. Duration end: a running/releasing run past its duration begins draining.
    _check_duration(db, run, now)

    # 4. Auto-abort policies (a subset per the contract).
    _check_auto_abort(db, run, leases, now, drivers)

    # 5. Draining runs whose grace has elapsed get their fleet destroyed.
    _reap_draining(db, run, drivers, now)

    # 6. Completion: all leases resolved -> terminal state.
    maybe_complete_run(db, run)


def _lapse_stale_leases(db, run, leases, boot_time, now):
    # type: (Session, Run, Sequence[WorkerLease], datetime.datetime, datetime.datetime) -> None
    """Mark claimed/ready/running leases lost once their heartbeat deadline passes.

    Deadline = ``max(last_heartbeat_at + LEASE_LAPSE_S, boot_time + BOOT_GRACE_S)``
    so steady state lapses 30 s after the last ack, but a control-plane restart
    grants the whole estate 60 s to re-establish contact before any lapse.
    """
    live = (LEASE_CLAIMED, LEASE_READY, LEASE_RUNNING)
    boot_deadline = _as_aware(boot_time) + datetime.timedelta(seconds=BOOT_GRACE_S)
    for lease in leases:
        if lease.state not in live:
            continue
        last = _as_aware(lease.last_heartbeat_at or run.created_at or boot_time)
        hb_deadline = last + datetime.timedelta(seconds=LEASE_LAPSE_S)
        deadline = max(hb_deadline, boot_deadline)
        if now > deadline:
            lease.state = LEASE_LOST
            append_event(db, run, "lease_lost",
                         {"slot": lease.slot, "holder": lease.holder,
                          "silent_for_s": round(_seconds_between(last, now), 1)})
            log.info("run %s slot %s lease lost (silent %.0fs)",
                     run.id, lease.slot, _seconds_between(last, now))


def _check_duration(db, run, now):
    # type: (Session, Run, datetime.datetime) -> None
    """Begin draining a run that has reached its bounded duration.

    Duration is measured from the run's T0 (the pacing anchor). Unbounded runs
    (no ``duration_s``) never end on their own; they finish on stop or when all
    workers finalise.
    """
    if run.state not in (STATE_RUNNING, STATE_RELEASING):
        return
    if run.t0 is None:
        return
    snap = run.spec_snapshot_json or {}
    duration_s = snap.get("duration_s")
    if not duration_s:
        return
    end_at = _as_aware(run.t0) + datetime.timedelta(seconds=float(duration_s))
    if now >= end_at:
        transition_run(db, run, STATE_DRAINING,
                       {"reason": "duration-complete", "duration_s": duration_s},
                       end_reason="duration-complete")
        log.info("run %s reached duration %ss; draining", run.id, duration_s)


def _check_auto_abort(db, run, leases, now, drivers=None):
    # type: (Session, Run, Sequence[WorkerLease], datetime.datetime, Optional[Mapping[str, ExecutionDriver]]) -> None
    """Fail a run whose fleet has degraded past the auto-abort thresholds.

    Two policies (a subset of the design's set):

    * **Lost subset**: more than ``AUTO_ABORT_LOST_FRACTION`` of leases lost and
      the condition has persisted for ``AUTO_ABORT_LOST_S`` (measured from the
      most recent lapse) -> fail.
    * **HEC auth failure**: at least half the fleet reported ``auth_failed`` ->
      fail and flag the target unhealthy (the token is almost certainly bad).
    """
    if run.state in TERMINAL_STATES or run.state == STATE_DRAINING:
        return
    total = len(leases)
    if total < 1:
        return

    # The lost-subset abort only applies to a released run: before T0, missing
    # workers are handled by the release gate (partial release / strict fail),
    # and never-claimed stragglers absorbed by a degraded release must not be
    # mistaken for a mid-run collapse.
    if run.t0 is not None:
        # Count only leases that were lost *after running* (they carry a last
        # heartbeat instant); never-heartbeated stragglers do not count.
        lost_running = [
            l for l in leases
            if l.state == LEASE_LOST and l.last_heartbeat_at is not None
        ]
        if len(lost_running) > AUTO_ABORT_LOST_FRACTION * total:
            # Persisted for the abort window? Use the most recent lapse instant
            # (the condition must have held that long, not just flickered).
            latest_lapse = max(l.last_heartbeat_at for l in lost_running)
            if _seconds_between(latest_lapse, now) >= AUTO_ABORT_LOST_S:
                transition_run(db, run, STATE_FAILED,
                               {"lost": len(lost_running), "total": total},
                               end_reason="auto-abort-lost")
                log.warning("run %s auto-aborted: %d/%d leases lost",
                            run.id, len(lost_running), total)
                _destroy_fleet(db, run, drivers, "auto-abort-lost")
                return

    # HEC auth failure across half the fleet.
    auth_slots = _auth_failed_slots(db, run)
    if auth_slots and len(auth_slots) >= max(1, total // 2):
        transition_run(db, run, STATE_FAILED,
                       {"auth_failed_slots": sorted(auth_slots)},
                       end_reason="auto-abort-auth")
        _flag_target_unhealthy(db, run, "HEC auth failed across the fleet")
        log.warning("run %s auto-aborted: HEC auth failed on %d/%d slots",
                    run.id, len(auth_slots), total)
        _destroy_fleet(db, run, drivers, "auto-abort-auth")


def _reap_draining(db, run, drivers, now):
    # type: (Session, Run, Mapping[str, ExecutionDriver], datetime.datetime) -> None
    """Destroy a draining run's fleet once the stop grace has elapsed.

    A run enters ``draining`` on stop or duration end; workers drain within the
    SIGTERM budget. After ``STOP_GRACE_S`` past the drain event we destroy the
    workload so a wedged worker cannot pin the fleet open. Completion to the
    terminal state is handled once the leases resolve.
    """
    if run.state != STATE_DRAINING:
        return
    drain_since = _last_state_ts(db, run, STATE_DRAINING)
    if drain_since is None:
        return
    if _seconds_between(drain_since, now) < STOP_GRACE_S:
        return
    _destroy_fleet(db, run, drivers, "stop-grace")
    # Any lease still not final after the grace is declared lost so the run can
    # complete rather than hang on an unresponsive worker.
    for lease in get_run_leases(db, run):
        if lease.state not in (LEASE_DONE, LEASE_LOST):
            lease.state = LEASE_LOST
            append_event(db, run, "lease_lost",
                         {"slot": lease.slot, "reason": "drain-grace-expired"})


def _destroy_fleet(db, run, drivers, why):
    # type: (Session, Run, Optional[Mapping[str, ExecutionDriver]], str) -> None
    """Best-effort ``driver.destroy`` for a run's fleet (idempotent).

    Used on auto-abort and after the stop grace. A ``None`` drivers map (or a run
    with no resolvable driver / no ``DriverRef``) is a no-op: the workload either
    was never created or its driver is not reachable this tick, and a failed
    destroy is logged, never raised (a reap must not stall the supervisor).
    """
    ref = driver_ref_of(run)
    if ref is None or drivers is None:
        return
    driver = get_run_driver(db, run, drivers)
    if driver is None:
        return
    try:
        driver.destroy(ref)
        append_event(db, run, "fleet_destroyed", {"why": why})
    except Exception as exc:
        log.warning("run %s driver.destroy failed (%s): %s", run.id, why, exc)
        append_event(db, run, "driver_error", {"op": "destroy", "error": str(exc)})


def _auth_failed_slots(db, run):
    # type: (Session, Run) -> set
    """Return the set of slots that have reported a HEC auth failure."""
    stmt = select(RunEvent).where(
        RunEvent.run_id == run.id, RunEvent.kind == "hec_auth_failed")
    slots = set()
    for event in db.execute(stmt).scalars().all():
        slot = (event.detail_json or {}).get("slot")
        if slot is not None:
            slots.add(slot)
    return slots


def _flag_target_unhealthy(db, run, detail):
    # type: (Session, Run, str) -> None
    """Mark the run's target ``red`` (used when the fleet reports HEC auth fail)."""
    spec = getattr(run, "spec", None)
    target = getattr(spec, "target", None) if spec is not None else None
    if target is None:
        return
    target.health_state = "red"
    target.health_detail = detail
    target.last_health_at = utcnow()


def _last_state_ts(db, run, state):
    # type: (Session, Run, str) -> Optional[datetime.datetime]
    """Timestamp of the most recent ``state`` run_event transitioning *to* ``state``."""
    # Autoflush is off in this app; flush so a transition appended earlier in the
    # same tick is visible to this read-back query.
    db.flush()
    stmt = (
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.kind == "state")
        .order_by(RunEvent.ts.desc())
    )
    for event in db.execute(stmt).scalars().all():
        if (event.detail_json or {}).get("to") == state:
            return event.ts
    return None


def _as_aware(value):
    # type: (datetime.datetime) -> datetime.datetime
    """Coerce a datetime to tz-aware UTC (SQLite round-trips lose the tzinfo).

    The models store tz-aware UTC, but a value read back from SQLite can be
    naive; assume such values are UTC so comparisons against :func:`utcnow`
    never raise ``can't compare offset-naive and offset-aware``.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _seconds_between(earlier, later):
    # type: (Optional[datetime.datetime], datetime.datetime) -> float
    """Seconds from ``earlier`` to ``later``, tolerating naive/aware mixes.

    SQLite may hand back naive datetimes; both sides are coerced to aware UTC so
    the subtraction never raises.
    """
    if earlier is None:
        return 0.0
    return (_as_aware(later) - _as_aware(earlier)).total_seconds()


def reconcile_on_boot(db, drivers):
    # type: (Session, Mapping[str, ExecutionDriver]) -> None
    """Reconcile DB runs against live driver workloads at startup.

    Two phases, in order:

    1. **DB-driven adopt/fail** (the source of truth): for each non-terminal DB
       run, probe its stored :class:`DriverRef` via ``driver.status``. A run whose
       workload is still present is adopted (left running); a run whose workload
       has vanished (or that never got a ``DriverRef``) is failed as orphaned.

    2. **Stray fleet sweep**: for each fleet driver that supports enumeration
       (:meth:`~server.drivers.base.ExecutionDriver.list_run_ids`, the optional
       7th method), list every labelled workload it owns and destroy any whose
       run id is **not** a live (non-terminal) DB run — an orphan fleet left
       blasting a target after a control-plane crash that missed a snapshot. A
       workload mapping to a live run is never touched. A driver that cannot
       enumerate (``NotImplementedError``) or whose enumeration errors is skipped
       for the sweep — an enumeration failure is treated as "skip", never as "all
       strays" (which would destroy the estate).

    Writes a ``run_event`` (or a loud audit log for a run-less stray) per action.
    """
    active_states = (
        STATE_PROVISIONING, STATE_RELEASING, STATE_RUNNING, STATE_DRAINING,
        STATE_PREPARING, STATE_PENDING,
    )
    runs = list(db.execute(select(Run).where(Run.state.in_(active_states))).scalars().all())
    for run in runs:
        ref = driver_ref_of(run)
        if ref is None:
            # Never launched (crash mid-provision): no workload to adopt.
            transition_run(db, run, STATE_FAILED,
                           {"reason": "no driver ref at boot"},
                           end_reason="orphaned")
            log.warning("boot: run %s failed (no driver ref)", run.id)
            continue
        driver = get_run_driver(db, run, drivers)
        if driver is None:
            log.info("boot: run %s fleet driver unavailable; leaving as-is", run.id)
            continue
        try:
            status = driver.status(ref)
        except Exception as exc:
            log.warning("boot: run %s status probe failed: %s", run.id, exc)
            continue
        if status.running <= 0 and status.desired <= 0:
            # Workload is gone: fail the orphaned DB run.
            transition_run(db, run, STATE_FAILED,
                           {"reason": "workload absent at boot"},
                           end_reason="orphaned")
            for lease in get_run_leases(db, run):
                if lease.state not in (LEASE_DONE, LEASE_LOST):
                    lease.state = LEASE_LOST
            log.warning("boot: run %s failed (workload gone)", run.id)
        else:
            # Adopt: the fleet is still up; the supervisor resumes driving it.
            append_event(db, run, "adopted",
                         {"desired": status.desired, "running": status.running})
            log.info("boot: adopted run %s (desired=%d running=%d)",
                     run.id, status.desired, status.running)

    # Phase 2: sweep stray workloads with no live DB run. Runs after phase 1 so a
    # run just failed/adopted above is reflected in the live set.
    _sweep_stray_fleets(db, drivers)


def _live_run_ids(db):
    # type: (Session) -> set
    """The run ids of every non-terminal (live) run — the stray-sweep whitelist.

    A workload whose run id is in this set maps to a run the control plane still
    owns and must never be destroyed as a stray. Read after phase 1's flush so a
    run failed this boot (its workload legitimately gone) is already excluded.
    """
    db.flush()
    stmt = select(Run.id).where(Run.state.notin_(tuple(TERMINAL_STATES)))
    return {row for row in db.execute(stmt).scalars().all()}


def _sweep_stray_fleets(db, drivers):
    # type: (Session, Mapping[str, ExecutionDriver]) -> None
    """Destroy labelled workloads whose run id is not a live DB run.

    For each distinct fleet driver, enumerate its owned workloads via
    :meth:`~server.drivers.base.ExecutionDriver.list_run_ids`; any run id absent
    from :func:`_live_run_ids` is a stray and is destroyed on a synthesised ref.

    Safety invariants:

    * a driver that cannot enumerate (``NotImplementedError``) is skipped;
    * an enumeration **error** (``DriverError``/any exception) is skipped and
      logged — never coerced into "everything is a stray";
    * a workload mapping to a live run is never destroyed;
    * a per-stray destroy failure is logged and the sweep continues.
    """
    if not drivers:
        return
    live = _live_run_ids(db)
    # One enumeration per distinct driver instance (a fleet map may point several
    # names at the same driver; id() de-dupes so we do not list an estate twice).
    seen = set()  # type: set
    for fleet_name, driver in drivers.items():
        if driver is None or id(driver) in seen:
            continue
        seen.add(id(driver))
        try:
            owned = driver.list_run_ids()
        except NotImplementedError:
            log.info("boot sweep: driver for fleet %s cannot enumerate; skipping",
                     fleet_name)
            continue
        except Exception as exc:  # DriverError, transport blip, etc.
            # Never treat "could not list" as "no workloads == all strays".
            log.warning("boot sweep: enumeration for fleet %s failed (%s); "
                        "skipping sweep for this driver", fleet_name, exc)
            continue
        strays = {rid for rid in owned if rid not in live}
        if not strays:
            log.info("boot sweep: fleet %s clean (%d owned, 0 stray)",
                     fleet_name, len(owned))
            continue
        for run_id in sorted(strays):
            _destroy_stray(db, driver, fleet_name, run_id)


def _destroy_stray(db, driver, fleet_name, run_id):
    # type: (Session, ExecutionDriver, str, int) -> None
    """Destroy one stray workload (run id has no live DB run) on a synthesised ref.

    The ref is reconstructed from the driver's own naming scheme (the workload
    was created with ``stoker.run=<id>`` and named ``stoker-run-<id>``), so
    ``driver.destroy`` addresses exactly the labelled workload the sweep found.
    Loud by design: this is a fleet the control plane lost track of, so it is
    recorded on both the failed run's audit trail (when the row still exists) and
    the log. A destroy failure is logged, never raised (one wedged stray must not
    stall boot).
    """
    ref = _synthesise_stray_ref(driver, run_id)
    log.warning("boot sweep: STRAY fleet run=%s on fleet %s (no live DB run) -> "
                "destroying", run_id, fleet_name)
    try:
        driver.destroy(ref)
    except Exception as exc:
        log.warning("boot sweep: destroy of stray run=%s (fleet %s) failed: %s",
                    run_id, fleet_name, exc)
        _audit_stray(db, run_id, {"fleet": fleet_name, "action": "destroy_failed",
                                  "error": str(exc)})
        return
    _audit_stray(db, run_id, {"fleet": fleet_name, "action": "destroyed"})


def _synthesise_stray_ref(driver, run_id):
    # type: (ExecutionDriver, int) -> DriverRef
    """Build a destroyable :class:`DriverRef` for a stray, per the driver's kind.

    Enumeration yields only a run id; ``driver.destroy`` needs a ref. Every driver
    names a run's workload deterministically from its id (``stoker-run-<id>`` for
    swarm services and k8s Jobs), so the ref is reconstructed rather than stored.
    The driver ``kind`` is read off the instance (``_KIND``); ``raw`` always
    carries ``run_id`` so the FakeDriver — whose native fleet id is opaque — can
    still resolve the target by run id.
    """
    kind = getattr(driver, "_KIND", None) or _driver_kind(driver)
    name = "stoker-run-%s" % run_id
    raw = {"run_id": run_id, "name": name}  # type: Dict[str, Any]
    if kind == "k8s":
        # k8s destroy addresses the Job by name within a namespace.
        raw["namespace"] = getattr(driver, "_namespace", None) or "stoker"
    return DriverRef(kind=kind or "", id=name, raw=raw)


def _driver_kind(driver):
    # type: (ExecutionDriver) -> str
    """Best-effort driver-kind label from the class name (fallback for _KIND)."""
    cls = driver.__class__.__name__.lower()
    if "swarm" in cls:
        return "swarm"
    if "k8s" in cls or "kube" in cls:
        return "k8s"
    if "fake" in cls:
        return "fake"
    return ""


def _audit_stray(db, run_id, detail):
    # type: (Session, int, Dict[str, Any]) -> None
    """Record a stray-sweep action on the run's audit trail when the row exists.

    A stray usually has a terminal (failed/orphaned) run row it belonged to; the
    event lands there for traceability. When no row exists at all (the snapshot
    that would have created it was lost — the very crash this sweep guards
    against) the action is captured in the log only, since ``run_events.run_id``
    is a foreign key and cannot reference a nonexistent run.
    """
    run = db.get(Run, run_id)
    if run is not None:
        append_event(db, run, "stray_destroyed", detail)


# --------------------------------------------------------------------------- #
# Agent-facing transitions (STUBBED — Core fills). These are called by the
# agent routes; they return exactly what the route serialises to the worker.
# --------------------------------------------------------------------------- #

def claim_lease(db, run, holder, hint_slot=None, settings=None):
    # type: (Session, Run, str, Optional[int], Optional[Settings]) -> WorkerLease
    """Issue a lease to a claiming worker and return it.

    Issue the lowest free lease (honour ``hint_slot`` when that slot's lease is
    free). Record ``holder``, set state ``claimed``, stamp ``last_heartbeat_at``,
    set ``effective_t0`` (run T0 on first claim, ``now`` on re-issue). A re-claim
    of a still-held lease by the same holder is idempotent (returns the same
    lease). The route turns the returned lease into a slice via
    :func:`build_slice`.
    """
    from fastapi import HTTPException

    if run.state in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="run is not accepting claims")

    # Serialise concurrent claims for this run. A whole fleet boots and claims at
    # once (swarm has no stable slot, so every worker hint_slot=None and races for
    # the lowest free lease). Without a lock two workers both read slot 0 as free
    # and double-allocate it. with_for_update takes row locks on Postgres (real
    # parallelism via the threadpool); SQLite already serialises writers so this
    # is a harmless no-op there.
    db.execute(
        select(WorkerLease).where(WorkerLease.run_id == run.id).with_for_update()
    )
    leases = get_run_leases(db, run)

    # Idempotent re-claim: a holder already holding a live lease gets it back
    # unchanged (a retried claim after a dropped response must not double-issue).
    for lease in leases:
        if (lease.holder == holder
                and lease.state in (LEASE_CLAIMED, LEASE_READY, LEASE_RUNNING)):
            lease.last_heartbeat_at = utcnow()
            append_event(db, run, "claim",
                         {"slot": lease.slot, "holder": holder, "reissue": "same-holder"},
                         actor="agent")
            return lease

    # A slot is claimable when its lease is free (never taken) or lost (its
    # previous holder lapsed; the share was freed for re-claim). Honour
    # ``hint_slot`` when that slot is claimable, else the lowest claimable slot.
    lease = _next_claimable(leases, hint_slot=hint_slot)
    if lease is None:
        raise HTTPException(status_code=409, detail="no free lease available")

    now = utcnow()
    taking_over = lease.state == LEASE_LOST
    # Fence every claim with a fresh lease_id. A first (free) claim must not hand
    # out the seeded id: were two claimers ever to converge on one row they would
    # otherwise share a lease_id and neither would be superseded. A takeover also
    # supersedes the lapsed holder's old id on its next heartbeat.
    lease.lease_id = new_lease_id()
    if taking_over:
        lease.restarts = (lease.restarts or 0) + 1

    # Re-issue on an already-released run anchors to now so the replacement
    # starts with zero backlog (the worker's effective_t0 pacing anchor). A
    # first claim before release anchors to the run T0 (None until release).
    reissue = run.t0 is not None
    lease.effective_t0 = now if reissue else run.t0
    lease.holder = holder
    lease.state = LEASE_CLAIMED
    lease.last_heartbeat_at = now
    append_event(db, run, "claim",
                 {"slot": lease.slot, "holder": holder,
                  "reissue": bool(reissue), "takeover": bool(taking_over)},
                 actor="agent")
    return lease


def _next_claimable(leases, hint_slot=None):
    # type: (Sequence[WorkerLease], Optional[int]) -> Optional[WorkerLease]
    """Pick the lease to issue on a claim: lowest free-or-lost slot.

    A ``free`` slot was never taken; a ``lost`` slot's previous holder lapsed and
    its share is available for re-claim (the claim then re-fences with a fresh
    lease_id). ``hint_slot`` wins when that slot is claimable.
    """
    claimable = [l for l in leases if l.state in (LEASE_FREE, LEASE_LOST)]
    if not claimable:
        return None
    if hint_slot is not None:
        for lease in claimable:
            if lease.slot == hint_slot:
                return lease
    return min(claimable, key=lambda l: l.slot)


def mark_ready(db, run, slot, lease_id):
    # type: (Session, Run, int, Optional[str]) -> None
    """Mark a lease ready after the worker has warmed its engine.

    Validates ``lease_id`` is the current holder of ``slot`` (else the route
    returns 409). When all N leases are ready (or the provisioning timeout has
    fired) set ``t0 = now + RELEASE_DELAY_S`` and move the run to
    releasing/running via :func:`evaluate_release`.
    """
    from fastapi import HTTPException

    lease = find_lease(db, run, slot)
    if lease is None:
        raise HTTPException(status_code=409, detail="unknown slot")
    if not is_lease_holder(lease, lease_id):
        # Not the current holder of this slot: fencing rejects with 409.
        raise HTTPException(status_code=409, detail="lease is not the slot holder")

    now = utcnow()
    lease.last_heartbeat_at = now
    if lease.state in (LEASE_CLAIMED, LEASE_READY):
        lease.state = LEASE_READY
        append_event(db, run, "ready", {"slot": slot}, actor="agent")
    # All non-lost leases ready -> set T0 and release the fleet.
    evaluate_release(db, run)


def record_heartbeat(db, run, slot, lease_id, payload, settings=None):
    # type: (Session, Run, int, Optional[str], Mapping[str, Any], Optional[Settings]) -> Dict[str, Any]
    """Process a heartbeat and return the command dict for the worker.

    A successful heartbeat renews the lease (``last_heartbeat_at = now``) and
    appends the counters to ``metric_samples`` (parse defensively via
    :func:`counters_from_payload`). The returned command is one of (built with
    the helpers below):

    * ``{"command": "superseded"}`` when ``lease_id`` is not the slot holder,
    * ``{"command": "drain"}`` when the run is draining/stopping or the
      ``protocol_version`` is unsupported,
    * ``{"command": "release", "t0": "<iso>"}`` once T0 is set until the worker
      has acked past it,
    * ``{"command": "retarget", "share": {...}}`` when the stored share changed,
    * ``{"command": "continue"}`` otherwise.

    May additionally carry ``"jwt": "<fresh>"`` when the current token is within
    ``JWT_REFRESH_FRACTION`` of expiry (see :func:`maybe_refresh_jwt`).

    Returns:
        the command dict (the route wraps it in ``HeartbeatCommand``).
    """
    if settings is None:
        settings = get_settings()

    # The raw bearer rides in under "_bearer" for the JWT-refresh decision only.
    # Pop it before anything touches metric_samples; never log it.
    payload = dict(payload)
    bearer = payload.pop("_bearer", None)

    lease = find_lease(db, run, slot)
    if not is_lease_holder(lease, lease_id):
        # This lease_id no longer owns the slot (superseded / removed): the
        # worker treats superseded as a fatal drain. No renewal, no counters.
        return cmd_superseded()

    now = utcnow()
    lease.last_heartbeat_at = now

    # Persist the counters (defensively parsed; ignores the popped _bearer).
    db.add(build_metric_sample(run, slot, payload))

    # HEC auth failure is not a metric_samples column; surface it on the audit
    # trail so the supervisor can auto-abort when it spans half the fleet.
    if payload.get("auth_failed"):
        append_event(db, run, "hec_auth_failed", {"slot": slot}, actor="agent")

    protocol_version = payload.get("protocol_version", 1)

    command = _heartbeat_command(db, run, lease, protocol_version, now)

    # Rolling JWT refresh: attach a fresh token when the presented one is close
    # to expiry. Never attached to a superseded/terminal path (handled above).
    fresh = maybe_refresh_jwt(run, bearer, settings=settings)
    if fresh:
        command = dict(command)
        command["jwt"] = fresh
    return command


# Protocol versions the control plane speaks to workers. A worker announcing
# anything outside this set is told to drain (a clean, logged eviction rather
# than a silent mismatch).
SUPPORTED_PROTOCOL_VERSIONS = frozenset((1,))


def _heartbeat_command(db, run, lease, protocol_version, now):
    # type: (Session, Run, WorkerLease, Any, datetime.datetime) -> Dict[str, Any]
    """Decide the heartbeat command for a renewed lease (pure precedence).

    Precedence: drain (run stopping / unsupported protocol) > release (T0 set,
    worker not yet past it) > retarget (stored share changed) > continue.
    """
    # Drain: the run is winding down, or the worker speaks a protocol we do not.
    if run.state in (STATE_DRAINING,) or run.state in TERMINAL_STATES:
        return cmd_drain()
    if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        log.info("run %s slot %s heartbeat protocol_version %r unsupported; draining",
                 run.id, lease.slot, protocol_version)
        return cmd_drain()

    # A recovered lease (was lost, still the holder) is restored to running once
    # T0 has passed, or ready before it.
    if lease.state == LEASE_LOST:
        lease.state = LEASE_RUNNING if run.t0 is not None else LEASE_READY

    # Release: T0 is set and this worker has not yet been promoted past it. The
    # worker acks release in its pre-generation poll; once T0 has elapsed we
    # promote the lease to running and stop repeating the release command.
    if run.t0 is not None and lease.state in (LEASE_CLAIMED, LEASE_READY):
        if now >= _as_aware(run.t0):
            lease.state = LEASE_RUNNING
            append_event(db, run, "running", {"slot": lease.slot}, actor="agent")
            # Once the whole fleet is running, promote the run to running.
            _maybe_mark_running(db, run)
            return cmd_continue()
        return cmd_release(run.t0)

    # Retarget: a scale/rescale changed this slot's share; push it once.
    share = lease.share_json or {}
    if RETARGET_MARKER in share:
        clear_retarget(lease)
        if lease.state in (LEASE_CLAIMED, LEASE_READY):
            lease.state = LEASE_RUNNING if run.t0 is not None else lease.state
        return cmd_retarget(public_share(share))

    # Steady state.
    if lease.state == LEASE_READY and run.t0 is not None:
        lease.state = LEASE_RUNNING
    return cmd_continue()


def _maybe_mark_running(db, run):
    # type: (Session, Run) -> None
    """Advance a releasing run to ``running`` once a lease is generating.

    The run reaches ``running`` as soon as the first worker crosses T0; leases
    that are still catching up do not hold the run back (the release gate has
    already fired).
    """
    if run.state == STATE_RELEASING:
        transition_run(db, run, STATE_RUNNING)


def record_final(db, run, slot, summary, log_tail):
    # type: (Session, Run, int, Mapping[str, Any], Sequence[str]) -> None
    """Record a worker's final report and finalise its lease.

    Store ``final_log_tail``, fold ``summary`` into ``runs.totals_json`` (via
    :func:`fold_totals`), mark the lease ``done``. When all leases are done/lost
    move the run to its terminal state as the drain reason dictates (via
    :func:`maybe_complete_run`).
    """
    from fastapi import HTTPException

    lease = find_lease(db, run, slot)
    if lease is None:
        raise HTTPException(status_code=409, detail="unknown slot")

    # Idempotent: a duplicate final (retried POST) must not double-fold totals.
    already_final = lease.state == LEASE_DONE
    lease.final_log_tail_json = list(log_tail or [])
    if not already_final:
        fold_totals(run, summary)
        lease.state = LEASE_DONE
        append_event(db, run, "final",
                     {"slot": slot, "reason": (summary or {}).get("reason")},
                     actor="agent")

    # When every lease has resolved (done/lost) the run reaches its terminal
    # state; the reason recorded on the run selects completed/stopped/failed.
    maybe_complete_run(db, run)


# --------------------------------------------------------------------------- #
# Release / completion evaluation (STUBBED — Core fills; called by the above).
# --------------------------------------------------------------------------- #

def evaluate_release(db, run, force_partial=False):
    # type: (Session, Run, bool) -> bool
    """Evaluate whether to set T0 and release the fleet.

    When every non-lost lease is ready (or ``force_partial`` from the supervisor
    timeout), set ``t0 = now + RELEASE_DELAY_S``, move ``provisioning`` ->
    ``releasing``, and (on partial release) re-apportion across the ready slots
    and set ``degraded``. Returns True when T0 was set this call.
    """
    if run.t0 is not None:
        return False  # already released
    if run.state not in (STATE_PROVISIONING, STATE_PREPARING, STATE_RELEASING):
        return False

    leases = get_run_leases(db, run)
    ready = [l for l in leases if l.state == LEASE_READY]
    if not ready:
        return False  # nothing to release yet

    pending = [l for l in leases if l.state in (LEASE_FREE, LEASE_CLAIMED)]

    if pending and not force_partial:
        return False  # wait for the stragglers (or the supervisor timeout)

    if pending and force_partial:
        # Partial release: the stragglers are declared lost, their slots freed
        # from the apportionment, and the run runs degraded unless strict.
        snap = run.spec_snapshot_json or {}
        if snap.get("strict_release"):
            for lease in pending:
                lease.state = LEASE_LOST
            transition_run(db, run, STATE_FAILED,
                           {"missing_slots": [l.slot for l in pending]},
                           end_reason="strict-release-timeout")
            return False
        for lease in pending:
            lease.state = LEASE_LOST
        run.degraded = True
        # Re-apportion the run rate across only the ready slots so the delivered
        # rate stays on target with fewer workers.
        _reapportion_ready(run, ready)
        append_event(db, run, "partial_release",
                     {"ready_slots": [l.slot for l in ready],
                      "lost_slots": [l.slot for l in pending]})

    run.t0 = utcnow() + datetime.timedelta(seconds=RELEASE_DELAY_S)
    transition_run(db, run, STATE_RELEASING,
                   {"t0": iso_from_dt(run.t0),
                    "workers_ready": len(ready)})
    log.info("run %s released: T0=%s (%d ready%s)", run.id, iso_from_dt(run.t0),
             len(ready), ", degraded" if run.degraded else "")
    return True


def _reapportion_ready(run, ready_leases):
    # type: (Run, Sequence[WorkerLease]) -> None
    """Re-apportion the run's total rate across a ready subset of leases.

    Used on a partial (degraded) release: the same run rate is split across the
    slots that actually reported ready, flagging each to retarget so the workers
    pick up their larger share on the next heartbeat.
    """
    snap = run.spec_snapshot_json or {}
    rate_mode = snap.get("rate_mode") or "eps"
    rate_value = snap.get("rate_value")
    n = len(ready_leases)
    if n < 1:
        return
    shares = build_share_list(rate_mode, rate_value, n)
    for lease, share in zip(sorted(ready_leases, key=lambda l: l.slot), shares):
        mark_retarget(lease, share)


def maybe_complete_run(db, run):
    # type: (Session, Run) -> bool
    """Move a run to its terminal state when all leases are done/lost.

    Chooses ``completed`` / ``stopped`` / ``failed`` from the run's
    ``end_reason`` and current state (a draining run whose stop was operator-
    initiated ends ``stopped``; natural duration end ends ``completed``; an
    auto-abort ends ``failed``). Returns True when it transitioned.
    """
    if run.state in TERMINAL_STATES:
        return False
    # Completion only applies once a run has been released (running) or is
    # winding down (draining). Before T0 (pending/preparing/provisioning/
    # releasing) a lost or free lease is normal churn — a lapsed worker may
    # recover or a replacement may claim its slot — so the run must not
    # terminate on it; the release gate and auto-abort own the pre-T0 path.
    if run.state not in (STATE_RUNNING, STATE_DRAINING):
        return False
    leases = get_run_leases(db, run)
    if not leases:
        return False
    # A run completes when no lease is still *live* (a worker actively holding
    # it) and at least one lease has reached a terminal outcome. A ``free`` lease
    # has no holder and never finalises, so it counts as resolved for completion
    # (this lets a drained/stopped run with never-claimed slots finish); but a
    # run whose leases are all ``free`` has nothing done/lost yet, so it does not
    # complete prematurely.
    live = (LEASE_CLAIMED, LEASE_READY, LEASE_RUNNING)
    if any(l.state in live for l in leases):
        return False  # at least one worker still live
    if not any(l.state in (LEASE_DONE, LEASE_LOST) for l in leases):
        return False  # nothing has actually run/resolved yet

    reason = run.end_reason
    terminal = _terminal_state_for(run, reason, leases)
    transition_run(db, run, terminal,
                   {"leases": {"done": sum(1 for l in leases if l.state == LEASE_DONE),
                               "lost": sum(1 for l in leases if l.state == LEASE_LOST)}},
                   end_reason=reason or _default_end_reason(terminal, leases))
    return True


def _terminal_state_for(run, reason, leases):
    # type: (Run, Optional[str], Sequence[WorkerLease]) -> str
    """Pick the terminal state for a fully-resolved run.

    An operator stop ends ``stopped``; an auto-abort / provision failure ends
    ``failed``; anything else (natural duration end, drain-complete, all workers
    reported final) ends ``completed``. A run where *every* lease is lost (no
    worker ever finalised) is a failure, not a completion.
    """
    if reason in _FAILED_REASONS:
        return STATE_FAILED
    if reason in _STOPPED_REASONS:
        return STATE_STOPPED
    # No worker finalised cleanly (every resolved lease was lost): a failure,
    # not a completion.
    done = [l for l in leases if l.state == LEASE_DONE]
    lost = [l for l in leases if l.state == LEASE_LOST]
    if lost and not done:
        return STATE_FAILED
    return STATE_COMPLETED


def _default_end_reason(terminal, leases):
    # type: (str, Sequence[WorkerLease]) -> str
    if terminal == STATE_COMPLETED:
        return "completed"
    if terminal == STATE_STOPPED:
        return "operator-stop"
    done = [l for l in leases if l.state == LEASE_DONE]
    lost = [l for l in leases if l.state == LEASE_LOST]
    if lost and not done:
        return "all-workers-lost"
    return "failed"


# End reasons that select each terminal state when a run fully resolves.
_STOPPED_REASONS = frozenset(("operator-stop",))
_FAILED_REASONS = frozenset((
    "provision-failed", "strict-release-timeout", "auto-abort-lost",
    "auto-abort-auth", "all-workers-lost", "orphaned",
))


# --------------------------------------------------------------------------- #
# Pure helpers — IMPLEMENTED here so every builder shares one copy.
# --------------------------------------------------------------------------- #

def append_event(db, run, kind, detail=None, actor="system"):
    # type: (Session, Run, str, Optional[Dict[str, Any]], str) -> RunEvent
    """Append a ``run_events`` row (the audit trail for every transition).

    Does not commit; the caller's unit of work owns the transaction. Returns the
    new event (already added to the session).
    """
    event = RunEvent(
        run_id=run.id,
        ts=utcnow(),
        actor=actor,
        kind=kind,
        detail_json=detail or {},
    )
    db.add(event)
    return event


def transition_run(db, run, new_state, detail=None, actor="system", end_reason=None):
    # type: (Session, Run, str, Optional[Dict[str, Any]], str, Optional[str]) -> None
    """Set ``run.state`` and append a ``state`` run_event recording from/to.

    Terminal states additionally stamp ``ended_at`` (and ``end_reason`` when
    provided). Idempotent for an unchanged state (still no-ops rather than
    writing a spurious event) unless ``end_reason`` is being set.
    """
    old_state = run.state
    changed = old_state != new_state
    if changed:
        run.state = new_state
    if end_reason is not None and run.end_reason != end_reason:
        run.end_reason = end_reason
        changed = True
    if new_state in TERMINAL_STATES and run.ended_at is None:
        run.ended_at = utcnow()
        changed = True
    if not changed:
        return
    payload = {"from": old_state, "to": new_state}
    if end_reason is not None:
        payload["end_reason"] = end_reason
    if detail:
        payload.update(detail)
    append_event(db, run, "state", payload, actor=actor)
    log.info("run %s: %s -> %s%s", run.id, old_state, new_state,
             (" (%s)" % end_reason) if end_reason else "")
    # Dogfood: ship a stoker:job event for this transition when enabled. Entirely
    # optional and failure-isolated — a HEC hiccup must never break a transition.
    _emit_dogfood_transition(run, old_state, new_state)


def _emit_dogfood_transition(run, old_state, new_state):
    # type: (Run, str, str) -> None
    """Best-effort dogfood job-event emit for a state change (never raises).

    Delegates to :mod:`server.metrics_lifecycle`, which no-ops when dogfood is
    disabled and swallows any HEC error. Imported lazily to avoid a module import
    cycle and wrapped so a telemetry fault can never disturb the lifecycle.
    """
    try:
        from . import metrics_lifecycle

        metrics_lifecycle.emit_run_transition_event(run, old_state, new_state)
    except Exception as exc:  # pragma: no cover - defensive; telemetry is optional
        log.debug("dogfood transition emit skipped: %s", type(exc).__name__)


def new_lease_id():
    # type: () -> str
    """Generate a globally-unique lease id (``le_<hex>``)."""
    return "le_" + secrets.token_hex(6)


def next_free_slot(leases, hint_slot=None):
    # type: (Sequence[WorkerLease], Optional[int]) -> Optional[WorkerLease]
    """Pick the lease to issue on a claim.

    Honour ``hint_slot`` when that slot's lease is free; otherwise return the
    lowest-slot free lease. Returns ``None`` when no lease is free.
    """
    free = [l for l in leases if l.state == LEASE_FREE]
    if not free:
        return None
    if hint_slot is not None:
        for lease in free:
            if lease.slot == hint_slot:
                return lease
    return min(free, key=lambda l: l.slot)


def public_share(share):
    # type: (Optional[Mapping[str, Any]]) -> Dict[str, float]
    """Return the wire-visible single-key share, dropping private markers.

    A lease's ``share_json`` carries the single rate-mode key the worker reads
    (``eps`` | ``per_day_gb`` | ``count``) and may additionally carry private
    bookkeeping keys prefixed ``_`` (e.g. ``_retarget`` set by scale/rescale so
    the next heartbeat pushes the change). Those never cross the wire.
    """
    out = {}  # type: Dict[str, float]
    for key, value in (share or {}).items():
        if key.startswith("_"):
            continue
        out[key] = value
    return out


RETARGET_MARKER = "_retarget"


def mark_retarget(lease, share):
    # type: (WorkerLease, Optional[Mapping[str, float]]) -> None
    """Set a lease's share and flag it so the next heartbeat pushes ``retarget``.

    Writes the new single-key share into ``share_json`` and stamps the private
    ``_retarget`` marker. Re-assigns ``share_json`` (rather than mutating in
    place) so the ORM tracks the change on a JSON column.
    """
    new_share = dict(share or {})
    new_share[RETARGET_MARKER] = True
    lease.share_json = new_share


def clear_retarget(lease):
    # type: (WorkerLease) -> None
    """Drop the ``_retarget`` marker once its share has been pushed to the worker."""
    share = dict(lease.share_json or {})
    if RETARGET_MARKER in share:
        share.pop(RETARGET_MARKER, None)
        lease.share_json = share


def share_for_mode(rate_mode, value):
    # type: (str, Optional[float]) -> Dict[str, float]
    """Build a single-key share dict for a rate mode (matches the worker).

    ``eps`` -> ``{"eps": v}``, ``per_day_gb`` -> ``{"per_day_gb": v}``,
    ``count_interval`` -> ``{"count": v}``. The value is kept as a float; the
    worker coerces it. ``count_interval`` with ``value`` None yields
    ``{"count": 0}``.
    """
    if rate_mode == "eps":
        return {"eps": float(value or 0.0)}
    if rate_mode == "per_day_gb":
        return {"per_day_gb": float(value or 0.0)}
    if rate_mode == "count_interval":
        return {"count": float(value or 0.0)}
    raise ValueError("unknown rate_mode %r" % rate_mode)


def substitute_slot(value, slot):
    # type: (Any, int) -> Any
    """Substitute ``{slot}`` in a string override value; pass non-strings through."""
    if isinstance(value, str):
        return value.replace("{slot}", str(slot))
    return value


def resolve_overrides(base_overrides, slot):
    # type: (Optional[Mapping[str, Any]], int) -> Dict[str, str]
    """Apply ``{slot}`` substitution across an overrides map for a given slot.

    Drops keys whose value is None (matching the worker's slice semantics, which
    filters null override values).
    """
    out = {}  # type: Dict[str, str]
    for key, value in (base_overrides or {}).items():
        if value is None:
            continue
        out[str(key)] = str(substitute_slot(value, slot))
    return out


def apportion_weights(workers):
    # type: (int) -> List[float]
    """Equal weights for ``workers`` slots (the default apportionment weight)."""
    return [1.0] * max(1, workers)


def build_share_list(rate_mode, rate_value, workers):
    # type: (str, Optional[float], int) -> List[Dict[str, float]]
    """Apportion a run's rate across ``workers`` slots into per-slot shares.

    Thin wrapper over :func:`server.engines.apportion.apportion_shares` so the
    lifecycle and its callers share one apportionment entry point.
    """
    from .engines.apportion import apportion_shares

    return apportion_shares(rate_mode, rate_value, workers, apportion_weights(workers))


def seed_leases(db, run, shares):
    # type: (Session, Run, Sequence[Dict[str, float]]) -> List[WorkerLease]
    """Create ``free`` ``worker_leases`` rows for a run from a share list.

    One lease per slot (index == slot), each with a fresh ``lease_id`` and the
    supplied single-key share. Added to the session, not committed.
    """
    leases = []  # type: List[WorkerLease]
    for slot, share in enumerate(shares):
        lease = WorkerLease(
            run_id=run.id,
            slot=slot,
            share_json=dict(share),
            lease_id=new_lease_id(),
            state=LEASE_FREE,
            restarts=0,
        )
        db.add(lease)
        leases.append(lease)
    return leases


def get_run_leases(db, run):
    # type: (Session, Run) -> List[WorkerLease]
    """Return a run's leases ordered by slot."""
    stmt = select(WorkerLease).where(WorkerLease.run_id == run.id).order_by(WorkerLease.slot)
    return list(db.execute(stmt).scalars().all())


def find_lease(db, run, slot, lease_id=None):
    # type: (Session, Run, int, Optional[str]) -> Optional[WorkerLease]
    """Find a run's lease by slot (and optionally assert its ``lease_id``)."""
    stmt = select(WorkerLease).where(
        WorkerLease.run_id == run.id, WorkerLease.slot == slot)
    lease = db.execute(stmt).scalars().first()
    if lease is None:
        return None
    if lease_id is not None and lease.lease_id != lease_id:
        return None
    return lease


def is_lease_holder(lease, lease_id):
    # type: (Optional[WorkerLease], Optional[str]) -> bool
    """True when ``lease`` exists and its ``lease_id`` matches (fencing check)."""
    return lease is not None and lease.lease_id == lease_id


def build_bundle_ref(run, settings=None):
    # type: (Run, Optional[Settings]) -> Dict[str, Any]
    """Build the slice ``bundle`` object (public URL + sha256) for a run.

    The URL is ``{PUBLIC_BASE_URL}/api/agent/bundles/<digest>.tgz``; the digest
    is the run's bundle digest. Returns ``{"url": None, "sha256": None}`` shape
    only when the run has no bundle (a provisioning invariant the Core enforces).
    """
    if settings is None:
        settings = get_settings()
    digest = None
    bundle = getattr(run, "bundle", None)
    if bundle is not None:
        digest = bundle.digest
    elif run.resolved_sha:
        digest = run.resolved_sha
    url = None
    if digest:
        url = "%s/api/agent/bundles/%s.tgz" % (settings.public_base_url, digest)
    return {"url": url, "sha256": digest}


def build_slice(run, lease, settings=None):
    # type: (Run, WorkerLease, Optional[Settings]) -> Dict[str, Any]
    """Build the spec slice (claim response) for a lease.

    Produces exactly the JSON ``SpecSlice.from_claim`` parses:
    ``run_id, slot, total_workers, lease_id, engine, bundle{url,sha256},
    share{one key}, duration_s, hec{url,index,sourcetype,gzip,ack},
    overrides{...}, telemetry{interval_s}, released, effective_t0``.

    Values come from the frozen ``spec_snapshot_json`` (non-secret): the target
    HEC url/index, the engine, the per-stanza overrides with ``{slot}`` applied,
    the telemetry interval and the duration. ``share`` is the lease's stored
    share. ``released`` reflects whether T0 is set; ``effective_t0`` is the
    lease's own anchor (ISO 8601 Z) when set, else null. The HEC **token is
    never included** (the driver projects it as ``STOKER_HEC_TOKEN``).
    """
    if settings is None:
        settings = get_settings()
    snap = run.spec_snapshot_json or {}
    target = snap.get("target") or {}
    hec = {
        "url": target.get("hec_url"),
        "index": snap.get("index") or target.get("default_index"),
        "sourcetype": snap.get("sourcetype"),
        "gzip": True,
        "ack": False,
    }
    total_workers = int(snap.get("workers") or 1)
    overrides = resolve_overrides(snap.get("overrides"), lease.slot)
    duration_s = snap.get("duration_s")
    effective = lease.effective_t0
    slice_doc = {
        "run_id": run.id,
        "slot": lease.slot,
        "total_workers": total_workers,
        "lease_id": lease.lease_id,
        "engine": snap.get("engine") or "eventgen",
        "bundle": build_bundle_ref(run, settings=settings),
        "share": public_share(lease.share_json),
        "duration_s": float(duration_s) if duration_s else None,
        "hec": hec,
        "overrides": overrides,
        "telemetry": {"interval_s": float(snap.get("telemetry_interval_s") or 5)},
        "released": run.t0 is not None,
        "effective_t0": iso_from_dt(effective),
    }
    # A backfill run carries the historical window for the worker (both engines).
    if snap.get("backfill"):
        slice_doc["backfill"] = snap["backfill"]
    return slice_doc


def iso_from_dt(value):
    # type: (Optional[datetime.datetime]) -> Optional[str]
    """Format a (tz-aware or naive) datetime as an ISO 8601 ``Z`` string.

    Naive datetimes are assumed UTC (the models store tz-aware UTC, but a value
    round-tripped through SQLite may come back naive). Returns None for None.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return format_iso8601(value.timestamp())


def build_spec_snapshot(spec, target, overrides=None, rate_mode=None,
                        rate_value=None, duration_s=None, backfill=None):
    # type: (Spec, Target, Optional[Mapping[str, str]], Optional[str], Optional[float], Optional[float], Optional[Dict[str, Any]]) -> Dict[str, Any]
    """Freeze a spec into ``spec_snapshot_json`` (non-secret only).

    Embeds the target by id plus non-secret fields (name, hec_url, default_index,
    verify_tls, env_tag) — **never** the token. Merges last-minute ``overrides``
    over the spec's stored overrides. A GET-body test asserts no secret material
    appears in any snapshot.

    ``rate_mode`` / ``rate_value`` / ``duration_s`` override the spec's own values
    when given (a **backfill** run overrides them to an eps delivery cap + a
    duration backstop). ``backfill`` (``{start_s, end_s, resolution_s}``) is
    frozen into the snapshot so ``build_slice`` can hand it to the worker.
    """
    merged_overrides = dict(spec.overrides_json or {})
    if overrides:
        merged_overrides.update(overrides)
    snap = {
        "spec_id": spec.id,
        "name": spec.name,
        "engine": spec.engine,
        "ref": spec.ref,
        "rate_mode": rate_mode if rate_mode is not None else spec.rate_mode,
        "rate_value": rate_value if rate_value is not None else spec.rate_value,
        "interval_s": spec.interval_s,
        "workers": spec.workers,
        "duration_s": duration_s if duration_s is not None else spec.duration_s,
        "fleet": spec.fleet,
        "strict_release": spec.strict_release,
        "overrides": merged_overrides,
        "index": merged_overrides.get("index"),
        "sourcetype": merged_overrides.get("sourcetype"),
        "telemetry_interval_s": (spec.driver_opts_json or {}).get("telemetry_interval_s", 5),
        "driver_opts": spec.driver_opts_json or {},
        "target": {
            "id": target.id,
            "name": target.name,
            "hec_url": target.hec_url,
            "default_index": target.default_index,
            "verify_tls": target.verify_tls,
            "env_tag": target.env_tag,
        },
    }
    if backfill is not None:
        snap["backfill"] = backfill
    return snap


def build_run_snapshot(run, spec, target, hec_token, settings=None, workers=None,
                       duration_s=None):
    # type: (Run, Spec, Target, Optional[str], Optional[Settings], Optional[int], Optional[float]) -> RunSnapshot
    """Build the :class:`RunSnapshot` a driver needs to launch the fleet.

    Projects the worker environment: ``STOKER_RUN_ID``, ``STOKER_CONTROL_URL``
    (= ``PUBLIC_BASE_URL``), ``STOKER_RUN_JWT``, ``STOKER_TOTAL_WORKERS``, the HEC
    token as ``STOKER_HEC_TOKEN`` (the driver, not the slice, carries the secret)
    and, for a non-default engine (rawreplay/Piston), ``STOKER_ENGINE`` so the
    worker launches the right engine. ``labels`` always includes
    ``stoker.run=<id>``.

    ``workers`` overrides the projected ``STOKER_TOTAL_WORKERS`` (the provisioner
    passes the single-worker-clamped count for a replay run); it defaults to the
    spec's own worker count.

    ``hec_token`` is the decrypted target token (the caller decrypts once); it is
    placed only in the env projection, never logged.

    ``driver_opts`` carries the spec's own knobs (cpu/mem requests, namespace,
    placement) plus the run's bounded ``duration_s`` when set, so a driver can
    give the workload a hard deadline (the K8sDriver's
    ``activeDeadlineSeconds = duration + 300``). Drivers that do not need it (the
    SwarmDriver) simply ignore the key.
    """
    if settings is None:
        settings = get_settings()
    if workers is None:
        workers = spec.workers
    jwt_token = crypto.mint_run_jwt(run.id, run.jwt_kid or crypto.new_kid(), settings=settings)
    env = {
        "STOKER_RUN_ID": str(run.id),
        "STOKER_CONTROL_URL": settings.public_base_url,
        "STOKER_RUN_JWT": jwt_token,
        "STOKER_TOTAL_WORKERS": str(workers),
    }
    # Select the worker engine. eventgen is the default (the worker assumes it
    # when STOKER_ENGINE is unset), so only project the var for a non-default
    # engine (rawreplay/Piston) to keep the eventgen env byte-for-byte unchanged.
    engine = (spec.engine or "eventgen").strip()
    if engine and engine != "eventgen":
        env["STOKER_ENGINE"] = engine
    if hec_token:
        env["STOKER_HEC_TOKEN"] = hec_token
    driver_opts = dict(spec.driver_opts_json or {})
    # Surface the bounded duration to the driver (for a hard workload deadline).
    # Unbounded runs (duration_s falsy) leave it absent so no deadline is set. A
    # backfill run passes an explicit duration_s backstop that overrides the spec.
    effective_duration = duration_s if duration_s is not None else spec.duration_s
    if effective_duration:
        driver_opts.setdefault("duration_s", effective_duration)
    return RunSnapshot(
        run_id=run.id,
        image=settings.worker_image,
        env=env,
        labels={"stoker.run": str(run.id)},
        driver_opts=driver_opts,
        stop_grace_s=STOP_GRACE_S,
    )


def counters_from_payload(payload):
    # type: (Mapping[str, Any]) -> Dict[str, Any]
    """Extract the metric-sample counters from a heartbeat body, defensively.

    Only known counter keys are pulled; non-numeric values become ``None`` so a
    malformed heartbeat never rejects the insert. ``bps`` is derived nowhere
    here (the worker sends it or it stays null).
    """
    int_keys = (
        "events_total", "bytes_total", "hec_2xx", "hec_4xx", "hec_5xx",
        "hec_timeouts", "retries", "queue_depth",
    )
    float_keys = ("eps", "bps", "lag_s", "rss_mb", "cpu_pct")
    out = {}  # type: Dict[str, Any]
    for key in int_keys:
        out[key] = _coerce_int(payload.get(key))
    for key in float_keys:
        out[key] = _coerce_float(payload.get(key))
    return out


def build_metric_sample(run, slot, payload):
    # type: (Run, int, Mapping[str, Any]) -> MetricSample
    """Build (not add) a :class:`MetricSample` from a heartbeat payload."""
    counters = counters_from_payload(payload)
    return MetricSample(run_id=run.id, slot=slot, ts=utcnow(), **counters)


def fold_totals(run, summary):
    # type: (Run, Mapping[str, Any]) -> Dict[str, Any]
    """Fold a worker's final ``summary`` into ``runs.totals_json`` and return it.

    Sums numeric keys across workers (events_total, bytes_total, hec_* etc.);
    non-numeric keys keep the last writer. Mutates and returns ``run.totals_json``.
    """
    totals = dict(run.totals_json or {})
    for key, value in (summary or {}).items():
        if isinstance(value, bool):
            totals[key] = value
        elif isinstance(value, (int, float)):
            totals[key] = totals.get(key, 0) + value
        else:
            totals[key] = value
    run.totals_json = totals
    return totals


def maybe_refresh_jwt(run, current_token, settings=None):
    # type: (Run, Optional[str], Optional[Settings]) -> Optional[str]
    """Return a fresh JWT when the presented token is within 20% of expiry.

    Returns ``None`` when no refresh is needed (or no token is presented / the
    run has no kid). The heartbeat command builder attaches the result as
    ``jwt`` for a rolling refresh.
    """
    if settings is None:
        settings = get_settings()
    if not current_token or not run.jwt_kid:
        return None
    remaining = crypto.jwt_seconds_remaining(current_token, settings=settings)
    if remaining is None:
        return None
    if remaining <= settings.jwt_ttl_s * JWT_REFRESH_FRACTION:
        return crypto.mint_run_jwt(run.id, run.jwt_kid, settings=settings)
    return None


# -- heartbeat command builders (pure) -------------------------------------- #

def cmd_continue():
    # type: () -> Dict[str, Any]
    return {"command": "continue"}


def cmd_superseded():
    # type: () -> Dict[str, Any]
    return {"command": "superseded"}


def cmd_drain():
    # type: () -> Dict[str, Any]
    return {"command": "drain"}


def cmd_release(t0):
    # type: (datetime.datetime) -> Dict[str, Any]
    """Build a ``release`` command carrying an ISO 8601 Z T0 timestamp."""
    return {"command": "release", "t0": iso_from_dt(t0)}


def cmd_retarget(share):
    # type: (Mapping[str, float]) -> Dict[str, Any]
    return {"command": "retarget", "share": dict(share)}


def seed_fleets(db, settings=None):
    # type: (Session, Optional[Settings]) -> None
    """Seed the ``fake-local`` and ``swarm-local`` fleets if absent (first boot).

    ``fake-local`` uses the in-process driver; ``swarm-local`` records the
    Portainer endpoint id + host from config. Idempotent: an existing fleet of
    the same name is left untouched.
    """
    if settings is None:
        settings = get_settings()
    existing = {f.name for f in db.execute(select(Fleet)).scalars().all()}
    if "fake-local" not in existing:
        db.add(Fleet(name="fake-local", driver="fake", config_json={}))
        log.info("seeded fleet fake-local")
    if "swarm-local" not in existing:
        db.add(Fleet(
            name="swarm-local",
            driver="swarm",
            config_json={
                "portainer_endpoint": settings.portainer_endpoint,
                "portainer_host": settings.portainer_host,
            },
        ))
        log.info("seeded fleet swarm-local (endpoint %s)", settings.portainer_endpoint)
    db.commit()


def get_run_driver(db, run, drivers):
    # type: (Session, Run, Mapping[str, ExecutionDriver]) -> Optional[ExecutionDriver]
    """Resolve the driver for a run from a fleet-name -> driver mapping.

    Looks up the run's spec's fleet name; falls back to constructing one via the
    driver factory when the mapping lacks it (so the supervisor can act on a run
    whose fleet was not pre-seeded into the map).
    """
    fleet_name = None
    spec = getattr(run, "spec", None)
    if spec is not None:
        fleet_name = spec.fleet
    if fleet_name is None:
        snap = run.spec_snapshot_json or {}
        fleet_name = snap.get("fleet")
    if fleet_name and fleet_name in drivers:
        return drivers[fleet_name]
    if fleet_name:
        from .drivers import get_driver

        fleet = db.execute(select(Fleet).where(Fleet.name == fleet_name)).scalars().first()
        if fleet is not None:
            return get_driver(fleet)
    return None


def driver_ref_of(run):
    # type: (Run) -> Optional[DriverRef]
    """Decode a run's stored :class:`DriverRef` (``None`` when unprovisioned)."""
    return DriverRef.from_json(run.driver_ref_json)


# -- small numeric coercions ------------------------------------------------ #

def _coerce_int(value):
    # type: (Any) -> Optional[int]
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value):
    # type: (Any) -> Optional[float]
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    # states
    "STATE_PENDING", "STATE_PREPARING", "STATE_PROVISIONING", "STATE_RELEASING",
    "STATE_RUNNING", "STATE_DRAINING", "STATE_COMPLETED", "STATE_STOPPED",
    "STATE_FAILED", "TERMINAL_STATES",
    "LEASE_FREE", "LEASE_CLAIMED", "LEASE_READY", "LEASE_RUNNING", "LEASE_LOST",
    "LEASE_DONE",
    # windows
    "RELEASE_DELAY_S", "PROVISION_TIMEOUT_S", "LEASE_LAPSE_S", "BOOT_GRACE_S",
    "STOP_GRACE_S", "AUTO_ABORT_LOST_FRACTION", "AUTO_ABORT_LOST_S",
    "JWT_REFRESH_FRACTION",
    # engine policy
    "SINGLE_WORKER_ENGINES", "effective_workers",
    # domain (stubbed)
    "provision_run", "stop_run", "scale_run", "rescale_run", "supervisor_tick",
    "reconcile_on_boot", "claim_lease", "mark_ready", "record_heartbeat",
    "record_final", "evaluate_release", "maybe_complete_run",
    # helpers (implemented)
    "append_event", "transition_run", "new_lease_id", "next_free_slot",
    "share_for_mode", "substitute_slot", "resolve_overrides", "apportion_weights",
    "build_share_list", "seed_leases", "get_run_leases", "find_lease",
    "is_lease_holder", "build_bundle_ref", "build_slice", "iso_from_dt",
    "public_share", "mark_retarget", "clear_retarget",
    "build_spec_snapshot",
    "build_run_snapshot", "counters_from_payload", "build_metric_sample",
    "fold_totals", "maybe_refresh_jwt", "cmd_continue", "cmd_superseded",
    "cmd_drain", "cmd_release", "cmd_retarget", "seed_fleets", "get_run_driver",
    "driver_ref_of",
]
