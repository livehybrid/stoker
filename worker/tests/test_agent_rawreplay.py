"""End-to-end agent test for the PISTON raw-replay engine.

Mirrors ``test_agent_standalone.py`` but drives the ``rawreplay`` engine instead
of eventgen: the real Agent, TokenBucket, SocketServer and StandaloneControl run
against the **real** ``RawReplayRunner`` subprocess (``python -m
stoker_rawreplay``) replaying a tiny dataset, and a fake HEC sink records
everything. This proves the whole path — claim -> skip conf-rewrite -> rawreplay
engine -> unix socket -> token bucket -> HEC — without eventgen.

Two pacing shapes:

* **RATE** (eps): the engine emits ``time = null`` HOT and loops the dataset; the
  agent's token bucket paces delivery, so delivered volume tracks rate x duration
  within a small tolerance (the dataset loops to fill the duration).
* **CADENCE** (count_interval): the run is not gated; the engine self-paces by the
  recorded inter-event gaps and stamps ``time = now + offset``. The dataset plays
  once, so the sink receives exactly the dataset's events.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from stoker_agent.agent import Agent
from stoker_agent.config import load_config
from stoker_agent.engine import RawReplayRunner

# Reuse the fake HEC sink from the eventgen standalone test (same interface).
from test_agent_standalone import FakeHec


# Timestamped events so CADENCE mode has real inter-event gaps to reproduce.
DATASET_LINES = [
    "2026-07-10T08:00:00.000Z alpha srcip=10.0.0.1 action=login",
    "2026-07-10T08:00:00.100Z bravo srcip=10.0.0.2 action=read",
    "2026-07-10T08:00:00.250Z charlie srcip=10.0.0.3 action=write",
    "2026-07-10T08:00:00.400Z delta srcip=10.0.0.4 action=delete",
]


def make_replay_pack(tmp_path, mode="rate", time_multiple=1.0):
    """A minimal, well-formed rawreplay pack.

    Ships default/eventgen.conf (a mode=replay stanza) so the pack is a valid
    bundle, a dataset under dataset/, and a pack.yaml declaring engine=rawreplay
    plus the replay: section the agent's bundle loader reads.
    """
    pack = tmp_path / "replaypack"
    (pack / "default").mkdir(parents=True)
    (pack / "dataset").mkdir()
    (pack / "dataset" / "events.log").write_text(
        "".join(l + "\n" for l in DATASET_LINES), encoding="utf-8")
    (pack / "default" / "eventgen.conf").write_text(
        "[replaypack]\n"
        "mode = replay\n"
        "sampleFile = dataset/events.log\n"
        "timeMultiple = %s\n" % time_multiple,
        encoding="utf-8")
    (pack / "pack.yaml").write_text(
        "name: replaypack\n"
        "engine: rawreplay\n"
        "replay:\n"
        "  dataset: dataset/events.log\n"
        "  mode: %s\n"
        "  time_multiple: %s\n"
        "estimates:\n"
        "  bytes_per_event: 60\n" % (mode, time_multiple),
        encoding="utf-8")
    return str(pack)


def make_rate_agent(tmp_path, rate=100, duration="3"):
    """Standalone agent, engine=rawreplay, eps (gated -> RATE engine mode)."""
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_ENGINE": "rawreplay",
        "STOKER_BUNDLE": make_replay_pack(tmp_path, mode="rate"),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_SOURCETYPE": "attack:data",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": str(rate),
        "STOKER_DURATION_S": duration,
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    cfg = load_config(env)
    sinks = []

    def hec_factory(url, token, gzip_enabled, verify_tls, ack):
        sink = FakeHec(url, token, gzip_enabled, verify_tls, ack)
        sinks.append(sink)
        return sink

    # Default rawreplay factory -> the REAL RawReplayRunner subprocess.
    agent = Agent(cfg, hec_factory=hec_factory)
    return agent, sinks


@pytest.mark.timeout(60)
def test_rawreplay_rate_drives_engine_through_socket_to_hec(tmp_path):
    rate, duration = 100, 3.0
    agent, sinks = make_rate_agent(tmp_path, rate=rate, duration=str(int(duration)))
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()))
    thread.start()
    thread.join(50)
    assert not thread.is_alive(), "agent did not finish"
    assert result == [0]

    sink = sinks[0]
    expected = rate * duration
    delivered = len(sink)
    # The dataset (4 lines) loops to fill the duration; the token bucket paces to
    # the eps share, so delivered tracks rate x duration.
    assert abs(delivered - expected) <= expected * 0.05, \
        "delivered %d, expected %d +/- 5%%" % (delivered, expected)

    # The wiring the slice declared reached the sink.
    assert sink.url == "http://fake-hec:8088"
    assert sink.token == "tok"
    assert sink.flush_timeout == 20.0
    # Slice overrides stamped on every envelope (RATE: the engine sent null meta,
    # the agent filled index/sourcetype from the standalone slice).
    ev = sink.events[0]
    assert ev["index"] == "loadtest"
    assert ev["sourcetype"] == "attack:data"
    # RATE: the agent stamped "now" (engine sent time=null).
    assert ev["time"] > 0
    # The event body is a verbatim dataset line (byte-for-byte replay).
    assert ev["event"] in DATASET_LINES


@pytest.mark.timeout(60)
def test_rawreplay_cadence_replays_dataset_once_engine_paced(tmp_path):
    # count_interval -> ungated -> CADENCE engine mode (engine-paced). The dataset
    # plays through once, so the sink receives exactly its events (in order).
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_ENGINE": "rawreplay",
        # time_multiple 0.05: the recorded 0.1/0.15/0.15s gaps replay in ~0.02s
        # each, so the whole capture streams in well under a second.
        "STOKER_BUNDLE": make_replay_pack(tmp_path, mode="cadence",
                                          time_multiple=0.05),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "count_interval",
        "STOKER_DURATION_S": "",  # unbounded; the run ends when the engine exits
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    cfg = load_config(env)
    sinks = []

    def hec_factory(url, token, gzip_enabled, verify_tls, ack):
        sink = FakeHec(url, token, gzip_enabled, verify_tls, ack)
        sinks.append(sink)
        return sink

    agent = Agent(cfg, hec_factory=hec_factory)
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()))
    thread.start()
    # The engine exits after streaming the 4-line dataset once; the agent then
    # drains on engine-exit. Give it room, then assert it finished on its own.
    thread.join(50)
    assert not thread.is_alive(), "agent did not finish after the engine exited"
    assert result == [0]

    sink = sinks[0]
    # Exactly the dataset, once, in order (cadence plays through a single time).
    assert [e["event"] for e in sink.events] == DATASET_LINES
    # CADENCE: the engine stamped monotonic times (agent did NOT gate/stamp).
    times = [e["time"] for e in sink.events]
    assert all(times[i] <= times[i + 1] for i in range(len(times) - 1))
    assert all(t > 0 for t in times)
    # Metadata still filled from the slice defaults.
    assert sink.events[0]["index"] == "loadtest"


@pytest.mark.timeout(60)
def test_rawreplay_missing_replay_config_fails_config(tmp_path):
    # An engine=rawreplay run against a pack with NO replay: section is a hard
    # config error (EXIT_CONFIG=2): the agent drains cleanly rather than hanging.
    pack = tmp_path / "noreplay"
    (pack / "default").mkdir(parents=True)
    (pack / "samples").mkdir()
    (pack / "samples" / "flat.sample").write_text("event\n", encoding="utf-8")
    (pack / "default" / "eventgen.conf").write_text(
        "[flat.sample]\ncount = 1\ninterval = 1\n", encoding="utf-8")
    (pack / "pack.yaml").write_text(
        "name: noreplay\nengine: rawreplay\n"
        "estimates:\n  bytes_per_event: 20\n", encoding="utf-8")
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_ENGINE": "rawreplay",
        "STOKER_BUNDLE": str(pack),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "50",
        "STOKER_DURATION_S": "2",
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    agent = Agent(load_config(env),
                  hec_factory=lambda url, token, gzip_enabled, verify_tls, ack:
                  FakeHec(url, token, gzip_enabled, verify_tls, ack))
    rc = agent.run()
    assert rc == 2  # EXIT_CONFIG


@pytest.mark.timeout(60)
def test_rawreplay_uses_injected_stub_engine_factory(tmp_path):
    """The agent routes a rawreplay slice through the injected rawreplay factory.

    A stub factory records that it was called with a replay view whose mode was
    derived from the run's pacing (rate_mode=eps -> "rate"), and returns a stub
    engine that floods the socket. This isolates the agent's rawreplay branch
    from the real subprocess.
    """
    import sys

    from stoker_agent.engine import EngineRunner

    stub_flood = r"""
