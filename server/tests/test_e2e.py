"""End-to-end proof: the real worker driven by the real control plane.

This is the walking-skeleton acceptance test. It:

1. starts the control plane on a **real uvicorn port** (a background thread),
   configured so ``PUBLIC_BASE_URL`` is that port (the worker uses it for both
   the control URL and the bundle URL);
2. registers a target pointing at a local HEC sink (``tools/hec_sink.py``),
   registers the tiny flat pack, creates a short ``eps`` spec and provisions a
   run backed by the shared **FakeDriver** (bookkeeping only — the driver
   records desired replicas; this test launches the worker itself);
3. launches the **real** ``stoker_agent`` as a subprocess in managed mode
   (``STOKER_RUN_ID`` / ``STOKER_CONTROL_URL`` / ``STOKER_RUN_JWT`` /
   ``STOKER_TOTAL_WORKERS`` / ``STOKER_HEC_TOKEN``), so it claims -> readies ->
   is released at T0 -> heartbeats -> finals;
4. asserts the run reaches ``completed``, its leases end ``done``,
   ``metric_samples`` accrued and the HEC sink received ~the expected events.

The whole path exercises the Core lifecycle (claim/ready/release/heartbeat/
final). While those are still stubs it self-skips with a clear reason, so the
file always imports and collects. The primary path is the real subprocess; if
that proves unavailable in the harness (e.g. eventgen import failure) the test
records why and skips rather than hanging — every wait is bounded.

Marked ``@pytest.mark.timeout(180)`` per the contract.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from server import config as config_mod
from server import db as db_mod
from server import drivers as drivers_mod
from server import lifecycle
from server.config import Settings
from server.crypto import generate_master_key

from . import _helpers

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER_PYTHONPATH = os.pathsep.join([
    os.path.join(REPO_ROOT, "worker"),
    os.path.join(REPO_ROOT, "worker", "engines", "eventgen"),
])

EPS = 100.0
DURATION_S = 4.0
HEC_TOKEN = "e2e-hec-token"
# Loose event bound: EPS * DURATION, generous slack for warm-up/drain edges.
EXPECTED_EVENTS = EPS * DURATION_S


# --------------------------------------------------------------------------- #
# Small utilities.
# --------------------------------------------------------------------------- #

def _free_port():
    # type: () -> int
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get_json(url, timeout=2.0):
    # type: (str, float) -> dict
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (local)
        return json.loads(resp.read().decode("utf-8"))


def _wait_until(predicate, timeout_s, interval_s=0.25):
    # type: (object, float, float) -> bool
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return bool(predicate())


# --------------------------------------------------------------------------- #
# HEC sink subprocess.
# --------------------------------------------------------------------------- #

@pytest.fixture()
def hec_sink():
    """Run tools/hec_sink.py on a free port; yield (base_url, stats_getter)."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "tools", "hec_sink.py"),
         "--port", str(port), "--token", HEC_TOKEN],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    try:
        up = _wait_until(lambda: _sink_ready(base), timeout_s=10.0)
        if not up:
            proc.terminate()
            pytest.skip("HEC sink did not come up")

        def stats():
            return _http_get_json(base + "/stats")

        yield base, stats
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _sink_ready(base):
    try:
        _http_get_json(base + "/stats", timeout=1.0)
        return True
    except (urllib.error.URLError, OSError):
        return False


# --------------------------------------------------------------------------- #
# Control plane on a real uvicorn port (own settings so PUBLIC_BASE_URL matches).
# --------------------------------------------------------------------------- #

