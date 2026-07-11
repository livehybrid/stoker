import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stoker_agent.control import (FENCE_PAUSE_S, ControlClient, DeadManError,
                                  StandaloneControl, SupersededError)
from stoker_agent.slice import parse_iso8601

SLICE_DOC = {
    "run_id": 812, "slot": 2, "total_workers": 4, "lease_id": "le_9f",
    "engine": "eventgen",
    "bundle": {"url": "https://ctl/bundles/x.tgz", "sha256": "abc"},
    "share": {"eps": 1543},
    "duration_s": 14400,
    "hec": {"url": "http://h:8088", "index": "loadtest"},
    "released": False,
}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        record = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(raw) if raw else {},
        }
        self.server.requests.append(record)
        endpoint = self.path.rstrip("/").rsplit("/", 1)[-1]
        script = self.server.scripts.get(endpoint) or []
        status, doc = script.pop(0) if script else (200, {})
        payload = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class FakeControlPlane(object):
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.scripts = {}
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def url(self):
        host, port = self.server.server_address
        return "http://%s:%d" % (host, port)

    @property
    def requests(self):
        return self.server.requests

    def script(self, endpoint, responses):
        self.server.scripts[endpoint] = list(responses)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeClock(object):
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, duration):
        # sleeping advances the fake clock: deterministic backoff tests
        self.t += duration
        self.slept.append(duration)

    slept = None

    def with_log(self):
        self.slept = []
        return self


@pytest.fixture
def plane():
    plane = FakeControlPlane()
    yield plane
    plane.close()


def make_client(plane, clock=None, deadman_s=600.0, jwt="jwt-1"):
    clock = clock or FakeClock().with_log()
    client = ControlClient(plane.url, 812, jwt, deadman_s=deadman_s,
                           clock=clock, sleep=clock.sleep)
    return client, clock


class TestClaim:
    def test_claim_returns_slice(self, plane):
        plane.script("claim", [(200, SLICE_DOC)])
        client, _ = make_client(plane)
        doc = client.claim("worker-h1", hint_slot=2)
        assert doc["lease_id"] == "le_9f"
        req = plane.requests[0]
        assert req["path"] == "/api/agent/runs/812/claim"
        assert req["auth"] == "Bearer jwt-1"
        assert req["body"] == {"holder": "worker-h1", "hint_slot": 2,
                               "protocol_version": 1}

    def test_claim_omits_absent_hint_slot(self, plane):
        plane.script("claim", [(200, SLICE_DOC)])
        client, _ = make_client(plane)
        client.claim("worker-h1")
        assert "hint_slot" not in plane.requests[0]["body"]

    def test_claim_retries_with_backoff(self, plane):
        plane.script("claim", [(500, {}), (503, {}), (200, SLICE_DOC)])
        client, clock = make_client(plane)
        doc = client.claim("worker-h1")
        assert doc["run_id"] == 812
        assert len(plane.requests) == 3
        assert len(clock.slept) == 2
        # jittered exponential: base 0.5, x2, jitter 0.5..1.5
        assert 0.25 <= clock.slept[0] <= 0.75
        assert 0.5 <= clock.slept[1] <= 1.5

    def test_claim_gives_up_at_deadman(self, plane):
        plane.script("claim", [(500, {})] * 50)
        clock = FakeClock().with_log()
        real_sleep = clock.sleep

        def big_sleep(duration):
            real_sleep(duration)
            clock.t += 100  # each retry burns 100 s of wall clock

        client = ControlClient(plane.url, 812, "jwt-1", deadman_s=300,
                               clock=clock, sleep=big_sleep)
        with pytest.raises(DeadManError):
            client.claim("worker-h1")


