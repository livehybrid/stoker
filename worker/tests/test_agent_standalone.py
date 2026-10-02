"""End-to-end standalone run without eventgen.

The real Agent, TokenBucket, SocketServer and StandaloneControl run
against a stub engine subprocess that floods envelopes into the unix
socket, and a fake HEC sink that records everything. Delivered volume
must match rate x duration within 1 %.
"""

import json
import math
import sys
import threading
import time

import pytest

from stoker_agent.agent import EXIT_DEADMAN, Agent
from stoker_agent.config import load_config
from stoker_agent.engine import EngineRunner
from stoker_agent.slice import SpecSlice

STUB_SCRIPT = r"""
import json, os, socket, sys, time
path = os.environ["STOKER_OUTPUT_SOCKET"]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
deadline = time.time() + 10
while True:
    try:
        s.connect(path)
        break
    except OSError:
        if time.time() > deadline:
            sys.exit(1)
        time.sleep(0.02)
i = 0
try:
    while True:
        line = json.dumps({"time": None, "host": None, "source": None,
                           "sourcetype": None, "index": None,
                           "event": "stub event %d" % i}) + "\n"
        s.sendall(line.encode("utf-8"))
        i += 1
except OSError:
    pass
"""


class StubEngine(EngineRunner):
    """Real subprocess management, stub generator command."""

    def _command(self):
        return [sys.executable, "-u", "-c", STUB_SCRIPT]


class FakeHec(object):
    def __init__(self, url, token, gzip_enabled, verify_tls, ack):
        self.url = url
        self.token = token
        self.gzip_enabled = gzip_enabled
        self.verify_tls = verify_tls
        self.ack = ack
        self.events = []
        self.flush_timeout = None
        self.stopped = False
        self._lock = threading.Lock()

    def put(self, envelope):
        if self.stopped:
            raise RuntimeError("stopped")
        with self._lock:
            self.events.append(envelope)

    def put_lines(self, lines):
        if self.stopped:
            raise RuntimeError("stopped")
        with self._lock:
            self.events.extend(lines)

    def snapshot(self):
        with self._lock:
            n = len(self.events)
        return {
            "events_total": n, "bytes_total": n * 20,
            "hec_2xx": 1 if n else 0, "hec_4xx": 0, "hec_5xx": 0,
            "hec_timeouts": 0, "retries": 0, "dropped": 0,
            "dropped_invalid": 0, "queue_depth": 0, "auth_failed": False,
        }

    def begin_stop(self):
        self.stopped = True

    def flush_and_stop(self, timeout_s):
        self.flush_timeout = timeout_s
        self.stopped = True
        return True

    def __len__(self):
        with self._lock:
            return len(self.events)


def make_pack(tmp_path):
    pack = tmp_path / "pack"
    (pack / "default").mkdir(parents=True)
    (pack / "samples").mkdir()
    (pack / "samples" / "flat.sample").write_text("the quick brown fox\n")
    (pack / "default" / "eventgen.conf").write_text(
        "[flat.sample]\n"
        "count = 10\n"
        "interval = 10\n"
        "outputMode = httpevent\n"
        "index = wrong\n"
    )
    (pack / "pack.yaml").write_text(
        "name: tiny\n"
        "estimates:\n"
        "  bytes_per_event: 20\n"
    )
    return str(pack)


def make_agent(tmp_path, rate=100, duration="4", extra_env=None):
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": make_pack(tmp_path),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": str(rate),
        "STOKER_DURATION_S": duration,
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    if extra_env:
        env.update(extra_env)
    cfg = load_config(env)
    sinks = []

    def hec_factory(url, token, gzip_enabled, verify_tls, ack):
        sink = FakeHec(url, token, gzip_enabled, verify_tls, ack)
        sinks.append(sink)
        return sink

    agent = Agent(cfg,
                  hec_factory=hec_factory,
                  engine_factory=lambda conf, sock, cwd=None, extra_env=None:
                  StubEngine(conf, sock, cwd=cwd))
    return agent, sinks


