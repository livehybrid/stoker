"""End-to-end: launch a real (small) run and assert what landed in Splunk.

Two flows, both kept deliberately tiny (a few hundred events at most) so storage
and search time stay negligible:

* metrics backfill -> a metrics-type index (deterministic point count),
* a low-eps eventgen run -> an events index.

Each run tags its data with a unique ``source`` override so the Splunk count is
isolated from anything else in the index. The run's own delivered total (from the
control plane) is always asserted; the Splunk count is asserted additionally when
Splunk is configured (``SPLUNK_URL`` + creds), which is the real "did it land"
check the user asked for.

Requires a HEC target (``STOKER_TEST_HEC_URL`` / ``_HEC_TOKEN``) and the relevant
index env var; skips cleanly otherwise.
"""

from __future__ import annotations

import math

import pytest


def _delivered(run):
    # type: (dict) -> int
    totals = run.get("totals_json") or {}
    return int(totals.get("events_total") or 0)


def test_metrics_backfill_lands_in_splunk(api, cfg, make_target, make_spec, metric_pack,
                                          splunk, unique):
    """A metrics backfill sweeps a fixed grid: ticks x series measurements, at
    historical timestamps. Deterministic count (+/- one grid-alignment tick)."""
    if not cfg.metric_index:
        pytest.skip("set STOKER_TEST_METRIC_INDEX (a metrics-type index) for the metrics e2e")

    target = make_target(cfg.metric_index)
    spec = make_spec(metric_pack["id"], target["id"], engine="metrics",
                     rate_mode="count_interval",
                     interval_s=int(metric_pack["config"]["resolution_s"]),
                     overrides={"source": unique})

    run = api.wait_for_run(
        api.launch_run(spec["id"], {"backfill_window_s": cfg.backfill_window_s,
                                    "backfill_resolution_s": cfg.backfill_res_s})["run_id"],
        timeout_s=cfg.poll_timeout_s)
    assert run["state"] == "completed", "run ended %s (%s)" % (run["state"], run.get("end_reason"))

    series = metric_pack["series_count"]
    ticks = math.ceil(cfg.backfill_window_s / cfg.backfill_res_s)
    # Grid alignment can add one leading tick; allow +/- one tick's worth.
    lo, hi = (ticks - 1) * series, (ticks + 1) * series
    delivered = _delivered(run)
    assert lo <= delivered <= hi, (
        "delivered %d not in [%d, %d] (ticks=%d series=%d)" % (delivered, lo, hi, ticks, series))

    if splunk is None:
        pytest.skip("Splunk not configured - asserted the run's delivered total only")
    count = splunk.count(
        'index=%s source="%s"' % (cfg.metric_index, unique),
        earliest="-%ds" % (cfg.backfill_window_s + 900),
        metric="harness.req.count", poll_until=delivered)
    assert count >= delivered, "Splunk metric count %d < delivered %d" % (count, delivered)


def test_eventgen_small_run_lands_in_splunk(api, cfg, make_target, make_spec, splunk, unique):
    """A short low-eps eventgen run delivers ~eps x duration events to an events
    index; assert the run completed and (with Splunk) that the events are there."""
    if not cfg.event_index:
        pytest.skip("set STOKER_TEST_INDEX (an events index) for the eventgen e2e")
    pack = next((p for p in api.ok(api.get("/api/packs")) if p["name"] == cfg.eventgen_pack), None)
    if pack is None:
        pytest.skip("eventgen pack %r not present (sync a sample-packs repo)" % cfg.eventgen_pack)

    target = make_target(cfg.event_index)
    spec = make_spec(pack["id"], target["id"], engine="eventgen",
                     rate_mode="eps", rate_value=cfg.eps, duration_s=cfg.duration_s,
                     overrides={"source": unique})

    run = api.wait_for_run(api.launch_run(spec["id"])["run_id"], timeout_s=cfg.poll_timeout_s)
    assert run["state"] == "completed", "run ended %s (%s)" % (run["state"], run.get("end_reason"))

    delivered = _delivered(run)
    nominal = cfg.eps * cfg.duration_s
    # Ramp-up + drain make the exact total fuzzy; assert the right order of magnitude.
    assert delivered > 0, "run completed but delivered 0 events"
    assert delivered <= nominal * 2.5, "delivered %d far above nominal %d" % (delivered, nominal)

    if splunk is None:
        pytest.skip("Splunk not configured - asserted the run's delivered total only")
    count = splunk.count(
        'index=%s source="%s"' % (cfg.event_index, unique),
        earliest="-%ds" % (cfg.duration_s + 900), poll_until=delivered)
    # Splunk should hold essentially everything the run reported delivering.
    assert count >= max(1, int(delivered * 0.9)), (
        "Splunk event count %d < ~delivered %d" % (count, delivered))
