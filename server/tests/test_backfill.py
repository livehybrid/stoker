"""Backfill: control-plane provisioning, the estimate endpoint, and the slice."""
from __future__ import annotations

from sqlalchemy import select

from server import lifecycle
from server.models import Run, WorkerLease

from . import _helpers
from .test_metric_packs import _valid_config


def _metric_pack(client):
    return client.post("/api/metric-packs",
                       json={"name": "kpi", "config": _valid_config()}).json()


def _metric_spec(client, mp_id, target_id):
    return client.post("/api/specs", json={
        "name": "m", "pack_id": mp_id, "target_id": target_id, "engine": "metrics",
        "rate_mode": "count_interval", "rate_value": 2, "interval_s": 10,
        "workers": 1, "fleet": "fake-local"}).json()


# ---- estimate ----

def test_backfill_estimate_metrics(client, db_session, settings):
    target = _helpers.make_target(db_session, settings=settings)
    db_session.commit()
    mp = _metric_pack(client)
    spec = _metric_spec(client, mp["id"], target.id)
    r = client.post("/api/specs/%d/backfill_estimate" % spec["id"],
                    json={"window_s": 3600, "resolution_s": 60})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["engine"] == "metrics"
    assert b["series"] == 2                    # _valid_config: 2 products
    assert b["events"] == 60 * 2               # 3600/60 = 60 ticks x 2 series
    assert b["cap_eps"] == 5000.0
    assert b["bytes"] and b["bytes"] > 0
    assert "duplicate" in b["warning"].lower()


def test_backfill_estimate_eventgen(client, db_session, settings, make_pack):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=100.0, workers=1,
                              fleet="fake-local")
    db_session.commit()
    r = client.post("/api/specs/%d/backfill_estimate" % spec.id, json={"window_s": 600})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["engine"] == "eventgen"
    assert b["events"] == 600 * 100            # window x live eps
    assert b["series"] is None


# ---- provisioning ----

def test_metrics_backfill_run_overrides_rate_and_carries_window(
        client, db_session, settings, fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    db_session.commit()
    mp = _metric_pack(client)
    spec = _metric_spec(client, mp["id"], target.id)
    run = client.post("/api/specs/%d/run" % spec["id"],
                      json={"backfill_window_s": 3600, "backfill_resolution_s": 60})
    assert run.status_code in (200, 201), run.text
    r = db_session.get(Run, run.json()["run_id"])
    snap = r.spec_snapshot_json
    assert snap["rate_mode"] == "eps"          # overridden from count_interval
    assert snap["rate_value"] == 5000.0        # delivery cap
    assert snap["backfill"]["start_s"] < snap["backfill"]["end_s"]
    assert snap["backfill"]["resolution_s"] == 60
    assert snap["duration_s"] and snap["duration_s"] > 0
    # the claim slice carries the window + an eps share
    lease = db_session.execute(
        select(WorkerLease).where(WorkerLease.run_id == r.id)).scalars().first()
    slice_doc = lifecycle.build_slice(r, lease, settings=settings)
    assert slice_doc["backfill"]["resolution_s"] == 60
    assert "eps" in slice_doc["share"]


def test_eventgen_backfill_run_provisions(client, db_session, settings, make_pack, fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=100.0, workers=1,
                              fleet="fake-local")
    db_session.commit()
    run = client.post("/api/specs/%d/run" % spec.id, json={"backfill_window_s": 600})
    assert run.status_code in (200, 201), run.text
    snap = db_session.get(Run, run.json()["run_id"]).spec_snapshot_json
    assert snap["rate_mode"] == "eps"
    assert snap["backfill"]["start_s"] < snap["backfill"]["end_s"]
    assert snap["duration_s"] and snap["duration_s"] > 0


def test_normal_run_carries_no_backfill(client, db_session, settings, make_pack, fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, workers=1, fleet="fake-local")
    db_session.commit()
    run = client.post("/api/specs/%d/run" % spec.id, json={})
    r = db_session.get(Run, run.json()["run_id"])
    assert "backfill" not in (r.spec_snapshot_json or {})
    lease = db_session.execute(
        select(WorkerLease).where(WorkerLease.run_id == r.id)).scalars().first()
    assert "backfill" not in lifecycle.build_slice(r, lease, settings=settings)
