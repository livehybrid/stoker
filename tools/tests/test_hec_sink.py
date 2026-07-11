from __future__ import annotations

import gzip
import json
import threading
import urllib.error
import urllib.request

import pytest

import hec_sink

TOKEN = "t0ken-abc"


def _start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return "http://127.0.0.1:%d" % server.server_address[1]


@pytest.fixture
def sink():
    server = hec_sink.build_server("127.0.0.1", 0, token=TOKEN)
    base = _start(server)
    yield server, base
    server.shutdown()
    server.server_close()


@pytest.fixture
def open_sink():
    server = hec_sink.build_server("127.0.0.1", 0, token=None)
    base = _start(server)
    yield server, base
    server.shutdown()
    server.server_close()


def _request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _post_events(base, body, token=TOKEN, gzipped=False, extra_headers=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = "Splunk " + token
    if gzipped:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    headers.update(extra_headers or {})
    return _request(base + "/services/collector/event", data=body, headers=headers)


def _ndjson(events):
    return ("\n".join(json.dumps(e) for e in events)).encode("utf-8")


def test_ndjson_batch_counted(sink):
    server, base = sink
    events = [{"time": 1.0 + i, "event": "line %d" % i} for i in range(3)]
    status, body = _post_events(base, _ndjson(events))
    assert status == 200
    assert body == {"text": "Success", "code": 0}
    snap = server.stats.snapshot()
    assert snap["events"] == 3
    assert snap["requests"] == 1
    assert snap["bytes"] > 0


def test_gzip_body_counted(sink):
    server, base = sink
    events = [{"event": "gz %d" % i} for i in range(5)]
    raw = _ndjson(events)
    status, _ = _post_events(base, raw, gzipped=True)
    assert status == 200
    snap = server.stats.snapshot()
    assert snap["events"] == 5
    # bytes counted uncompressed
    assert snap["bytes"] == len(raw)


def test_concatenated_json_counted(sink):
    server, base = sink
    body = b'{"event": "a"}{"event": "b"} {"event": "c"}'
    status, _ = _post_events(base, body)
    assert status == 200
    assert server.stats.snapshot()["events"] == 3


def test_missing_token_401(sink):
    server, base = sink
    status, body = _post_events(base, _ndjson([{"event": "x"}]), token=None)
    assert status == 401
    assert body["code"] == 2
    assert server.stats.snapshot()["events"] == 0


def test_wrong_token_403(sink):
    server, base = sink
    status, body = _post_events(base, _ndjson([{"event": "x"}]), token="wrong")
    assert status == 403
    assert body["code"] == 4
    assert server.stats.snapshot()["events"] == 0


def test_bearer_scheme_accepted(sink):
    server, base = sink
    status, _ = _request(
        base + "/services/collector/event",
        data=_ndjson([{"event": "x"}]),
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert status == 200
    assert server.stats.snapshot()["events"] == 1


def test_no_token_configured_accepts_anonymous(open_sink):
    server, base = open_sink
    status, _ = _post_events(base, _ndjson([{"event": "x"}]), token=None)
    assert status == 200
    assert server.stats.snapshot()["events"] == 1


def test_invalid_json_400(sink):
    server, base = sink
    status, body = _post_events(base, b'{"event": "ok"}not-json')
    assert status == 400
    assert body["code"] == 6
    snap = server.stats.snapshot()
    assert snap["events"] == 0
    assert snap["rejected_requests"] == 1


def test_missing_event_field_400(sink):
    server, base = sink
    status, body = _post_events(base, b'{"time": 1.0}')
    assert status == 400
    assert body["code"] == 12
    assert server.stats.snapshot()["events"] == 0


def test_empty_body_400(sink):
    server, base = sink
    status, body = _post_events(base, b"")
    assert status == 400
    assert body["code"] == 5


def test_unknown_path_404(sink):
    server, base = sink
    status, _ = _post_events(base, _ndjson([{"event": "x"}]))
    assert status == 200
    status, _ = _request(
        base + "/services/other",
        data=b"{}",
        headers={"Authorization": "Splunk " + TOKEN},
    )
    assert status == 404
    assert server.stats.snapshot()["events"] == 1


def test_health_endpoint(sink):
    _, base = sink
    status, body = _request(base + "/services/collector/health")
    assert status == 200
    assert body["code"] == 17


def test_stats_endpoint_shape(sink):
    server, base = sink
    _post_events(base, _ndjson([{"event": "x"}, {"event": "y"}]))
    status, body = _request(base + "/stats")
    assert status == 200
    for key in ("requests", "events", "bytes", "rejected_requests", "uptime_s"):
        assert key in body
    assert body["events"] == 2


def test_keepalive_multiple_posts_one_connection(sink):
    # The agent uses pooled keep-alive sessions; the sink must stay in sync
    # across sequential posts, including after an auth failure.
    server, base = sink
    _post_events(base, _ndjson([{"event": "1"}]))
    _post_events(base, _ndjson([{"event": "2"}]), token="wrong")
    _post_events(base, _ndjson([{"event": "3"}]))
    snap = server.stats.snapshot()
    assert snap["events"] == 2
    assert snap["rejected_requests"] == 1


def test_parse_events_rejects_non_object():
    with pytest.raises(ValueError):
        hec_sink.parse_events('["not", "an", "object"]')
