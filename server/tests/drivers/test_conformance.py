"""ExecutionDriver conformance suite + SwarmDriver request-shape unit tests.

Two layers:

1. **Conformance walk** (``test_conformance_*``), parametrised over every driver
   available in this environment. Always includes the in-process ``FakeDriver``
   and a ``SwarmDriver`` wired to an in-memory fake Portainer (so the real
   Swarm code paths run without a swarm); a ``SwarmDriver`` against a *live*
   Portainer is added only when ``STOKER_TEST_PORTAINER=1``. Each asserts the
   contract's create -> status(desired=N) -> scale -> stop -> destroy transitions
   and that destroy is idempotent.

2. **SwarmDriver request-shape unit tests** (``test_swarm_*``): drive the driver
   through a mocked ``httpx`` transport and assert the exact Portainer/Docker
   calls -- the service-create ``ServiceSpec`` shape, the read-version-then-update
   scale/stop flow, idempotent 404 destroy, task folding in ``status`` and the
   multiplexed log stream decode. These never touch a network.

The fake Portainer models just enough of the Docker Engine service API
(``/services/create``, ``/services/{id}`` inspect, ``/services/{id}/update``,
``DELETE /services/{id}``, ``/tasks``, ``/services/{id}/logs``) for the driver to
believe it is talking to a real swarm.
"""

from __future__ import annotations

import json
import os
import struct
from typing import Any, Dict, List, Optional

import httpx
import pytest

from server.drivers.base import DriverError, DriverRef, DriverStatus, RunSnapshot
from server.drivers.fake import FakeDriver
from server.drivers.swarm import SwarmDriver, service_name


# --------------------------------------------------------------------------- #
# In-memory Portainer/Docker services API backed by httpx.MockTransport.
# --------------------------------------------------------------------------- #