@pytest.mark.timeout(60)
def test_rate_accuracy_within_one_percent(tmp_path):
    rate, duration = 100, 4.0
    agent, sinks = make_agent(tmp_path, rate=rate, duration=str(int(duration)))
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()))
    thread.start()
    thread.join(40)
    assert not thread.is_alive(), "agent did not finish"
    assert result == [0]

    sink = sinks[0]
    expected = rate * duration
    delivered = len(sink)
    assert abs(delivered - expected) <= expected * 0.01, \
        "delivered %d, expected %d +/- 1%%" % (delivered, expected)

    # the sink received the wiring the slice declared
    assert sink.url == "http://fake-hec:8088"
    assert sink.token == "tok"
    assert sink.gzip_enabled is True
    assert sink.ack is False
    assert sink.flush_timeout == 20.0
    # slice overrides stamped on every envelope; engine's index stripped
    assert sink.events[0]["index"] == "loadtest"
    assert sink.events[0]["event"].startswith("stub event")
    assert sink.events[0]["time"] > 0


@pytest.mark.timeout(60)
def test_sigterm_drains_cleanly(tmp_path):
    agent, sinks = make_agent(tmp_path, rate=200, duration="")  # unbounded
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()))
    thread.start()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if sinks and len(sinks[0]) >= 20:
            break
        time.sleep(0.05)
    assert sinks and len(sinks[0]) >= 20, "agent never started generating"

    start = time.monotonic()
    agent.request_drain("signal-15")  # what the SIGTERM handler calls
    thread.join(40)
    drain_took = time.monotonic() - start
    assert not thread.is_alive(), "agent did not exit after drain request"
    assert result == [0]
    assert drain_took < 45.0
    assert sinks[0].stopped is True
    assert sinks[0].flush_timeout == 20.0


@pytest.mark.timeout(60)
def test_socket_file_cleaned_up(tmp_path):
    import os
    agent, _ = make_agent(tmp_path, rate=50, duration="1")
    assert agent.run() == 0
    assert not os.path.exists(str(tmp_path / "out.sock"))


class AuthFailingHec(FakeHec):
    def snapshot(self):
        snap = FakeHec.snapshot(self)
        snap["auth_failed"] = True
        return snap


@pytest.mark.timeout(60)
def test_standalone_hec_auth_failure_exits_3(tmp_path):
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": make_pack(tmp_path),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "bad-token",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "50",
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    agent = Agent(load_config(env),
                  hec_factory=AuthFailingHec,
                  engine_factory=lambda conf, sock, cwd=None, extra_env=None:
                  StubEngine(conf, sock, cwd=cwd))
    assert agent.run() == 3


class _DeadControl(object):
    """Managed control plane that is up for claim/ready then dies: every
    heartbeat misses and the dead-man window has elapsed."""

    def heartbeat(self, payload):
        return None

    def deadman_expired(self):
        return True

    def should_pause(self):
        return False

    def seconds_since_ack(self):
        return 9999.0


def test_await_release_self_evicts_on_deadman():
    """Regression (protocol_security#1): a control plane that dies after
    ready() but before release must not hang the worker pre-T0. The dead-man
    guard in _await_release drains and sets exit code 4 instead of looping."""
    env = {
        "STOKER_RUN_ID": "1",
        "STOKER_CONTROL_URL": "http://ctl.invalid",
        "STOKER_RUN_JWT": "jwt",
        "STOKER_TOTAL_WORKERS": "1",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_METRICS_PORT": "0",
    }
    agent = Agent(load_config(env))  # bucket/hec unset: fencing early-returns
    sl = SpecSlice.from_claim({
        "run_id": 1, "slot": 0, "total_workers": 1, "lease_id": "le",
        "engine": "eventgen",
        "bundle": {"url": "/tmp/pack"}, "share": {"eps": 100},
        "hec": {"url": "http://h:8088", "index": "loadtest"},
        "telemetry": {"interval_s": 0.01}, "released": False,
    })
    t0 = agent._await_release(_DeadControl(), sl)
    assert t0 is None
    assert agent._exit_code == EXIT_DEADMAN
    assert agent._drain_event.is_set()


def _metric_pack(tmp_path):
    """A tiny metric pack (stoker.json metricgen): 2 series, 1 metric, 10s grid."""
    pack = tmp_path / "mpack"
    (pack / "default").mkdir(parents=True)
    # metric bundles ship a stub eventgen.conf as a fallback; the agent ignores
    # it for the metrics engine but the bundle loader expects the pack shape.
    (pack / "default" / "eventgen.conf").write_text("[metrics]\nmode = metrics\n")
    (pack / "stoker.json").write_text(json.dumps({
        "name": "mtest",
        "metricgen": {
            "resolution_s": 10,
            "seed": 1,
            "dimensions": [{"key": "svc", "values": ["a", "b"]}],
            "metrics": [{"name": "t.count", "kind": "count",
                         "min": 1, "p95": 5, "max": 9}],
        },
    }))
    return str(pack)


