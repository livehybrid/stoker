"""SwarmDriver: an :class:`ExecutionDriver` backed by the Portainer API.

Portainer proxies the Docker Engine API under
``/api/endpoints/{ep}/docker/...``; this driver builds standard Docker Swarm
``ServiceSpec`` documents and drives them there. Identity of a run's fleet is a
single swarm service named ``stoker-run-<id>`` labelled ``stoker.run=<id>``;
the control plane owns worker identity (the lease), so ``status`` slot mapping is
best-effort observability only.

Contract (``server/CONTROL-PLANE.md`` -> ExecutionDriver / SwarmDriver):

* ``create``  = ``POST /docker/services/create`` with ``Mode.Replicated.Replicas=N``,
  ``ContainerSpec.Image`` + ``Env`` from the :class:`RunSnapshot`, labels
  ``stoker.run=<id>``, ``RestartPolicy.Condition=on-failure``, ``StopGracePeriod``
  (ns) and a node-spread placement preference.
* ``scale``   = read the live service version + spec, patch replicas, ``POST
  /services/{id}/update?version=N``.
* ``stop``    = scale to 0 replicas (the workers get SIGTERM and drain within
  their grace); ``destroy`` then removes the service.
* ``destroy`` = ``DELETE /services/{id}`` (idempotent: 404 == already gone).
* ``status``  = ``GET /tasks?filters={"service":["stoker-run-<id>"]}`` folded into
  a :class:`DriverStatus` (desired vs running + per-task slot/holder/node/state).
* ``logs``    = ``GET /services/{id}/logs`` (stdout+stderr, tail).

All calls use ``X-API-Key: <portainer_token>``, ``verify=False`` for the
self-signed Portainer cert, short timeouts, and raise :class:`DriverError` on any
non-2xx (except the idempotent 404 on destroy). The docker socket is never
mounted; everything goes over the Portainer HTTP API. No secret (token or JWT)
is ever logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

import httpx

from .base import DriverError, DriverRef, DriverStatus, NotFound, RunSnapshot
from ..config import get_settings

log = logging.getLogger("stoker.driver.swarm")

_KIND = "swarm"

# Task DesiredState / Status.State values swarm reports; "running" is the only
# state that counts a task as up. Everything else (new/pending/assigned/
# accepted/preparing/starting) is coming up; (complete/shutdown/failed/rejected/
# orphaned/remove) is going down.
_RUNNING_STATES = frozenset({"running"})


def service_name(run_id):
    # type: (Any) -> str
    """The swarm service name for a run (single source of the naming scheme)."""
    return "stoker-run-%s" % run_id


class SwarmDriver(object):
    """Portainer-backed execution driver (Docker Swarm services)."""

    def __init__(self, host, token, endpoint=6, verify_tls=False, timeout_s=10.0):
        # type: (Optional[str], Optional[str], int, bool, float) -> None
        self._host = (host or "").rstrip("/")
        # Secret: never logged.
        self._token = token
        self._endpoint = int(endpoint)
        self._verify_tls = verify_tls
        self._timeout = float(timeout_s)
        # Injectable for unit tests (a MockTransport); None means "build a real
        # client per call" so a long-lived driver never pins a dead connection.
        self._transport = None  # type: Optional[httpx.BaseTransport]

    @classmethod
    def from_fleet_config(cls, config):
        # type: (Optional[Dict[str, Any]]) -> "SwarmDriver"
        """Build from a ``fleets.config_json`` (+ process settings for secrets).

        The Portainer endpoint id and host come from the fleet config (falling
        back to global settings); the API token comes only from settings
        (``PORTAINER_TOKEN``), never persisted in the fleet row.
        """
        config = config or {}
        settings = get_settings()
        host = config.get("portainer_host") or settings.portainer_host
        endpoint = config.get("portainer_endpoint") or settings.portainer_endpoint
        verify = bool(config.get("verify_tls", False))
        return cls(
            host=host,
            token=settings.portainer_token,
            endpoint=int(endpoint),
            verify_tls=verify,
        )

    # -- HTTP plumbing ---------------------------------------------------- #

    def _docker_base(self):
        # type: () -> str
        if not self._host:
            raise DriverError("SwarmDriver has no Portainer host configured "
                              "(set PORTAINER_HOST or the fleet config)")
        return "%s/api/endpoints/%d/docker" % (self._host, self._endpoint)

    def _client(self):
        # type: () -> httpx.Client
        headers = {}
        if self._token:
            headers["X-API-Key"] = self._token
        kwargs = {
            "headers": headers,
            "verify": self._verify_tls,
            "timeout": self._timeout,
        }  # type: Dict[str, Any]
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def _request(self, method, path, params=None, json_body=None, ok=(200, 201)):
        # type: (str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], tuple) -> httpx.Response
        """One Portainer/Docker call. Raises :class:`DriverError` on non-``ok``.

        ``path`` is appended to the docker base (``/api/endpoints/{ep}/docker``).
        Secrets never appear in the raised message (only method + path + code).
        """
        url = self._docker_base() + path
        try:
            with self._client() as client:
                resp = client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise DriverError("swarm %s %s failed: %s" % (method, path, exc))
        if resp.status_code not in ok:
            # Docker error bodies are small JSON ({"message": ...}); include a
            # trimmed body for diagnosis but never the request (no token in path).
            detail = _trim(resp.text)
            msg = ("swarm %s %s -> HTTP %d%s"
                   % (method, path, resp.status_code, (": %s" % detail) if detail else ""))
            # A genuine 404 (workload gone) is distinct from a transient failure:
            # callers must not coerce a timeout/5xx into "absent".
            if resp.status_code == 404:
                raise NotFound(msg)
            raise DriverError(msg)
        return resp

    # -- ExecutionDriver -------------------------------------------------- #

    def create(self, run, workers):
        # type: (RunSnapshot, int) -> DriverRef
        if workers < 1:
            raise DriverError("workers must be >= 1")
        spec = self._service_spec(run, workers)
        name = spec["Name"]
        log.info("swarm create service %s desired=%d image=%s",
                 name, workers, run.image)
        resp = self._request("POST", "/services/create", json_body=spec)
        body = _json(resp)
        service_id = body.get("ID") or body.get("Id") or ""
        if not service_id:
            raise DriverError("swarm create %s returned no service ID" % name)
        return DriverRef(
            kind=_KIND,
            id=str(service_id),
            raw={"run_id": run.run_id, "name": name, "endpoint": self._endpoint},
        )

    def scale(self, ref, workers):
        # type: (DriverRef, int) -> None
        if workers < 0:
            raise DriverError("workers must be >= 0")
        self._update_replicas(ref, workers)
        log.info("swarm scaled service %s desired=%d", ref.id, workers)

    def stop(self, ref, grace_s):
        # type: (DriverRef, int) -> None
        # Draining a swarm service = scale it to zero. Each task gets SIGTERM and
        # drains within its own StopGracePeriod; the control plane also answers
        # `drain` on heartbeats so workers flush before the task dies. `destroy`
        # removes the service afterwards.
        self._update_replicas(ref, 0)
        log.info("swarm stopped (scaled to 0) service %s (grace %ds)", ref.id, grace_s)

    def destroy(self, ref):
        # type: (DriverRef) -> None
        # Idempotent: a 404 means the service is already gone -> success.
        resp = self._request("DELETE", "/services/%s" % ref.id,
                             ok=(200, 201, 404))
        if resp.status_code == 404:
            log.info("swarm destroy: service %s already gone", ref.id)
        else:
            log.info("swarm destroyed service %s", ref.id)

    def status(self, ref):
        # type: (DriverRef) -> DriverStatus
        desired = self._desired_replicas(ref)
        name = self._name_of(ref)
        filters = json.dumps({"service": [name]})
        resp = self._request("GET", "/tasks", params={"filters": filters})
        raw_tasks = _json(resp)
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        tasks = [_task_view(t) for t in raw_tasks]
        # Only live (non-terminal) tasks are meaningful for "running"; swarm
        # keeps historical shutdown/failed tasks in this list.
        running = sum(1 for t in tasks if t["state"] in _RUNNING_STATES)
        return DriverStatus(desired=desired, running=running, tasks=tasks)

    def logs(self, ref, slot, tail):
        # type: (DriverRef, Optional[int], int) -> str
        # Swarm has no stable slot; service logs are the fleet's combined stream.
        # `slot` is accepted for interface parity but not addressable here (the
        # lease, not the task, is identity) -> whole-service logs, tailed.
        params = {"stdout": "true", "stderr": "true", "timestamps": "false"}
        if tail and tail > 0:
            params["tail"] = str(int(tail))
        else:
            params["tail"] = "all"
        try:
            resp = self._request("GET", "/services/%s/logs" % ref.id, params=params)
        except DriverError as exc:
            # Logs are best-effort observability; never fail a caller over them.
            log.warning("swarm logs for %s unavailable: %s", ref.id, exc)
            return ""
        return _decode_logs(resp.content)

    # -- discovery (optional 7th method) ---------------------------------- #

    def list_run_ids(self):
        # type: () -> Set[int]
        """Return every run id this swarm owns, by the ``stoker.run`` label.

        ``GET /services?filters={"label":["stoker.run"]}`` returns all services
        carrying the label (Docker's presence-only label filter); each service's
        ``Spec.Labels['stoker.run']`` (or its ``stoker-run-<id>`` name as a
        fallback) parses to the run id. Boot reconciliation uses this to spot
        strays (a labelled service with no live DB run).

        A backend failure raises :class:`DriverError` (the caller skips the sweep
        rather than mistaking a hiccup for "no strays" -> destroy everything);
        services whose label does not parse to an int are skipped, not guessed.
        """
        filters = json.dumps({"label": ["stoker.run"]})
        resp = self._request("GET", "/services", params={"filters": filters})
        body = _json(resp)
        if not isinstance(body, list):
            # A malformed non-list response is a backend fault, not "no services";
            # raise so the sweep is skipped rather than treated as an empty estate.
            raise DriverError("swarm /services returned non-list body")
        run_ids = set()  # type: Set[int]
        for service in body:
            run_id = _service_run_id(service)
            if run_id is not None:
                run_ids.add(run_id)
        return run_ids

    # -- update mechanics ------------------------------------------------- #

    def _update_replicas(self, ref, replicas):
        # type: (DriverRef, int) -> None
        """Read the live service (for its version + spec), patch replicas, POST.

        Docker's ``/services/{id}/update`` requires the current object version
        and a full ``ServiceSpec``; we fetch the live one so we never clobber
        drift, mutate only ``Mode.Replicated.Replicas``, and POST with
        ``?version=<index>``.
        """
        service = self._get_service(ref.id)
        version = _version_index(service)
        spec = service.get("Spec")
        if not isinstance(spec, dict):
            raise DriverError("swarm service %s has no Spec to update" % ref.id)
        mode = spec.setdefault("Mode", {})
        replicated = mode.setdefault("Replicated", {})
        replicated["Replicas"] = int(replicas)
        # A replicated service must not carry a Global mode block.
        mode.pop("Global", None)
        self._request("POST", "/services/%s/update" % ref.id,
                      params={"version": version}, json_body=spec)

    def _get_service(self, service_id):
        # type: (str) -> Dict[str, Any]
        resp = self._request("GET", "/services/%s" % service_id, ok=(200,))
        body = _json(resp)
        if not isinstance(body, dict):
            raise DriverError("swarm service %s inspect returned non-object" % service_id)
        return body

    def _desired_replicas(self, ref):
        # type: (DriverRef) -> int
        try:
            service = self._get_service(ref.id)
        except NotFound:
            # Service genuinely gone (destroyed) -> desired 0, matching FakeDriver.
            return 0
        # Any other DriverError (timeout, 5xx, TLS blip) propagates: status()
        # must surface "unknown", never report a transient failure as desired=0,
        # which would let boot reconciliation orphan a live fleet.
        spec = service.get("Spec") or {}
        replicated = (spec.get("Mode") or {}).get("Replicated") or {}
        replicas = replicated.get("Replicas")
        try:
            return int(replicas) if replicas is not None else 0
        except (TypeError, ValueError):
            return 0

    def _name_of(self, ref):
        # type: (DriverRef) -> str
        name = (ref.raw or {}).get("name")
        if name:
            return str(name)
        run_id = (ref.raw or {}).get("run_id")
        if run_id is not None:
            return service_name(run_id)
        # Last resort: the service id is a valid filter target too.
        return ref.id

    # -- spec construction ------------------------------------------------ #

    def _service_spec(self, run, workers):
        # type: (RunSnapshot, int) -> Dict[str, Any]
        """Build the Docker Swarm ``ServiceSpec`` for a run's fleet.

        Env is projected as a Docker ``["K=V", ...]`` list from the snapshot;
        labels carry ``stoker.run=<id>`` on both the service and the container so
        boot reconciliation can find owned tasks. ``StopGracePeriod`` is in
        nanoseconds (Docker's unit). Placement spreads tasks across nodes.
        """
        run_label = str(run.run_id)
        labels = dict(run.labels or {})
        labels.setdefault("stoker.run", run_label)

        env_list = _env_list(run.env)
        stop_grace_ns = int(run.stop_grace_s) * 1_000_000_000

        container_spec = {
            "Image": run.image,
            "Env": env_list,
            "Labels": dict(labels),
            "StopGracePeriod": stop_grace_ns,
        }  # type: Dict[str, Any]

        placement = self._placement(run)

        task_template = {
            "ContainerSpec": container_spec,
            "RestartPolicy": {"Condition": "on-failure"},
            "Placement": placement,
        }  # type: Dict[str, Any]

        return {
            "Name": service_name(run.run_id),
            "Labels": labels,
            "TaskTemplate": task_template,
            "Mode": {"Replicated": {"Replicas": int(workers)}},
        }

    def _placement(self, run):
        # type: (RunSnapshot) -> Dict[str, Any]
        """Spread tasks across nodes; honour any driver_opts constraints.

        ``driver_opts.constraints`` (a list of docker constraint strings, e.g.
        ``["node.labels.stoker==true"]``) is passed through verbatim; the
        default preference spreads by ``node.id`` so a fleet isn't stacked on one
        host.
        """
        placement = {
            "Preferences": [{"Spread": {"SpreadDescriptor": "node.id"}}],
        }  # type: Dict[str, Any]
        opts = run.driver_opts or {}
        constraints = opts.get("constraints")
        if isinstance(constraints, list) and constraints:
            placement["Constraints"] = [str(c) for c in constraints]
        return placement


# --------------------------------------------------------------------------- #
# Module-level helpers (pure; unit-tested via the mocked transport path).
# --------------------------------------------------------------------------- #

def _env_list(env):
    # type: (Optional[Dict[str, str]]) -> List[str]
    """Render an env dict as Docker's ``["KEY=VALUE", ...]`` list, sorted.

    Sorting makes the produced spec deterministic (stable for tests and for
    diffing); values are coerced to str. ``None`` values are dropped.
    """
    if not env:
        return []
    items = []
    for key in sorted(env):
        value = env[key]
        if value is None:
            continue
        items.append("%s=%s" % (key, value))
    return items


def _service_run_id(service):
    # type: (Any) -> Optional[int]
    """Parse a swarm service doc to its ``stoker.run`` run id, or None.

    Prefers the ``Spec.Labels['stoker.run']`` label (the authoritative marker the
    driver stamps on create); falls back to the ``stoker-run-<id>`` service name
    suffix when the label is missing. A value that is not an integer yields None
    (skip it — the sweep must never destroy on a guessed id).
    """
    if not isinstance(service, dict):
        return None
    spec = service.get("Spec") or {}
    labels = spec.get("Labels") or {}
    raw = labels.get("stoker.run") if isinstance(labels, dict) else None
    if raw is None:
        # Fall back to the service name (stoker-run-<id>).
        name = spec.get("Name") or service.get("Name") or ""
        if isinstance(name, str) and name.startswith("stoker-run-"):
            raw = name[len("stoker-run-"):]
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _task_view(task):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    """Fold one swarm task doc into the best-effort ``{slot,holder,node,state}``.

    Swarm has no stable slot; ``Slot`` is the task's ordinal within the service
    (present for replicated services) and is exposed for observability only.
    ``holder`` is the task's container hostname when resolvable, else None.
    """
    if not isinstance(task, dict):
        return {"slot": None, "holder": None, "node": None, "state": None}
    status = task.get("Status") or {}
    state = status.get("State")
    slot = task.get("Slot")
    node = task.get("NodeID")
    # Container hostname (when swarm has assigned one) is the closest thing to
    # the worker's holder; fall back to the task id.
    holder = None
    container = (status.get("ContainerStatus") or {})
    holder = container.get("ContainerID") or task.get("ID")
    return {
        "slot": slot,
        "holder": holder,
        "node": node,
        "state": state.lower() if isinstance(state, str) else state,
    }


def _version_index(service):
    # type: (Dict[str, Any]) -> int
    version = service.get("Version") or {}
    index = version.get("Index")
    if index is None:
        raise DriverError("swarm service has no Version.Index for update")
    try:
        return int(index)
    except (TypeError, ValueError):
        raise DriverError("swarm service Version.Index is not an integer: %r" % index)


def _json(resp):
    # type: (httpx.Response) -> Any
    if not resp.content:
        return {}
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        raise DriverError("swarm response was not valid JSON")


def _trim(text, limit=300):
    # type: (Optional[str], int) -> str
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _decode_logs(raw):
    # type: (bytes) -> str
    """Decode a Docker service-logs stream to text.

    When the service has no TTY, Docker multiplexes stdout/stderr with an 8-byte
    header per frame (stream byte + 3 reserved + big-endian length). We strip
    those frames when present; otherwise decode as plain UTF-8. Either way the
    caller gets human-readable log text.
    """
    if not raw:
        return ""
    # Heuristic: multiplexed frames start with a stream byte in {0,1,2} and have
    # a zero-valued reserved triplet. If the first byte is printable ASCII the
    # stream is almost certainly un-muxed (tty / already-plain).
    if raw[0] in (0, 1, 2) and len(raw) >= 8 and raw[1] == 0 and raw[2] == 0 and raw[3] == 0:
        return _demux(raw)
    return raw.decode("utf-8", "replace")


def _demux(raw):
    # type: (bytes) -> str
    out = []  # type: List[bytes]
    i = 0
    n = len(raw)
    while i + 8 <= n:
        length = int.from_bytes(raw[i + 4:i + 8], "big")
        i += 8
        chunk = raw[i:i + length]
        out.append(chunk)
        i += length
    return b"".join(out).decode("utf-8", "replace")


__all__ = ["SwarmDriver", "service_name"]