class _FakePortainer:
    """A stateful fake of the subset of the Docker services API the driver uses.

    Records every request (method, path, params, json) in ``self.calls`` for
    assertions, and keeps a tiny service store so a full create -> status ->
    scale -> stop -> destroy walk behaves like a real swarm: replicas are stored,
    ``/tasks`` returns one running task per desired replica, and a deleted
    service 404s on subsequent inspect/delete.
    """

    def __init__(self):
        # type: () -> None
        self.services = {}  # type: Dict[str, Dict[str, Any]]
        self.calls = []  # type: List[Dict[str, Any]]
        self._next_id = 1
        self._version = 10

    # -- transport entry point ------------------------------------------- #

    def transport(self):
        # type: () -> httpx.MockTransport
        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        # type: (httpx.Request) -> httpx.Response
        path = request.url.path
        method = request.method
        body = request.content
        parsed = json.loads(body) if body else None
        self.calls.append({
            "method": method,
            "path": path,
            "params": dict(request.url.params),
            "json": parsed,
            "api_key": request.headers.get("X-API-Key"),
        })

        # Strip the Portainer/docker prefix to a bare docker route.
        marker = "/docker"
        idx = path.find(marker)
        route = path[idx + len(marker):] if idx >= 0 else path

        if route == "/services/create" and method == "POST":
            return self._create(parsed)
        if route == "/services" and method == "GET":
            return self._list_services(dict(request.url.params))
        if route == "/tasks" and method == "GET":
            return self._tasks(dict(request.url.params))
        if route.startswith("/services/"):
            rest = route[len("/services/"):]
            if rest.endswith("/update") and method == "POST":
                sid = rest[: -len("/update")]
                return self._update(sid, parsed, dict(request.url.params))
            if rest.endswith("/logs") and method == "GET":
                sid = rest[: -len("/logs")]
                return self._logs(sid, dict(request.url.params))
            if method == "GET":
                return self._inspect(rest)
            if method == "DELETE":
                return self._delete(rest)
        return httpx.Response(404, json={"message": "no route %s %s" % (method, route)})

    # -- handlers -------------------------------------------------------- #

    def _create(self, spec):
        # type: (Dict[str, Any]) -> httpx.Response
        sid = "svc%d" % self._next_id
        self._next_id += 1
        self._version += 1
        self.services[sid] = {
            "ID": sid,
            "Version": {"Index": self._version},
            "Spec": spec,
        }
        return httpx.Response(201, json={"ID": sid})

    def _inspect(self, sid):
        # type: (str) -> httpx.Response
        svc = self.services.get(sid)
        if svc is None:
            return httpx.Response(404, json={"message": "service %s not found" % sid})
        return httpx.Response(200, json=svc)

    def _update(self, sid, spec, params):
        # type: (str, Dict[str, Any], Dict[str, Any]) -> httpx.Response
        svc = self.services.get(sid)
        if svc is None:
            return httpx.Response(404, json={"message": "service %s not found" % sid})
        # Docker rejects an update whose ?version does not match the object.
        if str(params.get("version")) != str(svc["Version"]["Index"]):
            return httpx.Response(
                409, json={"message": "update out of sequence"})
        self._version += 1
        svc["Version"] = {"Index": self._version}
        svc["Spec"] = spec
        return httpx.Response(200, json={})

    def _delete(self, sid):
        # type: (str) -> httpx.Response
        if sid in self.services:
            del self.services[sid]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "service %s not found" % sid})

    def _list_services(self, params):
        # type: (Dict[str, Any]) -> httpx.Response
        """List services, honouring a ``{"label": [...]}`` filter (presence-only).

        Mirrors Docker's ``GET /services?filters={"label":["stoker.run"]}``: a
        bare ``key`` matches any service carrying that label; a ``key=value``
        matches exactly. Returns the full service docs (id, spec) the driver
        parses for the ``stoker.run`` label.
        """
        filters = json.loads(params.get("filters", "{}"))
        wanted_labels = list(filters.get("label", []))
        out = []  # type: List[Dict[str, Any]]
        for svc in self.services.values():
            labels = svc["Spec"].get("Labels", {}) or {}
            if wanted_labels and not _labels_match(labels, wanted_labels):
                continue
            out.append(svc)
        return httpx.Response(200, json=out)

    def _tasks(self, params):
        # type: (Dict[str, Any]) -> httpx.Response
        filters = json.loads(params.get("filters", "{}"))
        wanted = set(filters.get("service", []))
        tasks = []  # type: List[Dict[str, Any]]
        for svc in self.services.values():
            name = svc["Spec"].get("Name")
            if wanted and name not in wanted:
                continue
            replicas = (
                svc["Spec"].get("Mode", {}).get("Replicated", {}).get("Replicas", 0))
            for slot in range(int(replicas)):
                tasks.append({
                    "ID": "task-%s-%d" % (svc["ID"], slot),
                    "Slot": slot + 1,
                    "NodeID": "node%d" % (slot % 3),
                    "DesiredState": "running",
                    "Status": {
                        "State": "running",
                        "ContainerStatus": {"ContainerID": "c%s%d" % (svc["ID"], slot)},
                    },
                })
        return httpx.Response(200, json=tasks)

    def _logs(self, sid, params):
        # type: (str, Dict[str, Any]) -> httpx.Response
        if sid not in self.services:
            return httpx.Response(404, json={"message": "service %s not found" % sid})
        # Return a multiplexed (non-tty) Docker log stream: 8-byte header frames.
        frames = b""
        for line in (b"boot slot 0\n", b"boot slot 1\n"):
            frames += struct.pack(">BxxxL", 1, len(line)) + line
        return httpx.Response(200, content=frames)


def _labels_match(labels, wanted):
    # type: (Dict[str, Any], List[str]) -> bool
    """Docker label-filter semantics: bare ``key`` = presence, ``key=value`` = eq."""
    for want in wanted:
        if "=" in want:
            key, value = want.split("=", 1)
            if str(labels.get(key)) != value:
                return False
        elif want not in labels:
            return False
    return True


def _swarm_with_fake(portainer):
    # type: (_FakePortainer) -> SwarmDriver
    driver = SwarmDriver(host="https://portainer.test:9443", token="pk_test",
                         endpoint=6, verify_tls=False, timeout_s=5.0)
    driver._transport = portainer.transport()
    return driver


