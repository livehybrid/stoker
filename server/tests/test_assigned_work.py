"""Assigned-work heartbeat fields: worker-declared work held, persisted per lease.

A worker MAY declare on each heartbeat how much work it actually holds
(``assigned_work``: metrics = owned series, eventgen = stanzas that will emit)
plus a short ``assigned_reason`` when it holds none, so a slot legitimately
sitting at 0 EPS explains itself. The control plane persists the pair on the
lease under the private ``_assigned_work`` / ``_assigned_reason`` keys of
``share_json`` (the same ``_``-prefixed convention as ``_retarget``): never on
the agent wire (``public_share`` strips them) but visible on the operator
lease roster.

Backwards compatibility is the contract here, in BOTH directions: a heartbeat
without the fields (an old worker) must change nothing, and the fields riding
an otherwise-normal heartbeat (a new worker) must parse defensively — garbage
values are ignored rather than rejected.
"""

from __future__ import annotations

import pytest

from server import lifecycle

from . import _helpers

pytestmark = pytest.mark.usefixtures("fake_driver")


def _provisioned(db, make_pack, settings, fake_driver, **kw):
    ctx = _helpers.full_run(db, make_pack(), settings, driver=fake_driver, **kw)
    return ctx["run"]


def _claim(db, run, slot=0, holder="h0"):
    return lifecycle.claim_lease(db, run, holder, hint_slot=slot)


# --------------------------------------------------------------------------- #
# Pure helper: store_assigned_work.
# --------------------------------------------------------------------------- #

class _FakeLease(object):
    def __init__(self, share=None):
        self.share_json = share


def test_store_assigned_work_absent_leaves_lease_untouched():
    lease = _FakeLease({"eps": 250.0})
    lifecycle.store_assigned_work(lease, {"events_total": 5})
    assert lease.share_json == {"eps": 250.0}


def test_store_assigned_work_garbage_is_ignored():
    lease = _FakeLease({"eps": 250.0})
    lifecycle.store_assigned_work(lease, {"assigned_work": "not-a-number"})
    assert lease.share_json == {"eps": 250.0}


def test_store_assigned_work_zero_keeps_reason_and_truncates():
    lease = _FakeLease({"count": 0.0})
    lifecycle.store_assigned_work(
        lease, {"assigned_work": 0, "assigned_reason": "x" * 1000})
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 0
    reason = lease.share_json[lifecycle.ASSIGNED_REASON_KEY]
    assert reason == "x" * 300  # bounded so a hostile agent cannot bloat the row


def test_store_assigned_work_positive_drops_stale_reason():
    lease = _FakeLease({
        "count": 1.0,
        lifecycle.ASSIGNED_WORK_KEY: 0,
        lifecycle.ASSIGNED_REASON_KEY: "no series assigned",
    })
    lifecycle.store_assigned_work(lease, {"assigned_work": 3})
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 3
    assert lifecycle.ASSIGNED_REASON_KEY not in lease.share_json


def test_assigned_keys_never_cross_the_agent_wire():
    share = {"eps": 250.0, lifecycle.ASSIGNED_WORK_KEY: 0,
             lifecycle.ASSIGNED_REASON_KEY: "idle"}
    assert lifecycle.public_share(share) == {"eps": 250.0}


def test_mark_retarget_preserves_assigned_keys():
    lease = _FakeLease({
        "eps": 250.0,
        lifecycle.ASSIGNED_WORK_KEY: 0,
        lifecycle.ASSIGNED_REASON_KEY: "idle",
    })
    lifecycle.mark_retarget(lease, {"eps": 333.0})
    assert lease.share_json["eps"] == 333.0
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 0
    assert lease.share_json[lifecycle.ASSIGNED_REASON_KEY] == "idle"
    assert lease.share_json[lifecycle.RETARGET_MARKER] is True


# --------------------------------------------------------------------------- #
# record_heartbeat round-trip (lifecycle level).
# --------------------------------------------------------------------------- #

