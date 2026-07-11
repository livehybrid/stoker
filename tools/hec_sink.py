#!/usr/bin/env python3
"""HEC-compatible sink for testing Stoker workers.

Accepts Splunk HEC event POSTs, counts what it receives and reports totals
on /stats. Used by CI smoke jobs and by humans running the worker locally
without a real Splunk instance. Stdlib only, Python 3.9+.

Usage:
    python tools/hec_sink.py --port 18088 [--token TOKEN] [--verbose]

Endpoints:
    POST /services/collector/event  accept events (gzip aware, NDJSON or
                                    concatenated JSON, HEC-style responses)
    GET  /services/collector/health health probe, always 200 when up
    GET  /stats                     JSON counters

On SIGTERM or SIGINT the server shuts down cleanly and prints the final
counters as one JSON line on stdout.
"""
from __future__ import annotations

import argparse
import gzip
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

COLLECTOR_PATHS = (
    "/services/collector",
    "/services/collector/event",
    "/services/collector/event/1.0",
)


class Stats(object):
    """Thread-safe counters shared across handler threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.started = time.time()
        self.requests = 0
        self.events = 0
        self.bytes = 0
        self.rejected_requests = 0
        self.last_event_time = None  # type: Optional[float]

    def record_batch(self, n_events, n_bytes):
        with self._lock:
            self.requests += 1
            self.events += n_events
            self.bytes += n_bytes
            self.last_event_time = time.time()

    def record_rejected(self):
        with self._lock:
            self.requests += 1
            self.rejected_requests += 1

    def snapshot(self):
        with self._lock:
            return {
                "requests": self.requests,
                "events": self.events,
                "bytes": self.bytes,
                "rejected_requests": self.rejected_requests,
                "uptime_s": round(time.time() - self.started, 3),
                "last_event_time": self.last_event_time,
            }


def parse_events(text):
    # type: (str) -> List[dict]
    """Parse an HEC body: NDJSON or concatenated JSON objects.

    Raises ValueError on malformed JSON or a non-object payload.
    """
    decoder = json.JSONDecoder()
    events = []
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        obj, idx = decoder.raw_decode(text, idx)
        if not isinstance(obj, dict):
            raise ValueError("payload item is not a JSON object")
        events.append(obj)
    return events


class HecHandler(BaseHTTPRequestHandler):
    server_version = "hec-sink/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("hec-sink: %s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        # type: () -> Optional[bytes]
        """Read the request body; None means an error response was sent."""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._send_json(411, {"text": "Length Required", "code": 6})
            return None
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                self._send_json(400, {"text": "Invalid data format", "code": 6})
                return None
        return raw

    def _check_auth(self):
        # type: () -> bool
        token = getattr(self.server, "token", None)
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if not header:
            self._send_json(401, {"text": "Token is required", "code": 2})
            return False
        if header not in ("Splunk " + token, "Bearer " + token):
            self._send_json(403, {"text": "Invalid token", "code": 4})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/stats":
            self._send_json(200, self.server.stats.snapshot())
        elif path in ("/services/collector/health", "/services/collector/health/1.0"):
            self._send_json(200, {"text": "HEC is healthy", "code": 17})
        else:
            self._send_json(404, {"text": "Not Found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        stats = self.server.stats
        if path not in COLLECTOR_PATHS:
            self._send_json(404, {"text": "Not Found"})
            return
        # Body is read before the auth check so keep-alive connections
        # stay in sync after a 401/403.
        raw = self._read_body()
        if raw is None:
            stats.record_rejected()
            return
        if not self._check_auth():
            stats.record_rejected()
            return
        if not raw.strip():
            stats.record_rejected()
            self._send_json(400, {"text": "No data", "code": 5})
            return
        try:
            events = parse_events(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            stats.record_rejected()
            self._send_json(400, {"text": "Invalid data format", "code": 6})
            return
        for obj in events:
            if "event" not in obj:
                stats.record_rejected()
                self._send_json(400, {"text": "Event field is required", "code": 12})
                return
        stats.record_batch(len(events), len(raw))
        self._send_json(200, {"text": "Success", "code": 0})


def build_server(bind, port, token=None, verbose=False):
    # type: (str, int, Optional[str], bool) -> ThreadingHTTPServer
    server = ThreadingHTTPServer((bind, port), HecHandler)
    server.daemon_threads = True
    server.stats = Stats()
    server.token = token
    server.verbose = verbose
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="HEC-compatible counting sink")
    parser.add_argument("--bind", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8088, help="listen port (default 8088)")
    parser.add_argument("--token", default=None, help="require this HEC token (default: accept all)")
    parser.add_argument("--verbose", action="store_true", help="log every request to stderr")
    args = parser.parse_args(argv)

    server = build_server(args.bind, args.port, token=args.token, verbose=args.verbose)
    stop = threading.Event()

    def _handle_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sys.stderr.write(
        "hec-sink: listening on http://%s:%d (token %s)\n"
        % (args.bind, args.port, "required" if args.token else "not required")
    )
    sys.stderr.flush()

    stop.wait()
    server.shutdown()
    server.server_close()
    # Final counters on stdout so callers can capture them.
    print(json.dumps(server.stats.snapshot()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
