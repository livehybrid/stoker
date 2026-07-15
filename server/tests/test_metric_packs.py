"""Metric-pack lint/build + the metric-pack authoring & preview API."""
from __future__ import annotations

import filecmp
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import select

from server import bundles
from server.models import Bundle

from . import _helpers

_REPO = Path(__file__).resolve().parents[2]


def _valid_config(**over):
    cfg = {
        "resolution_s": 10,
        "seed": 7,
        "dimensions": [{"key": "product", "values": ["checkout", "search"]}],
        "metrics": [
            {"name": "store.requests", "kind": "count", "unit": "requests",
             "min": 5, "p95": 600, "max": 1200, "noise": 0.15,
             "pattern": {"type": "business_double_hump"},
             "scale": {"product": {"checkout": 1.0, "search": 2.0}}},
            {"name": "host.cpu.usage", "kind": "gauge", "unit": "percent",
             "min": 3, "p95": 70, "max": 98, "noise": 0.2,
             "pattern": {"type": "sine", "peak_h": 14}},
        ],
    }
    cfg.update(over)
    return cfg


# ---- directory metric packs (metricgen in stoker.json) ----

def _write_dir_metric_pack(root, config):
    import json
    import os
    pack_dir = os.path.join(str(root), "dirmetric")
    os.makedirs(os.path.join(pack_dir, "default"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write("[stub]\nmode = sample\n")
    with open(os.path.join(pack_dir, "stoker.json"), "w") as fh:
        json.dump({"name": "dirmetric", "engine": "metrics", "metricgen": config}, fh)
    with open(os.path.join(pack_dir, "pack.yaml"), "w") as fh:
        fh.write("name: dirmetric\nengine: metrics\n")
    return pack_dir


def test_register_directory_metric_pack_is_first_class(client, tmp_path):
    """A directory metric pack registered via POST /api/packs is stored with its
    metricgen as builder_config_json, so it behaves like a UI-authored one: the
    metric-pack detail endpoint (which reads builder_config_json) resolves it."""
    pack_dir = _write_dir_metric_pack(tmp_path, _valid_config())
    reg = client.post("/api/packs", json={"name": "dirmetric", "source_path": pack_dir})
    assert reg.status_code == 201, reg.text
    pid = reg.json()["id"]
    assert reg.json()["engines_json"] == ["metrics"]
    detail = client.get("/api/metric-packs/%d" % pid)
    assert detail.status_code == 200, detail.text
    assert detail.json()["series_count"] == 2  # one dim, two values


def test_shipped_web_store_metrics_pack_is_valid():
    """Guard the bundled sample metric pack: it lints as a metrics pack, its
    metricgen validates, and a bundle builds from it."""
    pack_dir = str(_REPO / "packs" / "web-store-metrics")
    lint = bundles.lint_pack(pack_dir)
    assert lint.ok, lint.errors
    assert lint.engines == ["metrics"]
    assert lint.metricgen is not None
    assert bundles.metrics_series_count(lint.metricgen) == 8  # 4 services x 2 regions
    assert bundles.lint_metrics_config(lint.metricgen) == []


# ---- vendored pattern module drift guard ----

def test_metricpatterns_is_byte_identical_to_worker():
    server_copy = _REPO / "server" / "metricpatterns.py"
    worker_copy = _REPO / "worker" / "engines" / "metrics" / "stoker_metrics" / "patterns.py"
    assert filecmp.cmp(str(server_copy), str(worker_copy), shallow=False), (
        "server/metricpatterns.py has drifted from the worker's patterns.py; "
        "re-copy so the preview and the worker compute the same curve")


# ---- lint_metrics_config ----

def test_lint_accepts_a_valid_config():
    assert bundles.lint_metrics_config(_valid_config()) == []


def test_lint_reports_specific_problems():
    bad = {
        "resolution_s": 0,
        "dimensions": [{"key": "", "values": []}],
        "metrics": [
            {"name": "x", "kind": "bogus", "min": 9, "p95": 5, "max": 1,
             "pattern": {"type": "nope"}, "scale": {"region": {}}},
            {"name": "x"},  # duplicate name
        ],
    }
    errors = " || ".join(bundles.lint_metrics_config(bad))
    assert "resolution_s must be > 0" in errors
    assert "non-empty string key" in errors
    assert "non-empty values list" in errors
    assert "kind must be one of" in errors
    assert "min <= p95 <= max" in errors
    assert "unknown pattern type" in errors
    assert "unknown dimension" in errors
    assert "duplicate metric name" in errors


def test_lint_rejects_oversized_matrix():
    cfg = _valid_config(dimensions=[
        {"key": "a", "values": [str(i) for i in range(100)]},
        {"key": "b", "values": [str(i) for i in range(100)]},
    ])
    errors = bundles.lint_metrics_config(cfg)
    assert any("cross-product" in e for e in errors)


def test_lint_rejects_empty_metrics():
    assert any("non-empty list" in e
               for e in bundles.lint_metrics_config({"metrics": []}))


# ---- build_from_metrics_config ----

def test_build_produces_a_metricgen_bundle(tmp_path):
    built = bundles.build_from_metrics_config("kpi", _valid_config(), bundle_dir=str(tmp_path))
    assert built.reused is False
    with tarfile.open(built.path) as tf:
        names = tf.getnames()
        assert any(n.endswith("default/eventgen.conf") for n in names)  # stub for the contract
        import json
        manifest = json.load(tf.extractfile(next(n for n in names if n.endswith("stoker.json"))))
    assert manifest["engine"] == "metrics"
    assert manifest["metricgen"]["metrics"][0]["name"] == "store.requests"
    assert manifest["sourcetypes"] == ["stoker:metric"]


def test_build_is_content_addressed_dedup(tmp_path):
    a = bundles.build_from_metrics_config("kpi", _valid_config(), bundle_dir=str(tmp_path))
    b = bundles.build_from_metrics_config("kpi", _valid_config(), bundle_dir=str(tmp_path))
    assert a.digest == b.digest
    assert b.reused is True


def test_build_rejects_invalid_config(tmp_path):
    with pytest.raises(bundles.BundleError):
        bundles.build_from_metrics_config("bad", {"metrics": []}, bundle_dir=str(tmp_path))


# ---- authoring API ----

def test_create_get_update_metric_pack(client):
    resp = client.post("/api/metric-packs",
                       json={"name": "kpi", "description": "store KPIs",
                             "config": _valid_config()})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["engines_json"] == ["metrics"]
    assert body["verified"] is True
    assert body["sourcetypes_json"] == ["stoker:metric"]
    pack_id = body["id"]

    got = client.get("/api/metric-packs/%d" % pack_id)
    assert got.status_code == 200
    detail = got.json()
    assert detail["config"]["metrics"][0]["name"] == "store.requests"
    assert detail["series_count"] == 2  # 2 products x 1 (single dim)

    upd = client.put("/api/metric-packs/%d" % pack_id, json={
        "name": "kpi-2", "config": _valid_config(
            dimensions=[{"key": "product", "values": ["a", "b", "c"]}])})
    assert upd.status_code == 200
    again = client.get("/api/metric-packs/%d" % pack_id).json()
    assert again["name"] == "kpi-2"
    assert again["series_count"] == 3


def test_create_metric_pack_rejects_bad_config(client):
    resp = client.post("/api/metric-packs",
                       json={"name": "bad", "config": {"metrics": []}})
    assert resp.status_code == 400
    assert "non-empty list" in resp.text


def test_get_unknown_metric_pack_404(client):
    assert client.get("/api/metric-packs/999999").status_code == 404


def test_metric_pack_appears_in_packs_list_as_metrics_engine(client):
    client.post("/api/metric-packs", json={"name": "kpi", "config": _valid_config()})
    packs = client.get("/api/packs").json()
    metric_packs = [p for p in packs if (p.get("engines_json") or []) == ["metrics"]]
    assert metric_packs and metric_packs[0]["source_path"].startswith("builder://")


# ---- preview ----

def test_preview_returns_a_daily_curve_with_scaled_guides(client):
    resp = client.post("/api/metric-packs/preview", json={
        "config": _valid_config(), "metric": "store.requests",
        "cell": {"product": "search"}, "points": 48})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "store.requests"
    assert body["kind"] == "count"
    assert len(body["points"]) == 48
    # search is scaled x2, so guides double the base 5/600/1200.
    assert body["guides"] == {"min": 10.0, "p95": 1200.0, "max": 2400.0}
    # business_double_hump: a clear morning/afternoon peak vs an overnight trough.
    acts = [p["activity"] for p in body["points"]]
    assert max(acts) > 0.8 and min(acts) < 0.15
    assert body["series_count"] == 2


def test_preview_defaults_to_first_metric_and_is_lenient(client):
    # A half-finished config (one metric still missing fields) still previews the
    # named good metric.
    cfg = {"metrics": [
        {"name": "good", "kind": "gauge", "min": 0, "p95": 50, "max": 100,
         "pattern": {"type": "sine"}},
        {"name": "wip"}]}
    resp = client.post("/api/metric-packs/preview",
                       json={"config": cfg, "metric": "good", "points": 24})
    assert resp.status_code == 200
    assert resp.json()["metric"] == "good"


# ---- integration: a metrics spec provisions and builds its bundle from config ----

def test_metrics_spec_run_builds_bundle_from_config(client, db_session, settings, fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    db_session.commit()
    mp = client.post("/api/metric-packs",
                     json={"name": "kpi", "config": _valid_config()}).json()

    resp = client.post("/api/specs", json={
        "name": "metrics-spec", "pack_id": mp["id"], "target_id": target.id,
        "engine": "metrics", "rate_mode": "count_interval", "rate_value": 2,
        "interval_s": 10, "workers": 1, "fleet": "fake-local"})
    assert resp.status_code == 201, resp.text
    spec_id = resp.json()["id"]

    run = client.post("/api/specs/%d/run" % spec_id, json={})
    assert run.status_code in (200, 201), run.text
    # Provisioning resolved the bundle via build_from_metrics_config -> a bundle
    # row now exists for this pack.
    bundle = db_session.execute(
        select(Bundle).where(Bundle.pack_id == mp["id"])).scalars().first()
    assert bundle is not None


def test_pack_preview_endpoints_do_not_crash_on_a_metric_pack(client):
    # Regression: the spec wizard's PackPicker calls GET /packs/{id}/preview when
    # a pack is selected. A metric pack has no eventgen source dir, so linting
    # source_path ("builder://metrics") used to 500 with "pack directory not
    # found". Both preview endpoints must now handle a metric pack gracefully.
    mp = client.post("/api/metric-packs",
                     json={"name": "kpi", "config": _valid_config()}).json()
    pid = mp["id"]

    pv = client.get("/api/packs/%d/preview" % pid)
    assert pv.status_code == 200, pv.text
    body = pv.json()
    assert body["stanzas"] == [] and body["lint_status"] == "ok"

    pr = client.get("/api/packs/%d/preview_run" % pid)
    assert pr.status_code == 200
    assert pr.json()["events"] == []
