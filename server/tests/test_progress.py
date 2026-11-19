"""Run progress: events done against planned, and a finish from the actual rate."""
from __future__ import annotations

import datetime

import pytest

from server import lifecycle, progress
from server.models import MetricSample, Run

from . import _helpers

UTC = datetime.timezone.utc
T0 = 1_800_000_000.0  # an arbitrary launch instant, epoch seconds


def _at(s):
    return datetime.datetime.fromtimestamp(s, tz=UTC)


def _backfill_run(client, db_session, settings, make_pack, body, rate_value=100.0, workers=2):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen", rate_mode="eps",
                              rate_value=rate_value, workers=workers, fleet="fake-local")
    db_session.commit()
    resp = client.post("/api/specs/%d/run" % spec.id, json=body)
    assert resp.status_code in (200, 201), resp.text
    run = db_session.get(Run, resp.json()["run_id"])
    run.state = lifecycle.STATE_RUNNING
    run.t0 = _at(T0)
    db_session.commit()
    return run


def _beat(db, run, slot, t, events):
    db.add(MetricSample(run_id=run.id, slot=slot, ts=_at(t), events_total=events))


def test_a_backfill_run_records_its_plan_and_request(client, db_session, settings, make_pack,
                                                     fake_driver):
    run = _backfill_run(client, db_session, settings, make_pack,
                        {"backfill_window_s": 3600, "backfill_cap_eps": 200})
    plan = run.spec_snapshot_json["backfill_plan"]
    assert plan["events"] == 3600 * 100  # the window at the spec's density
    assert plan["request"] == {"window_s": 3600, "start_s": None, "end_s": None,
                               "cap_eps": 200, "resolution_s": None}
    # The plan is the control plane's: the worker's slice does not carry it.
    assert "backfill_plan" not in run.spec_snapshot_json["backfill"]


def test_a_range_request_is_recorded_as_a_range(client, db_session, settings, make_pack,
                                                fake_driver):
    start, end = 1_782_864_000, 1_782_864_000 + 86400
    run = _backfill_run(client, db_session, settings, make_pack,
                        {"backfill_start_s": start, "backfill_end_s": end})
    req = run.spec_snapshot_json["backfill_plan"]["request"]
    assert (req["window_s"], req["start_s"], req["end_s"]) == (None, start, end)


def test_the_finish_comes_from_the_actual_rate_not_the_target(client, db_session, settings,
                                                              make_pack, fake_driver):
    # Asked for 200 eps across two workers; each holds 90.
    run = _backfill_run(client, db_session, settings, make_pack,
                        {"backfill_window_s": 3600, "backfill_cap_eps": 200})
    for i in range(121):  # 600 s of heartbeats every 5 s
        t = T0 + i * 5
        _beat(db_session, run, 0, t, 90 * i * 5)
        _beat(db_session, run, 1, t, 90 * i * 5)
    db_session.commit()
    now = T0 + 600

    out = progress.run_progress(db_session, run, now=now)
    assert out["kind"] == "backfill"
    assert out["planned_events"] == 360_000
    assert out["done_events"] == 108_000
    assert out["remaining_events"] == 252_000
    assert out["fraction"] == pytest.approx(0.3)
    assert out["recent_eps"] == pytest.approx(180.0)
    assert out["eta_s"] == pytest.approx(1400.0)  # 252,000 / 180
    assert out["expected_end"] == _at(now + 1400)
    assert out["target_end"] == _at(now + 1260)  # what the cap alone would say
    assert out["series"][-1] == {"ts": int(now * 1000), "done": 108_000}
    assert len(out["series"]) <= progress.DEFAULT_POINTS + 1


def test_a_restarted_slot_does_not_undo_its_events(client, db_session, settings, make_pack,
                                                   fake_driver):
    run = _backfill_run(client, db_session, settings, make_pack, {"backfill_window_s": 3600},
                        workers=1)
    _beat(db_session, run, 0, T0, 0)
    _beat(db_session, run, 0, T0 + 5, 500)
    _beat(db_session, run, 0, T0 + 10, 100)  # reclaimed: counts from zero again
    db_session.commit()
    out = progress.run_progress(db_session, run, now=T0 + 10)
    assert out["done_events"] == 600
    assert [p["done"] for p in out["series"]] == [0, 500, 600]


def test_no_finish_is_projected_while_paused_or_silent(client, db_session, settings, make_pack,
                                                       fake_driver):
    run = _backfill_run(client, db_session, settings, make_pack, {"backfill_window_s": 3600})
    for i in range(10):
        _beat(db_session, run, 0, T0 + i * 5, 100 * i * 5)
    db_session.commit()

    silent = progress.run_progress(db_session, run, now=T0 + 45 + progress.STALE_AFTER_S + 1)
    assert silent["stale"] is True
    assert silent["eta_s"] is None and silent["recent_eps"] is None

    run.state = lifecycle.STATE_PAUSED
    db_session.commit()
    paused = progress.run_progress(db_session, run, now=T0 + 50)
    assert paused["eta_s"] is None and paused["target_end"] is None
    assert paused["done_events"] == 4500


def test_a_completed_backfill_reads_as_done(client, db_session, settings, make_pack, fake_driver):
    run = _backfill_run(client, db_session, settings, make_pack, {"backfill_window_s": 60})
    _beat(db_session, run, 0, T0, 0)
    _beat(db_session, run, 0, T0 + 5, 5_990)  # density rounding leaves it a touch short
    run.state = lifecycle.STATE_COMPLETED
    db_session.commit()
    out = progress.run_progress(db_session, run, now=T0 + 3600)
    assert out["fraction"] == 1.0
    assert out["eta_s"] is None and out["recent_eps"] is None


def test_an_older_backfill_is_planned_from_its_window_and_density():
    snap = {"engine": "eventgen", "rate_value": 5000.0,
            "backfill": {"start_s": 0, "end_s": 100, "density_eps": 1666.0}}
    assert progress.planned_events(snap) == 166_600
    assert progress.planned_events({"engine": "metrics", "backfill": snap["backfill"]}) is None


def test_a_live_run_with_a_duration_ends_on_the_clock(client, db_session, settings, make_pack,
                                                      fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, duration_s=3600, workers=1)
    db_session.commit()
    run = db_session.get(Run, client.post("/api/specs/%d/run" % spec.id, json={}).json()["run_id"])
    run.state = lifecycle.STATE_RUNNING
    run.t0 = _at(T0)
    run.paused_s = 120.0
    db_session.commit()
    out = progress.run_progress(db_session, run, now=T0 + 1920)
    assert out["kind"] == "duration"
    assert out["fraction"] == pytest.approx(0.5)  # 1800 s run of 3600, 120 s paused
    assert out["expected_end"] == _at(T0 + 3600 + 120)


def test_the_progress_route(client, db_session, settings, make_pack, fake_driver):
    run = _backfill_run(client, db_session, settings, make_pack, {"backfill_window_s": 3600})
    body = client.get("/api/runs/%d/progress" % run.id).json()
    assert body["kind"] == "backfill" and body["planned_events"] == 360_000
    assert body["done_events"] == 0 and body["series"] == []
    assert client.get("/api/runs/999999/progress").status_code == 404
