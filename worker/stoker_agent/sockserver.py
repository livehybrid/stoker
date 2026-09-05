"""Unix socket listener: the agent side of the plugin protocol.

Accepts CONCURRENT connections (an engine restart reconnects; a multi-process
engine opens one per generator process), each served by its own reader thread.
Every reader does the same thing: read NDJSON envelopes, fill null metadata from
the slice, gate each event on the shared token bucket (skipped in count_interval
mode) and hand it to hec.put().

Backpressure is structural and unchanged by the concurrency: while the bucket is
paused or hec.put blocks, that reader stops recv()ing, its kernel buffer fills
and the plugin's blocking write stalls the producer behind it. The bucket is the
single thing that paces, so N producers still deliver exactly the run's rate --
the extra connections buy parallel GENERATION (eventgen is GIL-bound, so one
generator process is one core), not extra throughput past the bucket.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

from .pacing import TokenBucket
from .slice import SpecSlice

log = logging.getLogger("stoker.sock")

_META_FIELDS = ("index", "sourcetype", "source", "host")
_MAX_BUFFER = 4 * 1024 * 1024  # discard pathological unterminated lines
# Listener backlog. Must exceed 1: a multi-process engine opens one connection
# per generator process and they arrive together, so a backlog of 1 would refuse
# (or stall) every producer after the first.
_ACCEPT_BACKLOG = 64


def make_filler(spec):
    # type: (SpecSlice) -> Callable[[Dict[str, Any]], Dict[str, Any]]
    """Envelope metadata filler: run-declared overrides win over plugin
    values; slice hec defaults fill remaining nulls; None values are left
    for the HEC client to omit."""
    overrides = dict(spec.overrides)
    defaults = spec.hec_defaults()

    def fill(envelope):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        if envelope.get("time") is None:
            envelope["time"] = time.time()
        for field in _META_FIELDS:
            if field in overrides:
                envelope[field] = overrides[field]
            elif envelope.get(field) is None and defaults.get(field) is not None:
                envelope[field] = defaults[field]
        return envelope

    return fill


class SocketServer(object):
    """Listener thread for STOKER_OUTPUT_SOCKET."""

    def __init__(self, path, hec, bucket, filler, gated=True):
        # type: (str, Any, TokenBucket, Callable[[Dict[str, Any]], Dict[str, Any]], bool) -> None
        self._path = path
        self._hec = hec
        self._bucket = bucket
        self._fill = filler
        self._gated = gated
        self._stop = threading.Event()
        self._listener = None  # type: Optional[socket.socket]
        # Every live connection and its reader thread. CONCURRENT by design: the
        # engine may be several processes (eventgen `threading = process` forks a
        # generator process per worker, and each opens its OWN connection because
        # the output plugin's socket lives at module level). Serving one
        # connection at a time would accept exactly one generator and leave the
        # rest blocked on a socket nobody reads -- i.e. silently capped at one
        # producer. Each reader is independent; they share the token bucket (which
        # is the thing that actually paces) and the HEC queue.
        self._conns = []       # type: list
        self._conn_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="stoker-sock",
                                        daemon=True)
        self.received = 0
        self.malformed = 0

    def start(self):
        # Bind before returning so the engine can never race the listener.
        if os.path.exists(self._path):
            os.unlink(self._path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self._path)
        # Backlog for several concurrent engine processes, not 1.
        listener.listen(_ACCEPT_BACKLOG)
        listener.settimeout(0.5)
        self._listener = listener
        self._thread.start()

    def stop(self, join_timeout_s=5.0):
        # type: (float) -> None
        """Stop reading: pending unreleased socket data is intentionally
        dropped on drain (only the HEC queue is flushed, per contract).

        join_timeout_s bounds the reader join so the caller's drain deadline is
        honoured (the agent clamps it against the remaining drain budget). Every
        connection is shut down and every reader joined within that one budget."""
        self._stop.set()
        with self._conn_lock:
            conns = [c for c, _t in self._conns]
            threads = [t for _c, t in self._conns]
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        deadline = time.monotonic() + max(0.0, join_timeout_s)
        if self._thread.is_alive():
            self._thread.join(max(0.0, deadline - time.monotonic()))
        for thread in threads:
            if thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
        if os.path.exists(self._path):
            try:
                os.unlink(self._path)
            except OSError:
                pass

    def is_alive(self):
        return self._thread.is_alive()

    @property
    def connections(self):
        # type: () -> int
        """Live engine connections (>1 when the engine runs multi-process)."""
        with self._conn_lock:
            return len(self._conns)

    # -- internals -------------------------------------------------------

    def _run(self):
        """Accept loop: one reader thread per connection."""
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return  # listener closed by stop()
                thread = threading.Thread(
                    target=self._serve, args=(conn,),
                    name="stoker-sock-conn", daemon=True)
                with self._conn_lock:
                    self._conns.append((conn, thread))
                thread.start()
        finally:
            try:
                self._listener.close()
            except OSError:
                pass

    def _inc_received(self, n=1):
        # type: (int) -> None
        with self._counter_lock:
            self.received += n

    def _inc_malformed(self, n=1):
        # type: (int) -> None
        with self._counter_lock:
            self.malformed += n

    def _serve(self, conn):
        # type: (socket.socket) -> None
        """Read one connection to EOF (or drain), then retire it."""
        try:
            self._read_stream(conn)
        finally:
            with self._conn_lock:
                self._conns = [(c, t) for (c, t) in self._conns if c is not conn]
            try:
                conn.close()
            except OSError:
                pass

    def _read_stream(self, conn):
        # type: (socket.socket) -> None
        conn.settimeout(0.5)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                # EOF: flush any final unterminated line, then wait for a
                # reconnect (engine restart) via the accept loop.
                if buf.strip():
                    self._handle_line(buf)
                return
            buf += chunk
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    break
                line, buf = buf[:idx], buf[idx + 1:]
                if not self._handle_line(line):
                    return  # bucket closed: draining
            if len(buf) > _MAX_BUFFER:
                log.warning("discarding %d bytes of unterminated data", len(buf))
                self._inc_malformed()
                buf = b""

    def _handle_line(self, line):
        # type: (bytes) -> bool
        """Process one NDJSON line. Returns False only when draining."""
        line = line.strip()
        if not line:
            return True
        try:
            envelope = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._inc_malformed()
            return True
        if not isinstance(envelope, dict) or envelope.get("event") is None:
            self._inc_malformed()
            return True
        envelope = self._fill(envelope)
        if self._gated:
            if not self._bucket.acquire():
                return False  # closed for drain: drop and stop reading
        elif self._bucket.closed:
            return False
        try:
            self._hec.put(envelope)
        except RuntimeError:
            return False  # hec stopped during drain
        self._inc_received()
        return True
