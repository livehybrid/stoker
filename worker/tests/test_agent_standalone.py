"""End-to-end standalone run without eventgen.

The real Agent, TokenBucket, SocketServer and StandaloneControl run
against a stub engine subprocess that floods envelopes into the unix
socket, and a fake HEC sink that records everything. Delivered volume
must match rate x duration within 1 %.
"""

import sys
import threading
import time

import pytest

from stoker_agent.agent import Agent
from stoker_agent.config import load_config
from stoker_agent.engine import EngineRunner

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

    def snapshot(self):
        with self._lock:
            n = len(self.events)
        return {
            "events_total": n, "bytes_total": n * 20,
            "hec_2xx": 1 if n else 0, "hec_4xx": 0, "hec_5xx": 0,
            "hec_timeouts": 0, "retries": 0, "dropped": 0,
            "dropped_invalid": 0, "queue_depth": 0, "auth_failed": False,
        }

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


def make_agent(tmp_path, rate=100, duration="4"):
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
    cfg = load_config(env)
    sinks = []

    def hec_factory(url, token, gzip_enabled, verify_tls, ack):
        sink = FakeHec(url, token, gzip_enabled, verify_tls, ack)
        sinks.append(sink)
        return sink

    agent = Agent(cfg,
                  hec_factory=hec_factory,
                  engine_factory=lambda conf, sock: StubEngine(conf, sock))
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
                  engine_factory=lambda conf, sock: StubEngine(conf, sock))
    assert agent.run() == 3
