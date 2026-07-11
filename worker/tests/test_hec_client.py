"""Unit tests for stoker_agent.hec_client against the in-process HEC sink."""

from __future__ import annotations

import threading
import time

import pytest

from hec_sink import HecSink
from stoker_agent.hec_client import HecClient, serialise_envelope

pytestmark = pytest.mark.timeout(60)

TOKEN = "test-token-1234"


def make_client(url, **kwargs):
    defaults = dict(
        retry_base_s=0.01,
        retry_cap_s=0.05,
        request_timeout_s=5.0,
    )
    defaults.update(kwargs)
    return HecClient(url, TOKEN, **defaults)


def envelope(i=0, **overrides):
    doc = {
        "time": 1752234567.0 + i,
        "host": "host-%d" % i,
        "source": "gen",
        "sourcetype": "stoker:test",
        "index": "loadtest",
        "event": "event number %d" % i,
    }
    doc.update(overrides)
    return doc


def test_serialise_envelope_drops_nulls_and_requires_event():
    line = serialise_envelope(
        {"time": None, "host": "h", "source": None, "sourcetype": None,
         "index": "main", "event": "x"}
    )
    import json
    doc = json.loads(line.decode("utf-8"))
    assert doc == {"host": "h", "index": "main", "event": "x"}
    with pytest.raises(KeyError):
        serialise_envelope({"host": "h"})


def test_batching_by_size():
    with HecSink(token=TOKEN) as sink:
        client = make_client(
            sink.url, batch_ms=10000, batch_bytes=1500, senders=1
        )
        for i in range(40):
            client.put(envelope(i))
        # size-based flush must happen long before the 10 s window
        assert sink.wait_for_requests(1, timeout_s=5.0)
        assert len(sink.records[0]["raw"]) >= 1500
        assert client.flush_and_stop(10.0) is True
        assert sink.request_count >= 2
        assert len(sink.events) == 40
        assert client.snapshot()["events_total"] == 40


def test_batching_by_time():
    with HecSink(token=TOKEN) as sink:
        # default batch_bytes (512 KiB) can never be reached by 3 events
        client = make_client(sink.url, batch_ms=100, senders=1)
        start = time.monotonic()
        for i in range(3):
            client.put(envelope(i))
        assert sink.wait_for_events(3, timeout_s=3.0)
        assert time.monotonic() - start < 2.0
        assert sink.request_count <= 2
        assert client.flush_and_stop(5.0) is True


def test_gzip_round_trip_and_counters():
    with HecSink(token=TOKEN) as sink:
        client = make_client(sink.url, batch_ms=50, senders=2)
        sent = [envelope(i, event=u"café £ %d 温度" % i)
                for i in range(10)]
        sent[3]["source"] = None  # null meta key must be omitted on the wire
        for doc in sent:
            client.put(doc)
        assert client.flush_and_stop(10.0) is True

        assert all(rec["gzip"] for rec in sink.records)
        got = {ev["event"]: ev for ev in sink.events}
        assert len(got) == 10
        for doc in sent:
            wire = got[doc["event"]]
            for key in ("time", "host", "sourcetype", "index"):
                assert wire[key] == doc[key]
        assert "source" not in got[sent[3]["event"]]

        snap = client.snapshot()
        assert snap["events_total"] == 10
        assert snap["bytes_total"] == sum(len(r["raw"]) for r in sink.records)
        assert snap["hec_2xx"] == sink.request_count
        assert snap["hec_4xx"] == 0
        assert snap["hec_5xx"] == 0
        assert snap["hec_timeouts"] == 0
        assert snap["retries"] == 0
        assert snap["dropped"] == 0
        assert snap["dropped_invalid"] == 0
        assert snap["queue_depth"] == 0
        assert snap["auth_failed"] is False


def test_gzip_disabled_sends_plain_body():
    with HecSink(token=TOKEN) as sink:
        client = make_client(
            sink.url, gzip_enabled=False, batch_ms=50, senders=1
        )
        client.put(envelope(1))
        assert client.flush_and_stop(5.0) is True
        assert sink.request_count == 1
        assert sink.records[0]["gzip"] is False
        assert sink.events[0]["event"] == "event number 1"


def test_snapshot_has_exactly_the_contract_keys():
    with HecSink(token=TOKEN) as sink:
        client = make_client(sink.url, senders=1)
        expected = {
            "events_total", "bytes_total", "hec_2xx", "hec_4xx", "hec_5xx",
            "hec_timeouts", "retries", "dropped", "dropped_invalid",
            "queue_depth", "auth_failed",
        }
        assert set(client.snapshot()) == expected
        assert client.queue_depth == 0
        assert client.auth_failed is False
        assert client.events_total == 0
        client.flush_and_stop(5.0)


def test_5xx_retry_then_success():
    with HecSink(token=TOKEN, responses=[503, 200]) as sink:
        client = make_client(sink.url, batch_ms=20, senders=1)
        client.put(envelope(7))
        assert sink.wait_for_requests(2, timeout_s=5.0)
        assert client.flush_and_stop(5.0) is True

        assert [r["status"] for r in sink.records] == [503, 200]
        assert sink.records[0]["events"] == sink.records[1]["events"]
        snap = client.snapshot()
        assert snap["hec_5xx"] == 1
        assert snap["retries"] == 1
        assert snap["hec_2xx"] == 1
        assert snap["events_total"] == 1
        assert snap["dropped"] == 0