def _snapshot(run_id=812, workers=4):
    # type: (int, int) -> RunSnapshot
    return RunSnapshot(
        run_id=run_id,
        image="ghcr.io/livehybrid/stoker-worker@sha256:deadbeef",
        env={
            "STOKER_RUN_ID": str(run_id),
            "STOKER_CONTROL_URL": "https://stoker.test",
            "STOKER_RUN_JWT": "jwt.header.payload.sig",
            "STOKER_TOTAL_WORKERS": str(workers),
            "STOKER_HEC_TOKEN": "super-secret-hec-token",
        },
        labels={"stoker.run": str(run_id)},
        driver_opts={},
        stop_grace_s=45,
    )


# --------------------------------------------------------------------------- #
# Driver matrix for the parametrised conformance walk.
# --------------------------------------------------------------------------- #

def _driver_cases():
    # type: () -> List[Any]
    """Build the (id, factory) cases for every driver runnable here.

    * ``fake`` -- always.
    * ``swarm-mock`` -- always; a real SwarmDriver over an in-memory Portainer,
      so the swarm code paths are exercised in CI without a live cluster.
    * ``swarm-live`` -- only when ``STOKER_TEST_PORTAINER=1`` (drives a real
      Portainer via the env's PORTAINER_HOST/TOKEN/ENDPOINT).
    """
    cases = [
        pytest.param(("fake", lambda: FakeDriver()), id="fake"),
        pytest.param(
            ("swarm-mock", lambda: _swarm_with_fake(_FakePortainer())),
            id="swarm-mock"),
    ]
    if os.environ.get("STOKER_TEST_PORTAINER") == "1":
        cases.append(
            pytest.param(("swarm-live", _live_swarm), id="swarm-live"))
    return cases


def _live_swarm():
    # type: () -> SwarmDriver
    host = os.environ.get("PORTAINER_HOST")
    token = os.environ.get("PORTAINER_TOKEN")
    endpoint = int(os.environ.get("PORTAINER_ENDPOINT", "6"))
    if not host or not token:
        pytest.skip("STOKER_TEST_PORTAINER=1 but PORTAINER_HOST/TOKEN unset")
    return SwarmDriver(host=host, token=token, endpoint=endpoint)


@pytest.fixture(params=_driver_cases())
def driver_case(request):
    # type: (Any) -> Any
    name, factory = request.param
    return name, factory()


# --------------------------------------------------------------------------- #
# Conformance walk: create -> status(N) -> scale -> stop -> destroy (idempotent).
# --------------------------------------------------------------------------- #

def test_conformance_lifecycle(driver_case):
    """Every driver honours the six-method state machine identically."""
    name, driver = driver_case
    # A live-swarm run id must be unique-ish to avoid colliding service names.
    run_id = 990001 if name == "swarm-live" else 812
    snap = _snapshot(run_id=run_id, workers=3)

    ref = driver.create(snap, 3)
    try:
        assert isinstance(ref, DriverRef)
        assert ref.id

        status = driver.status(ref)
        assert isinstance(status, DriverStatus)
        assert status.desired == 3

        # scale up
        driver.scale(ref, 5)
        assert driver.status(ref).desired == 5

        # scale down
        driver.scale(ref, 2)
        assert driver.status(ref).desired == 2

        # stop drains to zero desired
        driver.stop(ref, grace_s=45)
        assert driver.status(ref).desired == 0
    finally:
        driver.destroy(ref)

    # After destroy the fleet reports gone, and destroy is idempotent.
    assert driver.status(ref).desired == 0
    driver.destroy(ref)  # must not raise
    assert driver.status(ref).desired == 0


def test_conformance_create_rejects_zero_workers(driver_case):
    _name, driver = driver_case
    with pytest.raises(DriverError):
        driver.create(_snapshot(workers=1), 0)