def test_heartbeat_without_fields_is_the_old_contract(
        db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver)
    lease = _claim(db_session, run)
    before = dict(lease.share_json or {})
    payload = _helpers.heartbeat_payload(lease, events_total=1)
    command = lifecycle.record_heartbeat(db_session, run, lease.slot,
                                         lease.lease_id, payload)
    assert command["command"] in ("continue", "release")
    assert lifecycle.ASSIGNED_WORK_KEY not in (lease.share_json or {})
    assert dict(lease.share_json or {}) == before


def test_heartbeat_persists_assigned_work_on_the_lease(
        db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver)
    lease = _claim(db_session, run)
    payload = _helpers.heartbeat_payload(
        lease, events_total=0, assigned_work=0,
        assigned_reason="no series assigned: slot 0 of 4 owns none")
    lifecycle.record_heartbeat(db_session, run, lease.slot, lease.lease_id, payload)
    db_session.flush()
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 0
    assert "owns none" in lease.share_json[lifecycle.ASSIGNED_REASON_KEY]
    # A later heartbeat reporting work clears the excuse.
    payload = _helpers.heartbeat_payload(lease, events_total=5, assigned_work=2)
    lifecycle.record_heartbeat(db_session, run, lease.slot, lease.lease_id, payload)
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 2
    assert lifecycle.ASSIGNED_REASON_KEY not in lease.share_json


def test_takeover_drops_previous_holders_assigned_report(
        db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _claim(db_session, run, slot=0, holder="h0")
    payload = _helpers.heartbeat_payload(lease, assigned_work=0,
                                         assigned_reason="idle")
    lifecycle.record_heartbeat(db_session, run, 0, lease.lease_id, payload)
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 0
    # The holder lapses; a replacement takes the slot over. The stale report
    # must not be shown against the new holder.
    lease.state = lifecycle.LEASE_LOST
    db_session.flush()
    lease = lifecycle.claim_lease(db_session, run, "h1")
    assert lease.holder == "h1"
    assert lifecycle.ASSIGNED_WORK_KEY not in (lease.share_json or {})
    assert lifecycle.ASSIGNED_REASON_KEY not in (lease.share_json or {})


# --------------------------------------------------------------------------- #
# Full wire round-trip through the agent route (new worker + old worker).
# --------------------------------------------------------------------------- #

def test_route_round_trip_with_and_without_assigned_fields(
        client, db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver)
    db_session.commit()
    headers = _helpers.auth_header(run, settings)

    slice_doc = client.post("/api/agent/runs/%d/claim" % run.id,
                            json={"holder": "w0", "protocol_version": 1},
                            headers=headers).json()
    body = {"slot": slice_doc["slot"], "lease_id": slice_doc["lease_id"],
            "protocol_version": 1, "state": "generating", "events_total": 0,
            "assigned_work": 0, "assigned_reason": "no series assigned"}
    resp = client.post("/api/agent/runs/%d/heartbeat" % run.id, json=body,
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["command"] in ("continue", "release")

    db_session.expire_all()
    lease = _helpers.leases_by_slot(db_session, run)[slice_doc["slot"]]
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 0
    assert lease.share_json[lifecycle.ASSIGNED_REASON_KEY] == "no series assigned"

    # An old worker's heartbeat (no assigned fields) still round-trips clean
    # and leaves the stored report alone (nothing new was declared).
    body = {"slot": slice_doc["slot"], "lease_id": slice_doc["lease_id"],
            "protocol_version": 1, "state": "generating", "events_total": 1}
    resp = client.post("/api/agent/runs/%d/heartbeat" % run.id, json=body,
                       headers=headers)
    assert resp.status_code == 200
    db_session.expire_all()
    lease = _helpers.leases_by_slot(db_session, run)[slice_doc["slot"]]
    assert lease.share_json[lifecycle.ASSIGNED_WORK_KEY] == 0
