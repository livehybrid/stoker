"""Dogfood telemetry aggregate: :func:`metrics_lifecycle._aggregate_run_metrics`.

The per-run ``stoker:metrics`` body the control plane ships to a HEC target,
which the shipped observability dashboard (dashboards/stoker-observability)
queries. These tests pin the field set the dashboard's SPL depends on.
"""

from __future__ import annotations

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