import json, os, socket, sys, time
path = os.environ["STOKER_OUTPUT_SOCKET"]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
deadline = time.time() + 10
while True:
    try:
        s.connect(path); break
    except OSError:
        if time.time() > deadline: sys.exit(1)
        time.sleep(0.02)
i = 0
try:
    while True:
        s.sendall((json.dumps({"time": None, "host": None, "source": None,
                   "sourcetype": None, "index": None,
                   "event": "replay stub %d" % i}) + "\n").encode())
        i += 1
except OSError:
    pass
"""

    class StubReplayEngine(EngineRunner):
        def _command(self):
            return [sys.executable, "-u", "-c", stub_flood]

    seen = {}

    def stub_factory(replay, socket_path, cwd=None, log_dir=None):
        seen["mode"] = replay.mode
        seen["dataset"] = replay.dataset
        seen["time_multiple"] = replay.time_multiple
        # conf_path is unused by the stub command; pass the log_dir as a stand-in.
        return StubReplayEngine(log_dir or ".", socket_path, cwd=cwd)

    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_ENGINE": "rawreplay",
        "STOKER_BUNDLE": make_replay_pack(tmp_path, mode="cadence",
                                          time_multiple=3.0),
        "STOKER_HEC_URL": "http://fake-hec:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",           # gated -> engine mode "rate"
        "STOKER_RATE_VALUE": "80",
        "STOKER_DURATION_S": "2",
        "STOKER_OUTPUT_SOCKET": str(tmp_path / "out.sock"),
        "STOKER_METRICS_PORT": "0",
        "STOKER_HEARTBEAT_S": "1",
    }
    cfg = load_config(env)
    sinks = []
    agent = Agent(cfg,
                  hec_factory=lambda url, token, gzip_enabled, verify_tls, ack:
                  sinks.append(FakeHec(url, token, gzip_enabled, verify_tls, ack))
                  or sinks[-1],
                  rawreplay_engine_factory=stub_factory)
    rc = agent.run()
    assert rc == 0
    # The agent overrode the pack's declared cadence with "rate" (rate_mode=eps).
    assert seen["mode"] == "rate"
    assert seen["dataset"].endswith("dataset/events.log")
    # time_multiple carried through from the pack unchanged.
    assert seen["time_multiple"] == 3.0
    # Roughly rate x duration delivered (the stub floods; the bucket paces).
    delivered = len(sinks[0])
    assert abs(delivered - 80 * 2.0) <= 80 * 2.0 * 0.1