@pytest.mark.timeout(90)
def test_metrics_backfill_delivers_full_sweep(tmp_path, monkeypatch):
    """Regression: a metrics backfill must NOT be warmed pre-T0.

    The metrics engine emits its whole window HOT and exits (unlike eventgen,
    which the agent paces). A backfill run is gated (eps at the cap), and warming
    a gated engine pre-T0 pushed the hot sweep into a paused pipeline: the socket
    reader stalled and the sweep was cut short to a handful of points. Started at
    T0 into an active bucket, the full grid (ceil(window/res) x series) lands.
    This drives the REAL metrics engine end to end, so it fails if the warm/T0
    gating regresses.
    """
    window, res, series = 300, 30, 2
    now = time.time()
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_ENGINE": "metrics",
        "STOKER_BUNDLE": _metric_pack(tmp_path),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "m",
        # eps mode -> gated (exactly how the control plane launches a backfill).
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "5000",
        "STOKER_DURATION_S": "25",   # backstop; the engine exits on its own first
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    # SpecSlice.from_standalone reads the backfill window from os.environ (via
    # _envf), not the load_config dict, so set it there (a real worker's process
    # env carries it). This is what turns the run into a backfill.
    monkeypatch.setenv("STOKER_BACKFILL_START_S", repr(now - window))
    monkeypatch.setenv("STOKER_BACKFILL_END_S", repr(now))
    monkeypatch.setenv("STOKER_BACKFILL_RESOLUTION_S", str(res))
    cfg = load_config(env)
    sinks = []

    def hec_factory(url, token, gzip_enabled, verify_tls, ack):
        sink = FakeHec(url, token, gzip_enabled, verify_tls, ack)
        sinks.append(sink)
        return sink

    # Default metrics_engine_factory -> the real `python -m stoker_metrics`.
    agent = Agent(cfg, hec_factory=hec_factory)
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()))
    thread.start()
    thread.join(75)
    assert not thread.is_alive(), "agent did not finish"
    assert result == [0]

    delivered = len(sinks[0])
    ticks = math.ceil(window / res)  # ~10
    # The full sweep is ticks x series (+/- one grid-alignment tick). The bug
    # delivered only a handful, so a generous floor still separates them.
    assert delivered >= (ticks - 1) * series, (
        "metrics backfill delivered %d points; expected the full sweep "
        "(~%d) - was it cut short pre-T0?" % (delivered, ticks * series))
    # Metric envelopes, stamped at historical times across the window.
    assert sinks[0].events[0]["event"] == "metric"
    stamps = [e["time"] for e in sinks[0].events if e.get("time")]
    assert max(stamps) - min(stamps) >= window - 2 * res, \
        "points are not spread across the historical window (grid, not backfill?)"


@pytest.mark.timeout(60)
def test_small_drain_budget_clamps_flush_timeout(tmp_path):
    """Regression (concurrency#4): the HEC flush timeout is clamped to the
    remaining global drain budget, so the whole drain stays bounded."""
    agent, sinks = make_agent(tmp_path, rate=100, duration="2",
                              extra_env={"STOKER_DRAIN_BUDGET_S": "5"})
    assert agent.run() == 0
    assert sinks[0].flush_timeout is not None
    assert sinks[0].flush_timeout <= 5.0