def test_conformance_status_after_create_reports_tasks(driver_case):
    name, driver = driver_case
    run_id = 990002 if name == "swarm-live" else 813
    ref = driver.create(_snapshot(run_id=run_id, workers=2), 2)
    try:
        status = driver.status(ref)
        assert status.desired == 2
        # tasks is always a list of best-effort dicts (may be empty on a fresh
        # live swarm before tasks schedule; the mock/fake report them at once).
        assert isinstance(status.tasks, list)
        for task in status.tasks:
            assert set(task.keys()) >= {"slot", "holder", "node", "state"}
    finally:
        driver.destroy(ref)


# --------------------------------------------------------------------------- #
# list_run_ids: the optional 7th (discovery-only) method used by the boot sweep.
# --------------------------------------------------------------------------- #

def test_conformance_list_run_ids_reports_owned_runs(driver_case):
    """Every enumerable driver reports the run ids of the workloads it owns."""
    name, driver = driver_case
    base = 990100 if name == "swarm-live" else 700
    r1, r2 = base + 1, base + 2
    ref1 = driver.create(_snapshot(run_id=r1, workers=2), 2)
    ref2 = driver.create(_snapshot(run_id=r2, workers=1), 1)
    try:
        owned = driver.list_run_ids()
        assert isinstance(owned, set)
        assert {r1, r2} <= owned
        # Destroying one drops it from the enumeration; the other remains.
        driver.destroy(ref1)
        owned_after = driver.list_run_ids()
        assert r1 not in owned_after
        assert r2 in owned_after
    finally:
        driver.destroy(ref1)
        driver.destroy(ref2)


def test_conformance_list_run_ids_empty_when_no_workloads(driver_case):
    """A driver owning nothing returns an empty set (never raises here)."""
    name, driver = driver_case
    if name == "swarm-live":
        pytest.skip("a live swarm may host unrelated stoker services")
    assert driver.list_run_ids() == set()


def test_fake_list_run_ids_excludes_destroyed():
    driver = FakeDriver()
    a = driver.create(_snapshot(run_id=41, workers=1), 1)
    driver.create(_snapshot(run_id=42, workers=1), 1)
    assert driver.list_run_ids() == {41, 42}
    driver.destroy(a)
    assert driver.list_run_ids() == {42}


def test_swarm_list_run_ids_filters_by_label_and_parses_id():
    """list_run_ids GETs /services with the stoker.run label filter and parses ids."""
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    driver.create(_snapshot(run_id=812, workers=2), 2)
    driver.create(_snapshot(run_id=99, workers=1), 1)

    portainer.calls.clear()
    owned = driver.list_run_ids()
    assert owned == {812, 99}

    call = [c for c in portainer.calls if c["path"].endswith("/services")][-1]
    assert call["method"] == "GET"
    filters = json.loads(call["params"]["filters"])
    assert filters == {"label": ["stoker.run"]}


def test_swarm_list_run_ids_ignores_unlabelled_and_unparseable_services():
    """A service with no/bad stoker.run label is skipped, not guessed."""
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    driver.create(_snapshot(run_id=812, workers=1), 1)
    # Inject a foreign service (not ours) and one with a non-integer label.
    portainer.services["svc-foreign"] = {
        "ID": "svc-foreign", "Version": {"Index": 1},
        "Spec": {"Name": "unrelated", "Labels": {"app": "other"}},
    }
    portainer.services["svc-bad"] = {
        "ID": "svc-bad", "Version": {"Index": 1},
        "Spec": {"Name": "stoker-run-x", "Labels": {"stoker.run": "not-an-int"}},
    }
    owned = driver.list_run_ids()
    # Only the well-formed stoker service is reported (the foreign one is filtered
    # out by the label; the bad-id one is filtered by the int parse).
    assert owned == {812}


def test_swarm_list_run_ids_raises_on_backend_error_not_empty():
    """A backend failure raises DriverError (never a silent empty == all-strays)."""
    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    driver = SwarmDriver(host="https://p:9443", token="t", endpoint=6)
    driver._transport = httpx.MockTransport(handler)
    with pytest.raises(DriverError):
        driver.list_run_ids()


