# -*- coding: utf-8 -*-
"""PISTON: the Stoker raw-replay engine.

An alternate worker engine to eventgen. Where eventgen *templates* events from
samples, Piston *replays* a recorded dataset byte-for-byte (e.g.
splunk/security_content attack_data captures), re-timestamped to now, at a chosen
rate (RATE mode) or reproducing the recorded cadence (CADENCE mode).

It speaks the exact agent socket protocol the eventgen ``stoker`` output plugin
speaks (see ``worker/engines/eventgen/.../plugins/output/stoker.py`` and
``docs/WORKER-CONTRACT.md`` "Unix socket protocol"): connect to
``STOKER_OUTPUT_SOCKET`` (AF_UNIX stream) and write one NDJSON envelope per line,

    {"time": <epoch float|null>, "host": null, "source": null,
     "sourcetype": null, "index": null, "event": "<raw line>"}

with blocking ``sendall`` (a stalled agent backpressures the engine). The agent
fills the null metadata from the run slice and does HEC delivery + pacing.

Modes (from ``STOKER_RAWREPLAY_MODE``):

* **rate** — emit ``time = null`` (the agent stamps *now*) and stream the dataset
  as fast as the socket accepts, looping from the top when the dataset is
  exhausted, until the socket closes on drain. The agent's token bucket delivers
  at the exact eps / per_day_gb share.
* **cadence** — self-paced: parse each line's timestamp, sleep the recorded delta
  ``x STOKER_RAWREPLAY_TIME_MULTIPLE`` and set ``time = now + cumulative_offset``
  so the replayed stream reproduces the original inter-event gaps. The agent does
  not gate in this mode (count_interval); the engine owns the pacing, matching the
  existing "replay is engine-paced, workers = 1" rule. The dataset plays once.

A connect failure at start is fatal (exit non-zero), like the eventgen plugin
(the agent always listens before launching the engine, so a failure to connect
means something is wrong and the run must not proceed silently).

Dependency-light: stdlib only.
"""

from __future__ import absolute_import

import gzip
import io
import json
import logging
import math
import os
import socket
import sys
import time

from .timestamps import TimestampParser

log = logging.getLogger("stoker.rawreplay")

MODE_RATE = "rate"
MODE_CADENCE = "cadence"

DEFAULT_SOCKET_PATH = "/tmp/stoker-output.sock"
DEFAULT_TIME_MULTIPLE = 1.0
# When a CADENCE line's timestamp cannot be parsed (or the delta is negative /
# non-finite) we advance by this fixed gap rather than stalling or time-travelling.
DEFAULT_FALLBACK_GAP_S = 0.1
# A connect at start may race the agent binding the listener; retry briefly. The
# agent binds before launching us, so this is a thin safety margin, not a wait.
_CONNECT_RETRY_S = 5.0
_CONNECT_RETRY_SLEEP_S = 0.02


class RawReplayError(Exception):
    """Fatal engine error (bad config, unreadable dataset, dead socket)."""


class Config(object):
    """Parsed environment contract for the rawreplay engine.

    Attributes:
        socket_path: STOKER_OUTPUT_SOCKET (AF_UNIX stream the agent listens on).
        dataset: STOKER_RAWREPLAY_DATASET, absolute path to the recorded dataset
            in the bundle (gzip-aware when it ends ``.gz``).
        mode: ``rate`` | ``cadence`` (STOKER_RAWREPLAY_MODE).
        time_multiple: cadence gap scale (STOKER_RAWREPLAY_TIME_MULTIPLE).
        ts_field: unused reserved hook (kept for parity with eventgen replay's
            ``timeField``); ts_regex is the operative override.
        ts_regex: optional regex whose first group is the timestamp text.
        ts_strptime: optional strptime format applied to the ts_regex capture.
        fallback_gap_s: cadence gap used when a line's timestamp is unparseable.
    """

    def __init__(self, socket_path, dataset, mode, time_multiple,
                 ts_field=None, ts_regex=None, ts_strptime=None,
                 fallback_gap_s=DEFAULT_FALLBACK_GAP_S):
        self.socket_path = socket_path
        self.dataset = dataset
        self.mode = mode
        self.time_multiple = time_multiple
        self.ts_field = ts_field
        self.ts_regex = ts_regex
        self.ts_strptime = ts_strptime
        self.fallback_gap_s = fallback_gap_s


