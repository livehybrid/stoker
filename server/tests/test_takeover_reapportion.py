"""Late claim of a lost lease after a partial (degraded) release.

Regression for an over-delivery bug: ``evaluate_release(force_partial=True)``
marks the stragglers LOST and re-apportions the FULL run rate across only the
ready slots — but the lost leases keep their original 1/N shares. A straggler
pod that then starts late and claims its lost lease would pick up that stale
pre-reapportionment share ON TOP of the already-full ready subset, so the
fleet's aggregate would exceed the requested rate.

The fix (``lifecycle._reapportion_on_takeover``): a lost-lease takeover on a
DEGRADED released run folds the claimed slot back into the current
apportionment — the claimer's share is rewritten in place (its slice carries
it) and the live workers whose shares differ are flagged to retarget, so the
aggregate stays exactly at the requested rate. On a NON-degraded run the lost
slot's stored share already is the current apportionment, and the takeover
must touch nothing (no share change, no spurious retargets) — a transiently
lost worker elsewhere could otherwise recover into a double-counted rate.
"""

from __future__ import annotations

import pytest

from server import lifecycle

from . import _helpers

pytestmark = pytest.mark.usefixtures("fake_driver")


def _degraded_release(db, make_pack, settings, fake_driver, workers=4,
                      ready_slots=(0, 1), rate_value=1000.0):
    """Provision, ready a subset, force a partial release; return the run."""
    ctx = _helpers.full_run(db, make_pack(), settings, driver=fake_driver,
                            workers=workers, rate_value=rate_value)
    run = ctx["run"]
    for slot in ready_slots:
        lease = lifecycle.claim_lease(db, run, "h%d" % slot, hint_slot=slot)
        lifecycle.mark_ready(db, run, slot, lease.lease_id)
    db.flush()
    released = lifecycle.evaluate_release(db, run, force_partial=True)
    db.flush()
    assert released and run.degraded and run.t0 is not None
    return run


def test_late_claim_joins_current_apportionment(
        db_session, make_pack, settings, fake_driver):
    # 1000 EPS / 4 slots: slots 0-1 ready (re-apportioned to 500 each on the
    # partial release), slots 2-3 lost with their stale 250 shares.
    run = _degraded_release(db_session, make_pack, settings, fake_driver)

    lease = lifecycle.claim_lease(db_session, run, "late-pod")
    db_session.flush()
    assert lease.slot == 2  # lowest claimable (lost) slot

    by_slot = _helpers.leases_by_slot(db_session, run)
    third = 1000.0 / 3.0
    # The late claimer gets a CURRENT share, not its stale pre-release 250.
    assert lease.share_json["eps"] == pytest.approx(third, rel=1e-9)
    # The live slots are pulled back onto the same apportionment via retarget.
    for slot in (0, 1):
        share = by_slot[slot].share_json
        assert share["eps"] == pytest.approx(third, rel=1e-9)
        assert share.get(lifecycle.RETARGET_MARKER) is True
    # Aggregate across the now-live fleet is exactly the requested rate.
    live_total = sum(
        lifecycle.public_share(by_slot[s].share_json).get("eps", 0.0)
        for s in (0, 1, 2))
    assert live_total == pytest.approx(1000.0, rel=1e-9)


def test_second_late_claim_converges_further(
        db_session, make_pack, settings, fake_driver):
    run = _degraded_release(db_session, make_pack, settings, fake_driver)
    lifecycle.claim_lease(db_session, run, "late-a")
    db_session.flush()
    lifecycle.claim_lease(db_session, run, "late-b")
    db_session.flush()

    by_slot = _helpers.leases_by_slot(db_session, run)
    live_total = sum(
        lifecycle.public_share(by_slot[s].share_json).get("eps", 0.0)
        for s in range(4))
    # All four slots live again: back to the full apportionment, exact total.
    assert live_total == pytest.approx(1000.0, rel=1e-9)
    for slot in range(4):
        assert lifecycle.public_share(by_slot[slot].share_json)["eps"] == \
            pytest.approx(250.0, rel=1e-9)


def test_takeover_on_non_degraded_run_changes_nothing(
        db_session, make_pack, settings, fake_driver):
    # A fully-released run whose slot 0 lapses: the replacement inherits the
    # slot's share unchanged and no other lease is retargeted (their shares
    # already are the current apportionment).
    ctx = _helpers.full_run(db_session, make_pack(), settings,
                            driver=fake_driver, workers=4, rate_value=1000.0)
    run = ctx["run"]
    for slot in range(4):
        lease = lifecycle.claim_lease(db_session, run, "h%d" % slot, hint_slot=slot)
        lifecycle.mark_ready(db_session, run, slot, lease.lease_id)
    db_session.flush()
    # mark_ready already released the fleet once the fourth slot readied.
    lifecycle.evaluate_release(db_session, run)
    assert run.t0 is not None
    assert run.degraded is False

    by_slot = _helpers.leases_by_slot(db_session, run)
    by_slot[0].state = lifecycle.LEASE_LOST
    db_session.flush()

    lease = lifecycle.claim_lease(db_session, run, "replacement")
    db_session.flush()
    assert lease.slot == 0
    assert lease.share_json["eps"] == pytest.approx(250.0, rel=1e-9)
    by_slot = _helpers.leases_by_slot(db_session, run)
    for slot in (1, 2, 3):
        assert lifecycle.RETARGET_MARKER not in (by_slot[slot].share_json or {})
        assert by_slot[slot].share_json["eps"] == pytest.approx(250.0, rel=1e-9)
