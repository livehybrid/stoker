"""Driver selection.

``get_driver(fleet_row)`` maps a ``fleets`` row (or a driver-name string) to a
concrete :class:`~server.drivers.base.ExecutionDriver`. Imports are lazy so the
foundation and the test suite never pull in the Swarm/Portainer client unless a
swarm fleet is actually used.

Drivers are cached per fleet name so one process shares a single client (and, for
the FakeDriver, a single in-memory store) across the request handlers and the
supervisor loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Union

from .base import (
    DriverError,
    DriverRef,
    DriverStatus,
    ExecutionDriver,
    RunSnapshot,
)

log = logging.getLogger("stoker.drivers")

_CACHE = {}  # type: Dict[str, ExecutionDriver]
_LOCK = threading.Lock()


def _fleet_attrs(fleet_row):
    # type: (Any) -> Dict[str, Any]
    """Extract (name, driver, config) from a Fleet ORM row or a plain string.

    Accepts either a ``models.Fleet`` (duck-typed on ``.driver`` / ``.name`` /
    ``.config_json``) or a bare driver-name string ("fake" | "swarm").
    """
    if isinstance(fleet_row, str):
        return {"name": fleet_row, "driver": fleet_row, "config": {}}
    driver = getattr(fleet_row, "driver", None)
    name = getattr(fleet_row, "name", None) or driver or "fake"
    config = getattr(fleet_row, "config_json", None) or {}
    return {"name": name, "driver": driver or "fake", "config": config}


def get_driver(fleet_row, cache=True):
    # type: (Union[Any, str], bool) -> ExecutionDriver
    """Return the driver for a fleet.

    ``driver == "swarm"`` lazily builds a :class:`~server.drivers.swarm.SwarmDriver`
    from the fleet's ``config_json`` (Portainer endpoint id + host) and process
    settings; anything else (``fake`` / ``k8s`` placeholder) yields a
    :class:`~server.drivers.fake.FakeDriver`. Instances are cached by fleet name
    unless ``cache`` is False (tests pass their own driver instead).
    """
    attrs = _fleet_attrs(fleet_row)
    name = attrs["name"]

    if cache:
        with _LOCK:
            existing = _CACHE.get(name)
            if existing is not None:
                return existing

    driver = _build(attrs)

    if cache:
        with _LOCK:
            _CACHE.setdefault(name, driver)
            return _CACHE[name]
    return driver


def _build(attrs):
    # type: (Dict[str, Any]) -> ExecutionDriver
    driver_name = attrs["driver"]
    config = attrs["config"] or {}
    if driver_name == "swarm":
        from .swarm import SwarmDriver

        log.info("building SwarmDriver for fleet %s", attrs["name"])
        return SwarmDriver.from_fleet_config(config)
    if driver_name in ("fake", "k8s"):
        # k8s driver is deferred (design Phase 2/3); a FakeDriver stands in so a
        # fake-local/k8s fleet never crashes the app this stage.
        if driver_name == "k8s":
            log.info("k8s driver deferred; using FakeDriver for fleet %s", attrs["name"])
        from .fake import FakeDriver

        return FakeDriver()
    log.warning("unknown driver %r for fleet %s; falling back to FakeDriver",
                driver_name, attrs["name"])
    from .fake import FakeDriver

    return FakeDriver()


def register_driver(name, driver):
    # type: (str, ExecutionDriver) -> None
    """Insert an explicit driver instance into the cache (tests use this to
    bind a fleet name to a shared FakeDriver)."""
    with _LOCK:
        _CACHE[name] = driver


def clear_cache():
    # type: () -> None
    """Drop all cached drivers (tests call this between sessions)."""
    with _LOCK:
        _CACHE.clear()


__all__ = [
    "get_driver",
    "register_driver",
    "clear_cache",
    "ExecutionDriver",
    "RunSnapshot",
    "DriverRef",
    "DriverStatus",
    "DriverError",
]
