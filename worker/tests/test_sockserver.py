import json
import socket
import threading
import time

import pytest

from stoker_agent.pacing import TokenBucket
from stoker_agent.slice import SpecSlice
from stoker_agent.sockserver import SocketServer, make_filler


class FakeHec(object):
    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def put(self, envelope):
        with self._lock:
            self.events.append(envelope)

    def __len__(self):
        with self._lock:
            return len(self.events)


def make_slice(overrides=None, sourcetype=None, source=None, host=None):
    return SpecSlice(
        run_id=1, slot=0, total_workers=1, lease_id="le",
        engine="eventgen", bundle_url="/x", bundle_sha256=None,
        rate_mode="eps", rate_value=100.0, duration_s=None,
        hec_url="http://h:8088", hec_index="loadtest",
        hec_sourcetype=sourcetype, hec_source=source, hec_host=host,
        hec_gzip=True, hec_ack=False,
        overrides=overrides if overrides is not None else {},
        telemetry_interval_s=5.0, released=False, effective_t0=None,
    )


def open_bucket():
    bucket = TokenBucket(1e6, catchup_s=5.0)
    bucket.anchor_at(time.time() - 1)  # ample quota
    return bucket


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def sock_path(tmp_path):
    return str(tmp_path / "out.sock")


def start_server(sock_path, spec=None, bucket=None, gated=True):
    hec = FakeHec()
    bucket = bucket or open_bucket()
    server = SocketServer(sock_path, hec, bucket,
                          make_filler(spec or make_slice()), gated=gated)
    server.start()
    return server, hec, bucket


def connect(sock_path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    return client


def envelope_line(event="hello", **meta):
    doc = {"time": None, "host": None, "source": None, "sourcetype": None,
           "index": None, "event": event}
    doc.update(meta)
    return (json.dumps(doc) + "\n").encode("utf-8")


class TestMakeFiller:
    def test_overrides_win_over_plugin_values(self):
        spec = make_slice(overrides={"host": "apigw-2", "index": "loadtest"})
        fill = make_filler(spec)
        env = fill({"time": 1.0, "host": "plugin-host", "index": "plugin-idx",
                    "sourcetype": None, "source": None, "event": "x"})
        assert env["host"] == "apigw-2"
        assert env["index"] == "loadtest"

    def test_defaults_fill_nulls_only(self):
        spec = make_slice(sourcetype="st_default")
        fill = make_filler(spec)
        env = fill({"time": 1.0, "host": None, "index": None,
                    "sourcetype": None, "source": "keep-me", "event": "x"})
        assert env["index"] == "loadtest"       # hec default fills null
        assert env["sourcetype"] == "st_default"
        assert env["source"] == "keep-me"       # plugin value kept
        assert env["host"] is None              # nothing declared anywhere

    def test_null_time_stamped_now(self):
        fill = make_filler(make_slice())
        before = time.time()
        env = fill({"time": None, "event": "x"})
        assert before <= env["time"] <= time.time()

    def test_plugin_time_kept(self):
        fill = make_filler(make_slice())
        env = fill({"time": 1752234567.123, "event": "x"})
        assert env["time"] == 1752234567.123


class TestSocketServer:
    def test_forwards_envelopes(self, sock_path):
        server, hec, _ = start_server(sock_path)
        try:
            client = connect(sock_path)
            for i in range(10):
                client.sendall(envelope_line("evt %d" % i))
            assert wait_for(lambda: len(hec) == 10)
            assert hec.events[0]["event"] == "evt 0"
            assert hec.events[0]["index"] == "loadtest"
            assert server.received == 10
            client.close()
        finally:
            server.stop()

    def test_partial_lines_reassembled(self, sock_path):
        server, hec, _ = start_server(sock_path)
        try:
            client = connect(sock_path)
            line = envelope_line("split-event")
            client.sendall(line[:7])
            time.sleep(0.05)
            client.sendall(line[7:20])
            time.sleep(0.05)
            client.sendall(line[20:])
            assert wait_for(lambda: len(hec) == 1)
            assert hec.events[0]["event"] == "split-event"
            client.close()
        finally:
            server.stop()

    def test_junk_lines_counted_and_skipped(self, sock_path):
        server, hec, _ = start_server(sock_path)
        try:
            client = connect(sock_path)
            client.sendall(b"this is not json\n")
            client.sendall(envelope_line("good-1"))
            client.sendall(b'{"no_event_key": 1}\n')
            client.sendall(b"\xff\xfe garbage\n")
            client.sendall(envelope_line("good-2"))
            client.sendall(b"\n")  # blank lines are not malformed
            assert wait_for(lambda: len(hec) == 2)
            assert wait_for(lambda: server.malformed == 3)
            assert [e["event"] for e in hec.events] == ["good-1", "good-2"]
            client.close()
        finally:
            server.stop()

    def test_reconnect_after_engine_restart(self, sock_path):
        server, hec, _ = start_server(sock_path)
        try:
            first = connect(sock_path)
            first.sendall(envelope_line("before-restart"))
            assert wait_for(lambda: len(hec) == 1)
            first.close()
            second = connect(sock_path)
            second.sendall(envelope_line("after-restart"))
            assert wait_for(lambda: len(hec) == 2)
            second.close()
        finally:
            server.stop()

    def test_paused_bucket_stalls_reading(self, sock_path):
        bucket = open_bucket()
        bucket.pause()
        server, hec, _ = start_server(sock_path, bucket=bucket)
        try:
            client = connect(sock_path)
            client.sendall(envelope_line("held"))
            time.sleep(0.3)
            assert len(hec) == 0  # backpressure: nothing flows while paused
            bucket.resume()
            assert wait_for(lambda: len(hec) == 1)
            client.close()
        finally:
            server.stop()

    def test_gating_skipped_in_count_interval_mode(self, sock_path):
        bucket = open_bucket()
        bucket.pause()  # would stall a gated server
        server, hec, _ = start_server(sock_path, bucket=bucket, gated=False)
        try:
            client = connect(sock_path)
            for i in range(5):
                client.sendall(envelope_line("free-%d" % i))
            assert wait_for(lambda: len(hec) == 5)
            client.close()
        finally:
            server.stop()

    def test_closed_bucket_stops_reader(self, sock_path):
        bucket = open_bucket()
        server, hec, _ = start_server(sock_path, bucket=bucket)
        try:
            client = connect(sock_path)
            client.sendall(envelope_line("one"))
            assert wait_for(lambda: len(hec) == 1)
            bucket.close()
            client.sendall(envelope_line("dropped-on-drain"))
            time.sleep(0.3)
            assert len(hec) == 1
            client.close()
        finally:
            server.stop()

    def test_listener_binds_before_start_returns(self, sock_path):
        server, hec, _ = start_server(sock_path)
        try:
            # immediate connect must succeed: agent always listens first
            client = connect(sock_path)
            client.close()
        finally:
            server.stop()

    def test_stop_removes_socket_file(self, sock_path):
        import os
        server, _, _ = start_server(sock_path)
        assert os.path.exists(sock_path)
        server.stop()
        assert not os.path.exists(sock_path)
