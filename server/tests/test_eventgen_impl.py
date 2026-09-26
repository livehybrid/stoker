"""The spec-level eventgen implementation knob.

``eventgen_impl`` (firebox | python, null = auto) and ``fast_envelope``
(false = classic socket envelope) live on the spec, round-trip through the API,
freeze into the run's spec snapshot, and reach the worker only as
``STOKER_EVENTGEN_IMPL`` / ``STOKER_FAST_ENVELOPE`` when pinned, so an
untouched spec's worker env is byte-for-byte unchanged. Workers report the
implementation they actually launched on heartbeats; the control plane keeps
it on the lease (``_engine_impl`` / ``_envelope``) like the assigned-work pair.
"""
from __future__ import annotations

import pytest

from server import crypto, lifecycle
from server.models import Run

from . import _helpers

pytestmark = pytest.mark.usefixtures("fake_driver")


def _spec_body(pack_id, target_id, **extra):
    body = {
        "name": "impl", "pack_id": pack_id, "target_id": target_id,
        "engine": "eventgen", "rate_mode": "eps", "rate_value": 100,
        "workers": 1, "fleet": "fake-local",
    }
    body.update(extra)
    return body


# ---- API round trip ----

def test_spec_api_round_trips_the_knob(client, db_session, settings, make_pack):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    db_session.commit()

    r = client.post("/api/specs", json=_spec_body(pack.id, target.id))
    assert r.status_code == 201, r.text
    assert r.json()["eventgen_impl"] is None      # auto
    assert r.json()["fast_envelope"] is None

    r = client.post("/api/specs", json=_spec_body(
        pack.id, target.id, eventgen_impl="Firebox", fast_envelope=False))
    assert r.status_code == 201, r.text
    spec = r.json()
    assert spec["eventgen_impl"] == "firebox"      # normalised
    assert spec["fast_envelope"] is False

    r = client.put("/api/specs/%d" % spec["id"], json={"eventgen_impl": "python"})
    assert r.status_code == 200, r.text
    assert r.json()["eventgen_impl"] == "python"
    assert r.json()["fast_envelope"] is False       # untouched by the patch

    r = client.put("/api/specs/%d" % spec["id"],
                   json={"eventgen_impl": "auto", "fast_envelope": None})
    assert r.status_code == 200, r.text
    assert r.json()["eventgen_impl"] is None
    assert r.json()["fast_envelope"] is None

    r = client.post("/api/specs", json=_spec_body(pack.id, target.id, eventgen_impl="cobol"))
    assert r.status_code == 422
    r = client.put("/api/specs/%d" % spec["id"], json={"eventgen_impl": "jinja"})
    assert r.status_code == 422


# ---- worker env projection ----

def _run_for(db, spec, target):
    run = Run(spec_id=spec.id, jwt_kid=crypto.new_kid(),
              spec_snapshot_json=lifecycle.build_spec_snapshot(spec, target))
    db.add(run)
    db.flush()
    return run


def test_untouched_spec_projects_nothing_new(db_session, settings, make_pack):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=500.0, workers=1)
    run = _run_for(db_session, spec, target)
    snap = lifecycle.build_run_snapshot(run, spec, target, "tok", settings=settings)
    assert "STOKER_EVENTGEN_IMPL" not in snap.env
    assert "STOKER_FAST_ENVELOPE" not in snap.env
    assert run.spec_snapshot_json["eventgen_impl"] == "auto"
    assert run.spec_snapshot_json["fast_envelope"] is True


def test_pinned_spec_projects_impl_and_envelope(db_session, settings, make_pack):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=500.0, workers=1)
    spec.eventgen_impl = "python"
    spec.fast_envelope = False
    db_session.flush()
    run = _run_for(db_session, spec, target)
    snap = lifecycle.build_run_snapshot(run, spec, target, "tok", settings=settings)
    assert snap.env["STOKER_EVENTGEN_IMPL"] == "python"
    assert snap.env["STOKER_FAST_ENVELOPE"] == "0"
    assert run.spec_snapshot_json["eventgen_impl"] == "python"
    assert run.spec_snapshot_json["fast_envelope"] is False

    spec.eventgen_impl = "firebox"
    spec.fast_envelope = True
    db_session.flush()
    snap = lifecycle.build_run_snapshot(run, spec, target, "tok", settings=settings)
    assert snap.env["STOKER_EVENTGEN_IMPL"] == "firebox"
    assert "STOKER_FAST_ENVELOPE" not in snap.env


def test_other_engines_ignore_the_knob(db_session, settings, make_pack):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="rawreplay",
                              rate_mode="eps", rate_value=500.0, workers=1)
    spec.eventgen_impl = "python"
    spec.fast_envelope = False
    db_session.flush()
    run = _run_for(db_session, spec, target)
    snap = lifecycle.build_run_snapshot(run, spec, target, "tok", settings=settings)
    assert snap.env["STOKER_ENGINE"] == "rawreplay"
    assert "STOKER_EVENTGEN_IMPL" not in snap.env
    assert "STOKER_FAST_ENVELOPE" not in snap.env


# ---- heartbeat report on the lease ----

class _FakeLease(object):
    def __init__(self, share=None):
        self.share_json = share


def test_store_engine_report_absent_or_garbage_is_ignored():
    lease = _FakeLease({"eps": 250.0})
    lifecycle.store_engine_report(lease, {"events_total": 5})
    assert lease.share_json == {"eps": 250.0}
    lifecycle.store_engine_report(lease, {"engine_impl": "cobol", "envelope": "hec"})
    assert lease.share_json == {"eps": 250.0}


def test_store_engine_report_keeps_impl_and_envelope():
    lease = _FakeLease({"eps": 250.0})
    lifecycle.store_engine_report(lease, {"engine_impl": "firebox", "envelope": "hec"})
    assert lease.share_json == {"eps": 250.0, "_engine_impl": "firebox", "_envelope": "hec"}
    before = lease.share_json
    lifecycle.store_engine_report(lease, {"engine_impl": "firebox", "envelope": "hec"})
    assert lease.share_json is before  # unchanged: no churn
    lifecycle.store_engine_report(lease, {"engine_impl": "python", "envelope": "weird"})
    assert lease.share_json == {"eps": 250.0, "_engine_impl": "python"}


def test_retarget_carries_the_engine_report():
    lease = _FakeLease({"eps": 250.0, "_engine_impl": "firebox", "_envelope": "hec"})
    lifecycle.mark_retarget(lease, {"eps": 300.0})
    assert lease.share_json["_engine_impl"] == "firebox"
    assert lease.share_json["_envelope"] == "hec"
    assert lease.share_json["eps"] == 300.0
