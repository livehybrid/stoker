"""InProcessDriver: run small workloads inside the control-plane container.

:class:`~server.drivers.fake.FakeDriver`'s spawn mode, productized: one run ==
N real ``stoker_agent`` subprocesses of the control-plane process, driven from
the regular UI like any fleet. The workers speak the normal managed contract
(claim a lease, download the bundle over HTTP, token-bucket pacing, HEC
delivery, heartbeats) — against ``http://127.0.0.1:<port>``, i.e. the very
server that spawned them. No swarm, no kubernetes, no Portainer.

This is for SMALL workloads by design — the workers share the control plane's
CPU/memory with the API and supervisor, with no container isolation:

* the worker count is capped (``STOKER_INPROCESS_MAX_WORKERS``, default 2;
  per-fleet ``config_json.max_workers`` overrides), enforced both at the
  submit gate (a friendly 422) and here in ``create``/``scale`` (backstop);
* custom-code packs (``bin/`` or a ``generator =`` stanza) are refused at the
  submit gate regardless of ``trusted_code`` — arbitrary pack Python must
  never run inside the control-plane container;
* the whole fleet is opt-in: ``STOKER_INPROCESS_FLEET=1`` seeds the
  ``inprocess-local`` fleet and allows the driver to build.

Requirements: the worker source tree must be present with its layout intact
(``<root>/stoker_agent`` + ``<root>/engines/...``) and importable — the server
image ships it at ``/app/worker`` (on ``PYTHONPATH``); a repo checkout uses the
``worker/`` directory next to the ``server`` package. The agent resolves its
engine roots relative to its own package, so preserving the layout is all the
engines need.

Per-worker collision avoidance (everything shares one filesystem + network
namespace, unlike a container-per-worker fleet): each slot gets its own
``STOKER_OUTPUT_SOCKET`` (the fixed default would collide), and the prometheus
sidecar port is disabled (``STOKER_METRICS_PORT=0``) — worker metrics already
flow to the control plane via heartbeats.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import tempfile
from typing import Any, Dict, Optional

from ..config import Settings, get_settings
from .base import DriverError
from .fake import FakeDriver

log = logging.getLogger("stoker.driver.inprocess")


class InProcessDriver(FakeDriver):
    """Spawn-mode FakeDriver with the guardrails for living in the server.

    Adds to the base spawn mode: the control-URL/PYTHONPATH/metrics-port env
    overrides, a per-slot output socket, a worker cap on ``create``/``scale``,
    and captured worker logs (so the UI's log tail works).
    """

    def __init__(self, control_url, worker_root, max_workers,
                 python_executable=None):
        # type: (str, str, int, Optional[str]) -> None
        overrides = {
            # The snapshot's control URL is built from PUBLIC_BASE_URL (an
            # external address); workers in this container talk to loopback so
            # a run never depends on hairpinning through the ingress.
            "STOKER_CONTROL_URL": control_url,
            # No per-worker prometheus listener: N workers in one netns would
            # fight over the fixed default port, and heartbeats already carry
            # the counters to the control plane.
            "STOKER_METRICS_PORT": "0",
            "PYTHONPATH": _worker_pythonpath(worker_root),
        }
        super(InProcessDriver, self).__init__(
            spawn=True, env_overrides=overrides, capture_logs=True,
            python_executable=python_executable)
        self._max_workers = int(max_workers)

    def create(self, run, workers):
        # type: (Any, int) -> Any
        self._require_within_cap(workers)
        return super(InProcessDriver, self).create(run, workers)

    def scale(self, ref, workers):
        # type: (Any, int) -> None
        self._require_within_cap(workers)
        super(InProcessDriver, self).scale(ref, workers)

    def _require_within_cap(self, workers):
        # type: (int) -> None
        if workers > self._max_workers:
            raise DriverError(
                "in-process fleet is capped at %d worker(s) (workers share the "
                "control plane's container); requested %d. Raise "
                "STOKER_INPROCESS_MAX_WORKERS or use a swarm/k8s fleet."
                % (self._max_workers, workers))

    def _slot_env(self, state, slot):
        # type: (Any, int) -> Dict[str, str]
        # One unix socket per worker: every spawned agent shares this
        # container's /tmp, so the agent's fixed default path would collide the
        # moment workers > 1 (or two runs overlap).
        sock = os.path.join(
            tempfile.gettempdir(),
            "stoker-inproc-%s-%s.sock" % (state.run_id, slot))
        return {"STOKER_OUTPUT_SOCKET": sock}


def build_inprocess_driver(config, settings=None):
    # type: (Optional[Dict[str, Any]], Optional[Settings]) -> InProcessDriver
    """Build the driver for an ``inprocess`` fleet row, or refuse loudly.

    Raises :class:`DriverError` (surfaced to the operator as a 422
    ``fleet_unavailable`` at launch) when the fleet is disabled or the worker
    source is not present — never a half-working driver.
    """
    if settings is None:
        settings = get_settings()
    if not settings.inprocess_fleet_enabled:
        raise DriverError(
            "the in-process fleet is disabled; set STOKER_INPROCESS_FLEET=1 on "
            "the control plane (workers then run inside its container) or use "
            "a swarm/k8s fleet")
    worker_root = _find_worker_root()
    if worker_root is None:
        raise DriverError(
            "the in-process fleet needs the worker source in the control-plane "
            "image (stoker_agent not importable and no worker/ tree found next "
            "to the server package); use an image built from server/Dockerfile "
            "or add the worker tree to PYTHONPATH")

    config = config or {}
    control_url = (config.get("control_url")
                   or "http://127.0.0.1:%d" % settings.port)
    max_workers = int(config.get("max_workers")
                      or settings.inprocess_max_workers)
    log.info("in-process fleet: worker root %s, control url %s, cap %d",
             worker_root, control_url, max_workers)
    return InProcessDriver(control_url=control_url, worker_root=worker_root,
                           max_workers=max_workers)


def _find_worker_root():
    # type: () -> Optional[str]
    """Locate the worker source root (the dir holding ``stoker_agent``).

    Two supported layouts, tried in order:

    1. ``stoker_agent`` already importable (the server image puts
       ``/app/worker`` on PYTHONPATH) — its parent directory is the root.
    2. A ``worker/`` tree next to the ``server`` package (a repo checkout
       running uvicorn from source).

    Returns None when neither holds; the caller turns that into an actionable
    DriverError rather than letting a spawn fail with ModuleNotFoundError.
    """
    try:
        spec = importlib.util.find_spec("stoker_agent")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        return os.path.dirname(os.path.dirname(os.path.abspath(spec.origin)))
    repo_worker = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        os.pardir, "worker")
    repo_worker = os.path.normpath(repo_worker)
    if os.path.isdir(os.path.join(repo_worker, "stoker_agent")):
        return repo_worker
    return None


def _worker_pythonpath(worker_root):
    # type: (str) -> str
    """The spawned agent's PYTHONPATH: the worker layout + the parent's path.

    Mirrors the worker image contract (worker/Dockerfile): the agent package
    root plus the vendored engine roots. The agent re-prepends engine roots for
    its engine subprocess anyway (relative to its own location), so the root
    entry is the load-bearing one; the engine entries keep the environment
    byte-compatible with the real worker image. The parent process's own
    PYTHONPATH is appended so nothing it relied on is lost.
    """
    paths = [
        worker_root,
        os.path.join(worker_root, "engines", "eventgen"),
        os.path.join(worker_root, "engines", "rawreplay"),
    ]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    return os.pathsep.join(paths)


__all__ = ["InProcessDriver", "build_inprocess_driver"]
