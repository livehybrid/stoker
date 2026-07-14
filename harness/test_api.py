"""API-contract tests: auth, resource CRUD, and the backfill estimate.

These need only ``STOKER_URL`` + ``STOKER_TOKEN`` (the estimate/target tests also
need a HEC target, so they skip without one). They launch nothing, so they are
cheap and safe to run against a live control plane.
"""

from __future__ import annotations

import math

from clients import StokerClient


def test_auth_is_required(cfg):
    """A request with no bearer token is rejected (401), proving the guard is on."""
    anon = StokerClient(cfg.base, token="", verify_tls=cfg.verify_tls)
    resp = anon.get("/api/targets")
    assert resp.status_code == 401, "expected 401 without a token, got %d" % resp.status_code


def test_target_crud(api, make_target, cfg):
    """Create a target, read it back in the list + by id, then teardown deletes it."""
    target = make_target(cfg.event_index or "main")
    assert target["id"] and target["default_index"]
    listed = api.ok(api.get("/api/targets"))
    assert any(t["id"] == target["id"] for t in listed)
    fetched = api.ok(api.get("/api/targets/%d" % target["id"]))
    assert fetched["name"] == target["name"]
    # The token must never be echoed back (write-only secret).
    assert "token" not in fetched


def test_metric_pack_has_expected_series(metric_pack):
    """The builder pack cross-products its dimensions: 1 dim x 2 values = 2 series."""
    assert metric_pack["series_count"] == 2
    assert metric_pack["config"]["resolution_s"] == 10


def test_backfill_estimate_metrics_fills_at_cap(api, make_target, make_spec, metric_pack, cfg):
    """A metrics backfill has no eps knob, so it fills at the 5000 ceiling."""
    target = make_target(cfg.metric_index or "metrics")
    spec = make_spec(metric_pack["id"], target["id"], engine="metrics",
                     rate_mode="count_interval",
                     interval_s=int(metric_pack["config"]["resolution_s"]))
    est = api.ok(api.post("/api/specs/%d/backfill_estimate" % spec["id"],
                          json={"window_s": 600, "resolution_s": 60}))
    assert est["deliver_eps"] == 5000.0            # no eps -> the cap
    assert est["events"] == math.ceil(600 / 60) * metric_pack["series_count"]


def test_backfill_estimate_eventgen_honours_eps(api, make_target, make_spec, cfg):
    """Regression for the rate fix: eventgen backfill delivers at the spec's eps
    (clamped to the cap), NOT forced to the cap; seconds = events / that rate."""
    packs = api.ok(api.get("/api/packs"))
    pack = next((p for p in packs if p["name"] == cfg.eventgen_pack), None)
    if pack is None:
        import pytest
        pytest.skip("eventgen pack %r not present (sync a sample-packs repo)" % cfg.eventgen_pack)
    target = make_target(cfg.event_index or "main")
    spec = make_spec(pack["id"], target["id"], engine="eventgen",
                     rate_mode="eps", rate_value=7.0)
    est = api.ok(api.post("/api/specs/%d/backfill_estimate" % spec["id"],
                          json={"window_s": 600}))
    assert est["deliver_eps"] == 7.0               # honoured, not 5000
    assert est["events"] == 600 * 7                # window x deliver_eps
    assert est["seconds"] == 600.0                 # events / deliver_eps (not / cap)