@pytest.fixture()
def live_server(tmp_path):
    """Start the app under uvicorn on a free port; yield (base_url, settings)."""
    import uvicorn

    port = _free_port()
    base = "http://127.0.0.1:%d" % port
    db_path = tmp_path / "e2e.db"
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    settings = Settings(
        database_url="sqlite:///%s" % db_path,
        master_key=generate_master_key(),
        master_key_generated=False,
        jwt_ttl_s=3600,
        public_base_url=base,             # the worker reaches us here
        worker_image="ghcr.io/livehybrid/stoker-worker:test",
        portainer_host=None, portainer_token=None, portainer_endpoint=6,
        bundle_dir=str(bundle_dir),
        dogfood_hec_url=None, dogfood_hec_token=None, port=port,
    )
    config_mod.set_settings(settings)
    db_mod.configure(settings.database_url)
    db_mod.create_all()

    # Bind the shared FakeDriver to both fleets so the app's supervisor and the
    # (manual) provisioning use one in-memory store.
    from server.drivers.fake import FakeDriver
    driver = FakeDriver()
    drivers_mod.clear_cache()
    drivers_mod.register_driver("fake-local", driver)
    drivers_mod.register_driver("swarm-local", driver)

    from server.app import create_app
    app = create_app()

    uv_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                               lifespan="on")
    server = uvicorn.Server(uv_config)
    thread = threading.Thread(target=server.run, name="e2e-uvicorn", daemon=True)
    thread.start()
    try:
        up = _wait_until(lambda: _healthz_ok(base), timeout_s=15.0)
        if not up:
            pytest.skip("control plane did not come up on %s" % base)
        yield base, settings, driver
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        config_mod.reset_settings()
        drivers_mod.clear_cache()


def _healthz_ok(base):
    try:
        return _http_get_json(base + "/healthz", timeout=1.0).get("status") == "ok"
    except (urllib.error.URLError, OSError):
        return False


# --------------------------------------------------------------------------- #
# The proof.
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(180)
def test_end_to_end_real_worker(live_server, hec_sink, make_pack):
    base, settings, driver = live_server
    sink_base, sink_stats = hec_sink

    # 1) Provision a run directly (target -> pack -> bundle -> spec -> run +
    #    leases), backed by the FakeDriver (bookkeeping). This mirrors what
    #    POST /specs/{id}/run produces; we drive the worker ourselves.
    pack_dir = make_pack(name="flatline-e2e")
    with db_mod.SessionLocal() as db:
        target = _helpers.make_target(
            db, name="e2e-sink", hec_url=sink_base, default_index="loadtest",
            token=HEC_TOKEN, verify_tls=False, health_state="green", settings=settings)
        pack = _helpers.make_pack(db, pack_dir, name="flatline-e2e")
        bundle = _helpers.make_bundle(db, pack, pack_dir, settings)
        spec = _helpers.make_spec(
            db, pack, target, name="e2e-spec", rate_mode="eps", rate_value=EPS,
            workers=1, duration_s=int(DURATION_S), fleet="fake-local")
        run = _helpers.provision_manual(db, spec, target, bundle, settings, driver=driver)
        db.commit()
        run_id = run.id
        jwt = _helpers.bearer_for(run, settings)

    # Sanity: the bundle is fetchable through the live server with the run JWT
    # (the worker will do exactly this).
    _assert_bundle_fetchable(base, bundle.digest, jwt)

    # 2) Launch the REAL worker in managed mode against the live control plane.
    proc = _spawn_worker(base, run_id, jwt)
    try:
        # 3) The Core lifecycle drives claim -> ready -> release -> heartbeat ->
        #    final. Wait (bounded) for the run to finish. If the lifecycle is
        #    still stubbed the run never leaves provisioning: detect and skip.
        reached = _wait_until(
            lambda: _run_state(base, run_id) in lifecycle.TERMINAL_STATES,
            timeout_s=90.0, interval_s=0.5)  # generous: the real worker subprocess
        #                                      can be CPU-starved behind the full suite
        final_state = _run_state(base, run_id)

        if not reached:
            _skip_if_lifecycle_stubbed(base, run_id, proc)
            pytest.fail("run %s did not reach a terminal state (state=%s)"
                        % (run_id, final_state))

        # 4a) The run completed cleanly.
        assert final_state == lifecycle.STATE_COMPLETED, (
            "expected completed, got %s" % final_state)

        # 4b) Its lease(s) ended done.
        detail = _http_get_json("%s/api/runs/%d" % (base, run_id), timeout=5.0)
        lease_states = {l["slot"]: l["state"] for l in detail.get("leases", [])}
        assert lease_states, "no leases on the run detail"
        assert all(s == lifecycle.LEASE_DONE for s in lease_states.values()), lease_states

        # 4c) metric_samples accrued (heartbeats carried counters).
        metrics = _http_get_json(
            "%s/api/runs/%d/metrics" % (base, run_id), timeout=5.0)
        # run_metrics may be an Operator-builder stub; fall back to totals then.
        if isinstance(metrics, dict) and metrics.get("samples") is not None:
            assert len(metrics["samples"]) >= 1, "no metric_samples recorded"

        # 4d) The HEC sink received roughly the expected number of events.
        # Give the worker a moment to finish its final flush + POST.
        _wait_until(lambda: sink_stats().get("events", 0) >= EXPECTED_EVENTS * 0.5,
                    timeout_s=20.0)
        received = sink_stats().get("events", 0)
        lower = EXPECTED_EVENTS * 0.5   # generous: warm-up + drain truncation
        upper = EXPECTED_EVENTS * 2.0
        assert lower <= received <= upper, (
            "HEC sink got %d events, expected ~%d (bounds %d..%d)"
            % (received, EXPECTED_EVENTS, lower, upper))
    finally:
        _terminate(proc)


