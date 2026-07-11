"""Reusable in-process HEC sink for tests.

A threaded stdlib HTTP server that mimics the Splunk HTTP Event Collector
event endpoint: decodes gzip bodies, parses NDJSON envelopes, checks the
Authorization header and replays a scripted sequence of responses.

Usage:

    with HecSink(token="secret", responses=[503, 200]) as sink:
        client = HecClient(sink.url, "secret")
        ...
        sink.wait_for_events(10)

``responses`` entries are either an int status or an (int, dict) tuple with
an explicit JSON body. The script is consumed one entry per authenticated
request; once exhausted every request gets 200. Requests failing the auth
check get 401 without consuming the script.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

_DEFAULT_BODIES = {
    200: {"text": "Success", "code": 0},
    400: {"text": "Invalid data format", "code": 6},
    401: {"text": "Invalid token", "code": 4},
    403: {"text": "Token disabled", "code": 1},
    500: {"text": "Internal server error", "code": 8},
    503: {"text": "Server is busy", "code": 9},
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class HecSink(object):
    """Records one dict per POST in ``records``:
    {"path", "status", "gzip", "auth_ok", "raw", "events"} where ``raw`` is
    the uncompressed body bytes and ``events`` the decoded NDJSON dicts.
    """

    def __init__(self, token=None, responses=None, delay_s=0.0):
        # type: (Optional[str], Optional[list], float) -> None
        self.token = token
        self.delay_s = delay_s
        self._responses = list(responses or [])
        self._lock = threading.Lock()
        self.records = []  # type: List[Dict[str, Any]]
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                sink._handle(self)

            def log_message(self, fmt, *args):
                pass

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="hec-sink", daemon=True
        )

    # -- lifecycle -------------------------------------------------------

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    # -- inspection --------------------------------------------------------

    @property
    def url(self):
        # type: () -> str
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    @property
    def request_count(self):
        # type: () -> int
        with self._lock:
            return len(self.records)

    @property
    def events(self):
        # type: () -> List[Dict[str, Any]]
        with self._lock:
            return [ev for rec in self.records for ev in rec["events"]]

    def wait_for_requests(self, n, timeout_s=5.0):
        # type: (int, float) -> bool
        return self._wait(lambda: self.request_count >= n, timeout_s)

    def wait_for_events(self, n, timeout_s=5.0):
        # type: (int, float) -> bool
        return self._wait(lambda: len(self.events) >= n, timeout_s)

    @staticmethod
    def _wait(predicate, timeout_s):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    # -- request handling ------------------------------------------------

    def _next_response(self):
        with self._lock:
            entry = self._responses.pop(0) if self._responses else 200
        if isinstance(entry, tuple):
            status, body = entry
        else:
            status = entry
            body = _DEFAULT_BODIES.get(
                status, {"text": "HTTP %d" % status, "code": -1}
            )
        return status, body

    def _handle(self, handler):
        if self.delay_s:
            time.sleep(self.delay_s)
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length)
        is_gzip = handler.headers.get("Content-Encoding") == "gzip"
        body = gzip.decompress(raw) if is_gzip else raw
        events = []
        for line in body.split(b"\n"):
            if line.strip():
                events.append(json.loads(line.decode("utf-8")))

        auth = handler.headers.get("Authorization")
        auth_ok = self.token is None or auth == "Splunk " + self.token
        if auth_ok:
            status, resp_body = self._next_response()
        else:
            status, resp_body = 401, _DEFAULT_BODIES[401]

        with self._lock:
            self.records.append({
                "path": handler.path,
                "status": status,
                "gzip": is_gzip,
                "auth_ok": auth_ok,
                "raw": body,
                "events": events,
            })

        payload = json.dumps(resp_body).encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        except OSError:
            # client gave up (timeout test); nothing to do
            pass