def test_5xx_exhausts_attempts_then_drops():
    with HecSink(token=TOKEN, responses=[503] * 5) as sink:
        client = make_client(sink.url, batch_ms=20, senders=1)
        client.put(envelope(1))
        assert client.flush_and_stop(10.0) is True
        assert sink.request_count == 5
        snap = client.snapshot()
        assert snap["hec_5xx"] == 5
        assert snap["retries"] == 4
        assert snap["dropped"] == 1
        assert snap["events_total"] == 0


def test_400_is_never_retried():
    responses = [(400, {"text": "Invalid data format", "code": 6})]
    with HecSink(token=TOKEN, responses=responses) as sink:
        client = make_client(sink.url, batch_ms=50, senders=1)
        for i in range(3):
            client.put(envelope(i))
        assert client.flush_and_stop(5.0) is True
        assert sink.request_count == 1
        snap = client.snapshot()
        assert snap["hec_4xx"] == 1
        assert snap["dropped_invalid"] == 3
        assert snap["retries"] == 0
        assert snap["dropped"] == 0
        assert snap["events_total"] == 0


def test_401_fails_fast_without_retry_or_exit():
    with HecSink(token=TOKEN) as sink:
        client = HecClient(
            sink.url, "wrong-token", batch_ms=50, senders=1,
            retry_base_s=0.01,
        )
        client.put(envelope(1))
        client.put(envelope(2))
        assert client.flush_and_stop(5.0) is True
        assert sink.request_count == 1
        assert sink.records[0]["auth_ok"] is False
        assert client.auth_failed is True
        assert client.auth_failed_event.is_set()
        snap = client.snapshot()
        assert snap["auth_failed"] is True
        assert snap["hec_4xx"] == 1
        assert snap["retries"] == 0
        assert snap["dropped"] == 2
        assert snap["events_total"] == 0


def test_timeouts_are_retried_then_dropped():
    with HecSink(token=TOKEN, delay_s=1.0) as sink:
        client = make_client(
            sink.url, batch_ms=20, senders=1,
            request_timeout_s=0.1, max_attempts=2,
        )
        client.put(envelope(1))
        assert client.flush_and_stop(10.0) is True
        snap = client.snapshot()
        assert snap["hec_timeouts"] == 2
        assert snap["retries"] == 1
        assert snap["dropped"] == 1
        assert snap["events_total"] == 0


def test_backpressure_blocks_put_when_queue_full():
    with HecSink(token=TOKEN, delay_s=0.25) as sink:
        client = make_client(
            sink.url, senders=1, queue_max=2, batch_ms=10, batch_bytes=200
        )
        start = time.monotonic()
        for i in range(8):
            client.put(envelope(i))
        elapsed = time.monotonic() - start
        # 1 sender at ~0.25 s per POST and a 2-slot queue: puts must stall
        assert elapsed > 0.4
        assert client.flush_and_stop(10.0) is True
        assert len(sink.events) == 8
        assert client.snapshot()["events_total"] == 8


def test_put_raises_after_stop():
    with HecSink(token=TOKEN) as sink:
        client = make_client(sink.url, senders=1)
        assert client.flush_and_stop(5.0) is True
        with pytest.raises(RuntimeError):
            client.put(envelope(1))


def test_flush_and_stop_is_bounded_under_persistent_failure():
    with HecSink(token=TOKEN, responses=[503] * 100) as sink:
        client = HecClient(
            sink.url, TOKEN, senders=1, batch_ms=10,
            retry_base_s=5.0, retry_cap_s=30.0,
        )
        client.put(envelope(1))
        # first attempt made, sender now sleeping in a 5 s backoff
        assert sink.wait_for_requests(1, timeout_s=5.0)
        start = time.monotonic()
        flushed = client.flush_and_stop(0.5)
        elapsed = time.monotonic() - start
        assert flushed is False
        assert elapsed < 3.0
        assert not any(t.is_alive() for t in client._threads)
        assert client.snapshot()["dropped"] == 1


def test_throughput_thousands_of_events_drain_quickly():
    with HecSink(token=TOKEN) as sink:
        client = make_client(sink.url, batch_ms=50)  # 4 senders, 512 KiB
        n = 5000
        start = time.monotonic()
        for i in range(n):
            client.put({"time": None, "host": None, "source": None,
                        "sourcetype": None, "index": "loadtest",
                        "event": "evt %d" % i})
        assert client.flush_and_stop(15.0) is True
        elapsed = time.monotonic() - start
        assert elapsed < 10.0
        assert client.snapshot()["events_total"] == n
        assert len(sink.events) == n


def test_concurrent_producers_lose_nothing():
    with HecSink(token=TOKEN) as sink:
        client = make_client(sink.url, batch_ms=20, queue_max=100)

        def produce(base):
            for i in range(200):
                client.put(envelope(base + i))

        threads = [
            threading.Thread(target=produce, args=(k * 1000,))
            for k in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert client.flush_and_stop(10.0) is True
        assert len(sink.events) == 800
        assert client.snapshot()["events_total"] == 800


def test_repr_and_headers_never_expose_token():
    with HecSink(token=TOKEN) as sink:
        client = make_client(sink.url, senders=1)
        assert TOKEN not in repr(client)
        client.flush_and_stop(5.0)