class TestHeartbeat:
    def test_success_returns_command(self, plane):
        plane.script("heartbeat", [(200, {"command": "continue"})])
        client, _ = make_client(plane)
        resp = client.heartbeat({"slot": 2, "lease_id": "le_9f"})
        assert resp == {"command": "continue"}
        body = plane.requests[0]["body"]
        assert body["protocol_version"] == 1
        assert body["slot"] == 2

    def test_release_command_with_t0(self, plane):
        plane.script("heartbeat", [
            (200, {"command": "release", "t0": "2026-07-11T12:00:00Z"})])
        client, _ = make_client(plane)
        resp = client.heartbeat({"slot": 2})
        assert resp["command"] == "release"
        assert parse_iso8601(resp["t0"]) > 0

    def test_retarget_command_passthrough(self, plane):
        plane.script("heartbeat", [
            (200, {"command": "retarget", "share": {"eps": 1200}})])
        client, _ = make_client(plane)
        resp = client.heartbeat({"slot": 2})
        assert resp["share"] == {"eps": 1200}

    def test_jwt_rolls_from_response(self, plane):
        plane.script("heartbeat", [
            (200, {"command": "continue", "jwt": "jwt-2"}),
            (200, {"command": "continue"}),
        ])
        client, _ = make_client(plane)
        client.heartbeat({"slot": 2})
        client.heartbeat({"slot": 2})
        assert plane.requests[0]["auth"] == "Bearer jwt-1"
        assert plane.requests[1]["auth"] == "Bearer jwt-2"

    def test_superseded_raises(self, plane):
        plane.script("heartbeat", [(200, {"command": "superseded"})])
        client, _ = make_client(plane)
        with pytest.raises(SupersededError):
            client.heartbeat({"slot": 2})

    def test_missed_heartbeat_returns_none(self, plane):
        plane.script("heartbeat", [(500, {})])
        client, _ = make_client(plane)
        assert client.heartbeat({"slot": 2}) is None

    def test_auth_failure_is_a_missed_ack_not_a_crash(self, plane):
        plane.script("heartbeat", [(403, {})])
        client, _ = make_client(plane)
        assert client.heartbeat({"slot": 2}) is None


class TestFencing:
    def test_pause_after_30s_without_ack(self, plane):
        plane.script("heartbeat", [(500, {}), (200, {"command": "continue"})])
        clock = FakeClock().with_log()
        client, _ = make_client(plane, clock=clock)
        assert client.should_pause() is False
        assert client.heartbeat({"slot": 2}) is None  # missed
        clock.t += FENCE_PAUSE_S + 1
        assert client.should_pause() is True
        # a successful heartbeat confirms the lease and lifts the pause
        assert client.heartbeat({"slot": 2}) is not None
        assert client.should_pause() is False

    def test_deadman_expiry(self, plane):
        clock = FakeClock().with_log()
        client, _ = make_client(plane, clock=clock, deadman_s=600)
        clock.t += 599
        assert client.deadman_expired() is False
        clock.t += 2
        assert client.deadman_expired() is True

    def test_ack_resets_deadman_window(self, plane):
        plane.script("heartbeat", [(200, {"command": "continue"})])
        clock = FakeClock().with_log()
        client, _ = make_client(plane, clock=clock, deadman_s=600)
        clock.t += 500
        client.heartbeat({"slot": 2})
        clock.t += 500
        assert client.deadman_expired() is False
        assert client.seconds_since_ack() == pytest.approx(500)


class TestFinal:
    def test_final_posts_summary_and_log_tail(self, plane):
        plane.script("final", [(200, {})])
        client, _ = make_client(plane)
        ok = client.final(2, {"events_total": 12000}, ["line1", "line2"])
        assert ok is True
        body = plane.requests[0]["body"]
        assert body["slot"] == 2
        assert body["summary"] == {"events_total": 12000}
        assert body["log_tail"] == ["line1", "line2"]

    def test_final_is_best_effort(self, plane):
        plane.script("final", [(500, {})] * 5)
        client, _ = make_client(plane)
        assert client.final(2, {}, []) is False
        assert len(plane.requests) == 3  # bounded attempts


class TestStandaloneControl:
    def test_first_heartbeat_releases_at_now_plus_2s(self):
        clock = FakeClock(start=1000.0)
        out = io.StringIO()
        control = StandaloneControl(clock=clock, out=out)
        resp = control.heartbeat({"slot": 0})
        assert resp["command"] == "release"
        assert parse_iso8601(resp["t0"]) == pytest.approx(1002.0)

    def test_subsequent_heartbeats_continue(self):
        control = StandaloneControl(clock=FakeClock(), out=io.StringIO())
        control.heartbeat({"slot": 0})
        assert control.heartbeat({"slot": 0}) == {"command": "continue"}

    def test_heartbeat_lines_logged_to_stdout(self):
        out = io.StringIO()
        control = StandaloneControl(clock=FakeClock(), out=out)
        control.heartbeat({"slot": 0, "events_total": 42})
        line = out.getvalue().splitlines()[0]
        assert line.startswith("[stoker] heartbeat ")
        doc = json.loads(line.split("[stoker] heartbeat ", 1)[1])
        assert doc["events_total"] == 42

    def test_fencing_never_triggers(self):
        control = StandaloneControl(clock=FakeClock(), out=io.StringIO())
        assert control.should_pause() is False
        assert control.deadman_expired() is False

    def test_final_logged(self):
        out = io.StringIO()
        control = StandaloneControl(clock=FakeClock(), out=out)
        assert control.final(0, {"events_total": 10}, []) is True
        assert "[stoker] final" in out.getvalue()
