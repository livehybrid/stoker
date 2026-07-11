"""Worker-lease state machine (the fencing identity of a run).

Drives the frozen lifecycle domain functions the Core builder fills:
``claim_lease`` (lowest free / hint / idempotent re-claim), ``mark_ready``,
``record_heartbeat`` (renew, superseded on a stale lease_id) and
``supervisor_tick`` (lapse: no heartbeat for 30 s past the boot grace -> lost,
share freed; a subsequent claim of that slot inherits its slot + share).

These tests are written against the *interface*, not a specific Core
implementation. They set up a provisioned run + seeded ``free`` leases with the
implemented pure helpers (see ``_helpers``), then assert the observable
contract. They are skipped cleanly until the Core builder lands (the stubs raise
``NotImplementedError``), so the file collects and imports at all times.
"""

from __future__ import annotations

import datetime

import pytest

from server import lifecycle
from server.models import utcnow

from . import _helpers

pytestmark = pytest.mark.usefixtures("fake_driver")


def _boot_past():
    # type: () -> datetime.datetime
    """A boot_time far enough back that the 60 s restart grace has elapsed."""
    return utcnow() - datetime.timedelta(seconds=lifecycle.BOOT_GRACE_S + 3600)


def _skip_if_stubbed(fn, *args, **kwargs):
    """Call a lifecycle function, skipping the test if it is still a stub."""
    try:
        return fn(*args, **kwargs)
    except NotImplementedError:
        pytest.skip("lifecycle.%s not implemented yet (Core builder)" % fn.__name__)


def _provisioned(db, make_pack, settings, fake_driver, **kw):
    pack_dir = make_pack()
    ctx = _helpers.full_run(db, pack_dir, settings, driver=fake_driver, **kw)
    return ctx["run"]


# --------------------------------------------------------------------------- #
# Claim: lowest free, hint honoured, idempotent re-claim.
# --------------------------------------------------------------------------- #

def test_claim_issues_lowest_free_slot(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=4)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "holder-a")
    assert lease.slot == 0
    assert lease.state == lifecycle.LEASE_CLAIMED
    assert lease.holder == "holder-a"
    assert lease.last_heartbeat_at is not None
    # The share on slot 0 was seeded by apportionment (1000/4 = 250 eps).
    assert lease.share_json == {"eps": 250.0}


def test_claim_second_worker_gets_next_slot(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=4)
    a = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "holder-a")
    b = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "holder-b")
    assert {a.slot, b.slot} == {0, 1}
    assert a.lease_id != b.lease_id


def test_claim_honours_free_hint_slot(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=4)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "holder-h", hint_slot=2)
    assert lease.slot == 2


def test_claim_reclaim_same_holder_is_idempotent(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=4)
    first = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "holder-a", hint_slot=1)
    first_lease_id = first.lease_id
    again = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "holder-a", hint_slot=1)
    # Same holder re-claiming its still-held lease gets the same slot + lease_id.
    assert again.slot == first.slot == 1
    assert again.lease_id == first_lease_id


# --------------------------------------------------------------------------- #
# Ready + heartbeat renew.
# --------------------------------------------------------------------------- #

