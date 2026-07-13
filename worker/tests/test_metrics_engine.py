# -*- coding: utf-8 -*-
"""Unit tests for the metrics engine: series matrix, sharding, envelope shape."""
from __future__ import absolute_import

import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_METRICS_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "engines", "metrics")
if _METRICS_DIR not in sys.path:
    sys.path.insert(0, _METRICS_DIR)

from stoker_metrics import engine as m  # noqa: E402


def _spec(**over):
    spec = {
        "resolution_s": 10,
        "seed": 42,
        "dimensions": [
            {"key": "product", "values": ["checkout", "search", "catalog"]},
            {"key": "region", "values": ["eu", "us"]},
        ],
        "metrics": [
            {"name": "store.requests", "kind": "count", "min": 5, "p95": 800,
             "max": 1500, "noise": 0.1, "pattern": {"type": "business_double_hump"},
             "scale": {"product": {"checkout": 1.0, "search": 2.5, "catalog": 1.8}}},
            {"name": "host.cpu.usage", "kind": "gauge", "min": 3, "p95": 65,
             "max": 98, "noise": 0.1, "pattern": {"type": "sine", "peak_h": 14}},
        ],
    }
    spec.update(over)
    return spec


# ---- series matrix ----

def test_build_series_is_the_dimension_cross_product():
    series = m.build_series(_spec())
    assert len(series) == 6  # 3 products x 2 regions
    # deterministic order, every combo present exactly once
    assert len({tuple(sorted(s.items())) for s in series}) == 6


def test_build_series_no_dimensions_is_single_unlabelled_series():
    assert m.build_series({"metrics": [{"name": "x"}]}) == [{}]


def test_shard_partitions_matrix_without_overlap():
    spec = _spec()
    full = m.build_series(spec)
    owned = []
    for slot in range(3):
        cfg = m.Config("/x", spec, slot, 3, 10)
        eng = m.MetricsEngine(cfg)
        owned.append(eng._series)
    flat = [tuple(sorted(s.items())) for shard in owned for s in shard]
    # union == full matrix, no duplicates
    assert sorted(flat) == sorted(tuple(sorted(s.items())) for s in full)
    assert len(flat) == len(set(flat)) == 6
    # balanced (largest-remainder-like): 3 workers over 6 series -> 2 each
    assert [len(s) for s in owned] == [2, 2, 2]


# ---- envelope shape ----

def test_encode_is_a_metric_envelope_with_fields():
    raw = m._encode(1783900000.0, {"metric_name:cpu": 42.0, "host": "h1"})
    doc = json.loads(raw.decode("utf-8"))
    assert doc["event"] == "metric"
    assert doc["time"] == 1783900000.0
    assert doc["fields"] == {"metric_name:cpu": 42.0, "host": "h1"}
    # metadata is null (agent stamps from the slice)
    for k in ("host", "source", "sourcetype", "index"):
        assert doc[k] is None


def test_fields_carry_all_metrics_plus_dimensions():
    cfg = m.Config("/x", _spec(), 0, 1, 10)
    eng = m.MetricsEngine(cfg)
    series = eng._series[0]
    fields = eng._fields_for(0, series, tick=1783900000.0)
    # dimensions present
    assert fields["product"] == series["product"]
    assert fields["region"] == series["region"]
    # one measurement per metric, multi-metric key convention
    assert "metric_name:store.requests" in fields
    assert "metric_name:host.cpu.usage" in fields
    # count kind -> integer; gauge within [3, 98]
    assert float(fields["metric_name:store.requests"]).is_integer()
    assert 3 <= fields["metric_name:host.cpu.usage"] <= 98


