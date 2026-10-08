"""Backfill: control-plane provisioning, the estimate endpoint, and the slice."""
from __future__ import annotations

import pytest

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
    assert b["deliver_eps"] == 5000.0          # metrics has no eps -> fills at the cap
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
    # The spec's eps is the DENSITY (how much history there is); delivery runs
    # at the cap. These three assertions previously read 100 / 60,000 / 600,
    # i.e. "a 600 s backfill takes 600 s", which was the bug.
    assert b["deliver_eps"] == lifecycle.DEFAULT_BACKFILL_CAP_EPS
    assert b["events"] == 600 * 100            # window x density, unchanged
    assert b["seconds"] == 60_000 / lifecycle.DEFAULT_BACKFILL_CAP_EPS
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
    assert snap["rate_value"] == 5000.0        # metrics has no eps -> fills at the cap
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
    # The run delivers at the cap, not at the spec's eps: the spec's eps says
    # how dense the history is, not how fast to send it. Was 100.0.
    assert snap["rate_value"] == lifecycle.DEFAULT_BACKFILL_CAP_EPS
    assert snap["backfill"]["start_s"] < snap["backfill"]["end_s"]
    assert snap["duration_s"] and snap["duration_s"] > 0


def test_plan_backfill_separates_density_from_delivery():
    """The spec's eps sets how dense the history is, the cap sets how fast.

    This test previously asserted ``seconds == window``, which encoded the bug:
    the two rates were the same number, so ``events / eps`` cancelled to the
    window and a backfill always took exactly as long as the period it covered.
    The assertions are changed deliberately.
    """
    now = 1_000_000.0
    # eventgen, eps below the cap: 10 eps of density over an hour is 36,000
    # events, pushed out at the 5000 eps cap in 7.2 s.
    p = lifecycle.plan_backfill("eventgen", 0, 10.0, 3600, None, None, now)
    assert p["density_eps"] == 10.0
    assert p["deliver_eps"] == lifecycle.DEFAULT_BACKFILL_CAP_EPS
    assert p["events"] == 3600 * 10                     # window x density
    assert p["seconds"] == 36000 / lifecycle.DEFAULT_BACKFILL_CAP_EPS

    # A density above the cap is legitimate (it describes the data, not the
    # wire), so it is NOT clamped; the delivery rate still is.
    p = lifecycle.plan_backfill("eventgen", 0, 50_000.0, 3600, None, None, now)
    assert p["density_eps"] == 50_000.0
    assert p["deliver_eps"] == lifecycle.DEFAULT_BACKFILL_CAP_EPS
    assert p["events"] == 3600 * 50_000

    # no eps (metrics / count_interval / per_day_gb) -> density falls back to
    # the cap, which is the previous behaviour for those modes.
    p = lifecycle.plan_backfill("metrics", 3, None, 3600, 60, None, now)
    assert p["deliver_eps"] == lifecycle.DEFAULT_BACKFILL_CAP_EPS
    assert p["events"] == 60 * 3               # 60 ticks x 3 series


def test_a_backfill_no_longer_takes_as_long_as_the_window():
    """The regression that made backfill look broken.

    With density and delivery conflated the delivery time was
    ``(window x eps) / eps == window`` for every input, so a four-year backfill
    took four years and no setting could change it. The giveaway is that raising
    the rate did not help: it raised the event count in lockstep.
    """
    year = 365.25 * 86400
    four_years = 4 * year
    p = lifecycle.plan_backfill("eventgen", 0, 40.0, four_years, None, None, 0.0)

    assert p["events"] == int(four_years * 40)          # genuinely 5.05 billion
    # The old behaviour: seconds == window. The fix must be far below it.
    assert p["seconds"] < four_years / 100
    assert p["seconds"] == pytest.approx(12 * 86400, rel=0.1)   # ~12 days

    # Raising the density raises the volume and so the time, which is the
    # honest relationship; it used to make no difference at all.
    denser = lifecycle.plan_backfill("eventgen", 0, 400.0, four_years, None, None, 0.0)
    assert denser["events"] == 10 * p["events"]
    assert denser["seconds"] == pytest.approx(10 * p["seconds"], rel=1e-6)


def test_the_cap_is_the_lever_for_how_fast_a_backfill_lands():
    """Lowering the cap must slow delivery without changing the data.

    The cap exists to protect the target; it must not quietly change how much
    history you get.
    """
    window = 30 * 86400
    fast = lifecycle.plan_backfill("eventgen", 0, 40.0, window, None, None, 0.0)
    slow = lifecycle.plan_backfill("eventgen", 0, 40.0, window, None, 500.0, 0.0)

    assert fast["events"] == slow["events"]             # same history either way
    assert slow["deliver_eps"] == 500.0
    assert slow["seconds"] == pytest.approx(fast["seconds"] * 10, rel=1e-6)