# --------------------------------------------------------------------------- #
# SwarmDriver request-shape unit tests (mocked transport; no network).
# --------------------------------------------------------------------------- #

def test_swarm_create_builds_service_spec():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    snap = _snapshot(run_id=812, workers=4)

    ref = driver.create(snap, 4)

    assert ref.kind == "swarm"
    assert ref.id == "svc1"
    assert ref.raw["name"] == "stoker-run-812"
    assert ref.raw["run_id"] == 812

    call = portainer.calls[-1]
    assert call["method"] == "POST"
    assert call["path"].endswith("/api/endpoints/6/docker/services/create")
    assert call["api_key"] == "pk_test"

    spec = call["json"]
    assert spec["Name"] == "stoker-run-812"
    assert spec["Mode"]["Replicated"]["Replicas"] == 4
    assert spec["Labels"]["stoker.run"] == "812"

    ct = spec["TaskTemplate"]["ContainerSpec"]
    assert ct["Image"] == "ghcr.io/livehybrid/stoker-worker@sha256:deadbeef"
    assert ct["Labels"]["stoker.run"] == "812"
    # StopGracePeriod is nanoseconds (45 s).
    assert ct["StopGracePeriod"] == 45 * 1_000_000_000
    # Env is Docker's ["K=V"] list and carries the projected worker env.
    env = set(ct["Env"])
    assert "STOKER_RUN_ID=812" in env
    assert "STOKER_TOTAL_WORKERS=4" in env
    assert "STOKER_RUN_JWT=jwt.header.payload.sig" in env
    assert "STOKER_HEC_TOKEN=super-secret-hec-token" in env

    # Restart + placement per the contract.
    tt = spec["TaskTemplate"]
    assert tt["RestartPolicy"]["Condition"] == "on-failure"
    prefs = tt["Placement"]["Preferences"]
    assert prefs == [{"Spread": {"SpreadDescriptor": "node.id"}}]


def test_swarm_create_passes_driver_opts_constraints():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    snap = _snapshot()
    snap.driver_opts = {"constraints": ["node.labels.stoker==true", "node.role==worker"]}

    driver.create(snap, 2)

    spec = portainer.calls[-1]["json"]
    constraints = spec["TaskTemplate"]["Placement"]["Constraints"]
    assert constraints == ["node.labels.stoker==true", "node.role==worker"]


def test_swarm_scale_reads_version_then_updates():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    ref = driver.create(_snapshot(), 3)
    version_at_create = portainer.services["svc1"]["Version"]["Index"]

    portainer.calls.clear()
    driver.scale(ref, 7)

    methods = [(c["method"], c["path"].rsplit("/docker", 1)[-1]) for c in portainer.calls]
    # Must GET the service (for its version) before POSTing the update.
    assert methods[0] == ("GET", "/services/svc1")
    assert methods[1][0] == "POST"
    assert methods[1][1] == "/services/svc1/update"

    update_call = portainer.calls[1]
    assert update_call["params"]["version"] == str(version_at_create)
    assert update_call["json"]["Mode"]["Replicated"]["Replicas"] == 7
    # The store now reflects the new desired count.
    assert driver.status(ref).desired == 7


def test_swarm_stop_scales_to_zero():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    ref = driver.create(_snapshot(), 4)

    driver.stop(ref, grace_s=45)

    update = [c for c in portainer.calls if c["path"].endswith("/update")][-1]
    assert update["json"]["Mode"]["Replicated"]["Replicas"] == 0
    assert driver.status(ref).desired == 0


def test_swarm_destroy_is_idempotent_on_404():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    ref = driver.create(_snapshot(), 2)

    driver.destroy(ref)
    assert "svc1" not in portainer.services

    # Second destroy hits a 404 which the driver treats as success.
    portainer.calls.clear()
    driver.destroy(ref)  # must not raise
    last = portainer.calls[-1]
    assert last["method"] == "DELETE"
    assert last["path"].endswith("/services/svc1")


