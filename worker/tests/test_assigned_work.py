# -*- coding: utf-8 -*-
"""Assigned-work reporting: an idle worker must explain itself.

Covers the three pieces added for the over-provisioned-fleet fix:

* ``confrewrite.assigned_stanza_count`` — the worker's own answer to "does
  this slot hold any work?" after the conf rewrite (count_interval can split
  every stanza count to zero on the surplus slots);
* the Agent's heartbeat payload carrying the optional ``assigned_work`` /
  ``assigned_reason`` fields (and OMITTING them until known, so the wire body
  stays byte-compatible with an old control plane);
* the metrics engine's loud WARNING when its stride shard of the series
  matrix is empty (the previous behaviour was a silent idle-until-drain).
"""
from __future__ import absolute_import

import json
import logging
import os
import sys

import pytest

from stoker_agent import confrewrite
from stoker_agent.agent import Agent, _metrics_series_total
from stoker_agent.config import load_config
from stoker_agent.slice import SpecSlice

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_METRICS_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "engines", "metrics")
if _METRICS_DIR not in sys.path:
    sys.path.insert(0, _METRICS_DIR)

from stoker_metrics import engine as metrics_engine  # noqa: E402


# --------------------------------------------------------------------------- #
# confrewrite.assigned_stanza_count
# --------------------------------------------------------------------------- #

def _conf(text, tmp_path, name="in.conf"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _rewritten(tmp_path, text, rate_mode, share, slot, total):
    src = _conf(text, tmp_path)
    dst = str(tmp_path / "out.conf")
    confrewrite.rewrite_file(src, dst, rate_mode, share, 1.15,
                            str(tmp_path), slot=slot, total_workers=total)
    return confrewrite.load_conf(dst)


def test_count_interval_surplus_slot_holds_no_stanzas(tmp_path):
    # The reported bug: count=6 across 10 workers -> slots 6-9 get 0.
    text = "[web]\nmode = sample\ncount = 6\ninterval = 10\n"
    starved = _rewritten(tmp_path, text, "count_interval", None, slot=9, total=10)
    assert confrewrite.assigned_stanza_count(starved, "count_interval") == 0
    fed = _rewritten(tmp_path, text, "count_interval", None, slot=0, total=10)
    assert confrewrite.assigned_stanza_count(fed, "count_interval") == 1


def test_undeclared_count_stanza_always_counts(tmp_path):
    text = "[a]\nmode = sample\ncount = 2\n\n[b]\nmode = sample\n"
    parser = _rewritten(tmp_path, text, "count_interval", None, slot=9, total=10)
    # [a] split to 0 on slot 9, but [b] has no count (engine default emits).
    assert confrewrite.assigned_stanza_count(parser, "count_interval") == 1


def test_gated_modes_assign_every_paced_stanza(tmp_path):
    text = "[a]\nmode = sample\ncount = 2\ninterval = 10\n\n" \
           "[b]\nmode = sample\ncount = 3\ninterval = 10\n"
    parser = _rewritten(tmp_path, text, "eps", 100.0, slot=3, total=4)
    assert confrewrite.assigned_stanza_count(parser, "eps") == 2


def test_replay_stanzas_always_count_as_assigned(tmp_path):
    parser = confrewrite.load_conf(_conf(
        "[cap]\nmode = replay\n\n[web]\nmode = sample\ncount = 1\n", tmp_path))
    # Even in count_interval on a starved slot, the replay stanza still emits.
    for section in confrewrite.sample_sections(parser):
        if section == "web":
            parser.set(section, "count", "0")
    assert confrewrite.assigned_stanza_count(parser, "count_interval") == 1


# --------------------------------------------------------------------------- #
# Agent heartbeat payload
# --------------------------------------------------------------------------- #

def _agent():
    env = {
        "STOKER_RUN_ID": "1",
        "STOKER_CONTROL_URL": "http://ctl.invalid",
        "STOKER_RUN_JWT": "jwt",
        "STOKER_TOTAL_WORKERS": "10",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_METRICS_PORT": "0",
    }
    return Agent(load_config(env))


def _slice(slot=7, total=10):
    return SpecSlice.from_claim({
        "run_id": 1, "slot": slot, "total_workers": total, "lease_id": "le",
        "engine": "metrics",
        "bundle": {"url": "/tmp/pack"}, "share": {"count": 0},
        "hec": {"url": "http://h:8088", "index": "loadtest"},
        "telemetry": {"interval_s": 0.01}, "released": False,
    })


def test_heartbeat_omits_assigned_until_known():
    agent = _agent()
    payload = agent._heartbeat_payload(_slice())
    assert "assigned_work" not in payload
    assert "assigned_reason" not in payload


def test_heartbeat_carries_assigned_work_and_reason_when_zero():
    agent = _agent()
    agent._set_assigned(0, "series", reason_when_zero="no series assigned: "
                        "the pack's series matrix has 4 series and slot 7 of "
                        "10 workers owns none")
    payload = agent._heartbeat_payload(_slice())
    assert payload["assigned_work"] == 0
    assert "owns none" in payload["assigned_reason"]


def test_heartbeat_positive_assigned_has_no_reason():
    agent = _agent()
    agent._set_assigned(3, "stanzas")
    payload = agent._heartbeat_payload(_slice())
    assert payload["assigned_work"] == 3
    assert "assigned_reason" not in payload


def test_heartbeat_reports_bps_from_bytes_total_delta(monkeypatch):
    """The dashboard's MB/s reads the heartbeat's bps; the control plane derives
    it nowhere, so the worker must compute it from the bytes_total delta exactly
    as it does eps. Without this the MB/s series stays flat at 0 while eps flows.
    """
    from stoker_agent import agent as agent_mod
    agent = _agent()
    sl = _slice()
    clock = {"t": 1000.0}
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: clock["t"])
    # First heartbeat seeds the baseline (no interval yet -> 0).
    p0 = agent._heartbeat_payload(sl, snap={"bytes_total": 5000, "events_total": 50})
    assert p0["bps"] == 0.0
    # 2 s later, +20000 bytes and +200 events -> 10000 B/s and 100 eps.
    clock["t"] = 1002.0
    p1 = agent._heartbeat_payload(sl, snap={"bytes_total": 25000, "events_total": 250})
    assert p1["bps"] == pytest.approx(10000.0)
    assert p1["eps"] == pytest.approx(100.0)


