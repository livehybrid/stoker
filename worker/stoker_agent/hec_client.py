"""Splunk HEC client for the Stoker worker agent.

Implements the "HEC client" section of docs/WORKER-CONTRACT.md: a bounded
in-memory queue feeding sender threads that batch envelopes into
newline-delimited JSON, optionally gzip them and POST to the HEC event
endpoint with a fixed retry policy and thread-safe counters.

The HEC token appears in exactly one place: the Authorization header.
It is never logged and never included in repr output.
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import random
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

log = logging.getLogger("stoker.hec")

_ENVELOPE_META_KEYS = ("time", "host", "source", "sourcetype", "index")

_COUNTER_KEYS = (
    "events_total",
    "bytes_total",
    "hec_2xx",
    "hec_4xx",
    "hec_5xx",
    "hec_timeouts",
    "retries",
    "dropped",
    "dropped_invalid",
)


def serialise_envelope(envelope):
    # type: (Dict[str, Any]) -> bytes
    """Serialise one envelope to a compact JSON line (no trailing newline).

    Keys with a None value are omitted so Splunk applies its own defaults;
    "event" is mandatory and raises KeyError when absent.
    """
    doc = {}
    for key in _ENVELOPE_META_KEYS:
        value = envelope.get(key)
        if value is not None:
            doc[key] = value
    doc["event"] = envelope["event"]
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class HecClient(object):
    """Batching, retrying HEC event sender with bounded memory.

    put() blocks when the internal queue is full (backpressure). Sender
    threads drain the queue into batches, flushing at ``batch_bytes`` of
    serialised NDJSON or ``batch_ms`` after the first event of a batch,
    whichever comes first.
    """

    def __init__(
        self,
        url,
        token,
        *,
        gzip_enabled=True,
        verify_tls=True,
        batch_bytes=512 * 1024,
        batch_ms=200,
        queue_max=5000,
        senders=4,
        ack=False,
        request_timeout_s=10.0,
        retry_base_s=0.5,
        retry_cap_s=30.0,
        max_attempts=5
    ):
        self._endpoint = url.rstrip("/") + "/services/collector/event"
        self._headers = {
            "Authorization": "Splunk " + token,
            "Content-Type": "application/json",
        }
        self._gzip_enabled = bool(gzip_enabled)
        if self._gzip_enabled:
            self._headers["Content-Encoding"] = "gzip"
        self._verify_tls = verify_tls
        self._batch_bytes = int(batch_bytes)
        self._batch_s = batch_ms / 1000.0
        self._request_timeout_s = request_timeout_s
        self._retry_base_s = retry_base_s
        self._retry_cap_s = retry_cap_s
        self._max_attempts = int(max_attempts)
        self.ack = bool(ack)  # parsed but inactive in Phase 0

        self._queue = queue.Queue(maxsize=int(queue_max))
        self._lock = threading.Lock()
        self._counters = dict.fromkeys(_COUNTER_KEYS, 0)
        self.auth_failed_event = threading.Event()
        self._stopping = threading.Event()
        self._abort = threading.Event()

        self._threads = []
        for i in range(int(senders)):
            t = threading.Thread(
                target=self._sender_run, name="hec-sender-%d" % i, daemon=True
            )
            self._threads.append(t)
            t.start()

    # -- producer side --------------------------------------------------

    def put(self, envelope):
        # type: (Dict[str, Any]) -> None
        """Enqueue one envelope; blocks while the queue is full."""
        while True:
            if self._stopping.is_set():
                raise RuntimeError("HecClient is stopped; put() rejected")
            try:
                self._queue.put(envelope, timeout=0.1)
                return
            except queue.Full:
                continue

    def flush_and_stop(self, timeout_s):
        # type: (float) -> bool
        """Stop accepting, drain queue and in-flight batches, join senders.

        Returns True when everything accepted was resolved (delivered or
        terminally counted) within ``timeout_s``. On timeout the senders
        are aborted: remaining in-flight events are counted as dropped.
        """
        self._stopping.set()
        deadline = time.monotonic() + max(0.0, timeout_s)
        for t in self._threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(remaining)
        flushed = self._queue.empty() and not any(
            t.is_alive() for t in self._threads
        )
        if not flushed:
            self._abort.set()
            for t in self._threads:
                t.join(1.0)
        return flushed

    # -- counters --------------------------------------------------------

    @property
    def queue_depth(self):
        # type: () -> int
        return self._queue.qsize()

    @property
    def auth_failed(self):
        # type: () -> bool
        return self.auth_failed_event.is_set()

    def __getattr__(self, name):
        # Plain counters (events_total, hec_2xx, ...) read under the lock.
        if name in _COUNTER_KEYS:
            with self._lock:
                return self._counters[name]
        raise AttributeError(name)

    def snapshot(self):
        # type: () -> Dict[str, Any]
        with self._lock:
            snap = dict(self._counters)
        snap["queue_depth"] = self._queue.qsize()
        snap["auth_failed"] = self.auth_failed_event.is_set()
        return snap

    def _inc(self, key, n=1):
        with self._lock:
            self._counters[key] += n

    # -- sender side -------------------------------------------------------

    def _sender_run(self):
        session = requests.Session()
        try:
            while True:
                batch = self._collect_batch()
                if batch is None:
                    return
                count, body = batch
                self._send_batch(session, count, body)
        finally:
            session.close()

    def _collect_batch(self):
        # type: () -> Optional[Tuple[int, bytes]]
        """Block for the first event, then fill until batch_bytes of NDJSON
        or batch_ms after the first event. Returns None when stopped and
        drained."""
        first = None
        while first is None:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._stopping.is_set():
                    return None
                continue
            first = self._serialise_or_count(item)

        lines = [first]
        size = len(first)
        deadline = time.monotonic() + self._batch_s
        while size < self._batch_bytes and not self._abort.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                if self._stopping.is_set():
                    break  # flush the partial batch immediately on drain
                continue
            line = self._serialise_or_count(item)
            if line is None:
                continue
            lines.append(line)
            size += len(line) + 1
        return len(lines), b"\n".join(lines)

    def _serialise_or_count(self, item):
        # type: (Dict[str, Any]) -> Optional[bytes]
        try:
            return serialise_envelope(item)
        except (TypeError, ValueError, KeyError):
            self._inc("dropped_invalid")
            log.warning("unserialisable envelope discarded")
            return None

    def _send_batch(self, session, count, body):
        # type: (requests.Session, int, bytes) -> None
        payload = gzip.compress(body, 6) if self._gzip_enabled else body
        for attempt in range(self._max_attempts):
            if self._abort.is_set():
                break
            transient = False
            try:
                resp = session.post(
                    self._endpoint,
                    data=payload,
                    headers=self._headers,
                    timeout=self._request_timeout_s,
                    verify=self._verify_tls,
                )
            except requests.exceptions.RequestException:
                # timeouts and connection resets are equally transient
                self._inc("hec_timeouts")
                transient = True
            else:
                status = resp.status_code
                if 200 <= status < 300:
                    self._inc("hec_2xx")
                    self._inc("events_total", count)
                    self._inc("bytes_total", len(body))
                    return
                if status == 400:
                    self._inc("hec_4xx")
                    self._inc("dropped_invalid", count)
                    text, code = self._parse_hec_error(resp)
                    log.warning(
                        "HEC rejected batch of %d events: code=%s text=%s",
                        count, code, text,
                    )
                    return
                if status in (401, 403):
                    self._inc("hec_4xx")
                    self._inc("dropped", count)
                    if not self.auth_failed_event.is_set():
                        log.error(
                            "HEC authentication failed (HTTP %d); "
                            "check the token", status,
                        )
                    self.auth_failed_event.set()
                    return
                if status >= 500:
                    self._inc("hec_5xx")
                    transient = True
                else:
                    # other 4xx and anything unexpected: non-retryable
                    self._inc("hec_4xx")
                    self._inc("dropped", count)
                    log.warning(
                        "HEC returned unexpected HTTP %d; "
                        "dropped batch of %d events", status, count,
                    )
                    return
            if transient and attempt + 1 < self._max_attempts:
                self._inc("retries")
                delay = self._retry_base_s * (2 ** attempt)
                delay = min(self._retry_cap_s, delay * random.uniform(0.5, 1.5))
                if self._abort.wait(delay):
                    break
        self._inc("dropped", count)
        log.warning(
            "HEC delivery abandoned after %d attempt(s); dropped %d events",
            self._max_attempts, count,
        )

    @staticmethod
    def _parse_hec_error(resp):
        # type: (requests.Response) -> Tuple[str, Optional[int]]
        try:
            doc = resp.json()
            return str(doc.get("text")), doc.get("code")
        except ValueError:
            return "<unparseable body>", None

    def __repr__(self):
        return "<HecClient endpoint=%r queue_depth=%d senders=%d>" % (
            self._endpoint, self._queue.qsize(), len(self._threads),
        )