def _get(env, key):
    # type: (dict, str) -> str
    val = env.get(key)
    if val is None:
        return None
    val = val.strip()
    return val if val else None


def load_config(env=None):
    # type: (dict) -> Config
    """Parse the rawreplay environment contract into a :class:`Config`.

    Raises :class:`RawReplayError` with a message naming the offending variable
    on any violation (mirrors the agent's ConfigError discipline).
    """
    if env is None:
        env = os.environ

    socket_path = _get(env, "STOKER_OUTPUT_SOCKET") or DEFAULT_SOCKET_PATH

    dataset = _get(env, "STOKER_RAWREPLAY_DATASET")
    if not dataset:
        raise RawReplayError("STOKER_RAWREPLAY_DATASET is required and not set")
    if not os.path.isfile(dataset):
        raise RawReplayError(
            "STOKER_RAWREPLAY_DATASET not found or not a file: %r" % dataset)

    mode = (_get(env, "STOKER_RAWREPLAY_MODE") or MODE_RATE).lower()
    if mode not in (MODE_RATE, MODE_CADENCE):
        raise RawReplayError(
            "STOKER_RAWREPLAY_MODE must be %r or %r, got %r"
            % (MODE_RATE, MODE_CADENCE, mode))

    tm_raw = _get(env, "STOKER_RAWREPLAY_TIME_MULTIPLE")
    time_multiple = DEFAULT_TIME_MULTIPLE
    if tm_raw is not None:
        try:
            time_multiple = float(tm_raw)
        except ValueError:
            raise RawReplayError(
                "STOKER_RAWREPLAY_TIME_MULTIPLE must be a number, got %r" % tm_raw)
        if time_multiple < 0:
            raise RawReplayError(
                "STOKER_RAWREPLAY_TIME_MULTIPLE must be >= 0, got %s" % time_multiple)

    fb_raw = _get(env, "STOKER_RAWREPLAY_FALLBACK_GAP_S")
    fallback_gap_s = DEFAULT_FALLBACK_GAP_S
    if fb_raw is not None:
        try:
            fallback_gap_s = float(fb_raw)
        except ValueError:
            raise RawReplayError(
                "STOKER_RAWREPLAY_FALLBACK_GAP_S must be a number, got %r" % fb_raw)
        if fallback_gap_s < 0:
            raise RawReplayError(
                "STOKER_RAWREPLAY_FALLBACK_GAP_S must be >= 0, got %s" % fallback_gap_s)

    return Config(
        socket_path=socket_path,
        dataset=dataset,
        mode=mode,
        time_multiple=time_multiple,
        ts_field=_get(env, "STOKER_RAWREPLAY_TS_FIELD"),
        ts_regex=_get(env, "STOKER_RAWREPLAY_TS_REGEX"),
        ts_strptime=_get(env, "STOKER_RAWREPLAY_TS_STRPTIME"),
        fallback_gap_s=fallback_gap_s,
    )


def _open_dataset(path):
    # type: (str) -> io.TextIOBase
    """Open a dataset for line iteration, transparently handling gzip.

    Returns a text-mode file object yielding ``str`` lines (UTF-8, replacing
    undecodable bytes so a binary-ish capture never kills the stream). ``.gz``
    datasets are decompressed on the fly.
    """
    if path.endswith(".gz"):
        raw = gzip.open(path, "rb")
    else:
        raw = open(path, "rb")
    # Wrap for text decoding with error replacement; newline="" keeps embedded
    # \r out of the way and lets us strip line endings ourselves.
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")


