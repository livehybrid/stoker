"""ExecutionDriver interface and its data-transfer types.

A driver launches and controls a worker fleet for a run. The control plane owns
identity (the lease); the driver is queried for desired/running counts and
never trusted as a store. Six methods, all synchronous, exactly as the contract
shows. Implementations live in ``fake.py`` (in-process) and ``swarm.py``
(Portainer). ``get_driver`` in ``drivers/__init__`` selects one per fleet.

The types below are plain dataclasses so they serialise cleanly into
``runs.driver_ref_json`` and cross the driver boundary without ORM coupling.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclasses.dataclass
class RunSnapshot:
    """Everything a driver needs to materialise a fleet for one run.

    ``env`` is the base worker environment (RUN_ID / CONTROL_URL / RUN_JWT /
    TOTAL_WORKERS and the HEC token projection); the driver adds placement and
    restart policy. ``labels`` always includes ``stoker.run=<id>`` so boot
    reconciliation can find owned workloads. ``driver_opts`` carries
    fleet/spec-specific knobs (e.g. placement constraints).
    """

    run_id: int
    image: str
    env: Dict[str, str]
    labels: Dict[str, str]
    driver_opts: Dict[str, Any]
    stop_grace_s: int = 45


@dataclasses.dataclass
class DriverRef:
    """Opaque handle to a created fleet, stored on ``runs.driver_ref_json``.

    ``kind`` names the driver ("fake" | "swarm" | ...), ``id`` is the driver's
    native identifier (service id, etc.), ``raw`` keeps any extra fields the
    driver needs to address the workload later. Round-trips through JSON.
    """

    kind: str
    id: str
    raw: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_json(self):
        # type: () -> Dict[str, Any]
        return {"kind": self.kind, "id": self.id, "raw": self.raw}

    @classmethod
    def from_json(cls, doc):
        # type: (Optional[Dict[str, Any]]) -> Optional["DriverRef"]
        if not doc:
            return None
        return cls(kind=doc.get("kind", ""), id=doc.get("id", ""), raw=doc.get("raw") or {})


@dataclasses.dataclass
class DriverStatus:
    """A driver's view of a fleet: desired vs running plus best-effort tasks.

    ``tasks`` is a list of dicts with best-effort keys ``slot`` / ``holder`` /
    ``node`` / ``state``. Swarm has no stable slot, so identity remains the
    lease; this is observability only, never authority.
    """

    desired: int
    running: int
    tasks: List[Dict[str, Any]] = dataclasses.field(default_factory=list)


class DriverError(Exception):
    """A driver operation failed (non-2xx from the backend, timeout, etc.)."""


class NotFound(DriverError):
    """The workload is genuinely absent (a 404 from the backend).

    Distinct from a transient failure (timeout, 5xx): callers that need to tell
    "gone" from "unknown" (e.g. status/boot reconciliation) must catch this and
    let other :class:`DriverError`s propagate so a hiccup is retried, never
    mistaken for a destroyed fleet.
    """


@runtime_checkable
class ExecutionDriver(Protocol):
    """The six-method fleet control surface. All calls are synchronous."""

    def create(self, run: RunSnapshot, workers: int) -> DriverRef:
        """Create the fleet at ``workers`` replicas; return its handle."""
        ...

    def scale(self, ref: DriverRef, workers: int) -> None:
        """Change the fleet's desired replica count."""
        ...

    def stop(self, ref: DriverRef, grace_s: int) -> None:
        """Signal the fleet to drain (SIGTERM) with ``grace_s`` before kill."""
        ...

    def destroy(self, ref: DriverRef) -> None:
        """Remove the fleet. Idempotent: destroying an absent fleet is a no-op."""
        ...

    def status(self, ref: DriverRef) -> DriverStatus:
        """Return the driver's desired/running/task view of the fleet."""
        ...

    def logs(self, ref: DriverRef, slot: Optional[int], tail: int) -> str:
        """Return up to ``tail`` recent log lines (whole fleet if slot None)."""
        ...


__all__ = [
    "RunSnapshot",
    "DriverRef",
    "DriverStatus",
    "DriverError",
    "ExecutionDriver",
]
