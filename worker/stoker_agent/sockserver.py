"""Unix socket listener: the agent side of the plugin protocol.

Accepts one connection at a time (engine restarts reconnect), reads NDJSON
envelopes, fills null metadata from the slice, gates each event on the
token bucket (skipped in count_interval mode) and hands it to hec.put().
Backpressure is structural: while the bucket is paused or hec.put blocks,
the reader stops recv()ing, the kernel buffer fills and the plugin's
blocking write stalls the engine.
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
        self._conn = None      # type: Optional[socket.socket]
        self._conn_lock = threading.Lock()
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
        listener.listen(1)
        listener.settimeout(0.5)
        self._listener = listener
        self._thread.start()

    def stop(self):
        """Stop reading: pending unreleased socket data is intentionally
        dropped on drain (only the HEC queue is flushed, per contract)."""
        self._stop.set()
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        if self._thread.is_alive():
            self._thread.join(5.0)
        if os.path.exists(self._path):
            try:
                os.unlink(self._path)
            except OSError:
                pass

    def is_alive(self):
        return self._thread.is_alive()

    # -- internals -------------------------------------------------------

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return  # listener closed by stop()
                with self._conn_lock:
                    self._conn = conn
                try:
                    self._read_stream(conn)
                finally:
                    with self._conn_lock:
                        self._conn = None
                    try:
                        conn.close()
                    except OSError:
                        pass
        finally:
            try:
                self._listener.close()
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
                self.malformed += 1
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
            self.malformed += 1
            return True
        if not isinstance(envelope, dict) or envelope.get("event") is None:
            self.malformed += 1
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
        self.received += 1
        return True
