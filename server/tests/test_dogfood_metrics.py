"""Dogfood telemetry aggregate: :func:`metrics_lifecycle._aggregate_run_metrics`.

The per-run ``stoker:metrics`` body the control plane ships to a HEC target,
which the shipped observability dashboard (dashboards/stoker-observability)
queries. These tests pin the field set the dashboard's SPL depends on.
"""

from __future__ import annotations

import pytest

from server import metrics_lifecycle
from server.models import MetricSample

from . import _helpers


def _live_run_with_sample(db, pack_dir, settings, rate_mode="eps",
                          rate_value=1000.0, **counters):
    """A provisioning run with one live (ready) lease carrying one sample."""
    ctx = _helpers.full_run(db, pack_dir, settings, workers=1,
                            rate_mode=rate_mode, rate_value=rate_value)
    run = ctx["run"]
    lease = _helpers.leases_by_slot(db, run)[0]
    lease.state = "ready"  # a live lease state
    db.add(MetricSample(run_id=run.id, slot=0, **counters))
    db.commit()
    return run


def test_aggregate_reports_target_eps_for_an_eps_run(
        db_session, make_pack, settings):
    """An eps run's aggregate carries target_eps so the dashboard can plot
    delivered-vs-target."""
    pack_dir = make_pack()
    run = _live_run_with_sample(
        db_session, pack_dir, settings, rate_mode="eps", rate_value=1000.0,
        eps=940.0, bps=250000.0, events_total=9400, bytes_total=2500000,
        hec_2xx=94, hec_4xx=0, hec_5xx=0, hec_timeouts=0, retries=0, lag_s=0.2)

    agg = metrics_lifecycle._aggregate_run_metrics(db_session, run)

    assert agg is not None
    assert agg["target_eps"] == 1000.0
    assert agg["eps"] == 940.0
    assert agg["lag_s_max"] == 0.2
    assert agg["hec_2xx"] == 94
    assert agg["live_workers"] == 1
    assert agg["reporting_workers"] == 1


def test_aggregate_omits_target_eps_for_non_eps_run(
        db_session, make_pack, settings):
    """per_day_gb / count_interval do not pace to an eps figure, so target_eps
    is absent rather than misleading."""
    pack_dir = make_pack()
    run = _live_run_with_sample(
        db_session, pack_dir, settings, rate_mode="per_day_gb", rate_value=50.0,
        eps=500.0, bps=120000.0, events_total=5000, bytes_total=1200000,
        hec_2xx=50, hec_4xx=0, hec_5xx=0, hec_timeouts=0, retries=0, lag_s=0.1)

    agg = metrics_lifecycle._aggregate_run_metrics(db_session, run)

    assert agg is not None
    assert "target_eps" not in agg
    assert agg["eps"] == 500.0


def test_aggregate_none_without_live_lease_samples(
        db_session, make_pack, settings):
    """No live lease with a sample -> no aggregate (nothing to report)."""
    ctx = _helpers.full_run(db_session, make_pack(), settings, workers=1,
                            rate_mode="eps", rate_value=1000.0)
    # Leases are seeded 'free' with no samples.
    assert metrics_lifecycle._aggregate_run_metrics(db_session, ctx["run"]) is None


# --------------------------------------------------------------------------- #
# stoker:worker: one event per lease, so a short fleet shows which slot is short.
# --------------------------------------------------------------------------- #

@pytest.fixture
def dogfood_settings(settings):
    object.__setattr__(settings, "dogfood_hec_url", "https://hec.invalid:8088")
    object.__setattr__(settings, "dogfood_hec_token", "not-a-real-token")
    return settings


def _two_slot_run(db, pack_dir, settings):
    """Slot 0 running with an engine at its core allowance; slot 1 unclaimed."""
    ctx = _helpers.full_run(db, pack_dir, settings, workers=2,
                            rate_mode="eps", rate_value=2000.0)
    run = ctx["run"]
    leases = _helpers.leases_by_slot(db, run)
    leases[0].state = "running"
    leases[0].holder = "worker-a"
    leases[0].share_json = {"eps": 1000.0, "_engine_impl": "firebox",
                            "_assigned_work": 3}
    leases[1].share_json = {"eps": 1000.0}
    db.add(MetricSample(run_id=run.id, slot=0, eps=600.0, bps=480000.0,
                        events_total=6000, bytes_total=4800000, lag_s=4.0,
                        queue_depth=0, hec_2xx=60, cpu_pct=3.0,
                        engine_cpu_pct=198.0, engine_threads=2,
                        cpu_limit_cores=2.0))
    db.commit()
    return run


def test_worker_bodies_cover_every_lease_and_say_why_a_slot_is_short(
        db_session, make_pack, settings):
    run = _two_slot_run(db_session, make_pack(), settings)
    rows = metrics_lifecycle._lease_samples(db_session, run)

    bodies = {b["slot"]: b for b in metrics_lifecycle._worker_bodies(run, rows)}

    assert set(bodies) == {0, 1}
    busy = bodies[0]
    assert busy["type"] == "worker_metrics"
    assert busy["lease_state"] == "running"
    assert busy["holder"] == "worker-a"
    assert busy["engine_impl"] == "firebox"
    assert busy["assigned_work"] == 3
    assert busy["share_eps"] == 1000.0
    assert busy["eps"] == 600.0
    assert busy["eps_of_share_pct"] == 60.0
    assert busy["engine_cpu_pct"] == 198.0
    assert busy["engine_threads"] == 2
    assert busy["engine_cpu_of_limit_pct"] == 99.0
    assert "sample_age_s" in busy
    assert not any(k.startswith("_") for k in busy)

    unheld = bodies[1]
    assert unheld["lease_state"] == "free"
    assert unheld["share_eps"] == 1000.0
    assert "eps" not in unheld and "sample_age_s" not in unheld


def test_aggregate_counts_unheld_slots_and_the_busiest_engine(
        db_session, make_pack, settings):
    run = _two_slot_run(db_session, make_pack(), settings)

    agg = metrics_lifecycle._aggregate_run_metrics(db_session, run)

    assert agg["unheld_slots"] == 1
    assert agg["engine_cpu_pct_max"] == 198.0
    assert agg["live_workers"] == 1


def test_the_metrics_pass_ships_worker_events_beside_the_aggregate(
        db_session, make_pack, dogfood_settings, monkeypatch):
    run = _two_slot_run(db_session, make_pack(), dogfood_settings)
    sent = []
    monkeypatch.setattr(metrics_lifecycle, "emit_hec_events",
                        lambda events, settings=None: sent.extend(events) or True)

    assert metrics_lifecycle.emit_active_run_metrics(db_session, dogfood_settings) == 1

    kinds = [e["sourcetype"] for e in sent]
    assert kinds.count("stoker:metrics") == 1
    assert kinds.count("stoker:worker") == 2
    assert {e["event"]["run_id"] for e in sent} == {run.id}

    sent.clear()
    assert metrics_lifecycle.emit_run_metrics(db_session, run, dogfood_settings)
    assert [e["sourcetype"] for e in sent].count("stoker:worker") == 2