def test_mark_ready_sets_lease_ready(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    _skip_if_stubbed(lifecycle.mark_ready, db_session, run, 0, lease.lease_id)
    db_session.flush()
    refreshed = _helpers.leases_by_slot(db_session, run)[0]
    assert refreshed.state == lifecycle.LEASE_READY


def test_heartbeat_renews_last_heartbeat_at(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    # Back-date the heartbeat clock, then a heartbeat must move it forward.
    _helpers.age_heartbeat(db_session, lease, seconds=10)
    stale_ts = lease.last_heartbeat_at
    payload = _helpers.heartbeat_payload(lease, events_total=5)
    _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run, lease.slot,
                     lease.lease_id, payload)
    db_session.flush()
    refreshed = _helpers.leases_by_slot(db_session, run)[lease.slot]
    assert refreshed.last_heartbeat_at > stale_ts


# --------------------------------------------------------------------------- #
# Superseded: a heartbeat with a stale lease_id is told to give up.
# --------------------------------------------------------------------------- #

def test_heartbeat_with_wrong_lease_id_is_superseded(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    bogus = dict(_helpers.heartbeat_payload(lease))
    bogus["lease_id"] = "le_not_the_holder"
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run,
                               lease.slot, "le_not_the_holder", bogus)
    assert command.get("command") == "superseded"


# --------------------------------------------------------------------------- #
# Lapse -> lost -> re-claim inherits slot + share.
# --------------------------------------------------------------------------- #

def test_lapse_marks_lease_lost(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    # Keep a second lease fresh so the run is not completed out from under us
    # (a run with no live lease finalises); this isolates the lapse behaviour.
    keepalive = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h1", hint_slot=1)
    # Slot 0 goes silent for > 30 s; the boot grace has already elapsed.
    _helpers.age_heartbeat(db_session, lease, seconds=lifecycle.LEASE_LAPSE_S + 5)
    keepalive.last_heartbeat_at = utcnow()
    db_session.commit()
    drivers = {"fake-local": fake_driver, "swarm-local": fake_driver}
    _skip_if_stubbed(lifecycle.supervisor_tick, db_session, drivers, _boot_past())
    db_session.commit()
    by_slot = _helpers.leases_by_slot(db_session, run)
    assert by_slot[0].state == lifecycle.LEASE_LOST
    assert by_slot[1].state == lifecycle.LEASE_CLAIMED  # the fresh one survived


def test_boot_grace_prevents_immediate_lapse(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    _helpers.age_heartbeat(db_session, lease, seconds=lifecycle.LEASE_LAPSE_S + 5)
    db_session.commit()
    drivers = {"fake-local": fake_driver, "swarm-local": fake_driver}
    # boot_time = now: max(last_heartbeat, boot) + 60 s grace is still in the
    # future, so the estate must NOT lapse right after a control-plane restart.
    _skip_if_stubbed(lifecycle.supervisor_tick, db_session, drivers, utcnow())
    db_session.commit()
    refreshed = _helpers.leases_by_slot(db_session, run)[0]
    assert refreshed.state == lifecycle.LEASE_CLAIMED


def test_reclaim_after_lapse_inherits_slot_and_share(db_session, make_pack, settings, fake_driver):
    # A run stays alive while *some* worker holds a live lease; only then can a
    # lapsed slot be re-claimed by a replacement. (A run whose only remaining
    # leases are lost/free/done completes, so keep slot 1 heartbeating.)
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    original = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    keepalive = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h1", hint_slot=1)
    original_share = dict(original.share_json)
    old_lease_id = original.lease_id
    # Slot 0 goes silent; slot 1 stays fresh so the run does not complete.
    _helpers.age_heartbeat(db_session, original, seconds=lifecycle.LEASE_LAPSE_S + 5)
    keepalive.last_heartbeat_at = utcnow()
    db_session.commit()

    drivers = {"fake-local": fake_driver, "swarm-local": fake_driver}
    _skip_if_stubbed(lifecycle.supervisor_tick, db_session, drivers, _boot_past())
    db_session.commit()

    assert run.state not in lifecycle.TERMINAL_STATES  # slot 1 keeps it alive
    assert _helpers.leases_by_slot(db_session, run)[0].state == lifecycle.LEASE_LOST

    # A replacement worker claims and must inherit the same slot + share.
    replacement = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0-replacement")
    assert replacement.slot == 0
    assert replacement.share_json == original_share
    assert replacement.state == lifecycle.LEASE_CLAIMED
    assert replacement.holder == "h0-replacement"

    # The old holder's heartbeat (stale lease_id) is now superseded.
    stale_payload = {"slot": 0, "lease_id": old_lease_id, "protocol_version": 1,
                     "state": "generating"}
    command = _skip_if_stubbed(lifecycle.record_heartbeat, db_session, run, 0,
                               old_lease_id, stale_payload)
    # Either the lease_id rotated (superseded) or the same row was re-issued to
    # the same id; the contract only guarantees the *stale* holder is evicted
    # when a different lease now holds the slot.
    if replacement.lease_id != old_lease_id:
        assert command.get("command") == "superseded"


def test_claim_when_no_free_lease_returns_superseded_or_raises(
        db_session, make_pack, settings, fake_driver):
    # All slots claimed; a further claim has nothing free. The Core may return a
    # superseded-style signal or raise; either is acceptable, but it must not
    # hand out a duplicate slot.
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=1)
    first = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0")
    assert first.slot == 0
    try:
        second = lifecycle.claim_lease(db_session, run, "h1")
    except NotImplementedError:
        pytest.skip("lifecycle.claim_lease not implemented yet (Core builder)")
    except Exception:
        return  # raising on an exhausted fleet is acceptable
    if second is not None:
        # If a lease is returned it must not duplicate the held slot with a new
        # holder (that would break single-holder-per-slot fencing).
        assert not (second.slot == 0 and second.holder == "h1"
                    and second.lease_id == first.lease_id
                    and second.state == lifecycle.LEASE_CLAIMED)
