"""The sharding guard as wired into ``POST /api/specs/{id}/run`` (gate 3b).

:mod:`server.tests.test_sharding` pins the prediction itself; these tests pin
the *wiring* — that the submit route actually consults it, that the 422 body
carries the fields the UI needs, and that the runs it must not touch still
launch. Without these the guard could be correct and never called, which is
exactly how the bug shipped: the maths for "how many workers own a series" was
already in ``bundles.metrics_series_count`` and simply nobody asked it before
provisioning a fleet.
"""

from __future__ import annotations

import json

from server.models import Run

from . import _helpers


def _metrics_config(*value_counts):
    # type: (*int) -> dict
    """A metricgen config whose series matrix is the product of value_counts."""
    dims = [
        {"key": "d%d" % i, "values": ["v%d" % v for v in range(n)]}
        for i, n in enumerate(value_counts)
    ]
    return {"resolution_s": 10, "dimensions": dims,
            "metrics": [{"name": "m", "kind": "gauge", "min": 0, "p95": 1, "max": 2}]}


def _make_metrics_pack(db, config, name="metricpack"):
    # type: (...) -> object
    """A metric pack row (builder config, no meaningful source_path)."""
    pack = _helpers.make_pack(db, "builder://metrics", name=name)
    pack.engines_json = ["metrics"]
    pack.builder_config_json = config
    db.flush()
    return pack


def test_run_rejects_metrics_fleet_larger_than_the_series_matrix(
        client, db_session, settings, fake_driver):
    """The reported bug: 4 series across 10 workers idles slots 4-9 silently."""
    target = _helpers.make_target(db_session, settings=settings)
    pack = _make_metrics_pack(db_session, _metrics_config(2, 2))
    spec = _helpers.make_spec(db_session, pack, target, engine="metrics",
                              rate_mode="count_interval", rate_value=None,
                              workers=10, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "workers_exceed_shardable_work"
    assert detail["active_workers"] == 4
    assert detail["suggested_workers"] == 4
    assert detail["limiting_factor"] == "metrics_series"
    # The operator must be told which slots would idle and what to do instead.
    assert "4-9" in detail["detail"]

    # Nothing was provisioned: the guard runs before the driver is touched.
    assert db_session.query(Run).count() == 0
    assert not fake_driver.list_run_ids()


def test_run_allows_a_metrics_fleet_that_exactly_fits_the_matrix(
        client, db_session, settings, fake_driver):
    """The guard bounds the fleet, it does not shrink it: 4 series / 4 workers
    is fully shardable and must still launch."""
    target = _helpers.make_target(db_session, settings=settings)
    pack = _make_metrics_pack(db_session, _metrics_config(2, 2))
    spec = _helpers.make_spec(db_session, pack, target, engine="metrics",
                              rate_mode="count_interval", rate_value=None,
                              workers=4, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 201
    run = db_session.get(Run, resp.json()["run_id"])
    assert run is not None
    assert set(_helpers.leases_by_slot(db_session, run).keys()) == {0, 1, 2, 3}


def test_run_rejects_count_interval_fleet_larger_than_the_stanza_counts(
        client, db_session, settings, make_pack, fake_driver):
    """count_interval splits an integer count by largest remainder, so a
    count of 6 across 10 workers leaves slots 6-9 emitting nothing."""
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack(count=6))
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="count_interval", rate_value=None,
                              workers=10, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "workers_exceed_shardable_work"
    assert detail["limiting_factor"] == "count_interval"
    assert detail["active_workers"] == 6
    assert detail["suggested_workers"] == 6


def test_gated_eps_run_is_unaffected_by_the_sharding_guard(
        client, db_session, settings, make_pack, fake_driver):
    """eps splits a CONTINUOUS rate, so every slot always gets a positive
    share — a 10-worker eps run over a 1-stanza pack must still launch."""
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack(count=6))
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=1000.0,
                              workers=10, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 201
    run = db_session.get(Run, resp.json()["run_id"])
    assert len(_helpers.leases_by_slot(db_session, run)) == 10


def test_rejection_body_never_leaks_the_target_token(
        client, db_session, settings, fake_driver):
    """The 422 is built from spec/pack facts only; no secret material rides
    along (the same invariant the other submit-gate tests assert)."""
    target = _helpers.make_target(db_session, token="hec-secret-token",
                                  settings=settings)
    pack = _make_metrics_pack(db_session, _metrics_config(3))
    spec = _helpers.make_spec(db_session, pack, target, engine="metrics",
                              rate_mode="count_interval", rate_value=None,
                              workers=8, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422
    assert "hec-secret-token" not in json.dumps(resp.json())