def _iter_lines(path):
    # type: (str) -> "generator"
    """Yield each raw line of the dataset without its trailing newline.

    Empty lines (blank or whitespace-only) are skipped: a recorded capture may
    have a trailing newline or blank separators, and an empty ``event`` is
    dropped by the agent's socket reader anyway.
    """
    fh = _open_dataset(path)
    try:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            yield line
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _encode(time_value, event):
    # type: (float, str) -> bytes
    """One NDJSON envelope line (UTF-8). Matches the eventgen stoker plugin.

    ``time`` is a finite epoch float or ``None`` (agent stamps now). Metadata
    fields are ``null``: the raw-replay engine never sets index/host/source/
    sourcetype; the agent fills them from the run slice's overrides/defaults.
    """
    if time_value is not None and not (isinstance(time_value, bool)):
        try:
            tv = float(time_value)
            if not math.isfinite(tv):
                tv = None
        except (TypeError, ValueError):
            tv = None
    else:
        tv = None
    envelope = {
        "time": tv,
        "host": None,
        "source": None,
        "sourcetype": None,
        "index": None,
        "event": event,
    }
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    try:
        return (text + "\n").encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates from binary-ish capture data: fall back to ASCII
        # escapes rather than kill the stream (mirrors the eventgen plugin).
        text = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


def connect(socket_path, deadline_s=_CONNECT_RETRY_S, clock=time.time,
            sleep=time.sleep):
    # type: (str, float, "callable", "callable") -> socket.socket
    """Connect to the agent's unix socket, retrying briefly for a startup race.

    The agent binds the listener before launching the engine, so a connect
    normally succeeds first try; the short retry only covers a launch/bind race.
    A persistent failure is fatal (raises :class:`RawReplayError`), exactly like
    the eventgen plugin's ``StokerSocketError`` at first use.
    """
    deadline = clock() + deadline_s
    last_exc = None
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(socket_path)
            return sock
        except OSError as exc:
            last_exc = exc
            try:
                sock.close()
            except OSError:
                pass
            if clock() >= deadline:
                # No secret in this message (socket path only).
                raise RawReplayError(
                    "cannot connect to agent socket %s: %s"
                    % (socket_path, last_exc))
            sleep(_CONNECT_RETRY_SLEEP_S)


