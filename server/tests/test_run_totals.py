"""Run totals on the run page: live from heartbeats until each final arrives.

``totals_json`` only grew when a worker reported its final summary, so a
running fleet showed no events and no volume at all, and a worker that never
reported (lost, killed in a teardown) was never counted.
"""

from __future__ import annotations

import datetime

from server import lifecycle
from server.models import MetricSample, utcnow

from . import _helpers


def _sample(db, run, slot, seconds_ago, **counters):
    db.add(MetricSample(
        run_id=run.id, slot=slot,
        ts=utcnow() - datetime.timedelta(seconds=seconds_ago), **counters))


def test_a_running_run_counts_up_from_its_heartbeats(
        client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=2, state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    for lease in _helpers.leases_by_slot(db_session, run).values():
        lease.state = lifecycle.LEASE_RUNNING
    # Cumulative counters: only the latest sample per slot counts.
    _sample(db_session, run, 0, 10, events_total=100, bytes_total=1000, hec_2xx=1)
    _sample(db_session, run, 0, 5, events_total=300, bytes_total=3000, hec_2xx=3)
    _sample(db_session, run, 1, 5, events_total=200, bytes_total=2000, hec_2xx=2)
    db_session.commit()

    detail = client.get("/api/runs/%d" % run.id).json()
    assert detail["totals_json"]["events_total"] == 500
    assert detail["totals_json"]["bytes_total"] == 5000
    assert detail["totals_json"]["hec_2xx"] == 5

    listed = {r["id"]: r for r in client.get("/api/runs").json()}
    assert listed[run.id]["totals_json"]["events_total"] == 500


def test_a_reported_final_is_not_counted_twice(
        client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=2, state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    leases = _helpers.leases_by_slot(db_session, run)
    # Slot 0 drained and its final is folded; slot 1 is still sending.
    leases[0].state = lifecycle.LEASE_DONE
    leases[1].state = lifecycle.LEASE_RUNNING
    run.totals_json = {"events_total": 400, "bytes_total": 4000, "reason": "drain"}
    _sample(db_session, run, 0, 5, events_total=390, bytes_total=3900)
    _sample(db_session, run, 1, 5, events_total=250, bytes_total=2500)
    db_session.commit()

    totals = client.get("/api/runs/%d" % run.id).json()["totals_json"]
    assert totals["events_total"] == 650
    assert totals["bytes_total"] == 6500
    assert totals["reason"] == "drain"


def test_a_worker_lost_before_its_final_still_counts(
        client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=1, state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    _helpers.leases_by_slot(db_session, run)[0].state = lifecycle.LEASE_LOST
    run.state = lifecycle.STATE_FAILED
    _sample(db_session, run, 0, 5, events_total=700, bytes_total=7000)
    db_session.commit()

    totals = client.get("/api/runs/%d" % run.id).json()["totals_json"]
    assert totals["events_total"] == 700


def test_the_stored_totals_are_left_alone(db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=1, state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    _helpers.leases_by_slot(db_session, run)[0].state = lifecycle.LEASE_RUNNING
    run.totals_json = {}
    _sample(db_session, run, 0, 5, events_total=50)
    db_session.flush()

    assert lifecycle.live_totals(db_session, [run])[run.id]["events_total"] == 50
    # What the workers reported is the record; the live view is derived.
    assert run.totals_json == {}