def test_the_duration_backstop_covers_the_delivery():
    """An eventgen backfill stops on the deadline, so it must outlast the work."""
    p = lifecycle.plan_backfill("eventgen", 0, 40.0, 30 * 86400, None, None, 0.0)
    assert p["duration_s"] > p["seconds"]
    assert p["duration_s"] >= p["seconds"] * 1.5


def test_backfill_survives_the_claim_response_model(client, db_session, settings, make_pack, fake_driver):
    """The claim endpoint's response_model MUST carry the backfill window.

    build_slice adds ``backfill`` to the slice, but FastAPI filters the claim
    response to :class:`SpecSliceOut`; if that schema omits ``backfill`` the
    field is silently dropped and no worker ever backfills (the field being in
    build_slice is necessary but NOT sufficient - it must survive serialisation).
    This drives the REAL endpoint, not build_slice directly, so it guards the
    response_model.
    """
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=50.0, workers=1,
                              fleet="fake-local")
    db_session.commit()
    launched = client.post("/api/specs/%d/run" % spec.id, json={"backfill_window_s": 600})
    assert launched.status_code in (200, 201), launched.text
    run = db_session.get(Run, launched.json()["run_id"])
    resp = client.post("/api/agent/runs/%d/claim" % run.id,
                       json={"holder": "w0", "hint_slot": 0},
                       headers=_helpers.auth_header(run, settings))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("backfill"), "claim response_model stripped the backfill window"
    assert body["backfill"]["start_s"] < body["backfill"]["end_s"]
    # The density has to survive the same allowlist. Without it the worker falls
    # back to generating at the DELIVERY rate, which is the bug that made a
    # backfill take exactly as long as the window it covered.
    assert body["backfill"]["density_eps"] == 50.0, \
        "claim response_model stripped the backfill density"
    # And it is genuinely a different number from the delivery share.
    assert body["share"]["eps"] != body["backfill"]["density_eps"]


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


def test_every_build_slice_key_is_declared_on_the_claim_response_model(
        db_session, settings, make_pack, fake_driver):
    """Generic guard: a FastAPI ``response_model`` is an ALLOWLIST.

    ``build_slice`` hands the claim route a plain dict; any key not declared on
    :class:`SpecSliceOut` is silently dropped from the wire, and the worker then
    behaves as if the control plane never sent it. That is exactly how the
    ``backfill`` window was lost (runs looked fine and delivered a live grid).
    Rather than remember to add a field to two places, assert the invariant: the
    keys build_slice emits must ALL be declared on the response model. Adding a
    slice field without declaring it fails here instead of silently in prod.
    """
    from server.schemas import SpecSliceOut

    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=2, rate_mode="eps", rate_value=100.0)
    run = ctx["run"]
    lease = _helpers.leases_by_slot(db_session, run)[0]
    slice_doc = lifecycle.build_slice(run, lease, settings=settings)

    declared = set(SpecSliceOut.model_fields)
    undeclared = set(slice_doc) - declared
    assert not undeclared, (
        "build_slice emits %s, which SpecSliceOut does not declare; the claim "
        "response_model will silently DROP these keys from every claim. Add "
        "them to SpecSliceOut." % sorted(undeclared))


def test_the_delivery_cap_can_be_raised_for_a_fleet_that_can_take_it():
    """The cap used to clamp DOWN only, so it was a hard 5000 eps ceiling.

    That made a four-year backfill take 11.7 days no matter how many workers
    were available, while a live run on the same fleet could deliver eight times
    faster. It is a default now; the per-worker submit ceiling is the real
    protection, and the launch route checks the backfill's own rate against it.
    """
    window = 4 * 365.25 * 86400
    default = lifecycle.plan_backfill("eventgen", 0, 40.0, window, None, None, 0.0)
    raised = lifecycle.plan_backfill("eventgen", 0, 40.0, window, None, 40_000.0, 0.0)

    assert default["deliver_eps"] == lifecycle.DEFAULT_BACKFILL_CAP_EPS
    assert raised["deliver_eps"] == 40_000.0
    assert raised["events"] == default["events"]       # same history
    assert raised["seconds"] == pytest.approx(default["seconds"] / 8, rel=1e-6)


