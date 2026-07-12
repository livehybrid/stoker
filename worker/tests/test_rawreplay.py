"""Unit tests for the PISTON raw-replay engine (worker/engines/rawreplay).

The engine is driven directly against a real AF_UNIX stream socket (an agent
stand-in), asserting the exact NDJSON envelope protocol the eventgen ``stoker``
output plugin speaks:

* RATE mode: ``time = null`` on every envelope, the dataset loops to fill the
  socket, and the loop stops cleanly when the socket closes (drain);
* CADENCE mode: the recorded inter-event gaps are reproduced ``x time_multiple``
  and each envelope carries ``time = base + cumulative_offset`` (monotonic);
* gzip datasets stream identically to plaintext;
* a connect failure at start is fatal (non-zero), like the eventgen plugin;
* the config parser rejects a bad contract, naming the offending variable.

The engine tree is added to sys.path exactly as the eventgen tests add theirs.
"""
from __future__ import annotations

import gzip
import json
import os
import socket
import sys
import threading
import time

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
RAWREPLAY_DIR = os.path.join(os.path.dirname(TESTS_DIR), "engines", "rawreplay")
if RAWREPLAY_DIR not in sys.path:
    sys.path.insert(0, RAWREPLAY_DIR)

from stoker_rawreplay.engine import (  # noqa: E402
    MODE_CADENCE,
    MODE_RATE,
    Config,
    RawReplayEngine,
    RawReplayError,
    connect,
    load_config,
)
from stoker_rawreplay.timestamps import TimestampParser  # noqa: E402

ENVELOPE_KEYS = {"time", "host", "source", "sourcetype", "index", "event"}


# --------------------------------------------------------------------------- #
# A socket collector: the agent side. Binds first (as the real agent does),
# accepts one connection, records every NDJSON envelope, and can close the
# connection after N events to exercise the RATE drain path.
# --------------------------------------------------------------------------- #

class Collector(object):
    def __init__(self, path, close_after=None):
        self.path = path
        self.close_after = close_after
        self.events = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(path)
        self._listener.listen(1)
        self._listener.settimeout(10.0)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._closed_conn = threading.Event()

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        try:
            conn, _ = self._listener.accept()
        except socket.timeout:
            return
        conn.settimeout(10.0)
        buf = b""
        try:
            while True:
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    self.events.append(json.loads(line.decode("utf-8")))
                    if (self.close_after is not None
                            and len(self.events) >= self.close_after):
                        try:
                            conn.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        conn.close()
                        self._closed_conn.set()
                        return
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._closed_conn.set()

    def join(self, timeout=10.0):
        self._thread.join(timeout)

    def close(self):
        try:
            self._listener.close()
        except OSError:
            pass


def write_dataset(path, lines, gzipped=False):
    body = "".join(l + "\n" for l in lines)
    if gzipped:
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(body)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    return path


ISO_LINES = [
    "2026-07-10T08:00:00.000Z event A",
    "2026-07-10T08:00:02.000Z event B",
    "2026-07-10T08:00:05.000Z event C",
]


# --------------------------------------------------------------------------- #
# RATE mode
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(30)
def test_rate_emits_null_time_envelopes(tmp_path):
    ds = write_dataset(str(tmp_path / "data.log"), ISO_LINES)
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=3).start()
    time.sleep(0.1)

    RawReplayEngine(Config(sock, ds, MODE_RATE, 1.0)).run()
    coll.join()

    assert len(coll.events) == 3
    for env in coll.events:
        assert set(env.keys()) == ENVELOPE_KEYS
        # RATE: the agent stamps now, so the engine emits null time and null meta.
        assert env["time"] is None
        assert env["host"] is None
        assert env["source"] is None
        assert env["sourcetype"] is None
        assert env["index"] is None
    assert [e["event"] for e in coll.events] == ISO_LINES
    coll.close()


@pytest.mark.timeout(30)
def test_rate_loops_dataset_until_socket_closes(tmp_path):
    # 3-line dataset, collector closes after 7 events -> must have looped.
    ds = write_dataset(str(tmp_path / "data.log"), ISO_LINES)
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=7).start()
    time.sleep(0.1)

    rc = RawReplayEngine(Config(sock, ds, MODE_RATE, 1.0)).run()
    coll.join()

    assert rc == 0
    assert len(coll.events) == 7
    events = [e["event"] for e in coll.events]
    # event A recorded at least twice: the dataset wrapped from the top.
    assert events.count("2026-07-10T08:00:00.000Z event A") >= 2
    # order preserved within each pass.
    assert events[:3] == ISO_LINES
    coll.close()


@pytest.mark.timeout(30)
def test_rate_returns_cleanly_on_broken_pipe(tmp_path):
    """A closed peer mid-run (drain) is a clean end, not a raised error."""
    ds = write_dataset(str(tmp_path / "data.log"), ["x %d" % i for i in range(500)])
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=5).start()
    time.sleep(0.1)

    rc = RawReplayEngine(Config(sock, ds, MODE_RATE, 1.0)).run()  # must not raise
    coll.join()
    assert rc == 0
    assert len(coll.events) == 5
    coll.close()