def test_swarm_status_folds_tasks_and_counts_running():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    ref = driver.create(_snapshot(), 3)

    status = driver.status(ref)
    assert status.desired == 3
    assert status.running == 3
    assert len(status.tasks) == 3

    # The tasks filter targets the run's service by name.
    tasks_call = [c for c in portainer.calls if c["path"].endswith("/tasks")][-1]
    filters = json.loads(tasks_call["params"]["filters"])
    assert filters == {"service": ["stoker-run-812"]}

    for task in status.tasks:
        assert task["state"] == "running"
        assert task["node"] in {"node0", "node1", "node2"}
        assert task["slot"] is not None
        assert task["holder"] is not None


def test_swarm_status_counts_only_running_tasks():
    """Historical shutdown/failed tasks must not inflate `running`."""
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    ref = driver.create(_snapshot(), 2)

    # Inject a stale terminal task alongside the live ones by wrapping the
    # fake's task handler to append a shutdown task to whatever it returns.
    original = portainer._tasks

    def patched(params):
        resp = original(params)
        data = json.loads(resp.content)
        data.append({
            "ID": "task-dead",
            "Slot": 99,
            "NodeID": "node0",
            "DesiredState": "shutdown",
            "Status": {"State": "shutdown", "ContainerStatus": {"ContainerID": "old"}},
        })
        return httpx.Response(200, json=data)

    portainer._tasks = patched  # type: ignore[assignment]
    status = driver.status(ref)
    assert status.desired == 2
    assert status.running == 2  # the shutdown task does not count
    assert len(status.tasks) == 3  # but it is still surfaced for observability
    states = {t["state"] for t in status.tasks}
    assert "shutdown" in states
    driver.destroy(ref)


def test_swarm_logs_demuxes_stream():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    ref = driver.create(_snapshot(), 1)

    text = driver.logs(ref, slot=None, tail=50)
    assert "boot slot 0" in text
    assert "boot slot 1" in text
    # The 8-byte frame headers must be stripped, not leaked into the text.
    assert "\x01" not in text

    logs_call = [c for c in portainer.calls if c["path"].endswith("/logs")][-1]
    assert logs_call["params"]["stdout"] == "true"
    assert logs_call["params"]["stderr"] == "true"
    assert logs_call["params"]["tail"] == "50"


def test_swarm_logs_best_effort_on_error():
    """A logs failure returns '' rather than raising (observability only)."""
    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    driver = SwarmDriver(host="https://p:9443", token="t", endpoint=6)
    driver._transport = httpx.MockTransport(handler)
    ref = DriverRef(kind="swarm", id="svc-x", raw={"name": "stoker-run-1", "run_id": 1})
    assert driver.logs(ref, slot=None, tail=10) == ""


def test_swarm_non_2xx_raises_driver_error_without_leaking_secret():
    def handler(request):
        return httpx.Response(500, json={"message": "internal error"})

    driver = SwarmDriver(host="https://p:9443", token="pk_secret_token", endpoint=6)
    driver._transport = httpx.MockTransport(handler)
    with pytest.raises(DriverError) as exc:
        driver.create(_snapshot(), 2)
    # The error is actionable but never contains the API key.
    assert "pk_secret_token" not in str(exc.value)
    assert "500" in str(exc.value)


def test_swarm_no_host_fails_loudly():
    driver = SwarmDriver(host=None, token="t", endpoint=6)
    with pytest.raises(DriverError) as exc:
        driver.create(_snapshot(), 2)
    assert "host" in str(exc.value).lower()


def test_swarm_status_after_destroy_reports_zero_desired():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    ref = driver.create(_snapshot(), 3)
    driver.destroy(ref)
    # Inspect 404s -> desired 0, matching the FakeDriver contract.
    assert driver.status(ref).desired == 0


def test_swarm_create_rejects_zero_workers_before_any_call():
    portainer = _FakePortainer()
    driver = _swarm_with_fake(portainer)
    with pytest.raises(DriverError):
        driver.create(_snapshot(), 0)
    assert portainer.calls == []  # rejected before touching the API


def test_service_name_scheme():
    assert service_name(812) == "stoker-run-812"
    assert service_name("abc") == "stoker-run-abc"