def test_scale_multipliers_shift_magnitude_per_dimension_value():
    cfg = m.Config("/x", _spec(), 0, 1, 10)
    eng = m.MetricsEngine(cfg)
    # Sum store.requests over many ticks per product; search (2.5x) > catalog
    # (1.8x) > checkout (1.0x).
    totals = {}
    for si, series in enumerate(eng._series):
        prod = series["product"]
        acc = 0.0
        for tick in range(0, 86400, 600):  # sample a full day
            acc += eng._value(si, eng._metrics[0], series, float(tick))
        totals[prod] = totals.get(prod, 0.0) + acc
    assert totals["search"] > totals["catalog"] > totals["checkout"]


def test_counter_metric_accumulates_across_ticks():
    spec = _spec(metrics=[{"name": "orders.total", "kind": "counter", "min": 0,
                           "p95": 40, "max": 90, "noise": 0.1,
                           "pattern": {"type": "business_hours"}}])
    cfg = m.Config("/x", spec, 0, 1, 10)
    eng = m.MetricsEngine(cfg)
    series = eng._series[0]
    seq = [eng._value(0, eng._metrics[0], series, float(t))
           for t in range(0, 3600, 60)]
    assert seq == sorted(seq)     # monotonic non-decreasing
    assert seq[-1] > seq[0]       # actually grew


# ---- config loading ----

def test_load_config_requires_metrics(tmp_path):
    cfg_file = tmp_path / "m.json"
    cfg_file.write_text(json.dumps({"resolution_s": 10}))
    env = {"STOKER_METRICS_CONFIG": str(cfg_file), "STOKER_OUTPUT_SOCKET": "/x"}
    with pytest.raises(m.MetricsError):
        m.load_config(env)


def test_load_config_reads_slot_total_resolution(tmp_path):
    cfg_file = tmp_path / "m.json"
    cfg_file.write_text(json.dumps(_spec()))
    env = {
        "STOKER_METRICS_CONFIG": str(cfg_file),
        "STOKER_OUTPUT_SOCKET": "/sock",
        "STOKER_METRICS_SLOT": "1",
        "STOKER_METRICS_TOTAL_WORKERS": "3",
        "STOKER_METRICS_RESOLUTION_S": "5",
    }
    cfg = m.load_config(env)
    assert (cfg.slot, cfg.total_workers, cfg.resolution_s) == (1, 3, 5.0)


def test_load_config_rejects_slot_ge_total(tmp_path):
    cfg_file = tmp_path / "m.json"
    cfg_file.write_text(json.dumps(_spec()))
    env = {"STOKER_METRICS_CONFIG": str(cfg_file), "STOKER_OUTPUT_SOCKET": "/s",
           "STOKER_METRICS_SLOT": "3", "STOKER_METRICS_TOTAL_WORKERS": "3"}
    with pytest.raises(m.MetricsError):
        m.load_config(env)


# ---- grid emission (fake socket + stepping clock) ----

class _FakeSock:
    def __init__(self, stop_after):
        self.sent = []
        self._stop_after = stop_after

    def sendall(self, data):
        if len(self.sent) >= self._stop_after:
            raise BrokenPipeError("drain")
        self.sent.append(data)

    def close(self):
        pass


def test_run_grid_emits_owned_series_each_tick():
    spec = _spec()
    cfg = m.Config("/x", spec, 0, 1, 10.0)
    # A clock that advances by the resolution every call so each loop is a new tick.
    ticks = iter([0.0, 0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    eng = m.MetricsEngine(cfg, clock=lambda: next(ticks), sleep=lambda s: None)
    sock = _FakeSock(stop_after=12)  # 6 series x 2 ticks then BrokenPipe
    eng._run_grid(sock)
    docs = [json.loads(b.decode("utf-8")) for b in sock.sent]
    assert len(docs) == 12
    assert all(d["event"] == "metric" for d in docs)
    # exactly two distinct grid timestamps, six series each
    times = sorted({d["time"] for d in docs})
    assert len(times) == 2
    for t in times:
        combos = {(d["fields"]["product"], d["fields"]["region"])
                  for d in docs if d["time"] == t}
        assert len(combos) == 6