@pytest.mark.timeout(30)
def test_rate_empty_dataset_does_not_spin(tmp_path):
    ds = write_dataset(str(tmp_path / "empty.log"), [])  # no lines
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock).start()
    time.sleep(0.1)
    # Would loop forever if not guarded; the timeout marker would catch a hang.
    rc = RawReplayEngine(Config(sock, ds, MODE_RATE, 1.0)).run()
    coll.close()
    coll.join()
    assert rc == 0
    assert coll.events == []


# --------------------------------------------------------------------------- #
# CADENCE mode
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(30)
def test_cadence_spaces_by_recorded_delta_times_multiple(tmp_path):
    ds = write_dataset(str(tmp_path / "data.log"), ISO_LINES)
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=3).start()
    time.sleep(0.1)

    # x0.1: gaps 2s->0.2s and 3s->0.3s; total run ~0.5s.
    t0 = time.monotonic()
    RawReplayEngine(Config(sock, ds, MODE_CADENCE, 0.1)).run()
    elapsed = time.monotonic() - t0
    coll.join()

    assert len(coll.events) == 3
    times = [e["time"] for e in coll.events]
    assert all(t is not None for t in times)
    # Monotonic, contiguous from a "now" anchor.
    assert times[0] <= times[1] <= times[2]
    # The stamped offsets reflect the scaled recorded deltas (tolerant of jitter).
    assert abs((times[1] - times[0]) - 0.2) < 0.08
    assert abs((times[2] - times[1]) - 0.3) < 0.08
    # Wall time actually slept ~0.5s (the engine is self-paced in cadence mode).
    assert 0.4 <= elapsed < 2.0
    coll.close()


@pytest.mark.timeout(30)
def test_cadence_time_multiple_zero_emits_hot_but_stamped(tmp_path):
    ds = write_dataset(str(tmp_path / "data.log"), ISO_LINES)
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=3).start()
    time.sleep(0.1)

    t0 = time.monotonic()
    RawReplayEngine(Config(sock, ds, MODE_CADENCE, 0.0)).run()
    elapsed = time.monotonic() - t0
    coll.join()

    assert len(coll.events) == 3
    # time_multiple=0 collapses every gap: all three stamped at the same instant.
    times = [e["time"] for e in coll.events]
    assert times[0] == times[1] == times[2]
    assert elapsed < 1.0  # no sleeping
    coll.close()


@pytest.mark.timeout(30)
def test_cadence_unparseable_timestamps_use_fallback_gap(tmp_path):
    # No parseable timestamps -> fixed fallback gap x multiple between events.
    ds = write_dataset(str(tmp_path / "data.log"),
                       ["no timestamp one", "no timestamp two", "no timestamp three"])
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=3).start()
    time.sleep(0.1)

    cfg = Config(sock, ds, MODE_CADENCE, 1.0, fallback_gap_s=0.05)
    t0 = time.monotonic()
    RawReplayEngine(cfg).run()
    elapsed = time.monotonic() - t0
    coll.join()

    assert len(coll.events) == 3
    times = [e["time"] for e in coll.events]
    # Two fallback gaps of 0.05s each between the three events.
    assert abs((times[1] - times[0]) - 0.05) < 0.04
    assert abs((times[2] - times[1]) - 0.05) < 0.04
    assert elapsed < 1.0
    coll.close()


@pytest.mark.timeout(30)
def test_cadence_out_of_order_does_not_time_travel(tmp_path):
    # Second event is EARLIER than the first: a negative delta must clamp to the
    # fallback gap, never a negative sleep or a backwards timestamp.
    lines = [
        "2026-07-10T08:00:05.000Z later first",
        "2026-07-10T08:00:00.000Z earlier second",
        "2026-07-10T08:00:06.000Z later third",
    ]
    ds = write_dataset(str(tmp_path / "data.log"), lines)
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=3).start()
    time.sleep(0.1)

    cfg = Config(sock, ds, MODE_CADENCE, 0.1, fallback_gap_s=0.02)
    RawReplayEngine(cfg).run()
    coll.join()

    times = [e["time"] for e in coll.events]
    assert times[0] <= times[1] <= times[2]  # never goes backwards
    coll.close()


# --------------------------------------------------------------------------- #
# gzip datasets
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(30)
def test_gzip_dataset_streams_like_plaintext(tmp_path):
    ds = write_dataset(str(tmp_path / "data.log.gz"),
                       ["gz event 1", "gz event 2", "gz event 3"], gzipped=True)
    sock = str(tmp_path / "out.sock")
    coll = Collector(sock, close_after=3).start()
    time.sleep(0.1)

    RawReplayEngine(Config(sock, ds, MODE_RATE, 1.0)).run()
    coll.join()

    assert [e["event"] for e in coll.events] == ["gz event 1", "gz event 2", "gz event 3"]
    for env in coll.events:
        assert env["time"] is None
        assert set(env.keys()) == ENVELOPE_KEYS
    coll.close()


# --------------------------------------------------------------------------- #
# Connect failure is fatal (like the eventgen plugin's StokerSocketError)
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(15)
def test_connect_to_missing_socket_is_fatal():
    with pytest.raises(RawReplayError):
        connect("/nonexistent/definitely/not/here.sock", deadline_s=0.1)


