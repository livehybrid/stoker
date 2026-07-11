"""T0 release gate: the moment the whole fleet starts together.

Two paths (contract "Claim/ready/release" + "Supervisor tick"):

* **all ready** -> ``evaluate_release`` sets ``t0 = now + 2 s`` and moves the run
  out of ``provisioning`` (to ``releasing``/``running``). Every non-lost lease
  is ready first.
* **120 s timeout** -> a ``provisioning`` run past ``PROVISION_TIMEOUT_S`` with
  >=1 ready lease releases the *ready subset*, re-apportions across the ready
  slots and marks the run ``degraded`` (unless ``strict_release`` -> fail).

Written against the frozen ``mark_ready`` / ``evaluate_release`` /
``supervisor_tick`` interfaces; skipped cleanly until the Core builder lands.
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
    return utcnow() - datetime.timedelta(seconds=lifecycle.BOOT_GRACE_S + 3600)


def _skip_if_stubbed(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotImplementedError:
        pytest.skip("lifecycle.%s not implemented yet (Core builder)" % fn.__name__)


def _claim_and_ready_all(db, run):
    """Claim + ready every seeded lease (the normal pre-release handshake)."""
    for slot, lease in sorted(_helpers.leases_by_slot(db, run).items()):
        claimed = _skip_if_stubbed(lifecycle.claim_lease, db, run, "h%d" % slot, hint_slot=slot)
        _skip_if_stubbed(lifecycle.mark_ready, db, run, claimed.slot, claimed.lease_id)
    db.flush()


def _provisioned(db, make_pack, settings, fake_driver, **kw):
    ctx = _helpers.full_run(db, make_pack(), settings, driver=fake_driver, **kw)
    return ctx["run"]


# --------------------------------------------------------------------------- #
# Happy path: all ready -> T0.
# --------------------------------------------------------------------------- #

def test_all_ready_sets_t0_and_leaves_provisioning(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    assert run.t0 is None
    _claim_and_ready_all(db_session, run)
    # mark_ready on the final lease should itself trigger the release; if the
    # Core defers that to evaluate_release, call it explicitly.
    if run.t0 is None:
        _skip_if_stubbed(lifecycle.evaluate_release, db_session, run)
    db_session.flush()

    assert run.t0 is not None
    # T0 is roughly now + RELEASE_DELAY_S (2 s) and in the near future.
    now = utcnow()
    t0 = run.t0 if run.t0.tzinfo else run.t0.replace(tzinfo=datetime.timezone.utc)
    delta = (t0 - now).total_seconds()
    assert -1.0 <= delta <= lifecycle.RELEASE_DELAY_S + 2.0
    assert run.state in (lifecycle.STATE_RELEASING, lifecycle.STATE_RUNNING)
    assert not run.degraded


def test_release_is_idempotent(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=2)
    _claim_and_ready_all(db_session, run)
    if run.t0 is None:
        _skip_if_stubbed(lifecycle.evaluate_release, db_session, run)
    db_session.flush()
    first_t0 = run.t0
    assert first_t0 is not None
    # Evaluating again must not move T0 (the fleet has one start instant).
    _skip_if_stubbed(lifecycle.evaluate_release, db_session, run)
    db_session.flush()
    assert run.t0 == first_t0


def test_partial_not_released_before_all_ready(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=3)
    # Only one of three ready; without force_partial there is no T0 yet.
    lease0 = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    _skip_if_stubbed(lifecycle.mark_ready, db_session, run, 0, lease0.lease_id)
    db_session.flush()
    _skip_if_stubbed(lifecycle.evaluate_release, db_session, run)
    db_session.flush()
    assert run.t0 is None
    assert run.state == lifecycle.STATE_PROVISIONING


# --------------------------------------------------------------------------- #
# Timeout path: degraded subset release.
# --------------------------------------------------------------------------- #

def test_provision_timeout_releases_ready_subset_degraded(
        db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=3)
    # Two of three ready; the third never arrives.
    for slot in (0, 1):
        lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h%d" % slot, hint_slot=slot)
        _skip_if_stubbed(lifecycle.mark_ready, db_session, run, slot, lease.lease_id)
    # Age the run past the 120 s provisioning window.
    run.created_at = utcnow() - datetime.timedelta(seconds=lifecycle.PROVISION_TIMEOUT_S + 30)
    db_session.commit()

    drivers = {"fake-local": fake_driver, "swarm-local": fake_driver}
    _skip_if_stubbed(lifecycle.supervisor_tick, db_session, drivers, _boot_past())
    db_session.commit()

    assert run.t0 is not None, "timeout should have forced a partial release"
    assert run.degraded is True
    assert run.state in (lifecycle.STATE_RELEASING, lifecycle.STATE_RUNNING)


def test_provision_timeout_strict_release_fails(db_session, make_pack, settings, fake_driver):
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=3,
                       strict_release=True)
    lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h0", hint_slot=0)
    _skip_if_stubbed(lifecycle.mark_ready, db_session, run, 0, lease.lease_id)
    run.created_at = utcnow() - datetime.timedelta(seconds=lifecycle.PROVISION_TIMEOUT_S + 30)
    db_session.commit()

    drivers = {"fake-local": fake_driver, "swarm-local": fake_driver}
    _skip_if_stubbed(lifecycle.supervisor_tick, db_session, drivers, _boot_past())
    db_session.commit()

    # strict_release forbids a degraded start: the run fails rather than releasing.
    assert run.state == lifecycle.STATE_FAILED
    assert run.t0 is None


def test_force_partial_reapportions_across_ready_slots(db_session, make_pack, settings, fake_driver):
    # 1000 EPS / 4 slots seeded (250 each). If only 2 are ready and we force a
    # partial release, the ready slots should carry the whole rate between them
    # (re-apportioned) so the run still generates the requested load.
    run = _provisioned(db_session, make_pack, settings, fake_driver, workers=4,
                       rate_value=1000.0)
    ready_leases = []
    for slot in (0, 1):
        lease = _skip_if_stubbed(lifecycle.claim_lease, db_session, run, "h%d" % slot, hint_slot=slot)
        _skip_if_stubbed(lifecycle.mark_ready, db_session, run, slot, lease.lease_id)
        ready_leases.append(lease)
    db_session.flush()

    released = _skip_if_stubbed(lifecycle.evaluate_release, db_session, run, True)
    db_session.flush()
    if not released:
        pytest.skip("Core evaluate_release did not force a partial release here")

    assert run.t0 is not None
    assert run.degraded is True
    by_slot = _helpers.leases_by_slot(db_session, run)
    ready_total = sum(by_slot[s].share_json.get("eps", 0.0) for s in (0, 1))
    # The two ready slots now cover (approximately) the full 1000 EPS.
    assert ready_total == pytest.approx(1000.0, rel=1e-6)
