#!/usr/bin/env python3
"""A throughput-oriented HEC sink for benchmarks.

Unlike tools/hec_sink.py (which parses every event to validate it), this one
only decompresses the body and counts events by newlines, so it can absorb far
more than one worker can send and never becomes the bottleneck being measured.
Same /stats shape as hec_sink.py (events, bytes, requests), so
tools/bench_engines.py can use either (``--sink fast``). Stdlib only.

    python tools/fast_sink.py --port 18089 [--token TOKEN]
"""
from __future__ import annotations

import argparse
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Stats(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.events = self.bytes = self.requests = self.rejected_requests = 0


def build_server(port, token=None, bind="127.0.0.1"):
    stats = _Stats()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/stats"):
                with stats.lock:
                    self._reply(200, {"events": stats.events, "bytes": stats.bytes,
                                      "requests": stats.requests,
                                      "rejected_requests": stats.rejected_requests})
            else:
                self._reply(200, {"text": "HEC is healthy", "code": 17})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if token and self.headers.get("Authorization") != "Splunk " + token:
                with stats.lock:
                    stats.requests += 1
                    stats.rejected_requests += 1
                self._reply(401, {"text": "Invalid token", "code": 4})
                return
            if self.headers.get("Content-Encoding") == "gzip":
                body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
            n = body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)
            with stats.lock:
                stats.requests += 1
                stats.events += n
                stats.bytes += len(body)
            self._reply(200, {"text": "Success", "code": 0})

    return ThreadingHTTPServer((bind, port), Handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=18089)
    ap.add_argument("--token", default=None)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args(argv)
    server = build_server(args.port, args.token, args.bind)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
