"""Heartbeat command channel + counter persistence.

``record_heartbeat`` renews the lease, appends the counters to
``metric_samples`` (parsed defensively) and returns the command the worker
obeys next:

* ``continue`` normally,
* ``release {t0}`` once T0 is set and until the worker acks past it,
* ``drain`` when the run is draining/stopping or the protocol is unsupported,
* ``superseded`` when the presented ``lease_id`` is not the slot holder,
* ``retarget {share}`` when the stored slot share changed (best-effort here).

It also threads the raw bearer as ``payload["_bearer"]`` so a token within 20 %
of expiry can be rolled via ``maybe_refresh_jwt`` and returned as ``jwt``; that
key must be popped before the counters are persisted and never logged.

Written against the frozen interface; skipped cleanly until the Core builder
lands. A couple of pure-helper checks (``counters_from_payload``,
``maybe_refresh_jwt``, the command builders) run unconditionally.
"""

from __future__ import annotations

import datetime

import pytest

from server import crypto, lifecycle
from server.models import MetricSample, utcnow

from . import _helpers

pytestmark = pytest.mark.usefixtures("fake_driver")


def _skip_if_stubbed(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotImplementedError:
        pytest.skip("lifecycle.%s not implemented yet (Core builder)" % fn.__name__)


def _provisioned(db, make_pack, settings, fake_driver, **kw):
    ctx = _helpers.full_run(db, make_pack(), settings, driver=fake_driver, **kw)
    return ctx["run"]


def _claim(db, run, slot=0, holder="h0"):
    return _skip_if_stubbed(lifecycle.claim_lease, db, run, holder, hint_slot=slot)


def _samples(db, run):
    return list(db.query(MetricSample).filter(MetricSample.run_id == run.id).all())


# --------------------------------------------------------------------------- #
# Pure helpers (always run — no Core dependency).
# --------------------------------------------------------------------------- #

def test_counters_from_payload_ignores_bearer_and_garbage():
    counters = lifecycle.counters_from_payload({
        "_bearer": "secret-jwt-value",
        "events_total": 10,
        "bytes_total": "not-a-number",
        "eps": 5.0,
        "unknown_key": 123,
        "state": "generating",
    })
    assert counters["events_total"] == 10
    assert counters["eps"] == 5.0
    assert counters["bytes_total"] is None  # non-numeric coerced to None
    assert "_bearer" not in counters
    assert "unknown_key" not in counters
    assert "state" not in counters


def test_command_builders_shapes():
    assert lifecycle.cmd_continue() == {"command": "continue"}
    assert lifecycle.cmd_superseded() == {"command": "superseded"}
    assert lifecycle.cmd_drain() == {"command": "drain"}
    t0 = utcnow() + datetime.timedelta(seconds=2)
    rel = lifecycle.cmd_release(t0)
    assert rel["command"] == "release"
    assert rel["t0"].endswith("Z")
    rt = lifecycle.cmd_retarget({"eps": 42.0})
    assert rt == {"command": "retarget", "share": {"eps": 42.0}}


def test_maybe_refresh_jwt_rolls_within_20pct(settings, db_session, make_pack, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    # A token with 60 s left is within 20 % of the 3600 s TTL (720 s) -> refresh.
    near_expiry = crypto.mint_run_jwt(run.id, run.jwt_kid, ttl_s=60, settings=settings)
    fresh = lifecycle.maybe_refresh_jwt(run, near_expiry, settings=settings)
    assert fresh is not None and fresh != near_expiry
    # The fresh token verifies for this run.
    assert crypto.verify_run_jwt(fresh, run.id, settings=settings)


def test_maybe_refresh_jwt_no_roll_when_fresh(settings, db_session, make_pack, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    healthy = crypto.mint_run_jwt(run.id, run.jwt_kid, ttl_s=3600, settings=settings)
    assert lifecycle.maybe_refresh_jwt(run, healthy, settings=settings) is None


# --------------------------------------------------------------------------- #
# record_heartbeat: commands + persistence (Core).
# --------------------------------------------------------------------------- #

def test_heartbeat_continue_before_t0(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    payload = _helpers.heartbeat_payload(lease, events_total=1)
    payload["_bearer"] = _helpers.bearer_for(run, settings)
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run,
                               lease.slot, lease.lease_id, payload)
    assert command.get("command") == "continue"


def test_heartbeat_persists_counters_and_pops_bearer(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    payload = _helpers.heartbeat_payload(
        lease, events_total=1234, bytes_total=567000, eps=250.0, hec_2xx=12,
        hec_4xx=0, hec_5xx=1, queue_depth=3, lag_s=0.4, rss_mb=88.5, cpu_pct=12.5)
    payload["_bearer"] = _helpers.bearer_for(run, settings)
    _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run, lease.slot,
                     lease.lease_id, payload)
    db_session.flush()

    rows = _samples(db_session, run)
    assert len(rows) == 1
    sample = rows[0]
    assert sample.slot == lease.slot
    assert sample.events_total == 1234
    assert sample.bytes_total == 567000
    assert sample.eps == pytest.approx(250.0)
    assert sample.hec_5xx == 1
    # No column is or could be named _bearer; the secret never reaches the DB.
    assert not hasattr(sample, "_bearer")


def test_heartbeat_never_persists_bearer_as_data(db_session, make_pack, settings, fake_driver):
    # Even a hostile payload that jams the JWT into a counter field must not leak
    # it: counters_from_payload only reads known numeric keys.
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    token = _helpers.bearer_for(run, settings)
    payload = _helpers.heartbeat_payload(lease, events_total=1)
    payload["_bearer"] = token
    _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run, lease.slot,
                     lease.lease_id, payload)
    db_session.flush()
    for sample in _samples(db_session, run):
        for value in vars(sample).values():
            assert token not in repr(value)


def test_heartbeat_rolls_jwt_when_near_expiry(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    near_expiry = crypto.mint_run_jwt(run.id, run.jwt_kid, ttl_s=60, settings=settings)
    payload = _helpers.heartbeat_payload(lease, events_total=1)
    payload["_bearer"] = near_expiry
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run,
                               lease.slot, lease.lease_id, payload, settings=settings)
    assert command.get("jwt"), "a near-expiry token should trigger a rolling refresh"
    assert command["jwt"] != near_expiry
    assert crypto.verify_run_jwt(command["jwt"], run.id, settings=settings)


def test_heartbeat_no_jwt_when_token_fresh(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    payload = _helpers.heartbeat_payload(lease, events_total=1)
    payload["_bearer"] = crypto.mint_run_jwt(run.id, run.jwt_kid, ttl_s=3600, settings=settings)
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run,
                               lease.slot, lease.lease_id, payload, settings=settings)
    assert command.get("jwt") is None


def test_heartbeat_release_after_t0(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    _skip_if_stubbed(lifecycle.mark_ready, db_session, run, lease.slot, lease.lease_id)
    if run.t0 is None:
        _skip_if_stubbed(lifecycle.evaluate_release, db_session, run)
    db_session.flush()
    if run.t0 is None:
        pytest.skip("release not set by Core in this configuration")

    payload = _helpers.heartbeat_payload(lease, state="generating", events_total=1)
    payload["_bearer"] = _helpers.bearer_for(run, settings)
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run,
                               lease.slot, lease.lease_id, payload)
    assert command.get("command") == "release"
    assert command.get("t0", "").endswith("Z")


def test_heartbeat_drain_when_run_draining(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    # Force the run into draining directly (operator stop path is separate).
    lifecycle.transition_run(db_session, run, lifecycle.STATE_DRAINING)
    db_session.flush()
    payload = _helpers.heartbeat_payload(lease, state="generating", events_total=1)
    payload["_bearer"] = _helpers.bearer_for(run, settings)
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run,
                               lease.slot, lease.lease_id, payload)
    assert command.get("command") == "drain"


def test_heartbeat_drain_on_unsupported_protocol(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run)
    payload = _helpers.heartbeat_payload(lease, events_total=1)
    payload["protocol_version"] = 999  # unsupported
    payload["_bearer"] = _helpers.bearer_for(run, settings)
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run,
                               lease.slot, lease.lease_id, payload)
    assert command.get("command") == "drain"


def test_heartbeat_superseded_on_stale_lease(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    _claim(db_session, run)  # slot 0 held by h0
    payload = {"slot": 0, "lease_id": "le_stale", "protocol_version": 1, "state": "generating"}
    payload["_bearer"] = _helpers.bearer_for(run, settings)
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run, 0,
                               "le_stale", payload)
    assert command.get("command") == "superseded"


def test_heartbeat_retarget_after_mark_retarget(db_session, make_pack, settings, fake_driver):
    # scale/rescale flag a live lease via mark_retarget (the shared helper);
    # the next heartbeat then pushes the new share as a retarget command, with
    # the private marker stripped from the wire share.
    if not hasattr(lifecycle, "mark_retarget"):
        pytest.skip("lifecycle.mark_retarget not present in this build")
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    lease = _claim(db_session, run, slot=0)
    _skip_if_stubbed(lifecycle.mark_ready, db_session, run, 0, lease.lease_id)
    if run.t0 is None:
        _skip_if_stubbed(lifecycle.evaluate_release, db_session, run, True)
    db_session.flush()

    # Drive the lease past T0 so it is RUNNING (release outranks retarget while a
    # worker is still being released). Back-date T0 and heartbeat once.
    run.t0 = utcnow() - datetime.timedelta(seconds=1)
    db_session.flush()
    promote = _helpers.heartbeat_payload(
        _helpers.leases_by_slot(db_session, run)[0], state="generating", events_total=1)
    promote["_bearer"] = _helpers.bearer_for(run, settings)
    first = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run, 0,
                             lease.lease_id, promote)
    assert first.get("command") == "continue"  # promoted past release
    assert _helpers.leases_by_slot(db_session, run)[0].state == lifecycle.LEASE_RUNNING

    # Now flag the live (running) lease for retarget exactly as scale/rescale would.
    by_slot = _helpers.leases_by_slot(db_session, run)
    lifecycle.mark_retarget(by_slot[0], {"eps": 999.0})
    db_session.flush()

    payload = _helpers.heartbeat_payload(by_slot[0], state="generating", events_total=2)
    payload["_bearer"] = _helpers.bearer_for(run, settings)
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run, 0,
                               by_slot[0].lease_id, payload)
    assert command.get("command") == "retarget"
    # The wire share carries the single rate key, never the private _retarget marker.
    assert command["share"] == {"eps": 999.0}
    assert not any(k.startswith("_") for k in command["share"])
