"""Regression tests for the pre-launch review hardening (2026-07-16).

Each test pins a specific fix so a future refactor cannot silently regress it:
the symlink-exfiltration guard, record_final lease fencing, the releasing/
lost-lease completion semantics, terminal-fleet reaping, the metric roll-up
counter kind, the metric-count cap, unknown-driver failure, the swarm resource
limits and the constant-time password verify.
"""
from __future__ import annotations

import datetime
import io
import os
import tarfile

import pytest
from sqlalchemy import select

from server import auth, bundles, lifecycle, metrics_lifecycle
from server.drivers import clear_cache, get_driver
from server.drivers.base import DriverError, RunSnapshot
from server.drivers.swarm import SwarmDriver
from server.models import MetricSample, utcnow

from . import _helpers as H


# --- #1: the bundle builder must not follow a symlink out of the pack tree -- #

def test_bundle_excludes_symlinked_files(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-MASTER-KEY", encoding="utf-8")
    pack = tmp_path / "evilpack"
    (pack / "default").mkdir(parents=True)
    (pack / "samples").mkdir()
    (pack / "default" / "eventgen.conf").write_text(
        "[s.sample]\nmode = sample\n", encoding="utf-8")
    (pack / "samples" / "s.sample").write_text("a real sample line\n", encoding="utf-8")
    # Hostile symlink pointing at a file OUTSIDE the pack tree.
    os.symlink(str(secret), str(pack / "samples" / "leak"))

    members = bundles._iter_pack_files(str(pack))
    assert not any(arc.endswith("/leak") for _full, arc in members), \
        "a symlinked pack file must never be bundled"

    raw = bundles.build_tarball_bytes(str(pack), {"engine": "eventgen"})
    tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    for name in tf.getnames():
        member = tf.getmember(name)
        if member.isfile():
            assert b"TOPSECRET" not in tf.extractfile(name).read(), \
                "the target of a symlink leaked into the bundle"


# --- #4: record_final fences on lease_id (a superseded worker is ignored) --- #

def test_record_final_ignores_superseded_lease(db_session, settings, fake_driver, make_pack):
    db = db_session
    ctx = H.full_run(db, make_pack(), settings, driver=fake_driver, workers=1,
                     state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    run.t0 = utcnow() - datetime.timedelta(seconds=5)
    lease = H.leases_by_slot(db, run)[0]
    lease.state = lifecycle.LEASE_RUNNING
    db.flush()

    # A superseded worker (stale lease_id) must not finalise the live lease.
    lifecycle.record_final(db, run, lease.slot, {"events": 5}, [], lease_id="stale-id")
    assert H.leases_by_slot(db, run)[0].state == lifecycle.LEASE_RUNNING
    assert run.state == lifecycle.STATE_RUNNING

    # The real holder finalises normally.
    lease = H.leases_by_slot(db, run)[0]
    lifecycle.record_final(db, run, lease.slot, {"events": 5}, [], lease_id=lease.lease_id)
    assert H.leases_by_slot(db, run)[0].state == lifecycle.LEASE_DONE


# --- #5: a releasing run completes when its worker reports final ------------ #

def test_releasing_run_completes_when_worker_reports_final(
        db_session, settings, fake_driver, make_pack):
    db = db_session
    ctx = H.full_run(db, make_pack(), settings, driver=fake_driver, workers=1,
                     state=lifecycle.STATE_RELEASING)
    run = ctx["run"]
    run.t0 = utcnow() + datetime.timedelta(seconds=2)  # released, not yet running
    lease = H.leases_by_slot(db, run)[0]
    lease.state = lifecycle.LEASE_READY
    db.flush()
    # The engine ran and exited before any post-T0 heartbeat promoted the run.
    lifecycle.record_final(db, run, lease.slot, {"reason": "engine-exit"}, [],
                           lease_id=lease.lease_id)
    assert run.state == lifecycle.STATE_COMPLETED, \
        "a releasing run whose worker finalises must not wedge in releasing"


# --- #6: a lost-only running run defers instead of failing instantly -------- #

def test_running_run_with_only_lost_lease_defers(
        db_session, settings, fake_driver, make_pack):
    db = db_session
    ctx = H.full_run(db, make_pack(), settings, driver=fake_driver, workers=1,
                     state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    run.t0 = utcnow() - datetime.timedelta(seconds=30)
    lease = H.leases_by_slot(db, run)[0]
    lease.state = lifecycle.LEASE_LOST
    lease.last_heartbeat_at = utcnow() - datetime.timedelta(seconds=45)
    db.flush()
    # A transient lapse (lost, nothing done) must NOT terminalise the run; the
    # auto-abort grace / a recovering heartbeat own that.
    assert lifecycle.maybe_complete_run(db, run) is False
    assert run.state == lifecycle.STATE_RUNNING

    # Once a worker actually finishes (done), completion proceeds.
    lease = H.leases_by_slot(db, run)[0]
    lease.state = lifecycle.LEASE_DONE
    db.flush()
    assert lifecycle.maybe_complete_run(db, run) is True
    assert run.state == lifecycle.STATE_COMPLETED


# --- #7: the supervisor reaps a naturally-completed fleet ------------------- #

def test_supervisor_reaps_completed_fleet(db_session, settings, fake_driver, make_pack):
    db = db_session
    ctx = H.full_run(db, make_pack(), settings, driver=fake_driver, workers=1,
                     state=lifecycle.STATE_COMPLETED)
    run = ctx["run"]
    run.ended_at = utcnow()
    db.commit()
    assert lifecycle.driver_ref_of(run) is not None
    assert not lifecycle._has_event(db, run, "fleet_destroyed")

    drivers = {"fake-local": fake_driver, "swarm-local": fake_driver}
    lifecycle.supervisor_tick(
        db, drivers, boot_time=utcnow() - datetime.timedelta(seconds=120))
    db.commit()

    assert lifecycle._has_event(db, run, "fleet_destroyed"), \
        "a completed run's fleet must be reaped by the supervisor"
    assert fake_driver.is_destroyed(lifecycle.driver_ref_of(run))


# --- #21: roll-up keeps LAST for cumulative HEC counters -------------------- #

def test_rollup_keeps_last_for_cumulative_counters(db_session, settings, fake_driver, make_pack):
    db = db_session
    ctx = H.full_run(db, make_pack(), settings, driver=fake_driver, workers=1)
    run = ctx["run"]
    # Older than the 48 h roll-up window, floored to a minute so the +5 s/+10 s
    # samples below can never straddle a 60 s bucket boundary (epoch // 60).
    old = (utcnow() - datetime.timedelta(hours=72)).replace(second=0, microsecond=0)
    # Three samples in one 60 s bucket; all counters cumulative (increasing).
    for i, ev in enumerate((10, 20, 30)):
        db.add(MetricSample(
            run_id=run.id, slot=0, ts=old + datetime.timedelta(seconds=i * 5),
            events_total=ev, bytes_total=ev * 100, hec_2xx=ev // 10, hec_4xx=0))
    db.commit()

    result = metrics_lifecycle.roll_up_and_prune(db, settings)
    assert result["aggregates"] == 1
    rows = db.execute(
        select(MetricSample).where(MetricSample.run_id == run.id)).scalars().all()
    assert len(rows) == 1
    agg = rows[0]
    # Cumulative counters keep the LAST value, never the sum (10+20+30=60 is wrong).
    assert agg.events_total == 30
    assert agg.hec_2xx == 3  # last (=30//10), not 1+2+3=6


# --- #19: the metric-count cap ---------------------------------------------- #

def test_lint_metrics_rejects_too_many_metrics():
    cfg = {
        "resolution_s": 10,
        "dimensions": [{"key": "host", "values": ["a", "b"]}],
        "metrics": [
            {"name": "m%d" % i, "kind": "gauge", "min": 0, "p95": 1, "max": 2,
             "pattern": {"type": "constant"}}
            for i in range(bundles._MAX_METRICS_PER_PACK + 1)
        ],
    }
    errors = bundles.lint_metrics_config(cfg)
    assert any("too many metrics" in e for e in errors)


# --- #26: an unknown driver name fails loudly (no silent FakeDriver) -------- #

def test_unknown_driver_name_raises(settings):
    clear_cache()
    with pytest.raises(DriverError):
        get_driver("nonesuch-driver", cache=False)


# --- #17: the swarm spec renders resource limits from driver_opts ----------- #

def test_swarm_spec_renders_resource_limits():
    driver = SwarmDriver(host="127.0.0.1", token=None, endpoint=1)
    snap = RunSnapshot(
        run_id=1, image="img", env={}, labels={"stoker.run": "1"},
        driver_opts={"resources": {"limits": {"cpus": 2, "memory_mb": 512},
                                   "reservations": {"cpus": 0.5, "memory_mb": 128}}},
        stop_grace_s=45)
    spec = driver._service_spec(snap, workers=2)
    res = spec["TaskTemplate"]["Resources"]
    assert res["Limits"]["NanoCPUs"] == 2_000_000_000
    assert res["Limits"]["MemoryBytes"] == 512 * 1024 * 1024
    assert res["Reservations"]["NanoCPUs"] == 500_000_000
    assert res["Reservations"]["MemoryBytes"] == 128 * 1024 * 1024


def test_swarm_spec_has_no_resources_without_driver_opts():
    driver = SwarmDriver(host="127.0.0.1", token=None, endpoint=1)
    snap = RunSnapshot(run_id=1, image="img", env={}, labels={"stoker.run": "1"},
                       driver_opts={}, stop_grace_s=45)
    spec = driver._service_spec(snap, workers=1)
    assert "Resources" not in spec["TaskTemplate"]


# --- #13: constant-time password verify ------------------------------------ #

def test_verify_password_constant_handles_absent_hash():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password_constant("correct horse battery staple", h) is True
    assert auth.verify_password_constant("wrong", h) is False
    # No stored hash (unknown/passwordless user): still False, but bcrypt time is
    # spent internally rather than short-circuiting (the anti-enumeration point).
    assert auth.verify_password_constant("anything", None) is False
