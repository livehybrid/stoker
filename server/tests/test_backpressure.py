"""Target-health backpressure: ``lifecycle._check_backpressure`` (opt-in).

When STOKER_BACKPRESSURE_DRAIN is on, a run whose HEC target is failing to
accept data (recent 5xx / timeouts across the fleet, sustained) is DRAINED and
its target flagged red. Off by default: no run changes behaviour unless enabled.
"""

from __future__ import annotations

import dataclasses
import datetime

from server import lifecycle
from server.lifecycle import utcnow
from server.models import MetricSample

from . import _helpers as H


def _bp_settings(base, **over):
    opts = dict(backpressure_drain_enabled=True,
                backpressure_min_failed_fraction=0.5,
                backpressure_sustained_s=60.0)
    opts.update(over)
    return dataclasses.replace(base, **opts)


def _running_run(db, make_pack, settings, workers=1):
    ctx = H.full_run(db, make_pack(), settings, workers=workers,
                     rate_mode="eps", rate_value=1000.0,
                     state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    run.t0 = utcnow() - datetime.timedelta(seconds=30)
    for lease in lifecycle.get_run_leases(db, run):
        lease.state = lifecycle.LEASE_RUNNING
    db.flush()
    return run


def _two_samples(db, run, slot, prev, latest, gap_s=30):
    """Two consecutive cumulative samples for a slot (prev older, latest now)."""
    now = utcnow()
    db.add(MetricSample(run_id=run.id, slot=slot,
                        ts=now - datetime.timedelta(seconds=gap_s), **prev))
    db.add(MetricSample(run_id=run.id, slot=slot, ts=now, **latest))
    db.flush()


def test_disabled_by_default_never_drains(db_session, make_pack, settings):
    """The unconfigured default leaves a failing run entirely alone."""
    db = db_session
    run = _running_run(db, make_pack, settings)
    _two_samples(db, run, 0,
                 prev=dict(hec_2xx=10, hec_5xx=0, hec_timeouts=0),
                 latest=dict(hec_2xx=10, hec_5xx=90, hec_timeouts=0))  # all failing

    lifecycle._check_backpressure(db, run, lifecycle.get_run_leases(db, run), utcnow(),
                                  settings=settings)  # dogfood-default: disabled

    assert run.state == lifecycle.STATE_RUNNING


def test_sustained_failure_drains_and_flags_target(db_session, make_pack, settings):
    db = db_session
    st = _bp_settings(settings, backpressure_sustained_s=60.0)
    run = _running_run(db, make_pack, st)
    leases = lifecycle.get_run_leases(db, run)
    # A failing delta: +5 success, +95 fail => 95% failed, over the 50% bar.
    _two_samples(db, run, 0,
                 prev=dict(hec_2xx=100, hec_5xx=0, hec_timeouts=0),
                 latest=dict(hec_2xx=105, hec_5xx=90, hec_timeouts=5))

    # First tick: starts the clock, does not drain yet.
    lifecycle._check_backpressure(db, run, leases, utcnow(), settings=st)
    assert run.state == lifecycle.STATE_RUNNING

    # A later tick, past the sustained window: drains.
    later = utcnow() + datetime.timedelta(seconds=61)
    lifecycle._check_backpressure(db, run, leases, later, settings=st)

    assert run.state == lifecycle.STATE_DRAINING
    assert run.end_reason == "backpressure-drain"
    target = run.spec.target
    assert target.health_state == "red"


def test_recovery_before_window_clears_and_does_not_drain(
        db_session, make_pack, settings):
    """A blip that recovers before the sustained window never drains."""
    db = db_session
    st = _bp_settings(settings, backpressure_sustained_s=60.0)
    run = _running_run(db, make_pack, st)
    leases = lifecycle.get_run_leases(db, run)

    # Tick 1: failing -> starts the clock.
    _two_samples(db, run, 0,
                 prev=dict(hec_2xx=100, hec_5xx=0, hec_timeouts=0),
                 latest=dict(hec_2xx=101, hec_5xx=99, hec_timeouts=0))
    lifecycle._check_backpressure(db, run, leases, utcnow(), settings=st)
    assert run.state == lifecycle.STATE_RUNNING

    # Tick 2: recovered (all success now) -> clears the streak.
    _two_samples(db, run, 0,
                 prev=dict(hec_2xx=200, hec_5xx=99, hec_timeouts=0),
                 latest=dict(hec_2xx=300, hec_5xx=99, hec_timeouts=0))
    lifecycle._check_backpressure(db, run, leases,
                                  utcnow() + datetime.timedelta(seconds=30), settings=st)

    # Tick 3: failing again but the earlier clock was reset, so it is only the
    # start of a fresh streak, not yet the sustained window -> no drain.
    _two_samples(db, run, 0,
                 prev=dict(hec_2xx=300, hec_5xx=99, hec_timeouts=0),
                 latest=dict(hec_2xx=301, hec_5xx=199, hec_timeouts=0))
    lifecycle._check_backpressure(db, run, leases,
                                  utcnow() + datetime.timedelta(seconds=61), settings=st)
    assert run.state == lifecycle.STATE_RUNNING


def test_healthy_delivery_never_trips(db_session, make_pack, settings):
    db = db_session
    st = _bp_settings(settings)
    run = _running_run(db, make_pack, st)
    leases = lifecycle.get_run_leases(db, run)
    # Almost all success.
    _two_samples(db, run, 0,
                 prev=dict(hec_2xx=1000, hec_5xx=2, hec_timeouts=0),
                 latest=dict(hec_2xx=2000, hec_5xx=3, hec_timeouts=0))

    lifecycle._check_backpressure(db, run, leases,
                                  utcnow() + datetime.timedelta(seconds=120), settings=st)
    assert run.state == lifecycle.STATE_RUNNING


def test_single_sample_slot_contributes_nothing(db_session, make_pack, settings):
    """A slot with only one (cumulative) sample is not a recent rate, so a
    just-started run cannot trip backpressure off its first heartbeat."""
    db = db_session
    st = _bp_settings(settings)
    run = _running_run(db, make_pack, st)
    db.add(MetricSample(run_id=run.id, slot=0, ts=utcnow(),
                        hec_2xx=0, hec_5xx=500, hec_timeouts=0))  # one big sample
    db.flush()

    ok, fail = lifecycle._recent_delivery_deltas(db, run, [0])
    assert (ok, fail) == (0, 0)
    lifecycle._check_backpressure(db, run, lifecycle.get_run_leases(db, run),
                                  utcnow() + datetime.timedelta(seconds=120), settings=st)
    assert run.state == lifecycle.STATE_RUNNING