def test_firebox_gets_the_hec_envelope_policy(tmp_path, monkeypatch):
    """With a firebox binary present the agent asks the engine for HEC-line
    envelopes (STOKER_ENVELOPE=hec + the slice's metadata policy) and forwards
    the lines as bytes; STOKER_FAST_ENVELOPE=0 restores the classic envelope."""
    import stat
    fake = tmp_path / "firebox"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("STOKER_FIREBOX_BIN", str(fake))
    monkeypatch.delenv("STOKER_ENGINE_CMD", raising=False)
    captured = {}

    def factory(conf, sock, cwd=None, extra_env=None):
        captured.update(extra_env or {})
        return StubEngine(conf, sock, cwd=cwd)

    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": make_pack(tmp_path),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_SOURCETYPE": "st_override",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "200",
        "STOKER_DURATION_S": "2",
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    sinks = []

    def hec_factory(url, token, gzip_enabled, verify_tls, ack):
        sink = FakeHec(url, token, gzip_enabled, verify_tls, ack)
        sinks.append(sink)
        return sink

    agent = Agent(load_config(env), hec_factory=hec_factory, engine_factory=factory)
    assert agent.run() == 0
    assert captured["STOKER_ENVELOPE"] == "hec"
    policy = json.loads(captured["STOKER_ENVELOPE_META"])
    assert policy["overrides"]["index"] == "loadtest"
    assert policy["overrides"]["sourcetype"] == "st_override"
    assert set(policy["defaults"]) == {"index", "sourcetype", "source", "host"}
    assert sinks[0].events, "nothing delivered"
    assert all(isinstance(e, bytes) for e in sinks[0].events)
    assert 300 <= len(sinks[0].events) <= 500  # ~200 eps x 2 s, paced

    # opt out: classic envelope, no policy env
    captured.clear()
    env["STOKER_FAST_ENVELOPE"] = "0"
    agent = Agent(load_config(env), hec_factory=hec_factory, engine_factory=factory)
    assert agent.run() == 0
    assert "STOKER_ENVELOPE" not in captured
    assert all(isinstance(e, dict) for e in sinks[1].events)


def test_engine_report_rides_the_heartbeat(tmp_path, monkeypatch):
    """The agent reports the eventgen implementation it launched and the
    socket envelope on every heartbeat (additive fields); nothing is reported
    under a STOKER_ENGINE_CMD override, whose launcher may be anything."""
    import stat
    fake = tmp_path / "firebox"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("STOKER_FIREBOX_BIN", str(fake))
    monkeypatch.delenv("STOKER_ENGINE_CMD", raising=False)
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": make_pack(tmp_path),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "100",
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
    }
    cfg = load_config(env)
    sl = SpecSlice.from_standalone(cfg)

    agent = Agent(cfg)
    assert agent._eventgen_envelope(cfg, sl, False)[0] == "hec"
    payload = agent._heartbeat_payload(sl)
    assert payload["engine_impl"] == "firebox"
    assert payload["envelope"] == "hec"

    env["STOKER_FAST_ENVELOPE"] = "0"
    cfg = load_config(env)
    agent = Agent(cfg)
    assert agent._eventgen_envelope(cfg, sl, False)[0] == "stoker"
    payload = agent._heartbeat_payload(sl)
    assert payload["engine_impl"] == "firebox"
    assert payload["envelope"] == "stoker"

    env.pop("STOKER_FAST_ENVELOPE")
    env["STOKER_EVENTGEN_IMPL"] = "python"
    monkeypatch.setenv("STOKER_EVENTGEN_IMPL", "python")
    agent = Agent(load_config(env))
    assert agent._eventgen_envelope(load_config(env), sl, False)[0] == "stoker"
    payload = agent._heartbeat_payload(sl)
    assert payload["engine_impl"] == "python"
    assert payload["envelope"] == "stoker"

    monkeypatch.delenv("STOKER_EVENTGEN_IMPL")
    monkeypatch.setenv("STOKER_ENGINE_CMD", "/bin/echo {conf}")
    agent = Agent(load_config(env))
    assert agent._eventgen_envelope(load_config(env), sl, False)[0] == "stoker"
    assert "engine_impl" not in agent._heartbeat_payload(sl)

    # other engines never report an eventgen implementation
    agent = Agent(load_config(env))
    assert agent._eventgen_envelope(load_config(env), sl, True)[0] == "stoker"
    assert "engine_impl" not in agent._heartbeat_payload(sl)