def test_metrics_series_total_matches_engine_matrix():
    metricgen = {
        "dimensions": [
            {"key": "product", "values": ["a", "b", "c"]},
            {"key": "region", "values": ["eu", "us"]},
            {"values": ["ignored-no-key"]},
        ],
        "metrics": [{"name": "m"}],
    }
    assert _metrics_series_total(metricgen) == \
        len(metrics_engine.build_series(metricgen))
    assert _metrics_series_total({"metrics": [{"name": "m"}]}) == 1


# --------------------------------------------------------------------------- #
# Metrics engine: the idle shard warns loudly.
# --------------------------------------------------------------------------- #

def _metric_spec():
    return {
        "resolution_s": 10,
        "dimensions": [{"key": "region", "values": ["eu", "us"]},
                       {"key": "product", "values": ["a", "b"]}],
        "metrics": [{"name": "m", "kind": "gauge", "min": 0, "p95": 1, "max": 2}],
    }


def test_engine_empty_shard_warns_with_slot_total_and_series(monkeypatch, caplog):
    cfg = metrics_engine.Config("/x", _metric_spec(), 7, 10, 10.0)
    eng = metrics_engine.MetricsEngine(cfg)
    assert eng.total_series == 4
    assert eng._series == []

    class _Sock(object):
        def close(self):
            pass

    monkeypatch.setattr(metrics_engine, "_connect",
                        lambda path, clock=None, sleep=None: _Sock())
    monkeypatch.setattr(eng, "_idle_until_closed", lambda sock: None)
    with caplog.at_level(logging.WARNING, logger="stoker.metrics"):
        rc = eng.run()
    assert rc == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the empty shard must be logged at WARNING"
    message = warnings[0].getMessage()
    assert "slot 7" in message
    assert "10 workers" in message
    assert "only 4" in message
    assert "nothing for the entire run" in message


def test_engine_populated_shard_does_not_warn(monkeypatch, caplog):
    cfg = metrics_engine.Config("/x", _metric_spec(), 0, 2, 10.0)
    eng = metrics_engine.MetricsEngine(cfg)
    assert len(eng._series) == 2
    with caplog.at_level(logging.WARNING, logger="stoker.metrics"):
        # No socket work: just assert construction stays quiet.
        pass
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
