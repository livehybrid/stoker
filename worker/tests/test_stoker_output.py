"""Unit tests for the stoker eventgen output plugin
(worker/engines/eventgen/splunk_eventgen/lib/plugins/output/stoker.py).

The plugin is driven directly with fake queue items shaped exactly like
the real ones built in lib/generatorplugin.py replace_tokens and
lib/eventgenoutput.py Output.send: _raw (str, always present) plus
index/host/hostRegex/source/sourcetype/_time, where _time is an int epoch
from the default path and a float from replay.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.join(os.path.dirname(TESTS_DIR), "engines", "eventgen")
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

# lib/logging_config runs dictConfig at import and opens RotatingFileHandlers
# in EVENTGEN_LOG_DIR; the in-package default may be absent or read-only.
os.environ.setdefault("EVENTGEN_LOG_DIR", tempfile.mkdtemp(prefix="eglogs"))

pytest.importorskip("splunk_eventgen")

from splunk_eventgen.lib.outputplugin import OutputPlugin  # noqa: E402
from splunk_eventgen.lib.plugins.output import stoker  # noqa: E402

PLUGIN_FILE = os.path.join(
    ENGINE_DIR, "splunk_eventgen", "lib", "plugins", "output", "stoker.py"
)

ENVELOPE_KEYS = ["time", "host", "source", "sourcetype", "index", "event"]


class FakeSample(object):
    """Only the attributes OutputPlugin.__init__ touches."""

    app = "stoker_test"
    name = "stanza1"
    outputMode = "stoker"


def queue_item(raw="2026-07-11 12:00:00 test event", **overrides):
    item = {
        "_raw": raw,
        "index": "main",
        "host": "host-1",
        "hostRegex": None,
        "source": "src.sample",
        "sourcetype": "stoker:test",
        "_time": 1752234567,
    }
    item.update(overrides)
    return item


def read_lines(conn, n, timeout=10.0):
    conn.settimeout(0.2)
    deadline = time.monotonic() + timeout
    buf = b""
    lines = []
    while len(lines) < n and time.monotonic() < deadline:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            lines.append(buf[:idx])
            buf = buf[idx + 1:]
    assert len(lines) >= n, "expected %d lines, got %d" % (n, len(lines))
    return lines


@pytest.fixture
def sock_path(monkeypatch):
    # tempfile.mkdtemp keeps the path short (AF_UNIX limit is 108 bytes).
    workdir = tempfile.mkdtemp(prefix="stksock")
    path = os.path.join(workdir, "s.sock")
    monkeypatch.setenv("STOKER_OUTPUT_SOCKET", path)
    stoker._CONNECTION.reset()
    yield path
    stoker._CONNECTION.reset()
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture
def listener(sock_path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    server.settimeout(5.0)
    conns = []
    try:
        yield server, conns
    finally:
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        server.close()


def accept(listener_fixture):
    server, conns = listener_fixture
    conn, _ = server.accept()
    conns.append(conn)
    return conn


class TestRegistration:
    def test_file_stem_matches_outputmode(self):
        # Registry key is "output.<filename stem>" (eventgen_core.py
        # _initializePlugins), so outputMode = stoker needs stoker.py.
        assert os.path.basename(PLUGIN_FILE) == "stoker.py"
        assert os.path.isfile(PLUGIN_FILE)

    def test_engine_style_load(self):
        # Mirror _initializePlugins: import the file by path, call load().
        spec = importlib.util.spec_from_file_location("stoker_regtest", PLUGIN_FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = module.load()
        assert isinstance(plugin, type)
        assert issubclass(plugin, OutputPlugin)
        assert plugin.name == "stoker"
        assert isinstance(plugin.MAXQUEUELENGTH, int) and plugin.MAXQUEUELENGTH > 0
        assert plugin.useOutputQueue is False

    def test_batch_lifecycle_instance_per_batch(self, listener):
        # Output.flush builds a new instance per batch; both must share the
        # module-level connection (exactly one accept ever succeeds).
        server, _ = listener
        first = stoker.StokerOutputPlugin(FakeSample())
        first.flush([queue_item(raw="one")])
        conn = accept(listener)
        second = stoker.StokerOutputPlugin(FakeSample())
        second.flush([queue_item(raw="two")])
        lines = read_lines(conn, 2)
        assert json.loads(lines[0])["event"] == "one"
        assert json.loads(lines[1])["event"] == "two"
        server.settimeout(0.2)
        with pytest.raises(socket.timeout):
            server.accept()


class TestEnvelope:
    def test_shape_and_mapping(self, listener):
        plugin = stoker.StokerOutputPlugin(FakeSample())
        plugin.flush([queue_item(raw="hello world\n")])
        conn = accept(listener)
        (line,) = read_lines(conn, 1)
        envelope = json.loads(line.decode("utf-8"))
        assert list(envelope.keys()) == ENVELOPE_KEYS
        assert envelope["event"] == "hello world"  # trailing newline stripped
        assert envelope["host"] == "host-1"
        assert envelope["source"] == "src.sample"
        assert envelope["sourcetype"] == "stoker:test"
        assert envelope["index"] == "main"
        assert envelope["time"] == 1752234567.0
        assert isinstance(envelope["time"], float)
        assert "hostRegex" not in envelope
        assert "_raw" not in envelope

    def test_multiline_event_is_one_ndjson_line(self, listener):
        raw = "line one\nline two\nline three\n"
        plugin = stoker.StokerOutputPlugin(FakeSample())
        plugin.flush([queue_item(raw=raw), queue_item(raw="after")])
        conn = accept(listener)
        lines = read_lines(conn, 2)
        assert json.loads(lines[0])["event"] == "line one\nline two\nline three"
        assert json.loads(lines[1])["event"] == "after"

    def test_missing_metadata_becomes_null(self, listener):
        # Jinja items can omit any key except _raw/_time; sample metadata
        # can itself be None when the conf never sets it.
        plugin = stoker.StokerOutputPlugin(FakeSample())
        plugin.flush([{"_raw": "bare"}])
        conn = accept(listener)
        (line,) = read_lines(conn, 1)
        envelope = json.loads(line)
        assert envelope["event"] == "bare"
        for key in ("time", "host", "source", "sourcetype", "index"):
            assert envelope[key] is None

    def test_utf8(self, listener):
        plugin = stoker.StokerOutputPlugin(FakeSample())
        plugin.flush([queue_item(raw="température 30°C → ok")])
        conn = accept(listener)
        (line,) = read_lines(conn, 1)
        envelope = json.loads(line.decode("utf-8"))
        assert envelope["event"] == "température 30°C → ok"


class TestTimestampMapping:
    def test_int_epoch_default_generator(self):
        assert stoker._epoch_or_none(1752234567) == 1752234567.0
        assert isinstance(stoker._epoch_or_none(1752234567), float)

    def test_float_epoch_replay_generator(self):
        assert stoker._epoch_or_none(1752234567.123) == 1752234567.123

    def test_jinja_numeric_string(self):
        assert stoker._epoch_or_none("1752234567.5") == 1752234567.5

    def test_unusable_values_map_to_null(self):
        assert stoker._epoch_or_none(None) is None
        assert stoker._epoch_or_none("not a time") is None
        assert stoker._epoch_or_none(True) is None
        assert stoker._epoch_or_none([1]) is None
        assert stoker._epoch_or_none(float("nan")) is None
        assert stoker._epoch_or_none(float("inf")) is None

    def test_missing_time_key(self):
        envelope = json.loads(stoker._encode({"_raw": "x"}).decode("utf-8"))
        assert envelope["time"] is None


class TestFailureSemantics:
    def test_connect_failure_raises(self, sock_path):
        # Nothing is listening at sock_path.
        plugin = stoker.StokerOutputPlugin(FakeSample())
        with pytest.raises(stoker.StokerSocketError):
            plugin.flush([queue_item()])
        # Sticky: no reconnect attempts, every later flush raises too.
        with pytest.raises(stoker.StokerSocketError):
            plugin.flush([queue_item()])

    def test_error_escapes_eventgen_exception_nets(self):
        # Output.bulksend and _generator_do_work catch bare Exception;
        # the plugin's error must not be swallowed by either.
        assert not issubclass(stoker.StokerSocketError, Exception)
        assert issubclass(stoker.StokerSocketError, BaseException)

    def test_broken_pipe_mid_run_raises(self, listener):
        plugin = stoker.StokerOutputPlugin(FakeSample())
        plugin.flush([queue_item(raw="warmup")])
        conn = accept(listener)
        read_lines(conn, 1)
        conn.close()
        with pytest.raises(stoker.StokerSocketError):
            for _ in range(50):
                plugin.flush([queue_item(raw="x" * 4096)])
                time.sleep(0.01)
        assert stoker._CONNECTION.dead
        with pytest.raises(stoker.StokerSocketError):
            plugin.flush([queue_item()])


class TestBackpressure:
    def test_sender_blocks_until_server_reads(self, listener):
        plugin = stoker.StokerOutputPlugin(FakeSample())
        plugin.flush([queue_item(raw="warmup")])
        conn = accept(listener)
        read_lines(conn, 1)

        # Shrink the in-flight window: AF_UNIX stream capacity on Linux is
        # governed by the sender's SO_SNDBUF.
        stoker._CONNECTION.sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_SNDBUF, 8192
        )
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)

        n_events = 64
        batch = [queue_item(raw="p" * 32768) for _ in range(n_events)]
        done = threading.Event()

        def sender():
            plugin.flush(batch)
            done.set()

        thread = threading.Thread(target=sender, daemon=True)
        thread.start()
        # ~2 MiB against a ~16 KiB window: the blocking sendall must stall
        # while the server refuses to read.
        assert not done.wait(0.5), "flush completed with no reader draining"

        lines = read_lines(conn, n_events, timeout=30.0)
        assert done.wait(5.0), "flush did not finish after server drained"
        thread.join(5.0)
        assert len(lines) == n_events
        for line in lines:
            assert json.loads(line)["event"] == "p" * 32768

    def test_concurrent_flushes_do_not_interleave_lines(self, listener):
        plugin_class = stoker.StokerOutputPlugin
        n_threads, per_thread = 4, 25
        errors = []

        def worker(tag):
            plugin = plugin_class(FakeSample())
            try:
                for i in range(per_thread):
                    plugin.flush([queue_item(raw="t%d-%d|%s" % (tag, i, "z" * 512))])
            except BaseException as exc:  # surfaced via errors
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t,), daemon=True)
            for t in range(n_threads)
        ]
        for thread in threads:
            thread.start()
        conn = accept(listener)
        lines = read_lines(conn, n_threads * per_thread, timeout=20.0)
        for thread in threads:
            thread.join(5.0)
        assert errors == []
        seen = set()
        for line in lines:
            envelope = json.loads(line)  # raises if writes interleaved
            seen.add(envelope["event"].split("|")[0])
        expected = {
            "t%d-%d" % (t, i)
            for t in range(n_threads)
            for i in range(per_thread)
        }
        assert seen == expected