def test_rate_shape_pack_scales_the_engine_and_the_bucket(tmp_path, monkeypatch):
    """rate_shape=pack: the rewritten conf asks the engine for the curve's
    PEAK, the bucket starts at the shaped rate for 'now', and each run-loop
    tick re-aims it (continuous owed(t))."""
    import json as _json
    from stoker_agent import shaping as _shaping
    pack = tmp_path / "shaped"
    (pack / "default").mkdir(parents=True)
    (pack / "samples").mkdir()
    (pack / "samples" / "s.sample").write_text("line\n")
    hours = {str(h): (2.0 if 9 <= h < 17 else 0.5) for h in range(24)}
    (pack / "default" / "eventgen.conf").write_text(
        "[s.sample]\ncount = 10\ninterval = 1\nhourOfDayRate = %s\n" % _json.dumps(hours))
    monkeypatch.setenv("STOKER_RATE_SHAPE", "pack")
    env = {
        "STOKER_STANDALONE": "1", "STOKER_BUNDLE": str(pack),
        "STOKER_HEC_URL": "http://fake-hec:8088", "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest", "STOKER_RATE_MODE": "eps", "STOKER_RATE_VALUE": "100",
        "STOKER_DURATION_S": "1", "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0", "STOKER_HEARTBEAT_S": "1",
    }
    cfg = load_config(env)
    sl = SpecSlice.from_standalone(cfg)
    assert sl.rate_shape == "pack"
    busy = time.mktime((2026, 9, 28, 10, 0, 0, 0, 0, -1))
    agent = Agent(cfg, clock=lambda: busy)

    class _P(object):
        conf_path = str(pack / "default" / "eventgen.conf")

    agent._setup_shape(sl, _P(), False)
    assert agent._shape is not None and agent._shape.peak_ratio == 2.0
    assert agent._engine_share(sl) == 200.0          # engine produces the peak
    assert abs(agent._shape.rate(100.0, busy) - 200.0) < 1e-6

    # a flat spec is untouched
    sl.rate_shape = None
    agent2 = Agent(cfg, clock=lambda: busy)
    agent2._setup_shape(sl, _P(), False)
    assert agent2._shape is None and agent2._engine_share(sl) == 100.0


def test_the_fleet_position_reaches_the_engine_on_every_path(tmp_path, monkeypatch):
    """The worker's slot must travel whatever the envelope.

    Under `pass`-scope rotation the identity is
    `(pass x workers + slot) x D + k`, so the slot is the only thing keeping two
    workers' identities apart. It used to ride only the HEC-line envelope, which
    meant STOKER_FAST_ENVELOPE=0 (a supported setting) left every worker at the
    default slot 0 of 1: all N workers would mint the SAME identities and merge
    every journey, with nothing in the data to show it.
    """
    import stat
    fake = tmp_path / "firebox"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("STOKER_FIREBOX_BIN", str(fake))
    monkeypatch.delenv("STOKER_ENGINE_CMD", raising=False)
    monkeypatch.delenv("STOKER_EVENTGEN_IMPL", raising=False)
    base = {
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": make_pack(tmp_path),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "100",
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
    }

    def envelope_env(extra=None, python=False, override=None):
        env = dict(base, **(extra or {}))
        if python:
            monkeypatch.setenv("STOKER_EVENTGEN_IMPL", "python")
        else:
            monkeypatch.delenv("STOKER_EVENTGEN_IMPL", raising=False)
        if override:
            monkeypatch.setenv("STOKER_ENGINE_CMD", override)
        else:
            monkeypatch.delenv("STOKER_ENGINE_CMD", raising=False)
        cfg = load_config(env)
        sl = SpecSlice.from_standalone(cfg)
        return Agent(cfg)._eventgen_envelope(cfg, sl, False)

    # The HEC-line envelope: rotation rides alongside the envelope metadata.
    name, extra = envelope_env()
    assert name == "hec"
    assert extra["STOKER_ROTATE_WORKERS"] == "1" and extra["STOKER_ROTATE_SLOT"] == "0"
    assert "STOKER_ENVELOPE_META" in extra

    # The classic envelope: this is the path that used to drop it.
    name, extra = envelope_env({"STOKER_FAST_ENVELOPE": "0"})
    assert name == "stoker"
    assert extra is not None, "the classic envelope dropped the fleet position"
    assert extra["STOKER_ROTATE_SLOT"] == "0"
    assert "STOKER_ENVELOPE" not in extra, "the classic envelope sets no envelope keys"

    # The Python engine (the run fails later at build_command) and a custom
    # launcher both still carry it, so neither can be the quiet path.
    for kwargs in ({"python": True}, {"override": "/bin/echo {conf}"}):
        name, extra = envelope_env(**kwargs)
        assert name == "stoker"
        assert extra and "STOKER_ROTATE_WORKERS" in extra, kwargs

    # Another engine (rawreplay/metrics) cannot hold a rotate token, so it
    # needs nothing.
    cfg = load_config(base)
    sl = SpecSlice.from_standalone(cfg)
    assert Agent(cfg)._eventgen_envelope(cfg, sl, True) == ("stoker", None)