def test_a_backfill_that_would_outrun_the_fleet_is_refused(
        client, db_session, settings, make_pack, fake_driver):
    """The guard that makes raising the cap safe.

    The launch-time ceiling check validates ``spec.rate_value``, which a
    backfill does not deliver at, so without a check of its own the cap could be
    raised to anything and flatten the target.
    """
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=40.0, workers=1,
                              fleet="fake-local")
    db_session.commit()

    # eventgen's per-worker ceiling is 10,000 eps; 200,000 on one worker is well
    # past it, and the spec's own 40 eps would never have caught it.
    r = client.post("/api/specs/%d/run" % spec.id,
                    json={"backfill_window_s": 3600, "backfill_cap_eps": 200_000})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "backfill_exceeds_ceiling"
    assert detail["deliver_eps"] == 200_000

    # ...and one within the ceiling is accepted.
    r = client.post("/api/specs/%d/run" % spec.id,
                    json={"backfill_window_s": 3600, "backfill_cap_eps": 1000})
    assert r.status_code in (200, 201), r.text


def test_the_delivery_cap_has_a_deployment_wide_default():
    """STOKER_BACKFILL_CAP_EPS, so an estate sets it once.

    The right delivery rate is a property of the estate (how much the target
    will take), not of one job, so requiring it on every launch was the wrong
    shape. Precedence: this launch > the deployment > the built-in default.
    """
    import dataclasses

    from server import config as config_mod

    base = config_mod.get_settings()
    estate = dataclasses.replace(base, backfill_cap_eps=25_000.0)
    window = 30 * 86400

    built_in = lifecycle.plan_backfill("eventgen", 0, 40.0, window, None, None, 0.0)
    assert built_in["deliver_eps"] == lifecycle.DEFAULT_BACKFILL_CAP_EPS

    deployment = lifecycle.plan_backfill("eventgen", 0, 40.0, window, None, None, 0.0,
                                         settings=estate)
    assert deployment["deliver_eps"] == 25_000.0

    # A launch still wins over the deployment default, in both directions.
    for asked in (8_000.0, 60_000.0):
        launched = lifecycle.plan_backfill("eventgen", 0, 40.0, window, None, asked,
                                           0.0, settings=estate)
        assert launched["deliver_eps"] == asked

    # The history itself never changes with the delivery rate.
    assert built_in["events"] == deployment["events"]


# --------------------------------------------------------------------------- #
# An explicit historical range
# --------------------------------------------------------------------------- #

def test_a_backfill_can_name_an_explicit_range():
    """"January to April", not only "the last 90 days".

    The window used to be a duration anchored to now, so any range you asked
    for still ended at the present.
    """
    import datetime as dt

    jan = dt.datetime(2026, 1, 1).timestamp()
    apr = dt.datetime(2026, 4, 1).timestamp()
    now = dt.datetime(2026, 10, 7).timestamp()

    p = lifecycle.plan_backfill("eventgen", 0, 40.0, None, None, None, now,
                                start_s=jan, end_s=apr)
    assert p["start_s"] == jan and p["end_s"] == apr
    assert p["window_s"] == pytest.approx(apr - jan)
    assert p["events"] == int((apr - jan) * 40)
    # Crucially it does NOT end at now.
    assert p["end_s"] < now


def test_the_last_n_form_still_ends_now():
    now = 1_000_000.0
    p = lifecycle.plan_backfill("eventgen", 0, 40.0, 3600, None, None, now)
    assert p["end_s"] == now
    assert p["start_s"] == now - 3600
    assert p["window_s"] == 3600


def test_an_explicit_range_wins_over_a_window():
    """The range is the more specific statement, so it takes precedence."""
    now = 1_000_000.0
    p = lifecycle.plan_backfill("eventgen", 0, 40.0, 99999, None, None, now,
                                start_s=now - 7200, end_s=now - 3600)
    assert p["window_s"] == 3600
    assert p["end_s"] == now - 3600


@pytest.mark.parametrize("start,end", [(100.0, 100.0), (200.0, 100.0)])
def test_a_backwards_range_is_refused(start, end):
    with pytest.raises(ValueError) as exc:
        lifecycle.plan_backfill("eventgen", 0, 40.0, None, None, None, 1000.0,
                                start_s=start, end_s=end)
    assert "after start" in str(exc.value)


def test_no_window_and_no_range_is_refused():
    with pytest.raises(ValueError) as exc:
        lifecycle.plan_backfill("eventgen", 0, 40.0, None, None, None, 1000.0)
    assert "needs a window" in str(exc.value)
