"""Regression tests for the Phase 1 stages 1-2 adversarial review findings.

- claim#race: a free claim rotates the lease_id (never hands out the seeded id),
  so two racers converging on one row can never share a fencing id.
- provision#zombie: a provisioning run whose workers all lapse before readying
  fails (provision-timeout) instead of pinning the target cap forever.
- driver#status: SwarmDriver.status() re-raises a transient (5xx/timeout) error
  and only reports desired=0 for a genuine 404, so boot reconciliation cannot
  orphan a live fleet on a Portainer hiccup.
"""

from __future__ import annotations

import datetime

import httpx
import pytest

from server import lifecycle
from server.drivers.base import DriverError, DriverRef
from server.drivers.swarm import SwarmDriver
from server.models import utcnow

from . import _helpers


def _boot_past():
    return utcnow() - datetime.timedelta(seconds=lifecycle.BOOT_GRACE_S + 3600)


# --------------------------------------------------------------------------- #
# claim#race: every real claim mints a fresh lease_id.
# --------------------------------------------------------------------------- #

def test_free_claim_rotates_lease_id(db_session, make_pack, settings, fake_driver):
    pack_dir = make_pack()
    run = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                            workers=4)["run"]
    seeded = _helpers.leases_by_slot(db_session, run)[0].lease_id
    lease = lifecycle.claim_lease(db_session, run, "holder-a")
    assert lease.slot == 0
    # The claim must NOT return the seeded placeholder id: a fresh fence means two
    # claimers can never end up co-holding one lease_id.
    assert lease.lease_id != seeded
    assert lease.lease_id  # non-empty


def test_two_free_claims_get_distinct_slots_and_lease_ids(db_session, make_pack,
                                                          settings, fake_driver):
    pack_dir = make_pack()
    run = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                            workers=4)["run"]
    a = lifecycle.claim_lease(db_session, run, "holder-a")
    b = lifecycle.claim_lease(db_session, run, "holder-b")
    assert {a.slot, b.slot} == {0, 1}
    assert a.lease_id != b.lease_id


def test_same_holder_reclaim_keeps_lease_id(db_session, make_pack, settings,
                                            fake_driver):
    # The idempotent re-claim path must be unaffected: a retried claim by the same
    # holder returns the same lease unchanged (same lease_id).
    pack_dir = make_pack()
    run = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                            workers=2)["run"]
    first = lifecycle.claim_lease(db_session, run, "holder-a")
    again = lifecycle.claim_lease(db_session, run, "holder-a")
    assert again.slot == first.slot
    assert again.lease_id == first.lease_id


# --------------------------------------------------------------------------- #
# provision#zombie: an all-lost provisioning run terminates.
# --------------------------------------------------------------------------- #

def test_provisioning_run_all_lost_before_ready_fails(db_session, make_pack,
                                                      settings, fake_driver):
    pack_dir = make_pack()
    run = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                            workers=2, fleet="fake-local")["run"]
    # Both workers claim, then die during warm-up before ever calling ready.
    for holder in ("w0", "w1"):
        lifecycle.claim_lease(db_session, run, holder)
    for lease in lifecycle.get_run_leases(db_session, run):
        _helpers.age_heartbeat(db_session, lease, 300)  # past LEASE_LAPSE_S
    # Age the run past the provisioning window so the timeout logic engages.
    run.created_at = utcnow() - datetime.timedelta(
        seconds=lifecycle.PROVISION_TIMEOUT_S + 60)
    db_session.commit()

    # supervisor_tick mutates the run in this same session (identity map); the
    # caller owns the commit, so assert on the in-session object directly.
    lifecycle.supervisor_tick(db_session, {"fake-local": fake_driver}, _boot_past())

    assert run.state == lifecycle.STATE_FAILED
    assert run.end_reason == "provision-timeout"


def test_provisioning_run_with_a_ready_worker_is_not_failed(db_session, make_pack,
                                                            settings, fake_driver):
    # A run with >=1 ready lease at the timeout must degrade-release, NOT fail via
    # the new all-lost terminal.
    pack_dir = make_pack()
    run = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                            workers=2, fleet="fake-local")["run"]
    a = lifecycle.claim_lease(db_session, run, "w0")
    lifecycle.mark_ready(db_session, run, a.slot, a.lease_id)
    run.created_at = utcnow() - datetime.timedelta(
        seconds=lifecycle.PROVISION_TIMEOUT_S + 60)
    db_session.commit()

    lifecycle.supervisor_tick(db_session, {"fake-local": fake_driver}, _boot_past())

    assert run.state != lifecycle.STATE_FAILED
    assert run.t0 is not None  # released (degraded) rather than failed


# --------------------------------------------------------------------------- #
# driver#status: transient errors do not collapse to desired=0.
# --------------------------------------------------------------------------- #

def _swarm_with(service_status, replicas=3):
    """A SwarmDriver whose service-inspect returns ``service_status`` and whose
    /tasks call returns an empty list."""
    def handler(request):
        # type: (httpx.Request) -> httpx.Response
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json=[])
        if service_status >= 400:
            return httpx.Response(service_status, json={"message": "boom"})
        return httpx.Response(200, json={
            "Spec": {"Mode": {"Replicated": {"Replicas": replicas}}}})
    driver = SwarmDriver(host="https://p:9443", token="t", endpoint=6)
    driver._transport = httpx.MockTransport(handler)
    return driver


def _ref():
    return DriverRef(kind="swarm", id="stoker-run-1",
                     raw={"run_id": 1, "name": "stoker-run-1", "endpoint": 6})


def test_status_reraises_transient_error():
    # A 500 (swarm leader re-election, Portainer blip) must NOT become desired=0.
    driver = _swarm_with(500)
    with pytest.raises(DriverError):
        driver.status(_ref())


def test_status_desired_zero_only_on_real_404():
    driver = _swarm_with(404)
    status = driver.status(_ref())
    assert status.desired == 0
    assert status.running == 0


def test_status_reports_live_desired():
    driver = _swarm_with(200, replicas=5)
    status = driver.status(_ref())
    assert status.desired == 5