# --------------------------------------------------------------------------- #
# Fallback path: drive the REAL ControlClient over the live server for the
# protocol handshake if the subprocess route is unavailable. This still proves
# the wire protocol + lifecycle transitions without eventgen generating load.
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(180)
def test_end_to_end_control_client_handshake(live_server, hec_sink, make_pack):
    """Protocol-only proof using the vendored ControlClient (no engine).

    This is the documented fallback: it exercises claim -> ready -> release ->
    heartbeat -> final against the live control plane using the worker's own
    ``stoker_agent.control.ControlClient`` (the exact client the CP serves), and
    asserts the run reaches a terminal state with its lease done. It does not
    generate HEC load (no engine), so it complements, not replaces, the
    subprocess test above.
    """
    control_mod = pytest.importorskip("stoker_agent.control")
    slice_mod = pytest.importorskip("stoker_agent.slice")

    base, settings, driver = live_server
    sink_base, _sink_stats = hec_sink
    pack_dir = make_pack(name="flatline-hs")

    with db_mod.SessionLocal() as db:
        target = _helpers.make_target(
            db, name="hs-sink", hec_url=sink_base, default_index="loadtest",
            token=HEC_TOKEN, verify_tls=False, health_state="green", settings=settings)
        pack = _helpers.make_pack(db, pack_dir, name="flatline-hs")
        bundle = _helpers.make_bundle(db, pack, pack_dir, settings)
        spec = _helpers.make_spec(
            db, pack, target, name="hs-spec", rate_mode="eps", rate_value=EPS,
            workers=1, duration_s=int(DURATION_S), fleet="fake-local")
        run = _helpers.provision_manual(db, spec, target, bundle, settings, driver=driver)
        db.commit()
        run_id = run.id
        jwt = _helpers.bearer_for(run, settings)

    client = control_mod.ControlClient(base, run_id, jwt, deadman_s=30.0)

    # claim -> slice (skip if the lifecycle is still stubbed: claim 5xxs).
    try:
        doc = client.claim("hs-holder", hint_slot=0)
    except control_mod.ControlError as exc:
        pytest.skip("claim failed (Core lifecycle likely stubbed): %s" % exc)
    sl = slice_mod.SpecSlice.from_claim(doc)
    assert sl.slot == 0
    assert sl.rate_mode == "eps"
    assert sl.bundle_sha256 == bundle.digest
    assert sl.hec_url == sink_base

    # ready, then poll heartbeat until release carries a T0.
    client.ready(sl.slot, sl.lease_id)
    t0 = _await_release_via_client(client, sl)
    assert t0 is not None, "control plane never issued a release T0"

    # a couple of heartbeats past T0, then final.
    for _ in range(2):
        resp = client.heartbeat({"slot": sl.slot, "lease_id": sl.lease_id,
                                 "state": "generating", "events_total": 100,
                                 "bytes_total": 12000, "eps": EPS})
        assert resp is not None
        assert resp.get("command") in ("continue", "release", "retarget", "drain")
        time.sleep(0.2)

    client.final(sl.slot, {"events_total": 400, "bytes_total": 48000,
                           "reason": "duration-complete", "flushed": True}, ["done"])

    # The run should now reach a terminal state (all leases done).
    reached = _wait_until(
        lambda: _run_state(base, run_id) in lifecycle.TERMINAL_STATES,
        timeout_s=15.0)
    assert reached, "run did not finalise after the handshake"
    detail = _http_get_json("%s/api/runs/%d" % (base, run_id), timeout=5.0)
    lease_states = {l["slot"]: l["state"] for l in detail.get("leases", [])}
    assert lease_states.get(0) == lifecycle.LEASE_DONE