class RawReplayEngine(object):
    """Streams a dataset to the agent socket in RATE or CADENCE mode.

    The engine is deliberately synchronous and single-threaded: one blocking
    ``sendall`` per line is the backpressure mechanism (a stalled agent stalls
    the loop, so no batch can ever be buffered without bound). ``run`` returns
    when the socket closes (RATE drain) or the dataset is exhausted (CADENCE), or
    raises :class:`RawReplayError` on a fatal socket error.
    """

    def __init__(self, config, clock=time.time, sleep=time.sleep):
        # type: (Config, "callable", "callable") -> None
        self._cfg = config
        self._clock = clock
        self._sleep = sleep
        self.emitted = 0  # events written (observable for tests/logging)

    def run(self):
        # type: () -> int
        cfg = self._cfg
        sock = connect(cfg.socket_path, clock=self._clock, sleep=self._sleep)
        log.info("rawreplay connected to %s; mode=%s dataset=%s time_multiple=%s",
                 cfg.socket_path, cfg.mode, cfg.dataset, cfg.time_multiple)
        try:
            if cfg.mode == MODE_RATE:
                self._run_rate(sock)
            else:
                self._run_cadence(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        log.info("rawreplay finished; emitted %d events", self.emitted)
        return 0

    # -- rate ------------------------------------------------------------- #

    def _run_rate(self, sock):
        # type: (socket.socket) -> None
        """Loop the dataset HOT with ``time = null`` until the socket closes.

        The agent stamps *now* on every envelope and its token bucket paces
        delivery at the exact share; the dataset loops to fill the run duration.
        A closed socket (``BrokenPipeError`` / ``EPIPE``) is the normal end of a
        RATE run (the agent stops reading on drain), so it is swallowed, not
        raised: the run completed, it was not a failure.
        """
        cfg = self._cfg
        while True:
            produced = 0
            for line in _iter_lines(cfg.dataset):
                produced += 1
                if not self._send(sock, _encode(None, line)):
                    return  # socket closed: drain complete
            if produced == 0:
                # An empty dataset in RATE mode would spin forever; stop instead.
                log.warning("rawreplay dataset %s yielded no events; stopping",
                            cfg.dataset)
                return

    # -- cadence ---------------------------------------------------------- #

    def _run_cadence(self, sock):
        # type: (socket.socket) -> None
        """Reproduce the recorded inter-event gaps, once, engine-paced.

        The first event anchors ``base = now``; each subsequent event sleeps
        ``(ts - prev_ts) x time_multiple`` (clamped to >= 0 via the fallback gap
        when the delta is negative, non-finite or unparseable) and is stamped
        ``time = base + cumulative_offset`` so the replayed timeline is contiguous
        from the run's start instant. The agent does not gate in this mode.
        """
        cfg = self._cfg
        parser = TimestampParser(regex=cfg.ts_regex, strptime_fmt=cfg.ts_strptime)
        tm = cfg.time_multiple
        base = None            # wall-clock anchor (now at first event)
        cumulative = 0.0       # seconds since base for the current event
        prev_ts = None         # last parsed epoch (for delta computation)
        parsed_any = False

        for line in _iter_lines(cfg.dataset):
            ts = parser.parse(line)
            if ts is not None:
                parsed_any = True

            if base is None:
                # First event: anchor now, offset 0.
                base = self._clock()
                cumulative = 0.0
            else:
                gap = self._gap(prev_ts, ts, tm, cfg.fallback_gap_s)
                if gap > 0:
                    self._sleep(gap)
                cumulative += gap

            if ts is not None:
                prev_ts = ts

            envelope = _encode(base + cumulative, line)
            if not self._send(sock, envelope):
                return  # socket closed early (drain): stop

        if not parsed_any:
            log.warning(
                "rawreplay cadence: no timestamps parsed in %s; used the fixed "
                "fallback gap (%.3fs) throughout. Supply STOKER_RAWREPLAY_TS_REGEX "
                "to pace by the recorded cadence.",
                cfg.dataset, cfg.fallback_gap_s)

    @staticmethod
    def _gap(prev_ts, ts, time_multiple, fallback_gap_s):
        # type: (float, float, float, float) -> float
        """The sleep/offset gap for a cadence step (never negative or NaN).

        Uses the recorded delta ``(ts - prev_ts) * time_multiple`` when both
        timestamps are known and the delta is finite and non-negative; otherwise
        the fixed fallback gap (also scaled by time_multiple so a slow/fast replay
        stays proportional). ``time_multiple = 0`` collapses every gap to 0 (emit
        as fast as the socket accepts but still cadence-stamped).
        """
        if time_multiple == 0:
            return 0.0
        if prev_ts is None or ts is None:
            return fallback_gap_s * time_multiple
        delta = (ts - prev_ts) * time_multiple
        if not math.isfinite(delta) or delta < 0:
            # Out-of-order or unparseable neighbour: do not time-travel or stall.
            return fallback_gap_s * time_multiple
        return delta

    # -- socket ----------------------------------------------------------- #

    def _send(self, sock, data):
        # type: (socket.socket, bytes) -> bool
        """Blocking write of one envelope. Returns False when the socket closed.

        A closed peer (BrokenPipe / EPIPE / connection reset) means the agent has
        stopped reading (drain): a clean end, so we return False to unwind the
        loop rather than raising. Any other OSError is a genuine fault and is
        raised as :class:`RawReplayError` (non-zero exit).
        """
        try:
            sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            return False
        except OSError as exc:
            # EPIPE can surface as a bare OSError on some platforms.
            import errno
            if exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ESHUTDOWN):
                return False
            raise RawReplayError("agent socket write failed: %s" % exc)
        self.emitted += 1
        return True


def main(argv=None, env=None):
    # type: (list, dict) -> int
    """``python -m stoker_rawreplay`` entrypoint.

    Exit codes: 0 clean (dataset streamed / socket drained), non-zero on a fatal
    config or socket error (so the agent's engine-exit path drains the run).
    """
    level = (env or os.environ).get("STOKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        cfg = load_config(env)
    except RawReplayError as exc:
        sys.stderr.write("stoker-rawreplay: config error: %s\n" % exc)
        return 2
    try:
        return RawReplayEngine(cfg).run()
    except RawReplayError as exc:
        sys.stderr.write("stoker-rawreplay: %s\n" % exc)
        return 1
