"""Operator pause / resume, and totals after a forced stop.

A paused run holds output (every heartbeat answers ``pause``), keeps its leases
renewed, and a bounded run's end moves out by the time it spent paused.
"""

from __future__ import annotations

import datetime

import pytest

from server import lifecycle
from server.drivers.base import DriverRef
from server.drivers.k8s import K8sDriver
from server.models import MetricSample, utcnow

from . import _helpers

pytestmark = pytest.mark.usefixtures("fake_driver")


def _running(db, make_pack, settings, fake_driver, **kw):
    ctx = _helpers.full_run(db, make_pack(), settings, driver=fake_driver,
                            state=lifecycle.STATE_RUNNING, **kw)
    run = ctx["run"]
    run.t0 = utcnow() - datetime.timedelta(seconds=10)
    for lease in _helpers.leases_by_slot(db, run).values():
        lease.state = lifecycle.LEASE_RUNNING
    db.commit()
    return run


def _beat(db, run, lease, settings):
    return lifecycle.record_heartbeat(
        db, run, lease.slot, lease.lease_id, _helpers.heartbeat_payload(lease),
        settings=settings)


def test_pause_then_resume_round_trip(client, db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=2)

    r = client.post("/api/runs/%d/pause" % run.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "paused"
    assert body["paused_at"] is not None

    # Pausing twice, or resuming a running run, is a clear 409.
    assert client.post("/api/runs/%d/pause" % run.id).status_code == 409

    r = client.post("/api/runs/%d/resume" % run.id)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"
    assert r.json()["paused_at"] is None
    assert client.post("/api/runs/%d/resume" % run.id).status_code == 409

    kinds = [(e["kind"], (e.get("detail_json") or {}).get("to"))
             for e in client.get("/api/runs/%d/events" % run.id).json()]
    assert ("state", "paused") in kinds
    assert ("state", "running") in kinds


def test_heartbeat_answers_pause_while_paused(db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=1)
    lease = _helpers.leases_by_slot(db_session, run)[0]
    lifecycle.pause_run(db_session, run, fake_driver)
    cmd = _beat(db_session, run, lease, settings)
    assert cmd["command"] == "pause"
    assert cmd["paused_s"] >= 0
    # The lease is still renewed while paused.
    assert lease.last_heartbeat_at is not None

    lifecycle.resume_run(db_session, run, fake_driver)
    cmd = _beat(db_session, run, lease, settings)
    assert cmd["command"] == "continue"


def test_a_pause_moves_the_duration_end_out(db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=1,
                   duration_s=60)
    run.t0 = utcnow() - datetime.timedelta(seconds=90)
    # Paused for 45 s earlier in the run: the end is t0 + 60 + 45, still ahead.
    run.paused_s = 45.0
    lifecycle._check_duration(db_session, run, utcnow())
    assert run.state == lifecycle.STATE_RUNNING
    # Past t0 + duration + paused: it drains.
    lifecycle._check_duration(db_session, run, utcnow() + datetime.timedelta(seconds=20))
    assert run.state == lifecycle.STATE_DRAINING


def test_a_paused_run_never_ends_on_duration(db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=1,
                   duration_s=5)
    lifecycle.pause_run(db_session, run, fake_driver)
    lifecycle._check_duration(db_session, run, utcnow() + datetime.timedelta(hours=1))
    assert run.state == lifecycle.STATE_PAUSED


def test_resume_banks_the_paused_time(db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=1)
    lifecycle.pause_run(db_session, run, fake_driver)
    run.paused_at = utcnow() - datetime.timedelta(seconds=30)
    lifecycle.resume_run(db_session, run, fake_driver)
    assert 29.0 <= run.paused_s <= 40.0
    assert run.paused_at is None


def test_scale_and_rescale_wait_for_resume(client, db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=2)
    assert client.post("/api/runs/%d/pause" % run.id).status_code == 200
    r = client.post("/api/runs/%d/scale" % run.id, json={"workers": 3})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "run_paused"
    r = client.post("/api/runs/%d/rescale" % run.id, json={"rate_value": 10})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "run_paused"


def test_engine_paced_runs_cannot_be_paused(client, db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=1,
                   rate_mode="count_interval", rate_value=10)
    r = client.post("/api/runs/%d/pause" % run.id)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "pause_unsupported"


def test_a_paused_run_can_be_stopped(client, db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=1)
    assert client.post("/api/runs/%d/pause" % run.id).status_code == 200
    r = client.post("/api/runs/%d/stop" % run.id, json={"force": True})
    assert r.status_code == 200, r.text
    detail = client.get("/api/runs/%d" % run.id).json()
    assert detail["state"] == "stopped"
    assert detail["paused_at"] is None


def test_force_stop_keeps_the_heartbeat_totals(client, db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=2)
    now = utcnow()
    db_session.add(MetricSample(run_id=run.id, slot=0, ts=now,
                                events_total=300, bytes_total=3000, hec_2xx=3))
    db_session.add(MetricSample(run_id=run.id, slot=1, ts=now,
                                events_total=200, bytes_total=2000, hec_2xx=2))
    db_session.commit()

    r = client.post("/api/runs/%d/stop" % run.id, json={"force": True})
    assert r.status_code == 200, r.text
    detail = client.get("/api/runs/%d" % run.id).json()
    assert detail["state"] == "stopped"
    assert detail["totals_json"]["events_total"] == 500
    assert detail["totals_json"]["bytes_total"] == 5000
    assert detail["totals_json"]["hec_2xx"] == 5
    listed = {x["id"]: x for x in client.get("/api/runs").json()}
    assert listed[run.id]["totals_json"]["events_total"] == 500


def test_force_stop_does_not_double_count_a_reported_final(
        client, db_session, settings, make_pack, fake_driver):
    run = _running(db_session, make_pack, settings, fake_driver, workers=2)
    leases = _helpers.leases_by_slot(db_session, run)
    leases[0].state = lifecycle.LEASE_DONE
    run.totals_json = {"events_total": 400, "bytes_total": 4000}
    now = utcnow()
    db_session.add(MetricSample(run_id=run.id, slot=0, ts=now, events_total=390,
                                bytes_total=3900))
    db_session.add(MetricSample(run_id=run.id, slot=1, ts=now, events_total=250,
                                bytes_total=2500))
    db_session.commit()

    client.post("/api/runs/%d/stop" % run.id, json={"force": True})
    totals = client.get("/api/runs/%d" % run.id).json()["totals_json"]
    assert totals["events_total"] == 650
    assert totals["bytes_total"] == 6500


class _Batch(object):
    def __init__(self):
        self.patches = []

    def patch_namespaced_job(self, name, namespace, body):
        self.patches.append((name, namespace, body))


def test_k8s_extend_deadline_recomputes_from_duration(monkeypatch):
    drv = K8sDriver.__new__(K8sDriver)
    batch = _Batch()
    monkeypatch.setattr(drv, "_batch_api", lambda: batch, raising=False)
    monkeypatch.setattr(drv, "_namespace_of", lambda ref: "ns", raising=False)
    monkeypatch.setattr(drv, "_call", lambda fn, **kw: fn(**kw), raising=False)
    ref = DriverRef(kind="k8s", id="stoker-run-1", raw={})
    drv.extend_deadline(ref, 600.0, 45.2)
    drv.extend_deadline(ref, 600.0, 45.2)
    assert batch.patches[-1][2] == {"spec": {"activeDeadlineSeconds": 600 + 300 + 46}}
    assert len({str(p) for p in batch.patches}) == 1  # never compounds
    drv.extend_deadline(ref, 0, 10)  # unbounded run: no deadline to move
    assert len(batch.patches) == 2