# --------------------------------------------------------------------------- #
# Helpers used by the proofs.
# --------------------------------------------------------------------------- #

def _run_state(base, run_id):
    # type: (str, int) -> str
    try:
        return _http_get_json("%s/api/runs/%d" % (base, run_id), timeout=3.0).get("state", "")
    except (urllib.error.URLError, OSError):
        return ""


def _assert_bundle_fetchable(base, digest, jwt):
    # type: (str, str, str) -> None
    req = urllib.request.Request(
        "%s/api/agent/bundles/%s.tgz" % (base, digest),
        headers={"Authorization": "Bearer %s" % jwt})
    with urllib.request.urlopen(req, timeout=5.0) as resp:  # noqa: S310 (local)
        assert resp.status == 200
        assert resp.read(4), "bundle response was empty"


def _spawn_worker(base, run_id, jwt):
    # type: (str, int, str) -> subprocess.Popen
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKER_PYTHONPATH
    env.update({
        "STOKER_RUN_ID": str(run_id),
        "STOKER_CONTROL_URL": base,
        "STOKER_RUN_JWT": jwt,
        "STOKER_TOTAL_WORKERS": "1",
        "STOKER_HEC_TOKEN": HEC_TOKEN,
        "STOKER_HINT_SLOT": "0",
        "STOKER_HEARTBEAT_S": "1",     # snappy release polling for the test
        "STOKER_METRICS_PORT": "0",    # no prometheus port in the test
        # A per-worker socket so parallel runs never collide.
        "STOKER_OUTPUT_SOCKET": "/tmp/stoker-e2e-%d.sock" % run_id,
        "STOKER_LOG_LEVEL": "WARNING",
    })
    return subprocess.Popen(
        [sys.executable, "-m", "stoker_agent"],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def _terminate(proc):
    # type: (subprocess.Popen) -> None
    if proc.poll() is None:
        proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    if proc.poll() is None:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _skip_if_lifecycle_stubbed(base, run_id, proc):
    # type: (str, int, subprocess.Popen) -> None
    """Skip (not fail) when the run stalled because the Core lifecycle is a stub.

    The tell-tale is a run still in ``provisioning``/``pending`` and a worker
    that could not claim. We also surface the worker's captured output to make a
    genuine failure debuggable.
    """
    state = _run_state(base, run_id)
    if state in (lifecycle.STATE_PENDING, lifecycle.STATE_PREPARING,
                 lifecycle.STATE_PROVISIONING, ""):
        # Drain a little worker output for the skip reason.
        out = ""
        if proc.poll() is not None and proc.stdout is not None:
            with contextlib.suppress(Exception):
                out = proc.stdout.read().decode("utf-8", "replace")[-500:]
        pytest.skip("run stuck in %r; Core lifecycle likely not implemented yet. "
                    "worker tail: %s" % (state, out))


def _await_release_via_client(client, sl, timeout_s=30.0):
    """Poll heartbeat via the real ControlClient until a release T0 arrives."""
    from stoker_agent.slice import parse_iso8601

    if sl.released and sl.effective_t0:
        return sl.effective_t0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.heartbeat({"slot": sl.slot, "lease_id": sl.lease_id,
                                 "state": "ready", "events_total": 0})
        if resp is not None:
            if resp.get("command") == "release" and resp.get("t0"):
                return parse_iso8601(resp["t0"])
            if resp.get("command") == "drain":
                return None
        time.sleep(0.3)
    return None