@pytest.mark.timeout(15)
def test_run_against_missing_socket_raises(tmp_path):
    ds = write_dataset(str(tmp_path / "data.log"), ISO_LINES)
    cfg = Config(str(tmp_path / "no.sock"), ds, MODE_RATE, 1.0)
    eng = RawReplayEngine(cfg)
    # A fatal connect is raised; the entrypoint maps it to a non-zero exit.
    with pytest.raises(RawReplayError):
        eng.run()


# --------------------------------------------------------------------------- #
# Config parsing (the STOKER_RAWREPLAY_* env contract)
# --------------------------------------------------------------------------- #

def test_load_config_requires_dataset():
    with pytest.raises(RawReplayError):
        load_config({"STOKER_OUTPUT_SOCKET": "/tmp/x.sock"})


def test_load_config_rejects_missing_dataset_file():
    with pytest.raises(RawReplayError):
        load_config({"STOKER_RAWREPLAY_DATASET": "/nope/missing.log"})


def test_load_config_rejects_bad_mode(tmp_path):
    ds = write_dataset(str(tmp_path / "d.log"), ISO_LINES)
    with pytest.raises(RawReplayError):
        load_config({"STOKER_RAWREPLAY_DATASET": ds,
                     "STOKER_RAWREPLAY_MODE": "sideways"})


def test_load_config_rejects_bad_time_multiple(tmp_path):
    ds = write_dataset(str(tmp_path / "d.log"), ISO_LINES)
    with pytest.raises(RawReplayError):
        load_config({"STOKER_RAWREPLAY_DATASET": ds,
                     "STOKER_RAWREPLAY_TIME_MULTIPLE": "fast"})
    with pytest.raises(RawReplayError):
        load_config({"STOKER_RAWREPLAY_DATASET": ds,
                     "STOKER_RAWREPLAY_TIME_MULTIPLE": "-1"})


def test_load_config_defaults_to_rate_mode(tmp_path):
    ds = write_dataset(str(tmp_path / "d.log"), ISO_LINES)
    cfg = load_config({"STOKER_RAWREPLAY_DATASET": ds})
    assert cfg.mode == MODE_RATE
    assert cfg.time_multiple == 1.0


def test_load_config_full(tmp_path):
    ds = write_dataset(str(tmp_path / "d.log"), ISO_LINES)
    cfg = load_config({
        "STOKER_OUTPUT_SOCKET": "/tmp/agent.sock",
        "STOKER_RAWREPLAY_DATASET": ds,
        "STOKER_RAWREPLAY_MODE": "cadence",
        "STOKER_RAWREPLAY_TIME_MULTIPLE": "2.5",
        "STOKER_RAWREPLAY_TS_REGEX": r"^(\S+)",
        "STOKER_RAWREPLAY_TS_STRPTIME": "%Y-%m-%dT%H:%M:%S",
        "STOKER_RAWREPLAY_TS_FIELD": "_time",
    })
    assert cfg.socket_path == "/tmp/agent.sock"
    assert cfg.mode == MODE_CADENCE
    assert cfg.time_multiple == 2.5
    assert cfg.ts_regex == r"^(\S+)"
    assert cfg.ts_strptime == "%Y-%m-%dT%H:%M:%S"
    assert cfg.ts_field == "_time"


# --------------------------------------------------------------------------- #
# Timestamp parser (best-effort, stdlib only)
# --------------------------------------------------------------------------- #

def test_parser_iso8601_with_fraction_and_zone():
    p = TimestampParser()
    a = p.parse("2026-07-10T08:00:00.000Z gw=edge")
    b = p.parse("2026-07-10T08:00:13.137Z gw=edge")
    assert a is not None and b is not None
    assert abs((b - a) - 13.137) < 1e-3
    # +01:00 wall clock is one hour earlier in UTC than the same clock at Z.
    z = p.parse("2026-07-10 07:00:00Z x")
    off = p.parse("2026-07-10 08:00:00+01:00 x")
    assert abs(z - off) < 1e-6


def test_parser_syslog_and_epoch():
    p = TimestampParser()
    assert p.parse("Jul 10 08:00:00 host sshd[1]: hi") is not None
    assert p.parse("1752134400 event") == 1752134400.0
    assert abs(p.parse("1752134400123 event") - 1752134400.123) < 1e-6


def test_parser_ignores_non_timestamp_digit_runs():
    p = TimestampParser()
    # A 32-char request id must not be misread as an epoch.
    assert p.parse("request_id=c700d84c6dd1ab79c2a764c319613698 no ts") is None
    assert p.parse("no timestamp anywhere here") is None


def test_parser_user_regex_with_strptime():
    p = TimestampParser(regex=r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
                        strptime_fmt="%Y-%m-%dT%H:%M:%S")
    got = p.parse("foo ts=2026-07-10T08:00:00 bar")
    # 2026-07-10T08:00:00 UTC
    from datetime import datetime, timezone
    expect = datetime(2026, 7, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(got - expect) < 1e-6
    assert p.parse("no ts field here") is None
